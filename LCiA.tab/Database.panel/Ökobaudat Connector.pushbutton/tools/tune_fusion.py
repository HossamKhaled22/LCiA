# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Tune the hybrid-fusion hyperparameters (RRF k, per-ranker weights, and the
BM25F candidate pool) on the ground truth, with leave-one-query-out (LOQO) CV -
the deterministic analog of `tune_bm25.py`, but for the fusion stage.

Motivation: the live hybrid fuses BM25F (fuzzy-filtered, narrow pool) + cosine
via plain RRF (k=60, equal weights). Earlier evaluation of the scoring pipeline flagged
that the asymmetric pool can let RRF demote semantically-strong / lexically-weak
hits (DE Hybrid < Semantic). This tool grid-searches:

    pool     ∈ {asymmetric (fuzzy-filtered BM25F), symmetric (full-corpus BM25F)}
    k        ∈ RRF constant
    weights  ∈ (w_bm25, w_sem)   weighted RRF: Σ_r w_r / (k + rank_r)

optimizing LOQO-CV MRR of the fused (hybrid) ranking. Per-query BM25F and cosine
rankings are pre-computed ONCE, so each grid point is a cheap re-fusion.

Query is **name-only** here (today's behaviour) so fusion tuning is orthogonal
to the query-enrichment ablation; the final pipeline combines the best of both.

Output: `LCiA_Extension_Cache/hybrid_params_v1.json` (consumed by the live
`window.py:_compute_rrf_scores` + the Validation tool once wired - step T5) and
`tools/tune_fusion_results.md`.

Prereqs (CPython 3): Ollama + bge-m3; `embeddings_a2_sphera_<lang>.bin`,
`ds_cache_v2_a2_sphera_<lang>.json`.

Usage:
    python tools/tune_fusion.py --lang en [--fine]
"""
from __future__ import print_function
import argparse
import io
import json
import os
import sys
import time
from collections import Counter

import numpy as np

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from search_helpers import (
    build_searchable, CorpusStats, bm25f_score,
    expand_query_with_typos, _tokenize, _fuzzy_match_inner,
    EmbeddingIndex, OllamaEmbeddingClient, bootstrap_ci,
    reciprocal_rank_fusion, bm25f_ranking,
)
from phase0_benchmark import _run_bm25f_variant

BTT_PARAMS = {"_typo_expand": True, "alpha_3g": 0.5}
GT_BY_LANG = {
    "en": "sample_project_a2_sphera_en_v0.6__opus-4.8.json",
    "de": "golden_nugget_a2_sphera_de_v0.6__opus-4.8.json",
}
# Live default = the config the current UI uses; the tuned winner is judged
# against this.
BASELINE = {"pool": "asymmetric", "k": 60, "w_bm25": 1.0, "w_sem": 1.0}


def _load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rank_first_u(ranked_uuids, accept):
    for i, u in enumerate(ranked_uuids, 1):
        if u in accept:
            return i
    return None


def _bm25f_full_ranking(query, datasets, searchables, stats):
    """BM25F+typo+trigram over the FULL corpus (no fuzzy filter) -> uuid list.
    This is the 'symmetric pool' candidate ranking for RRF. Delegates to the
    shared `search_helpers.bm25f_ranking` so the live tool reproduces it."""
    nl = (query or "").lower()
    if not nl:
        return []
    expanded = expand_query_with_typos(nl, stats)
    clean = {k: v for k, v in BTT_PARAMS.items() if not k.startswith("_")}
    items = [(ds["uuid"], hay) for ds, hay in zip(datasets, searchables)]
    return bm25f_ranking(nl, items, stats, params=clean, expanded_query=expanded)


def _fuse_rank(bm_ranking, sem_ranking, w_bm, w_sem, k):
    """Weighted RRF of the two rankings via the shared helper, then sort."""
    rrf = reciprocal_rank_fusion([bm_ranking, sem_ranking], k=k,
                                 weights=[w_bm, w_sem])
    return [u for u, _ in sorted(rrf.items(), key=lambda t: t[1], reverse=True)]


def _eval_cfg(cfg, idxs, pre):
    """Mean reciprocal rank of the fused ranking over the given query indices."""
    if not idxs:
        return 0.0
    s = 0.0
    for i in idxs:
        p = pre[i]
        bm = p["btt_full"] if cfg["pool"] == "symmetric" else p["btt_narrow"]
        ranked = _fuse_rank(bm, p["sem"], cfg["w_bm25"], cfg["w_sem"], cfg["k"])
        r = _rank_first_u(ranked, p["accept"])
        if r:
            s += 1.0 / r
    return s / len(idxs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en", choices=["en", "de"])
    ap.add_argument("--fine", action="store_true")
    ap.add_argument("--gt", default="")
    args = ap.parse_args()
    lang = args.lang

    cache_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                              "ds_cache_v2_a2_sphera_{}.json".format(lang))
    gt_path    = args.gt or os.path.join(PARENT, GT_BY_LANG[lang])
    bin_path   = os.path.join(PARENT, "LCiA_Extension_Cache",
                              "embeddings_a2_sphera_{}.bin".format(lang))
    for p in (cache_path, gt_path, bin_path):
        if not os.path.exists(p):
            print("ERROR: missing", p)
            return 1

    if args.fine:
        ks = [10, 20, 30, 60, 80, 100]
        weight_pairs = [(1.0, 1.0), (1.0, 1.5), (1.5, 1.0),
                        (1.0, 2.0), (2.0, 1.0), (1.0, 0.5), (0.5, 1.0)]
    else:
        ks = [10, 30, 60, 100]
        weight_pairs = [(1.0, 1.0), (1.0, 2.0), (2.0, 1.0),
                        (1.0, 0.5), (0.5, 1.0)]
    pools = ["asymmetric", "symmetric"]
    grid = [{"pool": pl, "k": k, "w_bm25": wb, "w_sem": ws}
            for pl in pools for k in ks for (wb, ws) in weight_pairs]

    cache = _load_json(cache_path)
    gt    = _load_json(gt_path)
    datasets    = cache["results"]
    searchables = [build_searchable(ds, lang=lang) for ds in datasets]
    print("Building CorpusStats over {} datasets...".format(len(datasets)))
    stats = CorpusStats.build(searchables)

    idx = EmbeddingIndex.load_bin(bin_path)
    sem_uuids, rows = [], []
    for ds in datasets:
        v = idx.get(ds["uuid"])
        if v is not None:
            sem_uuids.append(ds["uuid"])
            rows.append(v)
    mat = np.asarray(rows, dtype=np.float32)

    client = OllamaEmbeddingClient(model="bge-m3", timeout_ms=60000)
    if not client.is_available():
        print("ERROR: Ollama not reachable; aborting.")
        return 1
    print("Warming up bge-m3...")
    if client.embed("concrete") is None:
        print("ERROR: warm-up failed:", client.last_error)
        return 1

    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    n = len(match_entries)
    print("Pre-computing per-query rankings for {} queries...".format(n))

    # Batch-embed the (name-only) queries up front - one GPU forward pass per
    # batch instead of ~2.8 s HTTP round-trip per single /api/embed call.
    names = [e["name"] for e in match_entries]
    name_vecs = {}
    for i in range(0, len(names), 64):
        chunk = names[i:i + 64]
        vs = client.embed_batch(chunk)
        if vs and len(vs) == len(chunk):
            for s, v in zip(chunk, vs):
                name_vecs[s] = v
        else:
            for s in chunk:
                name_vecs[s] = client.embed(s)

    pre = []
    t_pre = time.time()
    for e in match_entries:
        q = e["name"]
        accept = set(e.get("acceptable_uuids", []) or [])
        c = e.get("correct_uuid", "")
        if c:
            accept.add(c)
        qv = name_vecs.get(q)
        if qv is not None:
            scores = mat.dot(np.asarray(qv, dtype=np.float32))
            sem = [sem_uuids[i] for i in np.argsort(-scores)]
        else:
            sem = []
        btt_narrow = [d["uuid"] for d, _ in _run_bm25f_variant(
            q, datasets, searchables, stats, BTT_PARAMS, None)]
        btt_full = _bm25f_full_ranking(q, datasets, searchables, stats)
        pre.append({"accept": accept, "sem": sem,
                    "btt_narrow": btt_narrow, "btt_full": btt_full})
    print("  pre-compute done ({:.1f}s)".format(time.time() - t_pre))

    all_idx = list(range(n))

    # Full-set grid (for reporting the landscape).
    grid_scored = []
    for cfg in grid:
        grid_scored.append((_eval_cfg(cfg, all_idx, pre), cfg))
    grid_scored.sort(key=lambda t: t[0], reverse=True)
    base_mrr = _eval_cfg(BASELINE, all_idx, pre)

    print("Baseline (asymmetric k=60 w=1,1) full-set MRR = {:.3f}".format(base_mrr))
    print("Top 5 configs (full-set MRR):")
    for m, cfg in grid_scored[:5]:
        print("  MRR={:.3f}  {}".format(m, cfg))

    # LOQO CV: per held-out query, pick the best config on the other n-1.
    print("LOQO CV over {} folds x {} configs...".format(n, len(grid)))
    fold_rrs, fold_picks = [], []
    for i in range(n):
        train = all_idx[:i] + all_idx[i+1:]
        best = (-1.0, None)
        for cfg in grid:
            m = _eval_cfg(cfg, train, pre)
            if m > best[0]:
                best = (m, cfg)
        test_rr = _eval_cfg(best[1], [i], pre)
        fold_rrs.append(test_rr)
        fold_picks.append((best[1]["pool"], best[1]["k"],
                           best[1]["w_bm25"], best[1]["w_sem"]))
    cv_mrr = sum(fold_rrs) / len(fold_rrs) if fold_rrs else 0.0
    cv_lo, cv_hi = bootstrap_ci(fold_rrs)[1:]

    counts = Counter(fold_picks)
    modal = counts.most_common(1)[0][0]
    recommended = {"pool": modal[0], "k": modal[1],
                   "w_bm25": modal[2], "w_sem": modal[3]}
    print("LOQO CV-MRR = {:.3f}  [{:.3f}, {:.3f}]".format(cv_mrr, cv_lo, cv_hi))
    print("Modal LOQO winner: {} (in {}/{} folds)".format(
        modal, counts.most_common(1)[0][1], n))

    out_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                            "hybrid_params_a2_sphera_{}.json".format(lang))
    payload = {
        "_schema": "hybrid_params_v1",
        "datastock": "a2_sphera_{}".format(lang),
        "params": recommended,
        "loqo_mrr": cv_mrr,
        "loqo_mrr_ci": [cv_lo, cv_hi],
        "baseline": BASELINE,
        "baseline_full_mrr": base_mrr,
        "best_full_mrr": grid_scored[0][0],
        "modal_count": counts.most_common(1)[0][1],
        "n_folds": n,
        "grid": {"pools": pools, "k": ks, "weights": weight_pairs},
        "note": "Consumed by window.py:_compute_rrf_scores + Validation tool "
                "once wired (step T5). Query is name-only; orthogonal to the "
                "query-enrichment ablation.",
    }
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
    print("Wrote", out_path)

    L = []
    L.append("# Hybrid-fusion tuning (LOQO CV) — {}".format(lang.upper()))
    L.append("")
    L.append("- Queries (folds): {}".format(n))
    L.append("- Grid: pool ∈ {}, k ∈ {}, weights(w_bm25,w_sem) ∈ {}".format(
        pools, ks, weight_pairs))
    L.append("- **Baseline** (live: asymmetric, k=60, w=1,1) full-set MRR: "
             "**{:.3f}**".format(base_mrr))
    L.append("- **LOQO CV-MRR** (tuned): **{:.3f}** [{:.3f}, {:.3f}]".format(
        cv_mrr, cv_lo, cv_hi))
    L.append("- **Modal winner**: pool=**{}**, k=**{}**, w_bm25=**{}**, "
             "w_sem=**{}** (in {}/{} folds)".format(
                 modal[0], modal[1], modal[2], modal[3],
                 counts.most_common(1)[0][1], n))
    L.append("")
    L.append("## Top 10 configs (full-set MRR)")
    L.append("")
    L.append("| MRR | pool | k | w_bm25 | w_sem |")
    L.append("|---:|---|---:|---:|---:|")
    for m, cfg in grid_scored[:10]:
        L.append("| {:.3f} | {} | {} | {} | {} |".format(
            m, cfg["pool"], cfg["k"], cfg["w_bm25"], cfg["w_sem"]))
    L.append("")
    md_path = os.path.join(HERE, "tune_fusion_results_{}.md".format(lang))
    with io.open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print("Wrote", md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
