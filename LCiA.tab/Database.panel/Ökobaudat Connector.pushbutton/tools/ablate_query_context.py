# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Ablation study - does BIM-context query enrichment improve retrieval?

Answers the thesis research question: *which BIM metadata fields, added to the
search query, raise rank quality against ÖKOBAUDAT - and which add noise?*

For each field-set variant (cumulative ladder V0…V8 + leave-one-out, defined in
`query_context.VARIANTS`) it re-builds the query from the GT's BIM fields,
re-ranks every match-labelled material, and reports R@K / MRR / nDCG@10 with
95 % CIs and a McNemar test vs. the name-only baseline (V0). Document side is
unchanged (the existing bilingual embedding) - only the *query* varies - so the
cosine ranking is vectorised with numpy (identical math, ~1000× faster than the
pure-Python loop) and the metric primitives are reused verbatim from
`search_helpers` (single source of truth, comparable to phase1_benchmark §4.x).

Prereqs (CPython 3): Ollama up with bge-m3; `embeddings_a2_sphera_<lang>.bin`
and `ds_cache_v2_a2_sphera_<lang>.json` present. `--reranker` additionally needs
`rerank_service.py` running on :11500.

Usage:
    python tools/ablate_query_context.py --lang en
    python tools/ablate_query_context.py --lang de --reranker
    python tools/ablate_query_context.py --lang en --variants V0_name,V8_ALL_+host_types
