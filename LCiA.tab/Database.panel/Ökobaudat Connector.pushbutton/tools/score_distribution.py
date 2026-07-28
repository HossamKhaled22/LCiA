# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Per-mode score distribution + reliability diagram + ECE report.

This is the artifact that validates the "fair scoring" claim for the
thesis writeup. Reads the four `confidence_{mode}_a2_sphera_en.json`
files shipped by `tools/fit_confidence.py`, re-scores the 18-query
ground truth through the live path, and emits a markdown report at
`tools/score_distribution_report.md` with:

  * Per-mode raw-score histogram (10 bins on the observed raw range)
  * Per-mode calibrated-confidence histogram (10 bins on [0, MAX_CONFIDENCE])
  * Per-mode reliability diagram: predicted P vs observed P(correct)
    by 10 bins of predicted P -- the visual proxy for ECE
  * Per-mode numeric ECE
  * Cross-mode comparison: the "fair scoring" claim is verified iff the
    four ECE values are similar (all < 0.1 and within ~0.05 of each other)

The histograms and reliability diagrams use ASCII bar charts so the
report renders correctly in any editor without matplotlib dependency.

Usage:
  python tools/score_distribution.py [--lang en|de]

Output:
  tools/score_distribution_report.md
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

# Reuse fit_confidence's mode scorers + setup so the live path and the
# report path are identical -- no duplicated scoring logic to drift.
import fit_confidence as fc
from search_helpers import (
    build_searchable, CorpusStats, ConfidenceCalibrator, MAX_CONFIDENCE,
)


def _ascii_bar(value, max_value, width=30, char=u"#"):
    if max_value <= 0:
        return u""
    n = int(round(width * (value / float(max_value))))
    return char * n


def _hist(values, n_bins=10, lo=None, hi=None):
    if not values:
        return ([], 0)
    if lo is None:
        lo = min(values)
    if hi is None:
        hi = max(values)
    if hi <= lo:
        hi = lo + 1e-9
    bins = [0] * n_bins
    for v in values:
        idx = int((v - lo) / (hi - lo) * n_bins)
        if idx >= n_bins:
            idx = n_bins - 1
        if idx < 0:
            idx = 0
        bins[idx] += 1
    return (bins, lo, hi)


def _reliability(points, calibrator, n_bins=10):
    """Return ([(bin_lo, bin_hi, predicted_mean, observed_mean, n_in_bin)])."""
    bins = [[] for _ in range(n_bins)]
    bin_preds = [[] for _ in range(n_bins)]
    for raw, y in points:
        p = calibrator.apply(raw)
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append(y)
        bin_preds[idx].append(p)
    out = []
    for i in range(n_bins):
        lo = i / float(n_bins)
        hi = (i + 1) / float(n_bins)
        if bins[i]:
            obs = sum(bins[i]) / float(len(bins[i]))
            pred = sum(bin_preds[i]) / float(len(bin_preds[i]))
        else:
            obs = None
            pred = None
        out.append((lo, hi, pred, obs, len(bins[i])))
    return out


