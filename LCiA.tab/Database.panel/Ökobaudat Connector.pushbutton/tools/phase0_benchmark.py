# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Phase 0 benchmark - Recall@K + MRR for regex, fuzzy, and BM25F variants.

Runs five search modes against the A2 Sphera EN dataset cache, using
the UK Sample Project ground truth at:

    sample_project_a2_sphera_en_v0.6__opus-4.8.json   (pushbutton root)

Modes compared:
    regex                       binary regex filter, ranked by legacy _fuzzy_score
    fuzzy                       legacy fuzzy filter + legacy _fuzzy_score ranker
    bm25f                       legacy fuzzy filter + BM25F ranker (Phase 0 v7)
    bm25f+typo                  BM25F + bounded Levenshtein query expansion
    bm25f+typo+trigram          BM25F + typo + character-3-gram BM25 blend

Headline metrics:
    Recall@{1,3,5,10}           per mode with 95% Wilson binomial CI
    MRR                          mean reciprocal rank, with 1000-replicate bootstrap CI
    Per-class breakdown          Recall@5 grouped by ground-truth class label

Outputs:
    phase0_benchmark.md (pushbutton root) -- markdown report
    phase0_benchmark.json (pushbutton root) -- machine-readable numbers

Usage (CPython 3, no Revit / IronPython needed):
    python tools/phase0_benchmark.py
"""
from __future__ import print_function
import io
import json
import os
import sys
import time

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from search_helpers import (
    build_searchable,
    search_regex,
    search_fuzzy,
    search_bm25f,
    topk_hit,
    rank_of_first_hit,
    CorpusStats,
    IsotonicCalibrator,
    expand_query_with_typos,
    _fuzzy_match_inner,
    bm25f_score,
    _tokenize,
    # Phase 1 - dense semantic retrieval
    EmbeddingIndex,
    OllamaEmbeddingClient,
    search_semantic,
    # Evaluation metrics (single source of truth - also used by the
    # live Validation pushbutton, so any drift would invalidate the
    # cross-environment fairness claim)
    wilson_ci, bootstrap_ci, WILSON_Z, BOOTSTRAP_B,
)

CACHE_PATH = os.path.join(
    PARENT, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_en.json"
)
GROUND_TRUTH = os.path.join(PARENT, "sample_project_a2_sphera_en_v0.6__opus-4.8.json")
OUT_PATH      = os.path.join(PARENT, "phase0_benchmark.md")
OUT_JSON_PATH = os.path.join(PARENT, "phase0_benchmark.json")
TOPK = (1, 3, 5, 10)
RUN_LANG = "en" if "_en.json" in CACHE_PATH else "de"


# ---------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------

def _load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_calibrator():
    """Best-effort isotonic calibrator load. Missing file is fine; the
    benchmark just reports raw + soft-bounded scores instead."""
    cal_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "calibration_a2_sphera_{}.json".format(RUN_LANG)
    )
    if not os.path.exists(cal_path):
        return None
    try:
        d = _load_json(cal_path)
        return IsotonicCalibrator.from_dict(d)
    except Exception as ex:
        print("Warn: could not load calibrator at {}: {}".format(cal_path, ex))
        return None


def _load_embedding_index():
    """Best-effort embedding sidecar load. Missing file disables
    semantic mode in the benchmark (with a printed warning) but does
    not abort - Phase 0 modes still run."""
    bin_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "embeddings_a2_sphera_{}.bin".format(RUN_LANG),
    )
    if not os.path.exists(bin_path):
        print("Note: no embedding sidecar at {} — semantic mode skipped.".format(
            os.path.basename(bin_path)))
        print("      Run `python embedding_prefetcher.py --source a2_sphera "
              "--lang {}` to enable it.".format(RUN_LANG))
        return None
    try:
        idx = EmbeddingIndex.load_bin(bin_path)
        print("Loaded {} semantic vectors (model={}, dim={}) from {}".format(
            len(idx), idx.model, idx.dim, os.path.basename(bin_path)))
        return idx
    except Exception as ex:
        print("Warn: could not load embedding sidecar at {}: {}".format(
            bin_path, ex))
        return None


def _make_ollama_client():
    """Construct + healthcheck an Ollama client. Returns None when
    Ollama isn't reachable; semantic mode skips in that case."""
    client = OllamaEmbeddingClient(model="bge-m3", timeout_ms=15000)
    if not client.is_available():
        print("Note: Ollama not reachable — semantic mode skipped. {}".format(
            client.last_error or ""))
        return None
    return client