"""
from __future__ import print_function
import argparse
import io
import json
import os
import sys
import time

import numpy as np

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _p in (PARENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from search_helpers import (
    search_fuzzy, CorpusStats, build_searchable,
    reciprocal_rank_fusion,
    EmbeddingIndex, OllamaEmbeddingClient, LocalRerankerClient,
    build_semantic_haystack,
    wilson_ci, bootstrap_ci, ndcg_at_k, mcnemar_pvalue,
)
from phase0_benchmark import _run_bm25f_variant
from query_context import build_enriched_query, VARIANTS, FIELD_KEYS, get_field_piece

try:
    from constants import (HYBRID_RRF_K, RERANKER_TOP_K,
                           RERANKER_BASE_URL, RERANKER_TIMEOUT_MS)
except Exception:
    HYBRID_RRF_K, RERANKER_TOP_K = 60, 20
    RERANKER_BASE_URL, RERANKER_TIMEOUT_MS = "http://127.0.0.1:11500", 15000

TOPK = (1, 3, 5, 10, 20)
# BM25F+trigram (NO typo expansion). `expand_query_with_typos` scans the whole
# vocabulary per query token, which is pathologically slow on the long enriched
# queries this ablation builds (V8 carries dozens of host_type tokens) and adds
# nothing on the clean, typo-free GT material names. Trigram blend is kept.
BTT_PARAMS = {"alpha_3g": 0.5}

GT_BY_LANG = {
    "en": "sample_project_a2_sphera_en_v0.6__opus-4.8.json",
    "de": "golden_nugget_a2_sphera_de_v0.6__opus-4.8.json",
}


# ── uuid-keyed ranking metrics (mirror search_helpers.topk_hit /
#    rank_of_first_hit, which take (ds, score) tuples; here every mode is
#    reduced to a ranked uuid list so semantic/hybrid/reranker are uniform). ──
def _topk_hit_u(ranked_uuids, accept, k):
    for u in ranked_uuids[:k]:
        if u in accept:
            return True
    return False


def _rank_first_u(ranked_uuids, accept):
    for i, u in enumerate(ranked_uuids, 1):
        if u in accept:
            return i
    return None


def _load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_ci(pt, lo, hi):
    return "{:.3f} [{:.3f}, {:.3f}]".format(pt, lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en", choices=["en", "de"])
    ap.add_argument("--hybrid", action="store_true",
                    help="also run hybrid (RRF of BM25F+cosine) — slower, "
                         "runs the lexical path on the enriched query")
    ap.add_argument("--reranker", action="store_true",
                    help="also run the cross-encoder rerank pass (slow; "
                         "implies --hybrid)")
    ap.add_argument("--bm25f", action="store_true",
                    help="also report the lexical bm25f+trigram mode")
    ap.add_argument("--variants", default="",
                    help="comma list to subset VARIANTS (default: all)")
    ap.add_argument("--gt", default="", help="override ground-truth path")
    args = ap.parse_args()
    lang = args.lang

    cache_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                              "ds_cache_v2_a2_sphera_{}.json".format(lang))
    gt_path    = args.gt or os.path.join(PARENT, GT_BY_LANG[lang])
    bin_path   = os.path.join(PARENT, "LCiA_Extension_Cache",
                              "embeddings_a2_sphera_{}.bin".format(lang))
    for p in (cache_path, gt_path, bin_path):
        if not os.path.exists(p):
            print("ERROR: missing required file:", p)
            return 1

    t0 = time.time()
    cache = _load_json(cache_path)
    gt    = _load_json(gt_path)
    datasets    = cache["results"]
    searchables = [build_searchable(ds, lang=lang) for ds in datasets]
    uuid_to_sh  = {ds["uuid"]: sh for ds, sh in zip(datasets, searchables)}
    print("Building CorpusStats over {} datasets...".format(len(datasets)))
    stats = CorpusStats.build(searchables)

    # Embedding matrix (numpy) over datasets that have a cached vector.
    idx = EmbeddingIndex.load_bin(bin_path)
    sem_uuids, rows = [], []
    for ds in datasets:
        v = idx.get(ds["uuid"])
        if v is not None:
            sem_uuids.append(ds["uuid"])
            rows.append(v)
    mat = np.asarray(rows, dtype=np.float32)            # (N, 1024), L2-normed
    print("Embedding matrix: {} x {} (model={})".format(
        mat.shape[0], mat.shape[1], getattr(idx, "model", "bge-m3")))

    client = OllamaEmbeddingClient(model="bge-m3", timeout_ms=60000)
    if not client.is_available():
        print("ERROR: Ollama not reachable; aborting.")
        return 1
    print("Warming up bge-m3...")
    if client.embed("concrete") is None:
        print("ERROR: warm-up embed failed:", client.last_error)
        return 1

    # Semantic is the default (and the research focus); the lexical / fusion
    # modes are opt-in because running BM25F on the long enriched query is the
    # expensive part. Skipped entirely when no lexical mode is requested.
    modes = ["semantic"]
    if args.bm25f:
        modes.append("bm25f")
    if args.hybrid:
        modes.append("hybrid")
    rr = None
    if args.reranker:
        rr = LocalRerankerClient(base_url=RERANKER_BASE_URL,
                                 timeout_ms=RERANKER_TIMEOUT_MS)
        if rr.is_available():
            if "hybrid" not in modes:
                modes.append("hybrid")     # reranker re-scores the hybrid top-K
            modes.append("reranker")
            print("Reranker sidecar ready:", rr.model)
        else:
            print("WARN: reranker sidecar not ready ({}); skipping that mode."
                  .format(rr.last_error))
            rr = None
    need_lexical = any(m in modes for m in ("bm25f", "hybrid", "reranker"))
    need_hybrid  = any(m in modes for m in ("hybrid", "reranker"))
    print("Modes:", modes)

    variants = list(VARIANTS)
    if args.variants:
        wanted = set(s.strip() for s in args.variants.split(",") if s.strip())
        variants = [(n, f) for (n, f) in variants if n in wanted]
    var_names = [n for n, _ in variants]

    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    n = len(match_entries)
    print("GT: {} match-labelled queries (lang={})".format(n, lang))

    # Per-field coverage over the GT (how many queries even carry each field).
    coverage = {f: sum(1 for e in match_entries if get_field_piece(e, f, lang))
                for f in FIELD_KEYS}

    # results[mode][variant] = {hits@k, anyhit, recip[], ndcg[], hit1[], hit5[]}
    results = {m: {} for m in modes}

    # Pre-embed every unique enriched query in batches. Ollama bge-m3 on GPU
    # spends ~2-3 s of HTTP/round-trip overhead per single /api/embed call but
    # fans a batched request into one forward pass - ~batch_size× throughput.
    all_q = set()
    for _vn, _fl in variants:
        for _e in match_entries:
            all_q.add(build_enriched_query(_e, _fl, lang))
    all_q = sorted(all_q)
    print("Pre-embedding {} unique queries (batch=64)...".format(len(all_q)))
    embed_cache = {}
    _BATCH = 64
    _t = time.time()
    for i in range(0, len(all_q), _BATCH):
        chunk = all_q[i:i + _BATCH]
        vecs = client.embed_batch(chunk)
        if not vecs or len(vecs) != len(chunk):
            for s in chunk:                       # per-item fallback on failure
                embed_cache[s] = client.embed(s)
        else:
            for s, v in zip(chunk, vecs):
                embed_cache[s] = v
        print("  {}/{} ({:.1f}s)".format(min(i + _BATCH, len(all_q)),
                                         len(all_q), time.time() - _t))

    def qvec(text):
        v = embed_cache.get(text)
        if v is None and text not in embed_cache:
            v = client.embed(text)                # safety net (rare)
            embed_cache[text] = v
        return v

    for vname, fields in variants:
        for m in modes:
            results[m][vname] = {
                "hits": {k: 0 for k in TOPK}, "any": 0,
                "recip": [], "ndcg": [], "hit1": [], "hit5": [],
            }
        t_v = time.time()
        for e in match_entries:
            q = build_enriched_query(e, fields, lang)
            correct = e.get("correct_uuid", "") or ""
            accept_only = list(e.get("acceptable_uuids", []) or [])
            accept = set(accept_only)
            if correct:
                accept.add(correct)

            ranked = {}     # mode -> ranked uuid list
            qv = qvec(q)
            if qv is not None:
                scores = mat.dot(np.asarray(qv, dtype=np.float32))
                sem_rank = [sem_uuids[i] for i in np.argsort(-scores)]
            else:
                sem_rank = []
            ranked["semantic"] = sem_rank

            btt_uuids = []
            if need_lexical:                     # skipped in semantic-only runs
                btt_uuids = [d["uuid"] for d, _ in _run_bm25f_variant(
                    q, datasets, searchables, stats, BTT_PARAMS, None)]
                if "bm25f" in modes:
                    ranked["bm25f"] = btt_uuids

            if need_hybrid:
                if btt_uuids and sem_rank:
                    rrf = reciprocal_rank_fusion([btt_uuids, sem_rank],
                                                 k=HYBRID_RRF_K)
                    hyb = [u for u, _ in sorted(rrf.items(),
                                                key=lambda t: t[1], reverse=True)]
                else:
                    hyb = btt_uuids or sem_rank
                ranked["hybrid"] = hyb

                if "reranker" in modes:
                    rer = hyb
                    topk = hyb[:RERANKER_TOP_K]
                    if topk:
                        docs = [build_semantic_haystack(uuid_to_sh.get(u, {}))
                                for u in topk]
                        logits = rr.rerank(q, docs)
                        if logits and len(logits) == len(topk):
                            reord = [u for u, _ in sorted(
                                zip(topk, logits), key=lambda t: t[1],
                                reverse=True)]
                            rer = reord + hyb[RERANKER_TOP_K:]
                    ranked["reranker"] = rer

            for m in modes:
                rk = ranked.get(m, [])
                R = results[m][vname]
                first = _rank_first_u(rk, accept)
                hit = first is not None
                if hit:
                    R["any"] += 1
                for k in TOPK:
                    if _topk_hit_u(rk, accept, k):
                        R["hits"][k] += 1
                R["recip"].append(1.0 / first if hit else 0.0)
                R["ndcg"].append(ndcg_at_k(rk, correct, accept_only, k=10))
                R["hit1"].append(1 if (first == 1) else 0)
                R["hit5"].append(1 if (hit and first <= 5) else 0)
        print("  variant {:<22} done ({:.1f}s)".format(
            vname, time.time() - t_v))

    duration = time.time() - t0

    # ── aggregate ──
    def agg(mode, vname):
        R = results[mode][vname]
        rec = {}
        for k in TOPK:
            p, lo, hi = wilson_ci(R["hits"][k], n)
            rec["r@{}".format(k)] = p
        pa, _, _ = wilson_ci(R["any"], n)
        rec["r@all"] = pa
        mpt, mlo, mhi = bootstrap_ci(R["recip"])
        rec["mrr"], rec["mrr_lo"], rec["mrr_hi"] = mpt, mlo, mhi
        npt, nlo, nhi = bootstrap_ci(R["ndcg"])
        rec["ndcg"], rec["ndcg_lo"], rec["ndcg_hi"] = npt, nlo, nhi
        return rec

    table = {m: {v: agg(m, v) for v in var_names} for m in modes}

    base = "V0_name" if "V0_name" in var_names else var_names[0]

    def mcnemar_vs_base(mode, vname, key):
        a = results[mode][vname][key]
        b = results[mode][base][key]
        bb = sum(1 for i in range(n) if a[i] == 1 and b[i] == 0)
        cc = sum(1 for i in range(n) if a[i] == 0 and b[i] == 1)
        return mcnemar_pvalue(bb, cc)

    # Per-field leave-one-out marginal (ALL minus field): ΔMRR = MRR(ALL)−MRR(ALL−f)
    all_v = "V8_ALL_+host_types"
    loo = {}
    if all_v in var_names:
        for f in FIELD_KEYS:
            if f == "name":
                continue
            lv = "LOO_-" + f
            if lv in var_names:
                loo[f] = {m: table[m][all_v]["mrr"] - table[m][lv]["mrr"]
                          for m in modes}

    # ── write JSON ──
    out_json = {
        "_schema": "ablation_query_context_v1",
        "lang": lang, "datastock": "a2_sphera", "n_match": n,
        "n_datasets": len(datasets), "modes": modes,
        "variants": var_names, "baseline": base,
        "coverage": coverage,
        "embedding_model": getattr(idx, "model", "bge-m3"),
        "rrf_k": HYBRID_RRF_K, "reranker_top_k": RERANKER_TOP_K,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": duration,
        "table": table, "loo_marginal_mrr": loo,
    }
    out_json_path = os.path.join(HERE,
                                 "ablation_query_context_{}.json".format(lang))
    with io.open(out_json_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out_json, indent=2, ensure_ascii=False))

    # ── write markdown ──
    L = []
    L.append("# Query-context ablation — {} (a2_sphera, n={})".format(
        lang.upper(), n))
    L.append("")
    L.append("Auto-generated by `tools/ablate_query_context.py`. Each variant "
             "rebuilds the **query** from BIM fields (document embedding "
             "unchanged); metrics reuse `search_helpers` primitives "
             "(comparable to the phase1 benchmark). Baseline = `{}`.".format(base))
    L.append("")
    L.append("- **Embedding**: {} · **RRF k**: {} · **reranker top-K**: {}"
             .format(getattr(idx, "model", "bge-m3"), HYBRID_RRF_K, RERANKER_TOP_K))
    L.append("- **Generated**: {} (run {:.1f}s)".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), duration))
    L.append("")

    L.append("## Field coverage (how many of the {} queries carry each field)"
             .format(n))
    L.append("")
    L.append("| Field | Coverage |")
    L.append("|---|---:|")
    for f in FIELD_KEYS:
        L.append("| {} | {}/{} |".format(f, coverage[f], n))
    L.append("")
    L.append("> A field present on few queries can only move those queries — "
             "read *value* (Δ when present) separately from *coverage*.")
    L.append("")

    for m in modes:
        L.append("## Mode: {}".format(m))
        L.append("")
        L.append("| Variant | R@1 | R@5 | R@10 | R@all | MRR [95% CI] | "
                 "nDCG@10 | ΔMRR vs V0 | McNemar p@1 |")
        L.append("|---|---:|---:|---:|---:|:--|---:|---:|---:|")
        for v in var_names:
            r = table[m][v]
            dmrr = r["mrr"] - table[m][base]["mrr"]
            pmc = mcnemar_vs_base(m, v, "hit1") if v != base else None
            L.append("| {} | {:.0f}% | {:.0f}% | {:.0f}% | {:.0f}% | {} | "
                     "{:.3f} | {:+.3f} | {} |".format(
                         v, 100*r["r@1"], 100*r["r@5"], 100*r["r@10"],
                         100*r["r@all"], _fmt_ci(r["mrr"], r["mrr_lo"], r["mrr_hi"]),
                         r["ndcg"], dmrr,
                         "—" if pmc is None else "{:.3f}".format(pmc)))
        L.append("")

    if loo:
        L.append("## Per-field marginal value (leave-one-out from ALL)")
        L.append("")
        L.append("ΔMRR = MRR(ALL) − MRR(ALL − field). **Positive ⇒ the field "
                 "HELPS** (removing it hurts); **negative ⇒ it ADDS NOISE** "
                 "(removing it helps).")
        L.append("")
        hdr = "| Field | coverage |" + "".join(" {} ΔMRR |".format(m) for m in modes)
        sep = "|---|---:|" + "---:|" * len(modes)
        L.append(hdr); L.append(sep)
        for f in FIELD_KEYS:
            if f not in loo:
                continue
            line = "| {} | {}/{} |".format(f, coverage[f], n)
            for m in modes:
                line += " {:+.3f} |".format(loo[f][m])
            L.append(line)
        L.append("")

    # Best-variant summary
    L.append("## Best variant per mode (by MRR)")
    L.append("")
    L.append("| Mode | Best variant | MRR | vs V0 |")
    L.append("|---|---|---:|---:|")
    for m in modes:
        best = max(var_names, key=lambda v: table[m][v]["mrr"])
        L.append("| {} | {} | {:.3f} | {:+.3f} |".format(
            m, best, table[m][best]["mrr"],
            table[m][best]["mrr"] - table[m][base]["mrr"]))
    L.append("")

    out_md_path = os.path.join(HERE,
                               "ablation_query_context_{}.md".format(lang))
    with io.open(out_md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")

    print("Wrote", out_md_path)
    print("Wrote", out_json_path)
    print("Run took {:.1f}s".format(duration))
    return 0


if __name__ == "__main__":
    sys.exit(main())