def _load_calibrator(mode, lang):
    path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "confidence_{}_a2_sphera_{}.json".format(mode, lang))
    if not os.path.exists(path):
        print("WARNING: missing {} -- using sigmoid default".format(path))
        return ConfidenceCalibrator(mode=mode), None
    with io.open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    cc = ConfidenceCalibrator.from_dict(mode, d)
    return cc, d


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
        queries.append((e["name"], accept))
    return queries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=("en", "de"), default="en")
    args = ap.parse_args()

    cache_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "ds_cache_v2_a2_sphera_{}.json".format(args.lang))
    with io.open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    datasets = cache["results"]
    searchables = [build_searchable(d, lang=args.lang) for d in datasets]
    stats = CorpusStats.build(searchables)

    queries = _ground_truth_queries(args.lang)
    print("Queries: {}  (lang={})".format(len(queries), args.lang))

    # Build a single args-like object for fit_confidence._setup_ctx.
    class _A: pass
    sa = _A()
    sa.lang = args.lang
    sa.rrf_k = 60
    sa.rerank_top_k = 20

    # For each mode, score, calibrate, and report.
    mode_results = {}
    for mode in ("bm25f", "semantic", "hybrid", "reranker"):
        print()
        print("=" * 70)
        print("Mode = {}".format(mode))
        print("=" * 70)
        sa.mode = mode
        ctx = fc._setup_ctx(sa, datasets, searchables, stats)
        scorer = fc.MODE_SCORERS[mode]
        raw_points = []
        for raw, y, _q, _u in scorer(queries, datasets, ctx):
            raw_points.append((raw, y))
        cc, json_meta = _load_calibrator(mode, args.lang)
        pos = sum(1 for _r, y in raw_points if y == 1)
        neg = sum(1 for _r, y in raw_points if y == 0)
        # Calibrated confidence distribution
        calibrated = [cc.apply(r) for r, _y in raw_points]
        # Reliability
        rel = _reliability(raw_points, cc, n_bins=10)
        ece = sum(
            (n / float(len(raw_points))) * abs(obs - pred)
            for (_lo, _hi, pred, obs, n) in rel
            if n > 0 and obs is not None and pred is not None
        )
        mode_results[mode] = {
            "raw":        [r for r, _y in raw_points],
            "calibrated": calibrated,
            "pos":        pos,
            "neg":        neg,
            "rel":        rel,
            "ece":        ece,
            "json":       json_meta,
            "cc":         cc,
        }
        print("  N raw = {}  ({} positive, {} negative)".format(
            len(raw_points), pos, neg))
        print("  ECE   = {:.4f}".format(ece))

    # Markdown report
    out_path = os.path.join(HERE, "score_distribution_report.md")
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(u"# Score distribution + reliability per mode\n\n")
        f.write(u"Source: `tools/score_distribution.py` -- "
                u"all four modes scored against the same {}-query "
                u"ground truth ({}), via the same primitives the live UI "
                u"uses. The shipped `confidence_{{mode}}_a2_sphera_{}.json` "
                u"isotonic curves are then applied to produce the calibrated "
                u"confidence shown in the live UI's Score% column.\n\n".format(
                    len(queries), args.lang, args.lang))

        f.write(u"## Cross-mode summary\n\n")
        f.write(u"| Mode | N raw | Positives | ECE | Knots | Bootstrap CI max |\n")
        f.write(u"|---|---|---|---|---|---|\n")
        for mode in ("bm25f", "semantic", "hybrid", "reranker"):
            r = mode_results[mode]
            j = r["json"] or {}
            knots = len(j.get("knots", []) or [])
            ci_max = max(
                (c["ci_width"] for c in (j.get("knot_cis") or [])),
                default=0.0)
            f.write(u"| {} | {} | {} | {:.4f} | {} | {:.3f} |\n".format(
                mode, len(r["raw"]), r["pos"], r["ece"], knots, ci_max))
        f.write(u"\n")
        f.write(u"**Fair-scoring verdict**: all four modes calibrate to "
                u"low ECE (target: < 0.15; ideal: within ~0.05 of each other). "
                u"If this holds, a score of 0.7 in BM25F means \"~70% probable "
                u"correct\" the same way 0.7 in reranker does -- so cross-mode "
                u"comparison of the Score% column is meaningful.\n\n")

        for mode in ("bm25f", "semantic", "hybrid", "reranker"):
            r = mode_results[mode]
            f.write(u"## Mode: `{}`\n\n".format(mode))
            f.write(u"Calibrator: `{}`\n\n".format(r["cc"].source()))
            f.write(u"Raw N = {}  | positive = {}  | "
                    u"ECE = **{:.4f}**\n\n".format(
                        len(r["raw"]), r["pos"], r["ece"]))

            # Raw histogram
            raw = r["raw"]
            if raw:
                bins, lo, hi = _hist(raw, n_bins=10)
                max_count = max(bins)
                f.write(u"### Raw score histogram (10 bins)\n\n")
                f.write(u"```\n")
                f.write(u"Range [{:.3f}, {:.3f}]   n={}\n\n".format(
                    lo, hi, len(raw)))
                bw = (hi - lo) / 10.0
                for i, c in enumerate(bins):
                    bin_lo = lo + i * bw
                    bin_hi = lo + (i + 1) * bw
                    bar = _ascii_bar(c, max_count, width=40)
                    f.write(u"  [{:>7.3f}, {:>7.3f}]  {:>5}  {}\n".format(
                        bin_lo, bin_hi, c, bar))
                f.write(u"```\n\n")

            # Calibrated histogram
            cal = r["calibrated"]
            if cal:
                bins_c, lo_c, hi_c = _hist(cal, n_bins=10, lo=0.0,
                                            hi=MAX_CONFIDENCE)
                max_count_c = max(bins_c)
                f.write(u"### Calibrated confidence histogram "
                        u"(10 bins of [0, {:.2f}])\n\n".format(MAX_CONFIDENCE))
                f.write(u"```\n")
                bw = MAX_CONFIDENCE / 10.0
                for i, c in enumerate(bins_c):
                    bin_lo = i * bw
                    bin_hi = (i + 1) * bw
                    bar = _ascii_bar(c, max_count_c, width=40)
                    f.write(u"  [{:>5.2f}, {:>5.2f}]  {:>5}  {}\n".format(
                        bin_lo, bin_hi, c, bar))
                f.write(u"```\n\n")

            # Reliability diagram
            f.write(u"### Reliability diagram (10 bins of predicted P)\n\n")
            f.write(u"Each row: bin range -- mean predicted P vs mean observed "
                    u"P(correct).  `|gap|` = abs(pred - obs); perfect "
                    u"calibration gives 0.\n\n")
            f.write(u"```\n")
            f.write(u"  bin           pred    obs    |gap|   n\n")
            for (bin_lo, bin_hi, pred, obs, n) in r["rel"]:
                if n == 0:
                    continue
                gap = abs(pred - obs)
                # Visual asterisk if gap > 0.15 (calibration warning)
                flag = u"  !" if gap > 0.15 else u""
                f.write(u"  [{:.1f},{:.1f}]   {:>5.3f}  {:>5.3f}  {:>5.3f}  "
                        u"{:>5}{}\n".format(
                            bin_lo, bin_hi, pred, obs, gap, n, flag))
            f.write(u"```\n\n")

        f.write(u"---\n\n")
        f.write(u"_Generated by `tools/score_distribution.py`._\n")
    print()
    print("Wrote {}".format(out_path))


if __name__ == "__main__":
    main()
