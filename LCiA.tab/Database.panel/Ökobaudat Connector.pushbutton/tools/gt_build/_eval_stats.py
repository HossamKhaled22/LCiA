# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Throwaway: summary statistics on the v0.4 gold standard for an honest eval."""
from __future__ import print_function
import io, json, os, statistics, collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GT = os.path.join(ROOT, "sample_project_a2_sphera_en_v0.4.json")

d = json.load(io.open(GT, encoding="utf-8"))
ents = d["entries"]
matches = [e for e in ents if e.get("label") == "match"]
skips = [e for e in ents if e.get("label") == "skip"]

acc_lens = [len(e.get("acceptable_uuids") or []) for e in matches]
top_lens = [len(e.get("top_10") or []) for e in matches]
nhr = [e for e in matches if e.get("needs_human_review")]
promoted = [e for e in matches if e.get("promoted")]
conf = [e.get("confidence") for e in matches if isinstance(e.get("confidence"), (int, float))]

# invariant check
bad_inv = 0
for e in matches:
    cu = e.get("correct_uuid")
    t10 = e.get("top_10") or []
    acc = e.get("acceptable_uuids") or []
    if not t10 or t10[0].get("uuid") != cu or not acc or acc[0] != cu:
        bad_inv += 1

# acceptable set size buckets
buckets = collections.Counter()
for n in acc_lens:
    if n <= 1: buckets["1"] += 1
    elif n <= 3: buckets["2-3"] += 1
    elif n <= 6: buckets["4-6"] += 1
    elif n <= 10: buckets["7-10"] += 1
    else: buckets["11+"] += 1


def stat(xs):
    if not xs: return "n/a"
    return "min=%g median=%g mean=%.2f max=%g" % (
        min(xs), statistics.median(xs), sum(xs) / float(len(xs)), max(xs))

print("=== v0.4 gold-standard composition ===")
print("version:", d.get("version"))
print("entries: %d  (match %d, skip %d)" % (len(ents), len(matches), len(skips)))
print("promoted matches: %d" % len(promoted))
print("needs_human_review: %d  (%.0f%% of matches)"
      % (len(nhr), 100.0 * len(nhr) / max(1, len(matches))))
print("invariant violations (correct==top10[0]==acc[0]):", bad_inv)
print()
print("acceptable_uuids per match:", stat(acc_lens))
print("  size buckets:", dict(buckets))
print("top_10 length:", stat(top_lens))
print("confidence:", stat(conf))
print()
# how many flagged are promoted vs organic
pr_ids = set(id(e) for e in promoted)
nhr_promoted = sum(1 for e in nhr if id(e) in pr_ids)
print("of %d flagged, %d are promoted fixtures, %d are organic construction"
      % (len(nhr), nhr_promoted, len(nhr) - nhr_promoted))
