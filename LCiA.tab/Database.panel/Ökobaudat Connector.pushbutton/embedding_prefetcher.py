#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Offline embedding pre-fetcher for ÖKOBAUDAT datasets (Phase 1).

For each dataset, builds a `name | classification` haystack, calls
Ollama (default model `bge-m3`) to get a 1024-d L2-normalised vector,
and writes the result to two artifacts:

  1. LanceDB table   LCiA_Extension_Cache/lancedb/embeddings_{src}_{lang}
     - canonical store. Carries scalar columns (`material_category`,
     `concrete_strength`, GWP/PENRT/PERE, …) merged from the indicator
     cache, available for downstream offline filtering.

  2. Binary sidecar  LCiA_Extension_Cache/embeddings_{src}_{lang}.bin
     - IronPython 2.7 runtime projection. The Revit plugin loads this
     via `search_helpers.EmbeddingIndex.load_bin`.

Prerequisites
-------------
1. CPython 3.9+ (does NOT run inside Revit / IronPython)
2. `pip install lancedb pyarrow`
3. Ollama running locally with the embedding model pulled:
       ollama pull bge-m3
       ollama serve
4. The dataset list cache must already be populated - open the
   Ökobaudat Connector in Revit at least once.

Usage
-----
# Recommended first run: ALL sources × BOTH languages (~25 min)
    python embedding_prefetcher.py --all --workers 5

# One source + language (fastest, ~3-5 min):
    python embedding_prefetcher.py --source a2_sphera --lang en

# All sources, DE only:
    python embedding_prefetcher.py --all-de --workers 5

# Force re-embedding (ignore existing rows):
    python embedding_prefetcher.py --source a2_sphera --lang en --force

# List sources:
    python embedding_prefetcher.py --list
