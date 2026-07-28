# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Grid-search BM25F hyperparameters (k1, b, alpha_3g) on the ground
truth, reporting MRR with leave-one-query-out (LOQO) cross-validation.

LOQO is the right CV for a small ground truth (n=18): for each held-out
query, the remaining 17 are used to pick the best params, then those
params are evaluated on the held-out query. The reported MRR is the
mean across all 18 held-out queries. This avoids the "tuning and
evaluating on the same set" criticism.

Output: tools/tune_bm25_results.md plus the recommended params
written to LCiA_Extension_Cache/bm25_params_v1.json (consumed at
runtime by the live UI when present, otherwise the module defaults
in search_helpers.py are used).

Usage:
  python tools/tune_bm25.py [--lang en|de] [--coarse | --fine]

--coarse runs a 3x3x3 grid (default, ~10 min).
--fine   runs a 5x5x5 grid (~45 min).
"""
from __future__ import print_function
import io
import json
import os
import sys
import argparse
import time

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from search_helpers import (
    build_searchable, CorpusStats, bm25f_score,
    expand_query_with_typos, _tokenize, _fuzzy_match_inner,
    rank_of_first_hit,
)


def _load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _eval_params(params, queries, datasets, searchables, stats):
    """Compute MRR for one param set. queries is list of (query, accept_set)."""
    if not queries:
        return 0.0
    rr_sum = 0.0
    for query, accept in queries:
        nl = (query or "").lower()
        tokens = _tokenize(nl)
        if not nl:
            continue
        expanded = expand_query_with_typos(nl, stats)
        ranked = []
        for ds, hay in zip(datasets, searchables):
            if not _fuzzy_match_inner(nl, tokens, hay):
                continue
            raw = bm25f_score(nl, hay, stats, params=params,
                              expanded_query=expanded)
            ranked.append((ds, raw))
        ranked.sort(key=lambda t: t[1], reverse=True)
        r = rank_of_first_hit(ranked, accept)
        if r <= len(ranked):
            rr_sum += 1.0 / r
    return rr_sum / len(queries)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en", choices=("en", "de"))
    ap.add_argument("--coarse", action="store_true", default=True)
    ap.add_argument("--fine", action="store_true", default=False)
    args = ap.parse_args()

    if args.fine:
        k1_grid    = [1.2, 1.5, 1.8, 2.0, 2.5]
        b_grid     = [0.0, 0.25, 0.5, 0.75, 1.0]
        alpha_grid = [0.0, 0.25, 0.5, 0.75, 1.0]
    else:
        k1_grid    = [1.2, 1.5, 2.0]
        b_grid     = [0.5, 0.75, 0.9]
        alpha_grid = [0.0, 0.5, 1.0]

    cache_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "ds_cache_v2_a2_sphera_{}.json".format(args.lang))
    gt_path = os.path.join(
        PARENT, "sample_project_a2_sphera_{}.json".format(args.lang))

    cache = _load_json(cache_path)
    gt    = _load_json(gt_path)
    datasets = cache["results"]
    searchables = [build_searchable(ds, lang=args.lang) for ds in datasets]
    stats = CorpusStats.build(searchables)
    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    queries = []
    for e in match_entries:
        accept = set(e.get("acceptable_uuids", []) or [])
        c = e.get("correct_uuid", "")
        if c:
            accept.add(c)
        queries.append((e["name"], accept))
    n = len(queries)
    print("Tuning on {} queries, |grid| = {}*{}*{} = {} combos".format(
        n, len(k1_grid), len(b_grid), len(alpha_grid),
        len(k1_grid) * len(b_grid) * len(alpha_grid)))

    t0 = time.time()
    grid_results = []
    for k1 in k1_grid:
        for b in b_grid:
            for a in alpha_grid:
                params = {"k1": k1, "b_name": b, "b_class": b, "alpha_3g": a}
                mrr_all = _eval_params(params, queries, datasets,
                                       searchables, stats)
                grid_results.append((mrr_all, k1, b, a))
                print("  k1={:.1f} b={:.2f} a={:.2f}  MRR_all={:.3f}".format(
                    k1, b, a, mrr_all))
    grid_results.sort(reverse=True)
    print("Grid done in {:.1f}s. Top 5:".format(time.time() - t0))
    for mrr_all, k1, b, a in grid_results[:5]:
        print("  MRR={:.3f}  k1={:.1f} b={:.2f} alpha_3g={:.2f}".format(
            mrr_all, k1, b, a))

    # LOQO CV using the best params on each fold.
    print()
    print("Leave-one-query-out CV (per-fold best on remaining 17)...")
    fold_rrs = []
    fold_picks = []
    for i in range(n):
        train_qs = queries[:i] + queries[i+1:]
        test_q   = [queries[i]]
        # Pick the best params for the training set
        best = (-1.0, None)
        for k1 in k1_grid:
            for b in b_grid:
                for a in alpha_grid:
                    params = {"k1": k1, "b_name": b, "b_class": b,
                              "alpha_3g": a}
                    m = _eval_params(params, train_qs, datasets,
                                     searchables, stats)
                    if m > best[0]:
                        best = (m, params)
        train_mrr, best_params = best
        test_mrr = _eval_params(best_params, test_q, datasets,
                                searchables, stats)
        fold_rrs.append(test_mrr)
        fold_picks.append(best_params)
        print("  fold {}/{}  held-out=\"{}\"  train_mrr={:.3f}  test_rr={:.3f}  params={}".format(
            i + 1, n, queries[i][0], train_mrr, test_mrr,
            tuple((best_params["k1"], best_params["b_name"],
                   best_params["alpha_3g"]))))
    cv_mrr = sum(fold_rrs) / len(fold_rrs) if fold_rrs else 0.0
    print()
    print("LOQO mean test RR (i.e. CV-MRR) = {:.3f}".format(cv_mrr))

    # The recommended params are the modal pick across folds (most common
    # winner), with ties broken by which one gives the highest cv_mrr.
    from collections import Counter
    pick_keys = [(p["k1"], p["b_name"], p["alpha_3g"]) for p in fold_picks]
    counts = Counter(pick_keys)
    modal = counts.most_common(1)[0][0]
    recommended = {"k1": modal[0], "b_name": modal[1], "b_class": modal[1],
                   "w_name": 1.0, "w_class": 0.5, "alpha_3g": modal[2]}
    print("Modal LOQO winner: {} (chosen by {} of {} folds)".format(
        modal, counts.most_common(1)[0][1], n))

    out_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                            "bm25_params_v1.json")
    payload = {
        "_schema":    "bm25_params_v1",
        "datastock":  "a2_sphera_{}".format(args.lang),
        "params":     recommended,
        "loqo_mrr":   cv_mrr,
        "modal_count": counts.most_common(1)[0][1],
        "n_folds":    n,
        "grid": {"k1": k1_grid, "b": b_grid, "alpha_3g": alpha_grid},
    }
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
    print("Wrote", out_path)

    # Markdown summary
    md_path = os.path.join(HERE, "tune_bm25_results.md")
    lines = []
    lines.append("# BM25F hyperparameter tuning (LOQO CV)")
    lines.append("")
    lines.append("- Language: **{}**".format(args.lang))
    lines.append("- Queries (folds): {}".format(n))
    lines.append("- Grid: k1 in {}, b in {}, alpha_3g in {}".format(
        k1_grid, b_grid, alpha_grid))
    lines.append("- LOQO mean test RR (CV-MRR): **{:.3f}**".format(cv_mrr))
    lines.append("- Modal winner: **k1={}, b={}, alpha_3g={}** (in {}/{} folds)".format(
        modal[0], modal[1], modal[2], counts.most_common(1)[0][1], n))
    lines.append("")
    lines.append("## Top 5 grid results (on full set, not CV)")
    lines.append("")
    lines.append("| MRR | k1 | b | alpha_3g |")
    lines.append("|---:|---:|---:|---:|")
    for mrr_all, k1, b, a in grid_results[:5]:
        lines.append("| {:.3f} | {} | {} | {} |".format(mrr_all, k1, b, a))
    with io.open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("Wrote", md_path)


if __name__ == "__main__":
    main()
