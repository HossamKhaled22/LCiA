# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 benchmark - full-stack Recall@K + MRR.

Extends the Phase 0 benchmark (regex / fuzzy / BM25F variants) with the
Phase 1 and Phase 1+ retrieval modes, in one comparable table:

    semantic    cosine over bge-m3 embeddings (Ollama)            [Phase 1]
    hybrid      Reciprocal Rank Fusion of BM25F+typo+trigram      [Phase 1+]
                and semantic (Cormack, Clarke & Buttcher 2009, k=60)
    reranker    cross-encoder rerank of the hybrid top-K          [Phase 1+]
                (BAAI/bge-reranker-v2-m3 via rerank_service.py)

Reuses `tools/phase0_benchmark.py` helpers and `search_helpers` primitives,
so the ranking + metric code is the SAME single source of truth as the
live UI and the Validation pushbutton (no drift, fair cross-mode compare).

Prereqs:
    * embeddings_a2_sphera_en.bin present in LCiA_Extension_Cache/
    * Ollama running with bge-m3   (http://localhost:11434)
    * rerank_service.py running    (http://127.0.0.1:11500) for reranker mode

Outputs (pushbutton root):
    phase1_benchmark.md, phase1_benchmark.json

Usage (CPython 3, no Revit needed):
    python tools/phase1_benchmark.py
"""
from __future__ import print_function
import io
import json
import os
import sys
import time

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _p in (PARENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import phase0_benchmark as p0
from search_helpers import (
    search_regex, search_fuzzy, CorpusStats,
    search_semantic, reciprocal_rank_fusion, rrf_max_score,
    LocalRerankerClient, OllamaEmbeddingClient, build_semantic_haystack,
    topk_hit, rank_of_first_hit, wilson_ci, bootstrap_ci,
)
try:
    from constants import (HYBRID_RRF_K, RERANKER_TOP_K,
                           RERANKER_BASE_URL, RERANKER_TIMEOUT_MS)
except Exception:
    HYBRID_RRF_K, RERANKER_TOP_K = 60, 20
    RERANKER_BASE_URL, RERANKER_TIMEOUT_MS = "http://127.0.0.1:11500", 15000

TOPK     = (1, 3, 5, 10, 20)
OUT_MD   = os.path.join(PARENT, "phase1_benchmark.md")
OUT_JSON = os.path.join(PARENT, "phase1_benchmark.json")

ALL_MODES = ["regex", "fuzzy", "bm25f", "bm25f+typo", "bm25f+typo+trigram",
             "semantic", "hybrid", "reranker"]


def _uuid(ds):
    return ds["uuid"] if isinstance(ds, dict) else getattr(ds, "uuid", None)


def main():
    t0 = time.time()
    cache = p0._load_json(p0.CACHE_PATH)
    gt    = p0._load_json(p0.GROUND_TRUTH)
    datasets    = cache["results"]
    searchables = [p0.build_searchable(ds, lang=p0.RUN_LANG) for ds in datasets]
    uuid_index  = {ds["uuid"]: ds for ds in datasets}
    uuid_to_sh  = {ds["uuid"]: sh for ds, sh in zip(datasets, searchables)}

    print("Building CorpusStats over {} datasets...".format(len(datasets)))
    stats = CorpusStats.build(searchables)
    calibrator = p0._load_calibrator()

    idx = p0._load_embedding_index()
    # Own client with a generous timeout: bge-m3's cold load (~20-30s when
    # other large models occupy Ollama) exceeds phase0's 15s default and
    # silently zeroed semantic mode on the first run. 60s absorbs any reload.
    client = None
    if idx is not None:
        client = OllamaEmbeddingClient(model="bge-m3", timeout_ms=60000)
        if not client.is_available():
            client = None
    semantic_ok = (idx is not None and client is not None)
    if not semantic_ok:
        print("ERROR: semantic backing unavailable (embeddings or Ollama). "
              "This benchmark needs them; aborting.")
        return 1
    # Warm the model so per-query embeds run at warm latency (~4s) rather
    # than each tripping the cold-load path.
    print("Warming up bge-m3 (cold load can take ~20-30s)...")
    _wv = client.embed("concrete")
    print("  warm-up: {} ({} dims)".format(
        "OK" if _wv else "FAILED: " + repr(client.last_error),
        len(_wv) if _wv else 0))
    if not _wv:
        print("ERROR: warm-up embed failed; aborting.")
        return 1

    rr = LocalRerankerClient(base_url=RERANKER_BASE_URL,
                             timeout_ms=RERANKER_TIMEOUT_MS)
    reranker_ok = rr.is_available()
    if reranker_ok:
        print("Reranker sidecar ready: {} ({})".format(rr.model, RERANKER_BASE_URL))
    else:
        print("Note: reranker sidecar not ready ({}) - 'reranker' mode will "
              "mirror 'hybrid'.".format(rr.last_error))

    entries = gt["entries"]
    match_entries = [e for e in entries if e.get("label") == "match"]
    skipped       = [e for e in entries if e.get("label") != "match"]
    n = len(match_entries)
    print("Loaded {} datasets, {} ground-truth entries ({} match-labelled)".format(
        len(datasets), len(entries), n))

    modes = list(ALL_MODES)
    topk_hits       = {m: {k: 0 for k in TOPK} for m in modes}
    any_hits        = {m: 0 for m in modes}
    rr_recip        = {m: [] for m in modes}
    per_class_hits5 = {m: {} for m in modes}
    per_class_n     = {}
    per_query_rows  = []

    btt_params = {"_typo_expand": True, "alpha_3g": 0.5}

    for entry in match_entries:
        q = entry["name"]
        correct = entry.get("correct_uuid", "")
        acceptable = set(entry.get("acceptable_uuids", []) or [])
        if correct:
            acceptable.add(correct)
        cls = entry.get("class", "")
        per_class_n[cls] = per_class_n.get(cls, 0) + 1

        ranked = {}
        ranked["regex"] = search_regex(q, datasets, searchables=searchables)
        ranked["fuzzy"] = search_fuzzy(q, datasets, searchables=searchables)
        ranked["bm25f"] = p0._run_bm25f_variant(
            q, datasets, searchables, stats, {}, calibrator)
        ranked["bm25f+typo"] = p0._run_bm25f_variant(
            q, datasets, searchables, stats, {"_typo_expand": True}, calibrator)
        btt = p0._run_bm25f_variant(
            q, datasets, searchables, stats, btt_params, calibrator)
        ranked["bm25f+typo+trigram"] = btt

        # Phase 1 - semantic (one Ollama embed per query, reused downstream)
        qv = client.embed(q)
        sem = search_semantic(q, datasets, searchables=searchables,
                              embedding_index=idx, ollama_client=client,
                              query_vector=qv) if qv is not None else []
        ranked["semantic"] = sem

        # Phase 1+ - hybrid (RRF of bm25f+typo+trigram and semantic rankings)
        if btt and sem:
            bm_uuids = [_uuid(d) for d, _ in btt]
            se_uuids = [_uuid(d) for d, _ in sem]
            rrf = reciprocal_rank_fusion([bm_uuids, se_uuids], k=HYBRID_RRF_K)
            hyb = sorted(
                [(uuid_index[u], s) for u, s in rrf.items() if u in uuid_index],
                key=lambda t: t[1], reverse=True)
        else:
            hyb = btt or sem
        ranked["hybrid"] = hyb

        # Phase 1+ - cross-encoder rerank of the hybrid top-K
        rer = hyb
        if hyb and reranker_ok:
            topk = hyb[:RERANKER_TOP_K]
            docs = [build_semantic_haystack(uuid_to_sh.get(_uuid(d), {}))
                    for d, _ in topk]
            scores = rr.rerank(q, docs)
            if scores and len(scores) == len(topk):
                reordered = sorted(zip([d for d, _ in topk], scores),
                                   key=lambda t: t[1], reverse=True)
                rer = [(d, s) for d, s in reordered] + hyb[RERANKER_TOP_K:]
        ranked["reranker"] = rer

        row = {"query": q, "class": cls,
               "correct": uuid_index.get(correct, {}).get("name", "?")[:50]}
        for m in modes:
            rk = ranked[m]
            hit = any(_uuid(d) in acceptable for d, _ in rk)
            if hit:
                any_hits[m] += 1
            for k in TOPK:
                if topk_hit(rk, acceptable, k):
                    topk_hits[m][k] += 1
            first = rank_of_first_hit(rk, acceptable)
            rr_recip[m].append(1.0 / first if (hit and first) else 0.0)
            h5 = topk_hit(rk, acceptable, 5)
            per_class_hits5[m][cls] = per_class_hits5[m].get(cls, 0) + (1 if h5 else 0)
            row[m + "_rank"] = first if hit else None
        per_query_rows.append(row)
        print("  q: {:<28} sem#{} hyb#{} rer#{}".format(
            q[:28],
            row.get("semantic_rank"), row.get("hybrid_rank"),
            row.get("reranker_rank")))

    duration = time.time() - t0

    # ----- aggregate tables -----
    recall_table = []
    for m in modes:
        rec = {"mode": m}
        for k in TOPK:
            p, lo, hi = wilson_ci(topk_hits[m][k], n)
            rec["recall@{}".format(k)] = {"hits": topk_hits[m][k], "n": n,
                                          "pct": p, "lo": lo, "hi": hi}
        p, lo, hi = wilson_ci(any_hits[m], n)
        rec["recall@all"] = {"hits": any_hits[m], "n": n, "pct": p, "lo": lo, "hi": hi}
        recall_table.append(rec)

    mrr_table = []
    for m in modes:
        pt, lo, hi = bootstrap_ci(rr_recip[m])
        mrr_table.append({"mode": m, "mrr": pt, "lo": lo, "hi": hi})

    classes = sorted(per_class_n.keys())
    per_class_table = []
    for c in classes:
        rowc = {"class": c, "n": per_class_n[c]}
        for m in modes:
            hits = per_class_hits5[m].get(c, 0)
            rowc[m] = {"hits": hits, "n": per_class_n[c],
                       "pct": hits / float(per_class_n[c]) if per_class_n[c] else 0.0}
        per_class_table.append(rowc)

    out_json = {
        "_schema":    "phase1_benchmark_v1",
        "datastock":  "a2_sphera_en",
        "n_match":    n,
        "n_datasets": len(datasets),
        "generated":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": duration,
        "modes":      modes,
        "embedding_model": getattr(idx, "model", "bge-m3"),
        "reranker_model":  (rr.model if reranker_ok else None),
        "rrf_k":           HYBRID_RRF_K,
        "reranker_top_k":  RERANKER_TOP_K,
        "recall_table":    recall_table,
        "mrr_table":       mrr_table,
        "per_class_table": per_class_table,
    }
    with io.open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out_json, indent=2, ensure_ascii=False, sort_keys=False))

    # ----- markdown -----
    L = []
    L.append("# Phase 1 benchmark - full-stack Recall@K + MRR")
    L.append("")
    L.append("Auto-generated by `tools/phase1_benchmark.py`. Lexical modes reproduce "
             "`phase0_benchmark.py`; semantic / hybrid / reranker add the Phase 1(+) stack.")
    L.append("")
    L.append("- **Datastock**: A2 Sphera EN ({} datasets)".format(len(datasets)))
    L.append("- **Ground truth**: UK Sample Project, {} entries "
             "({} match-labelled, {} skipped)".format(len(entries), n, len(skipped)))
    L.append("- **Embedding model**: {} (1024-d, Ollama)".format(getattr(idx, "model", "bge-m3")))
    L.append("- **Reranker**: {} (top-{})".format(
        rr.model if reranker_ok else "n/a", RERANKER_TOP_K))
    L.append("- **RRF**: Reciprocal Rank Fusion of BM25F+typo+trigram and semantic, k={}".format(HYBRID_RRF_K))
    L.append("- **CIs**: 95% Wilson for Recall@K; 1000-replicate bootstrap for MRR.")
    L.append("- **Generated**: {} (run {:.1f}s)".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), duration))
    L.append("")

    L.append("## Recall@K (% of {} queries, 95% Wilson CI)".format(n))
    L.append("")
    hdr = "| Mode |"
    sep = "|---|"
    for k in TOPK:
        hdr += " R@{} |".format(k)
        sep += "---:|"
    hdr += " R@all |"
    sep += "---:|"
    L.append(hdr)
    L.append(sep)
    for rec in recall_table:
        line = "| **{}** ".format(rec["mode"])
        for k in TOPK:
            c = rec["recall@{}".format(k)]
            line += "| {:.0f}% ".format(100 * c["pct"])
        c = rec["recall@all"]
        line += "| {:.0f}% |".format(100 * c["pct"])
        L.append(line)
    L.append("")

    L.append("## MRR (mean reciprocal rank, 1000-bootstrap 95% CI)")
    L.append("")
    L.append("| Mode | MRR | 95% CI |")
    L.append("|---|---:|:---:|")
    for r in mrr_table:
        L.append("| **{}** | {:.3f} | [{:.3f}, {:.3f}] |".format(
            r["mode"], r["mrr"], r["lo"], r["hi"]))
    L.append("")

    L.append("## Per-class Recall@5")
    L.append("")
    hdr = "| Class | n |"
    sep = "|---|---:|"
    for m in modes:
        hdr += " {} |".format(m)
        sep += "---:|"
    L.append(hdr)
    L.append(sep)
    for rowc in per_class_table:
        line = "| {} | {} ".format(rowc["class"], rowc["n"])
        for m in modes:
            line += "| {:.0f}% ".format(100 * rowc[m]["pct"])
        line += "|"
        L.append(line)
    L.append("")

    L.append("## Per-query rank of first hit")
    L.append("")
    hdr = "| Query | Class |"
    sep = "|---|---|"
    for m in modes:
        hdr += " {} |".format(m)
        sep += "---:|"
    L.append(hdr)
    L.append(sep)
    for r in per_query_rows:
        line = "| {} | {} ".format(r["query"], r["class"])
        for m in modes:
            rk = r[m + "_rank"]
            line += "| {} ".format(rk if rk is not None else "-")
        line += "|"
        L.append(line)
    L.append("")

    with io.open(OUT_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")

    print("Wrote", OUT_MD)
    print("Wrote", OUT_JSON)
    print("Run took {:.1f}s".format(duration))
    return 0


if __name__ == "__main__":
    sys.exit(main())