"""
from __future__ import print_function, division

import os
import sys
import json
import time
import argparse
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Line-buffer stdout so progress messages appear in real time even when
# this script is run from a subprocess (auto-build) or piped into a log
# file. The auto-build path also passes `-u`, but a manual run on the
# command line without `-u` would otherwise wait for the OS buffer to
# fill before showing any output.
try:
    sys.stdout.reconfigure(line_buffering=True)  # Python 3.7+
except Exception:
    pass

# Make sibling modules importable when run from any cwd.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    import lancedb
    import pyarrow as pa
except ImportError as ex:
    sys.stderr.write(
        "ERROR: `lancedb` and `pyarrow` are required.\n"
        "       Install with:  pip install lancedb pyarrow\n"
        "       Details: {}\n".format(ex))
    sys.exit(2)

from search_helpers import (
    OllamaEmbeddingClient,
    EmbeddingIndex,
    build_semantic_haystack,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_DIR   = os.path.join(SCRIPT_DIR, "LCiA_Extension_Cache")
LANCEDB_DIR = os.path.join(CACHE_DIR, "lancedb")

OLLAMA_URL  = "http://localhost:11434"
MODEL       = "bge-m3"
DIM         = 1024
MAX_WORKERS = 4
TIMEOUT_MS  = 60000
# Ollama's /api/embed accepts `"input": [..]`. One batched request loads
# the whole batch into a single GPU forward pass, so per-embed latency
# falls roughly as 1/batch_size. Empirically on RTX 4070 / bge-m3:
#   batch 1  → ~2.5 s/req → 0.4  embeds/s
#   batch 16 → ~2.7 s/req → 5.9  embeds/s
#   batch 32 → ~3.2 s/req → 10.0 embeds/s
# 32 is the sweet spot - larger batches plateau as GPU compute becomes
# the bottleneck and risk VRAM pressure under concurrency.
BATCH_SIZE  = 32

# Singleton-retry transformation ladder (Phase 1.5d). A small fraction
# of ÖKOBAUDAT names produce NaN in bge-m3 F16 even after the lowercase
# normalisation in OllamaEmbeddingClient - known triggers include the
# registered-trademark symbol `®` and long technical phrases. When a
# singleton retry fails, the prefetcher walks this ladder; the first
# transform that yields a valid vector wins. Ordered from least to
# most semantic-info-loss. Each entry is (name, fn).
_TRANSFORMATIONS = [
    ("trademark_escape",
     lambda s: s.replace(u"®", u"(R)")     # ®
                .replace(u"™", u"(TM)")    # ™
                .replace(u"©", u"(C)")),   # ©
    ("strip_non_ascii",
     lambda s: s.encode("ascii", "ignore").decode("ascii")),
    ("name_only",
     lambda s: s.split(u" | ", 1)[0]),
    ("truncate_60",
     lambda s: s[:60]),
    ("uppercase",
     lambda s: s.upper()),
]

ALL_SOURCES = ["a2_sphera", "a1_sphera", "a2_ecoinvent", "project_epds"]
ALL_LANGS   = ["de", "en"]

# Scalar indicators copied into the LanceDB row for offline filtering.
# Kept short - the indicator JSON already has the full set; the table
# just needs the columns we expect to filter on.
SCALAR_INDICATOR_KEYS = [
    ("GWP_total",      "gwp_total"),
    ("GWP_total_A1A5", "gwp_total_a1a5"),
    ("GWP_total_C1C4", "gwp_total_c1c4"),
    ("PENRT",          "penrt"),
    ("PERE",           "pere"),
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _build_schema():
    """LanceDB / pyarrow schema. Fixed-size vector column gives the
    best query latency and storage layout."""
    return pa.schema([
        pa.field("uuid",                    pa.string()),
        pa.field("name",                    pa.string()),
        pa.field("classification",          pa.string()),
        pa.field("haystack",                pa.string()),
        pa.field("vector",                  pa.list_(pa.float32(), DIM)),
        pa.field("material_category",       pa.string()),
        pa.field("concrete_strength",       pa.string()),
        pa.field("element_function",        pa.string()),
        pa.field("uses_secondary_material", pa.bool_()),
        pa.field("gwp_total",               pa.float32()),
        pa.field("gwp_total_a1a5",          pa.float32()),
        pa.field("gwp_total_c1c4",          pa.float32()),
        pa.field("penrt",                   pa.float32()),
        pa.field("pere",                    pa.float32()),
    ])


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_dataset_list(source, lang):
    """Read ds_cache_v2_{src}_{lang}.json and return its dataset list."""
    path = os.path.join(CACHE_DIR, "ds_cache_v2_{}_{}.json".format(source, lang))
    if not os.path.exists(path):
        return None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                data = json.load(f)
            return data.get("results", [])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def _load_indicator_cache(source, lang):
    """Read indicators_{src}_{lang}.json (produced by
    indicator_prefetcher.py). Returns {} if missing."""
    path = os.path.join(CACHE_DIR, "indicators_{}_{}.json".format(source, lang))
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("datasets", {})
    except Exception:
        return {}


def _gather_scalars(uuid, indicator_cache):
    """Extract scalar columns for one UUID from the indicator cache.
    Returns a dict suitable for a LanceDB row; uses None for missing
    floats so pyarrow stores them as null."""
    entry = indicator_cache.get(uuid)
    if not entry:
        return {}
    attrs = entry.get("attributes", {}) or {}
    out = {
        "material_category":       attrs.get("material_category") or "",
        "concrete_strength":       attrs.get("concrete_strength") or "",
        "element_function":        attrs.get("element_function") or "",
        "uses_secondary_material": bool(attrs.get("uses_secondary_material", False)),
    }
    for src_key, col_name in SCALAR_INDICATOR_KEYS:
        val = entry.get(src_key)
        out[col_name] = float(val) if isinstance(val, (int, float)) else None
    return out


# ---------------------------------------------------------------------------
# Embedding pipeline
# ---------------------------------------------------------------------------

def _build_row(ds, indicator_cache, vec, alt_names=None):
    """Assemble a LanceDB row dict from a dataset entry + embedding.

    `alt_names` maps uuid -> the other-language name so the stored
    `haystack` column matches the bilingual text that was embedded."""
    uuid = ds["uuid"]
    name = (ds.get("name") or "").strip()
    cls  = (ds.get("classification") or "").strip()
    haystack = build_semantic_haystack(
        {"name": name, "classification": cls,
         "name_alt": (alt_names or {}).get(uuid, "")})
    scalars = _gather_scalars(uuid, indicator_cache)
    row = {
        "uuid":           uuid,
        "name":           name,
        "classification": cls,
        "haystack":       haystack,
        "vector":         vec,
        "material_category":       scalars.get("material_category", ""),
        "concrete_strength":       scalars.get("concrete_strength", ""),
        "element_function":        scalars.get("element_function", ""),
        "uses_secondary_material": scalars.get("uses_secondary_material", False),
    }
    for _, col_name in SCALAR_INDICATOR_KEYS:
        row[col_name] = scalars.get(col_name)
    return row


# ---------------------------------------------------------------------------
# LanceDB helpers
# ---------------------------------------------------------------------------

def _connect_db():
    if not os.path.exists(LANCEDB_DIR):
        os.makedirs(LANCEDB_DIR)
    return lancedb.connect(LANCEDB_DIR)


def _open_or_create_table(db, table_name):
    """Open an existing table or create an empty one. Returns
    (tbl, existing_uuid_set)."""
    if table_name in db.table_names():
        tbl = db.open_table(table_name)
        try:
            uuids = {row["uuid"] for row in tbl.to_arrow().to_pylist()}
        except Exception:
            uuids = set()
        return tbl, uuids
    tbl = db.create_table(table_name, schema=_build_schema(), mode="overwrite")
    return tbl, set()


def _drop_existing_uuids(tbl, uuids_to_drop):
    """Delete rows whose UUID is in `uuids_to_drop` (used by --force)."""
    if not uuids_to_drop:
        return
    quoted = ",".join("'" + u.replace("'", "''") + "'" for u in uuids_to_drop)
    try:
        tbl.delete("uuid IN ({})".format(quoted))
    except Exception as ex:
        print("  [WARN] could not delete rows: {}".format(ex))


def _build_indexes(tbl):
    """Build vector + FTS indexes after a fresh write. Failures here
    are not fatal - newer LanceDB versions sometimes change index
    parameters; in that case the table still works for `.search()`."""
    try:
        if tbl.count_rows() >= 256:
            tbl.create_index(metric="cosine",
                             vector_column_name="vector",
                             replace=True)
            print("  Built LanceDB cosine vector index.")
    except Exception as ex:
        print("  [INFO] Vector index skipped: {}".format(ex))
    try:
        tbl.create_fts_index("haystack", replace=True)
        print("  Built FTS index on `haystack` (for future hybrid search).")
    except Exception as ex:
        print("  [INFO] FTS index skipped: {}".format(ex))


def _export_bin_from_table(tbl, source, lang):
    """Project the LanceDB `uuid` + `vector` columns into the .bin
    sidecar consumed by IronPython 2.7."""
    arrow_tbl = tbl.to_arrow()
    uuids = arrow_tbl["uuid"].to_pylist()
    vecs  = arrow_tbl["vector"].to_pylist()
    vectors_dict = {}
    for u, v in zip(uuids, vecs):
        vectors_dict[u] = [float(x) for x in v]
    idx = EmbeddingIndex(
        vectors=vectors_dict,
        model=MODEL,
        dim=DIM,
        source=source,
        lang=lang,
        haystack_fields=["name", "classification"],
    )
    bin_path = os.path.join(
        CACHE_DIR, "embeddings_{}_{}.bin".format(source, lang))
    idx.dump_bin(
        bin_path,
        uuid_order=uuids,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    )
    n_bytes = os.path.getsize(bin_path)
    print("  Wrote .bin sidecar ({} vectors, {:.1f} MB): {}".format(
        len(uuids), n_bytes / 1e6, bin_path))


# ---------------------------------------------------------------------------
# Cross-process build lock
# ---------------------------------------------------------------------------
#
# When the Connector's auto-build spawns the prefetcher, a Revit restart
# loses its in-memory `_prefetch_procs` dict and would re-spawn a second
# prefetcher for the same (source, lang). Two prefetchers contending on
# the same LanceDB table file-lock deadlock both. A filesystem PID lock
# fixes this without coupling the IronPython auto-build to OS-level
# process introspection.
#
# Format: `LCiA_Extension_Cache/.embedding_lock_{source}_{lang}` containing
# the owning PID as a decimal integer. A lock whose PID no longer points
# at a running python process is treated as stale and reclaimed.

def _lock_path(source, lang):
    return os.path.join(
        CACHE_DIR, ".embedding_lock_{}_{}".format(source, lang))


def _is_pid_alive(pid):
    """Best-effort liveness check. False positives (briefly) are
    tolerated - the auto-build will retry on the next Connector launch."""
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(int(pid), 0)
            return True
    except Exception:
        return False


def _acquire_build_lock(source, lang):
    """Return True iff this process owns the build lock. False means
    another live prefetcher is already building this combo."""
    path = _lock_path(source, lang)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                pid_str = (f.read() or "").strip()
            pid = int(pid_str) if pid_str else 0
            if pid and _is_pid_alive(pid) and pid != os.getpid():
                print("  [SKIP] Another prefetcher (PID {}) is already "
                      "building this combo. Lock: {}".format(pid, path))
                return False
        except Exception:
            # Malformed lock - treat as stale.
            pass
    try:
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR)
        with open(path, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as ex:
        print("  [WARN] Could not write lock file ({}). Continuing.".format(ex))
        return True


def _release_build_lock(source, lang):
    path = _lock_path(source, lang)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build(source, lang, workers=MAX_WORKERS, force=False):
    """Build (or incrementally update) the embedding index for one
    (source, lang)."""
    if not _acquire_build_lock(source, lang):
        return
    try:
        _build_inner(source, lang, workers=workers, force=force)
    finally:
        _release_build_lock(source, lang)


def _build_inner(source, lang, workers=MAX_WORKERS, force=False):
    print("\n  Loading dataset list: {} / {}".format(source, lang))
    datasets = _load_dataset_list(source, lang)
    if datasets is None:
        print("  [SKIP] ds_cache_v2_{}_{}.json not found.".format(source, lang))
        print("         Open the Ökobaudat Connector in Revit first.")
        return
    if not datasets:
        print("  [SKIP] Dataset list is empty.")
        return
    print("  {} datasets in cache.".format(len(datasets)))

    # Bilingual haystack: cross-reference the sibling-language ds_cache so
    # each dataset's vector embeds BOTH its German and English name. This is
    # what lets an English query match a German-named dataset (and vice
    # versa) reliably, instead of leaning on bge-m3's lossy cross-language
    # similarity. Classification is German in every cache; its English
    # translation is appended inside build_semantic_haystack.
    alt_lang = "en" if lang == "de" else "de"
    alt_list = _load_dataset_list(source, alt_lang) or []
    alt_names = {}
    for _d in alt_list:
        _u = _d.get("uuid")
        _n = (_d.get("name") or "").strip()
        if _u and _n:
            alt_names[_u] = _n
    if alt_names:
        print("  {} {}-language names loaded for bilingual haystack.".format(
            len(alt_names), alt_lang))
    else:
        print("  [INFO] No {}-language cache found — haystack will be "
              "mono-lingual for this build.".format(alt_lang))

    indicator_cache = _load_indicator_cache(source, lang)
    if indicator_cache:
        print("  {} datasets have indicator metadata (scalar columns).".format(
            len(indicator_cache)))
    else:
        print("  [INFO] No indicator cache for this source/lang — scalar "
              "columns will be blank. Run indicator_prefetcher.py to fill them.")

    # Healthcheck Ollama before doing any work.
    health_client = OllamaEmbeddingClient(
        base_url=OLLAMA_URL, model=MODEL, timeout_ms=TIMEOUT_MS)
    if not health_client.is_available():
        print("  [ERROR] Ollama not reachable at {}.".format(OLLAMA_URL))
        print("          Start it with `ollama serve` and ensure model "
              "`{}` is pulled.".format(MODEL))
        print("          Last error: {}".format(health_client.last_error))
        return

    db = _connect_db()
    table_name = "embeddings_{}_{}".format(source, lang)
    tbl, existing_uuids = _open_or_create_table(db, table_name)

    if force:
        to_fetch = [d for d in datasets if d.get("uuid")]
        if existing_uuids:
            print("  --force given: dropping {} existing rows.".format(
                len(existing_uuids)))
            _drop_existing_uuids(tbl, existing_uuids)
            existing_uuids = set()
    else:
        to_fetch = [d for d in datasets
                    if d.get("uuid") and d["uuid"] not in existing_uuids]

    if not to_fetch:
        print("  All embeddings already cached — nothing to do.")
        _export_bin_from_table(tbl, source, lang)
        return
    print("  {} datasets need embeddings.".format(len(to_fetch)))

    rows = []
    rows_lock = threading.Lock()
    done   = [0]
    errors = [0]
    total  = len(to_fetch)
    t_start = time.time()

    # Chunk `to_fetch` into batches of BATCH_SIZE. One HTTP call per
    # batch loads the whole batch into a single bge-m3 forward pass on
    # the GPU - empirically ~10× the throughput of one-input-per-call
    # on an RTX 4070. `workers` then runs N batched requests in parallel
    # to keep Ollama's intake busy while the GPU is grinding.
    batches = [to_fetch[i:i + BATCH_SIZE]
               for i in range(0, len(to_fetch), BATCH_SIZE)]
    print("  Embedding {} datasets in {} batches of {} (×{} parallel workers).".format(
        total, len(batches), BATCH_SIZE, workers))

    def _embed_with_fallback(prepared):
        """Try one batched call; if it fails, recursively bisect.
        Singletons retry once with a short backoff. This gracefully
        absorbs transient Ollama 500s (load-balance hiccups) without
        losing the whole batch - we end up doing slightly more requests
        but everything gets embedded eventually."""
        if not prepared:
            return []
        client = OllamaEmbeddingClient(
            base_url=OLLAMA_URL, model=MODEL, timeout_ms=TIMEOUT_MS)
        vecs = client.embed_batch([h for _, h in prepared])
        if vecs is not None and len(vecs) == len(prepared):
            return list(zip(prepared, vecs))
        # Singleton: try one retry with the original, then walk the
        # transformation ladder. First successful transform wins;
        # singletons reaching here are the ~0.05 % of inputs that
        # produce NaN in bge-m3 F16. The ladder preserves as much
        # semantic info as possible (trademark-escape ≪ strip-ASCII ≪
        # name-only ≪ truncate ≪ uppercase).
        if len(prepared) == 1:
            (ds, original_hay) = prepared[0]
            time.sleep(0.5)
            vecs = client.embed_batch([original_hay])
            if vecs and len(vecs) == 1:
                return [(prepared[0], vecs[0])]
            for tname, tfn in _TRANSFORMATIONS:
                try:
                    transformed = tfn(original_hay).strip()
                except Exception:
                    continue
                if not transformed or transformed == original_hay:
                    continue
                vecs = client.embed_batch([transformed])
                if vecs and len(vecs) == 1:
                    print("    [INFO] recovered via '{}': {}".format(
                        tname, (ds.get("uuid", "?") or "?")[:12]))
                    return [(prepared[0], vecs[0])]
            print("    [WARN] singleton embed failed after ladder: {} | {}".format(
                (ds.get("uuid", "?") or "?")[:12], client.last_error))
            return []
        # Bisect the batch - Ollama's 500s are usually OOM/queue-pressure
        # under wide payloads, not a property of any one input.
        print("    [INFO] batch of {} failed ({}); bisecting".format(
            len(prepared), (client.last_error or "")[:80]))
        mid = len(prepared) // 2
        return (_embed_with_fallback(prepared[:mid]) +
                _embed_with_fallback(prepared[mid:]))

    def _process_batch(batch):
        # Per-row prep - separate from the HTTP call so we can skip
        # empty haystacks without burning an Ollama request slot.
        prepared = []  # list of (ds, haystack)
        for ds in batch:
            name = (ds.get("name") or "").strip()
            cls  = (ds.get("classification") or "").strip()
            haystack = build_semantic_haystack(
                {"name": name, "classification": cls,
                 "name_alt": alt_names.get(ds.get("uuid"), "")})
            if haystack:
                prepared.append((ds, haystack))
            else:
                with rows_lock:
                    errors[0] += 1
        if not prepared:
            return
        results = _embed_with_fallback(prepared)
        with rows_lock:
            errors[0] += len(prepared) - len(results)
        batch_rows = []
        for (ds, _hay), vec in results:
            if len(vec) != DIM:
                with rows_lock:
                    errors[0] += 1
                continue
            batch_rows.append(_build_row(ds, indicator_cache, vec, alt_names))
        with rows_lock:
            rows.extend(batch_rows)
            done[0] += len(batch_rows)
            n = done[0]
            pct = 100 * n // total
            print("  {}/{} done ({}%)...".format(n, total, pct))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_batch, b): b for b in batches}
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                print("    [ERROR] worker exception: {}".format(exc))
                with rows_lock:
                    errors[0] += 1

    elapsed = time.time() - t_start
    rate = len(rows) / max(elapsed, 0.001)
    print("  Embedded {} vectors in {:.0f}s ({:.1f}/s).".format(
        len(rows), elapsed, rate))
    if errors[0]:
        print("  [WARN] {} datasets failed (re-run to retry).".format(errors[0]))

    # Write to LanceDB in one batch (faster than per-row inserts).
    if rows:
        tbl.add(rows)
        print("  Added {} rows to LanceDB table '{}' (total {} rows).".format(
            len(rows), table_name, tbl.count_rows()))
        _build_indexes(tbl)

    # Always re-export the sidecar so IronPython sees the latest.
    _export_bin_from_table(tbl, source, lang)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Pre-fetch dense embeddings for ÖKOBAUDAT datasets (Phase 1).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python embedding_prefetcher.py --all --workers 5
  python embedding_prefetcher.py --source a2_sphera --lang en
  python embedding_prefetcher.py --all-de
  python embedding_prefetcher.py --source a2_sphera --lang en --force

Prerequisites:
  pip install lancedb pyarrow
  ollama pull bge-m3
  ollama serve
""")
    p.add_argument("--source", choices=ALL_SOURCES,
                   help="One source key (mutually exclusive with --all)")
    p.add_argument("--lang", choices=ALL_LANGS, default="de",
                   help="Language (default de)")
    p.add_argument("--all", dest="all_combos", action="store_true",
                   help="All sources × both languages")
    p.add_argument("--all-de", dest="all_de", action="store_true",
                   help="All sources, DE only")
    p.add_argument("--workers", type=int, default=MAX_WORKERS,
                   help="Parallel batched HTTP workers (default {})".format(MAX_WORKERS))
    p.add_argument("--batch-size", dest="batch_size", type=int, default=BATCH_SIZE,
                   help="Inputs per /api/embed request (default {}; "
                        "lower if Ollama OOMs, raise if GPU isn't saturated)".format(BATCH_SIZE))
    p.add_argument("--force", action="store_true",
                   help="Re-embed every dataset, ignoring existing rows")
    p.add_argument("--list", dest="list_sources", action="store_true",
                   help="List sources and exit")
    return p.parse_args()