# ---------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------

def _run_bm25f_variant(query, datasets, searchables, stats, params, calibrator):
    """Run BM25F with given params. Identical filter to search_fuzzy."""
    nl = (query or "").lower()
    tokens = _tokenize(nl)
    if not nl:
        return []
    if (params or {}).get("_typo_expand", False):
        expanded = expand_query_with_typos(nl, stats)
    else:
        expanded = [(t, 1.0) for t in tokens]
    out = []
    for ds, hay in zip(datasets, searchables):
        if not _fuzzy_match_inner(nl, tokens, hay):
            continue
        # Strip the marker key from params before passing to bm25f_score
        clean_params = {k: v for k, v in (params or {}).items()
                        if not k.startswith("_")}
        raw = bm25f_score(nl, hay, stats, params=clean_params,
                          expanded_query=expanded)
        if calibrator is not None and calibrator.knots:
            out.append((ds, calibrator.apply(raw)))
        else:
            out.append((ds, raw))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _make_modes(stats, calibrator, embedding_index=None, ollama_client=None):
    """Return ordered list of (label, runner_fn). Each runner takes
    (query, datasets, searchables) and returns a ranked list. The
    semantic runner is included only when BOTH `embedding_index` and
    `ollama_client` are available - otherwise the benchmark reports
    the existing 5 Phase 0 modes and prints a note explaining the
    omission."""
    base_params = {}
    typo_params = {"_typo_expand": True}
    typo_trig_params = {"_typo_expand": True, "alpha_3g": 0.5}
    modes = [
        ("regex",              lambda q, ds, sh:
         search_regex(q, ds, searchables=sh)),
        ("fuzzy",              lambda q, ds, sh:
         search_fuzzy(q, ds, searchables=sh)),
        ("bm25f",              lambda q, ds, sh:
         _run_bm25f_variant(q, ds, sh, stats, base_params, calibrator)),
        ("bm25f+typo",         lambda q, ds, sh:
         _run_bm25f_variant(q, ds, sh, stats, typo_params, calibrator)),
        ("bm25f+typo+trigram", lambda q, ds, sh:
         _run_bm25f_variant(q, ds, sh, stats, typo_trig_params, calibrator)),
    ]
    if embedding_index is not None and ollama_client is not None:
        # Phase 1: cosine over bge-m3 embeddings. One Ollama call per
        # query (re-used across all candidate datasets within a query).
        modes.append(
            ("semantic", lambda q, ds, sh: search_semantic(
                q, ds, searchables=sh,
                embedding_index=embedding_index,
                ollama_client=ollama_client,
                lang=RUN_LANG)))
    return modes


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    t_start = time.time()
    cache = _load_json(CACHE_PATH)
    gt    = _load_json(GROUND_TRUTH)

    datasets = cache["results"]
    searchables = [build_searchable(ds, lang=RUN_LANG) for ds in datasets]
    uuid_index  = {ds["uuid"]: ds for ds in datasets}

    print("Building CorpusStats over {} datasets...".format(len(datasets)))
    stats = CorpusStats.build(searchables)
    print("  vocab: {} tokens; avgdl_name={:.2f}, avgdl_class={:.2f}".format(
        len(stats.vocab), stats.avgdl_name, stats.avgdl_class))

    calibrator = _load_calibrator()
    if calibrator is None:
        print("  no calibrator JSON found; scores are raw BM25F")
    else:
        print("  loaded isotonic calibrator ({} knots)".format(len(calibrator.knots)))

    # Phase 1 - try to enable semantic mode. Either failing component
    # silently downgrades to the 5-mode Phase 0 benchmark.
    embedding_index = _load_embedding_index()
    ollama_client   = _make_ollama_client() if embedding_index is not None else None

    entries = gt["entries"]
    match_entries = [e for e in entries if e.get("label") == "match"]
    skipped       = [e for e in entries if e.get("label") != "match"]
    n = len(match_entries)
    print("Loaded {} datasets, {} ground-truth entries ({} match-labelled)".format(
        len(datasets), len(entries), n))

    modes = _make_modes(stats, calibrator,
                        embedding_index=embedding_index,
                        ollama_client=ollama_client)
    mode_labels = [m[0] for m in modes]

    # Per-mode aggregates
    topk_hits = {m: {k: 0 for k in TOPK} for m in mode_labels}
    any_hits  = {m: 0 for m in mode_labels}
    ranks     = {m: [] for m in mode_labels}    # 1/rank for MRR (0 if miss)
    first_ranks = {m: [] for m in mode_labels}  # rank or None
    per_class_hits5 = {m: {} for m in mode_labels}   # cls -> hits@5
    per_class_n     = {}                             # cls -> count

    per_query_rows = []

    for entry in match_entries:
        query     = entry["name"]
        correct   = entry.get("correct_uuid", "")
        acceptable = set(entry.get("acceptable_uuids", []) or [])
        if correct:
            acceptable.add(correct)
        cls = entry.get("class", "")
        per_class_n[cls] = per_class_n.get(cls, 0) + 1

        row = {"query": query, "class": cls,
               "correct": uuid_index.get(correct, {}).get("name", "?")[:55]}

        for label, runner in modes:
            ranked = runner(query, datasets, searchables)
            any_hit = any(ds["uuid"] in acceptable for ds, _ in ranked)
            if any_hit:
                any_hits[label] += 1
            for k in TOPK:
                if topk_hit(ranked, acceptable, k):
                    topk_hits[label][k] += 1
            first = rank_of_first_hit(ranked, acceptable)
            if any_hit:
                first_ranks[label].append(first)
                ranks[label].append(1.0 / first)
            else:
                first_ranks[label].append(None)
                ranks[label].append(0.0)
            # Per-class Recall@5
            hit5 = topk_hit(ranked, acceptable, 5)
            per_class_hits5[label][cls] = per_class_hits5[label].get(cls, 0) + (1 if hit5 else 0)
            top1 = ranked[0][0]["name"][:55] if ranked else "-"
            row[label + "_top1"] = top1
            row[label + "_hit"]  = any_hit
            row[label + "_rank"] = first if any_hit else None
            row[label + "_n"]    = len(ranked)
        per_query_rows.append(row)

    duration = time.time() - t_start

    # ----- Recall@K table with Wilson CIs -----
    recall_table = []
    for m in mode_labels:
        rec = {"mode": m}
        for k in TOPK:
            p, lo, hi = wilson_ci(topk_hits[m][k], n)
            rec["recall@{}".format(k)] = {"hits": topk_hits[m][k],
                                          "n": n, "pct": p, "lo": lo, "hi": hi}
        p, lo, hi = wilson_ci(any_hits[m], n)
        rec["recall@all"] = {"hits": any_hits[m], "n": n,
                             "pct": p, "lo": lo, "hi": hi}
        recall_table.append(rec)

    # ----- MRR with bootstrap CI -----
    mrr_table = []
    for m in mode_labels:
        pt, lo, hi = bootstrap_ci(ranks[m])
        mrr_table.append({"mode": m, "mrr": pt, "lo": lo, "hi": hi})

    # ----- Per-class Recall@5 -----
    classes = sorted(per_class_n.keys())
    per_class_table = []
    for c in classes:
        row = {"class": c, "n": per_class_n[c]}
        for m in mode_labels:
            hits = per_class_hits5[m].get(c, 0)
            row[m] = {"hits": hits, "n": per_class_n[c],
                      "pct": (hits / float(per_class_n[c]) if per_class_n[c] else 0.0)}
        per_class_table.append(row)

    # ----- Write JSON -----
    out_json = {
        "_schema":   "phase0_benchmark_v2",
        "datastock": "a2_sphera_en",
        "n_match":   n,
        "n_datasets": len(datasets),
        "generated":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": duration,
        "modes":      mode_labels,
        "recall_table":   recall_table,
        "mrr_table":      mrr_table,
        "per_class_table": per_class_table,
        "corpus_stats": {
            "vocab_size":   len(stats.vocab),
            "avgdl_name":   stats.avgdl_name,
            "avgdl_class":  stats.avgdl_class,
        },
        "bm25_params":  {
            "k1":      1.5, "b_name": 0.75, "b_class": 0.75,
            "w_name":  1.0, "w_class": 0.5,
            "alpha_3g_for_typo_trigram_mode": 0.5,
        },
        "calibrator": ("isotonic ({} knots)".format(len(calibrator.knots))
                       if calibrator and calibrator.knots else None),
    }
    with io.open(OUT_JSON_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out_json, indent=2, ensure_ascii=False, sort_keys=False))

    # ----- Markdown report -----
    lines = []
    lines.append("# Phase 0 benchmark - regex / fuzzy / BM25F Recall@K + MRR")
    lines.append("")
    lines.append("Auto-generated by `tools/phase0_benchmark.py`. See "
                 "the validation methodology chapter of the thesis for context.")
    lines.append("")
    lines.append("- **Datastock**: A2 Sphera EN ({} datasets cached)".format(len(datasets)))
    lines.append("- **Ground truth**: UK Sample Project, {} entries "
                 "({} match-labelled, {} skip / no-match excluded)".format(
                     len(entries), n, len(skipped)))
    lines.append("- **Source**: `sample_project_a2_sphera_en_v0.6__opus-4.8.json` (pushbutton root)")
    lines.append("- **Hit definition**: dataset's UUID matches `correct_uuid` "
                 "or any UUID in `acceptable_uuids` for that ground-truth entry.")
    lines.append("- **CIs**: 95% Wilson binomial intervals for Recall@K; "
                 "1000-replicate bootstrap percentile intervals for MRR.")
    lines.append("- **Corpus stats**: vocab = {} tokens; "
                 "avgdl_name = {:.2f}, avgdl_class = {:.2f}".format(
                     len(stats.vocab), stats.avgdl_name, stats.avgdl_class))
    lines.append("- **BM25 params**: k1=1.5, b_name=0.75, b_class=0.75, "
                 "w_name=1.0, w_class=0.5; alpha_3g=0.5 for the trigram variant.")
    lines.append("- **Calibrator**: {}".format(out_json["calibrator"] or "none (raw BM25F + soft bound)"))
    lines.append("- **Generated**: {} (run took {:.2f}s)".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), duration))
    lines.append("")

    # Recall@K headline
    lines.append("## Headline - Recall@K (% of {} match-labelled queries, 95% Wilson CI)".format(n))
    lines.append("")
    hdr = "| Mode |"
    sep = "|---|"
    for k in TOPK:
        hdr += " Recall@{} |".format(k)
        sep += "---:|"
    hdr += " Recall (any) |"; sep += "---:|"
    lines.append(hdr); lines.append(sep)
    for rec in recall_table:
        row = "| **{}** ".format(rec["mode"])
        for k in TOPK:
            cell = rec["recall@{}".format(k)]
            row += "| {h}/{n} ({p:.0f}% [{lo:.0f}-{hi:.0f}]) ".format(
                h=cell["hits"], n=cell["n"],
                p=100*cell["pct"], lo=100*cell["lo"], hi=100*cell["hi"])
        any_cell = rec["recall@all"]
        row += "| {h}/{n} ({p:.0f}% [{lo:.0f}-{hi:.0f}]) |".format(
            h=any_cell["hits"], n=any_cell["n"],
            p=100*any_cell["pct"], lo=100*any_cell["lo"], hi=100*any_cell["hi"])
        lines.append(row)
    lines.append("")

    # MRR
    lines.append("## Mean Reciprocal Rank (MRR, 1000-bootstrap 95% CI)")
    lines.append("")
    lines.append("MRR is the mean of `1 / rank_of_first_hit` over all {} match-labelled queries "
                 "(0 contributed by complete misses). Higher is better.".format(n))
    lines.append("")
    lines.append("| Mode | MRR | 95% CI |")
    lines.append("|---|---:|:---:|")
    for r in mrr_table:
        lines.append("| **{}** | {:.3f} | [{:.3f}, {:.3f}] |".format(
            r["mode"], r["mrr"], r["lo"], r["hi"]))
    lines.append("")

    # Per-class Recall@5
    lines.append("## Per-class Recall@5")
    lines.append("")
    hdr = "| Class | n |"; sep = "|---|---:|"
    for m in mode_labels:
        hdr += " {} |".format(m); sep += "---:|"
    lines.append(hdr); lines.append(sep)
    for row in per_class_table:
        line = "| {} | {} ".format(row["class"], row["n"])
        for m in mode_labels:
            cell = row[m]
            line += "| {h}/{n} ({p:.0f}%) ".format(
                h=cell["hits"], n=cell["n"], p=100*cell["pct"])
        line += "|"
        lines.append(line)
    lines.append("")

    # Ablation
    lines.append("## Ablation: marginal Recall@5 lift")
    lines.append("")
    by_mode = {r["mode"]: r["recall@5"]["pct"] for r in recall_table}
    pieces = []
    pieces.append(("regex -> fuzzy (legacy)",
                   by_mode.get("fuzzy", 0) - by_mode.get("regex", 0)))
    pieces.append(("fuzzy -> bm25f (replace ranker)",
                   by_mode.get("bm25f", 0) - by_mode.get("fuzzy", 0)))
    pieces.append(("bm25f -> +typo (query expansion)",
                   by_mode.get("bm25f+typo", 0) - by_mode.get("bm25f", 0)))
    pieces.append(("+typo -> +trigram (compound-words)",
                   by_mode.get("bm25f+typo+trigram", 0) - by_mode.get("bm25f+typo", 0)))
    lines.append("| Step | dRecall@5 |")
    lines.append("|---|---:|")
    for name, d in pieces:
        sign = "+" if d >= 0 else ""
        lines.append("| {} | {}{:.1f} pp |".format(name, sign, 100 * d))
    lines.append("")

    # Per-query breakdown (compact)
    lines.append("## Per-query breakdown (rank of first hit per mode)")
    lines.append("")
    hdr = "| Query | Class | Expected (top accepted) |"; sep = "|---|---|---|"
    for m in mode_labels:
        hdr += " {} |".format(m); sep += "---:|"
    lines.append(hdr); lines.append(sep)
    for r in per_query_rows:
        line = "| {q} | {c} | {e} ".format(
            q=r["query"], c=r["class"], e=r["correct"])
        for m in mode_labels:
            rk = r[m + "_rank"]
            line += "| {} ".format(rk if rk is not None else "-")
        line += "|"
        lines.append(line)
    lines.append("")

    if skipped:
        lines.append("## Excluded entries")
        lines.append("")
        for e in skipped:
            lines.append("- **{}** - label `{}`. {}".format(
                e.get("name", "?"),
                e.get("label", "?"),
                e.get("notes", "").replace("\n", " "),
            ))
        lines.append("")

    # Interpretation
    lines.append("## Interpretation")
    lines.append("")
    lines.append("These numbers form the Phase 0 baseline floor that Phase 1 "
                 "(semantic embeddings) and Phase 4 (LLM rerank) must beat. "
                 "Every comparison below is between modes that share the same "
                 "binary candidate set (legacy fuzzy filter); only the ranker "
                 "differs.")
    lines.append("")

    out_dir = os.path.dirname(OUT_PATH)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    with io.open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print("Wrote", OUT_PATH)
    print("Wrote", OUT_JSON_PATH)
    print("Run took {:.2f}s".format(duration))


if __name__ == "__main__":
    main()
