# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Fit an isotonic calibrator that maps raw BM25F scores -> P(correct).

Method: Pool Adjacent Violators (PAV) algorithm for isotonic regression.
References:
  * Niculescu-Mizil & Caruana 2005, "Predicting Good Probabilities With
    Supervised Learning."
  * Barlow et al. 1972, "Statistical inference under order restrictions."

Inputs:
  * Dataset cache at LCiA_Extension_Cache/ds_cache_v2_a2_sphera_<lang>.json
  * Ground truth at sample_project_a2_sphera_<lang>.json (pushbutton root)

For each (query, dataset) pair we form a point (raw_BM25F_score, is_correct).
PAV produces a monotone step function; we emit it as a sorted list of
(score, probability) knots.

Output:
  LCiA_Extension_Cache/calibration_a2_sphera_<lang>.json

Usage:
  python tools/calibrate_score.py [--lang en|de] [--alpha-3g 0.5]

This is a CPython 3 offline tool (no Revit / IronPython needed). The
output is consumed at runtime by the IronPython UI via
search_helpers.IsotonicCalibrator.from_dict.
"""
from __future__ import print_function
import io
import json
import os
import sys
import argparse

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from search_helpers import (
    build_searchable, CorpusStats, bm25f_score,
    expand_query_with_typos, _tokenize, _fuzzy_match_inner,
    IsotonicCalibrator,
)


def pav(points):
    """Pool Adjacent Violators isotonic regression.

    Args:
        points: list of (x, y) pairs. y is binary {0, 1} or weights in [0, 1].

    Returns:
        Sorted list of (x_threshold, y_calibrated) knots representing the
        piecewise-constant monotone function. Consecutive points whose
        x values tie are pre-merged.
    """
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    # Initialise blocks: (sum_y, count, x_lo, x_hi)
    blocks = []
    for x, y in pts:
        if blocks and blocks[-1][2] == x:
            # Same x as previous block: merge directly
            s, c, lo, hi = blocks[-1]
            blocks[-1] = (s + y, c + 1, lo, x)
        else:
            blocks.append((float(y), 1, x, x))
    # Pool while previous block has higher mean than current
    i = 1
    while i < len(blocks):
        s_prev, c_prev, lo_prev, hi_prev = blocks[i - 1]
        s_cur,  c_cur,  lo_cur,  hi_cur  = blocks[i]
        if s_prev / c_prev > s_cur / c_cur:
            # Pool
            blocks[i - 1] = (s_prev + s_cur, c_prev + c_cur, lo_prev, hi_cur)
            del blocks[i]
            if i > 1:
                i -= 1
        else:
            i += 1
    # Emit knots: use the x_hi of each block as the threshold; the y
    # value is the block mean. To make the function continuous-looking
    # in `IsotonicCalibrator.apply`, emit one knot per block at x_hi.
    knots = []
    for s, c, lo, hi in blocks:
        knots.append((hi, s / c))
    # Sentinel knots at extremes to avoid surprising clamping when the
    # live query produces a raw score outside the training range. We
    # add a 0-score knot mapping to 0, and a high-score knot mapping
    # to 1, only if not already present.
    if knots and knots[0][0] > 0:
        knots.insert(0, (0.0, max(0.0, knots[0][1] * 0.5)))
    if knots and knots[-1][1] < 1.0:
        # Extrapolate one step above max raw, mapping toward 1.0
        top = knots[-1][0] * 1.5 + 1.0
        knots.append((top, min(1.0, knots[-1][1] * 1.2 + 0.05)))
    return knots


def _load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Fit BM25F isotonic calibrator")
    ap.add_argument("--lang", choices=("en", "de"), default="en")
    ap.add_argument("--alpha-3g", type=float, default=0.5,
                    help="Trigram BM25 blend weight in the calibration target.")
    ap.add_argument("--include-typo", action="store_true", default=True,
                    help="Use the typo-expanded query (default: True).")
    args = ap.parse_args()

    cache_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "ds_cache_v2_a2_sphera_{}.json".format(args.lang)
    )
    gt_path = os.path.join(
        PARENT, "sample_project_a2_sphera_{}.json".format(args.lang)
    )
    out_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "calibration_a2_sphera_{}.json".format(args.lang)
    )

    cache = _load_json(cache_path)
    gt    = _load_json(gt_path)
    datasets = cache["results"]
    searchables = [build_searchable(ds, lang=args.lang) for ds in datasets]
    stats = CorpusStats.build(searchables)
    print("Built CorpusStats: N={}, vocab={}, avgdl_name={:.2f}".format(
        stats.N, len(stats.vocab), stats.avgdl_name))

    params = {"alpha_3g": args.alpha_3g}
    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    print("Calibrating on {} match-labelled queries...".format(len(match_entries)))

    points = []
    for entry in match_entries:
        query = (entry["name"] or "").lower()
        accept = set(entry.get("acceptable_uuids", []) or [])
        c = entry.get("correct_uuid", "")
        if c:
            accept.add(c)
        if not accept:
            continue
        tokens = _tokenize(query)
        expanded = (expand_query_with_typos(query, stats)
                    if args.include_typo
                    else [(t, 1.0) for t in tokens])
        for ds, hay in zip(datasets, searchables):
            if not _fuzzy_match_inner(query, tokens, hay):
                continue
            raw = bm25f_score(query, hay, stats, params=params,
                              expanded_query=expanded)
            y = 1 if ds["uuid"] in accept else 0
            points.append((raw, y))
    print("Collected {} (raw_score, is_correct) points".format(len(points)))

    knots = pav(points)
    print("PAV reduced to {} knots".format(len(knots)))
    print("First five knots:", knots[:5])
    print("Last five knots:",  knots[-5:])

    cal = IsotonicCalibrator(knots)
    out = {
        "_schema":   "isotonic_v1",
        "datastock": "a2_sphera_{}".format(args.lang),
        "alpha_3g":  args.alpha_3g,
        "include_typo": args.include_typo,
        "n_points":  len(points),
        "n_knots":   len(knots),
        "knots":     [[float(x), float(p)] for x, p in knots],
    }
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(out, indent=2, ensure_ascii=False))
    print("Wrote", out_path)

    # Quick reliability check: bin (raw, is_correct) by predicted prob,
    # report observed accept rate per bin.
    bins = [[] for _ in range(10)]
    for raw, y in points:
        p = cal.apply(raw)
        idx = min(9, int(p * 10))
        bins[idx].append(y)
    print()
    print("Reliability check (10 bins of predicted probability):")
    print("  bin   predicted   observed   n")
    for i, b in enumerate(bins):
        if not b:
            continue
        lo = i / 10.0
        hi = (i + 1) / 10.0
        obs = sum(b) / float(len(b))
        print("  [{:.1f},{:.1f}]   {:.2f}-{:.2f}   {:.3f}   {}".format(
            lo, hi, lo, hi, obs, len(b)))


if __name__ == "__main__":
    main()
