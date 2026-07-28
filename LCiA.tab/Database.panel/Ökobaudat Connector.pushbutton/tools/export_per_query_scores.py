# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Per-row CSV export covering all four search modes' rank+score per
(query, candidate) pair.

Mirrors what the Validation pushbutton would produce, but runs offline
in CPython 3 using the same primitives. Useful for thesis error analysis:
"which queries did semantic beat BM25F on? Where did the reranker
disagree with hybrid?"

For each match-labeled query in the ground truth, the tool runs all four
modes, collects each candidate that appears in any mode's top-20, and
emits one row per (query, candidate) with rank+raw+calibrated score for
each mode plus the is_correct / is_acceptable labels.

Usage:
  python tools/export_per_query_scores.py [--lang en|de] [--top-n 20]

Output:
  tools/per_query_scores.csv  (UTF-8, ; delimiter to survive Excel
                               opening German content correctly)
"""
from __future__ import print_function
import argparse
import io
import json
import os
import sys

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

import fit_confidence as fc
from search_helpers import (
    build_searchable, CorpusStats, ConfidenceCalibrator,
)


def _load_calibrator(mode, lang):
    path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "confidence_{}_a2_sphera_{}.json".format(mode, lang))
    if not os.path.exists(path):
        return ConfidenceCalibrator(mode=mode)
    with io.open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return ConfidenceCalibrator.from_dict(mode, d)


def _ground_truth_queries(lang):
    gt_path = os.path.join(
        PARENT, "sample_project_a2_sphera_{}.json".format(lang))
    with io.open(gt_path, "r", encoding="utf-8") as f:
        gt = json.load(f)
    queries = []
    for e in gt["entries"]:
        if e.get("label") != "match":
            continue
        accept = set(e.get("acceptable_uuids", []) or [])
        c = e.get("correct_uuid", "")
        if c:
            accept.add(c)
        queries.append((e["name"], accept, c))
    return queries


def _csv_quote(s):
    if s is None:
        s = ""
    s = u"{}".format(s).replace(u'"', u'""')
    if u";" in s or u'"' in s or u"\n" in s:
        return u'"' + s + u'"'
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=("en", "de"), default="en")
    ap.add_argument("--top-n", type=int, default=20,
                    help="Per-mode top-N to consider for the CSV (default 20)")
    args = ap.parse_args()

    cache_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "ds_cache_v2_a2_sphera_{}.json".format(args.lang))
    with io.open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    datasets = cache["results"]
    searchables = [build_searchable(d, lang=args.lang) for d in datasets]
    stats = CorpusStats.build(searchables)
    ds_by_uuid = {ds.get("uuid"): ds for ds in datasets if ds.get("uuid")}

    queries = _ground_truth_queries(args.lang)
    print("Queries: {}".format(len(queries)))

    class _A: pass
    sa = _A()
    sa.lang = args.lang
    sa.rrf_k = 60
    sa.rerank_top_k = 20

    # Pre-build ctx for all modes once -- ctx is the same for
    # bm25f/semantic/hybrid; reranker adds reranker_client.
    sa.mode = "reranker"
    ctx_full = fc._setup_ctx(sa, datasets, searchables, stats)

    cals = {m: _load_calibrator(m, args.lang)
            for m in ("bm25f", "semantic", "hybrid", "reranker")}

    out_rows = []  # list of dicts
    for qi, (query, accept, correct) in enumerate(queries, start=1):
        print("[{}/{}] {!r}".format(qi, len(queries), query))
        # Score each mode independently, then merge.
        # Per-mode {uuid: (raw, calibrated, rank)}
        per_mode = {m: {} for m in ("bm25f", "semantic", "hybrid", "reranker")}
        for mode in ("bm25f", "semantic", "hybrid", "reranker"):
            sa.mode = mode
            scorer = fc.MODE_SCORERS[mode]
            scored = []
            for raw, _y, _q, uuid in scorer([(query, accept)], datasets, ctx_full):
                scored.append((uuid, raw))
            scored.sort(key=lambda t: t[1], reverse=True)
            for rank, (uuid, raw) in enumerate(scored, start=1):
                cal = cals[mode].apply(raw)
                per_mode[mode][uuid] = (raw, cal, rank)

        # Universe = any uuid in top-N of any mode
        universe = set()
        for mode in per_mode:
            top_uuids = sorted(
                per_mode[mode].items(),
                key=lambda t: t[1][2])[:args.top_n]
            for uuid, _ in top_uuids:
                universe.add(uuid)

        for uuid in universe:
            row = {
                "query":          query,
                "expected_uuid":  correct,
                "candidate_uuid": uuid,
                "candidate_name": (ds_by_uuid.get(uuid) or {}).get("name", ""),
                "is_correct":     1 if uuid == correct else 0,
                "is_acceptable":  1 if uuid in accept else 0,
            }
            for mode in ("bm25f", "semantic", "hybrid", "reranker"):
                info = per_mode[mode].get(uuid)
                if info is None:
                    row["rank_" + mode] = ""
                    row["raw_" + mode] = ""
                    row["score_" + mode] = ""
                else:
                    raw, cal, rank = info
                    row["rank_" + mode] = rank
                    row["raw_" + mode] = u"{:.6f}".format(raw)
                    row["score_" + mode] = u"{:.4f}".format(cal)
            out_rows.append(row)

    cols = ["query", "expected_uuid", "candidate_uuid", "candidate_name",
            "is_correct", "is_acceptable",
            "rank_bm25f", "raw_bm25f", "score_bm25f",
            "rank_semantic", "raw_semantic", "score_semantic",
            "rank_hybrid", "raw_hybrid", "score_hybrid",
            "rank_reranker", "raw_reranker", "score_reranker"]

    out_path = os.path.join(HERE, "per_query_scores.csv")
    with io.open(out_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(u";".join(cols) + u"\n")
        for row in out_rows:
            f.write(u";".join(_csv_quote(row.get(c, u""))
                                for c in cols) + u"\n")
    print()
    print("Wrote {} ({} rows across {} queries)".format(
        out_path, len(out_rows), len(queries)))


if __name__ == "__main__":
    main()