def main():
    args = _parse_args()
    if args.list_sources:
        print("Available sources:")
        for s in ALL_SOURCES:
            print("  " + s)
        return

    # Honour the per-run --batch-size by updating the module global.
    # Cleaner than threading it through every helper; the build()
    # function reads BATCH_SIZE directly.
    global BATCH_SIZE
    BATCH_SIZE = max(1, args.batch_size)

    header = "  Workers: {} | Batch: {} | Model: {} | Dim: {}".format(
        args.workers, BATCH_SIZE, MODEL, DIM)

    if args.all_combos:
        print("=" * 60)
        print("Embedding ALL sources × BOTH languages")
        print(header)
        print("=" * 60)
        for s in ALL_SOURCES:
            for l in ALL_LANGS:
                build(s, l, workers=args.workers, force=args.force)
        print("\nDone.")
    elif args.all_de:
        print("=" * 60)
        print("Embedding ALL sources × DE")
        print(header)
        print("=" * 60)
        for s in ALL_SOURCES:
            build(s, "de", workers=args.workers, force=args.force)
        print("\nDone.")
    elif args.source:
        print("=" * 60)
        print("Embedding: {} / {}".format(args.source, args.lang))
        print(header)
        print("=" * 60)
        build(args.source, args.lang, workers=args.workers, force=args.force)
        print("\nDone.")
    else:
        print("Please specify --source, --all, or --all-de. Use --help.")
        sys.exit(1)


if __name__ == "__main__":
    main()
