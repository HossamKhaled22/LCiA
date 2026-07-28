# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the BM25F scoring code in `search_helpers.py`.

Runs under both CPython 3 (offline) and IronPython 2.7 (inside Revit
via `import tools.test_bm25f`). No external test framework - uses plain
`assert` so it's portable to IronPython.

Usage:
    python tools/test_bm25f.py

Exits non-zero if any test fails. Prints PASS/FAIL per test.
"""
from __future__ import print_function
import math
import os
import sys

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from search_helpers import (
    CorpusStats, bm25f_score, expand_query_with_typos,
    _expand_with_origin, IsotonicCalibrator, soft_bound,
    explain_score, search_bm25f, _trigrams, _tokenize,
    BM25_K1, BM25_B_NAME, BM25_B_CLASS, BM25_W_NAME, BM25_W_CLASS,
    ConfidenceCalibrator, CONFIDENCE_DEFAULTS, MAX_CONFIDENCE, _sigmoid,
    # Evaluation metrics (Test 15)
    wilson_ci, bootstrap_ci, mrr, ndcg_at_k, mcnemar_pvalue,
)

# Pretty assert helpers
_passed = 0
_failed = 0

def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  ", name)
    else:
        _failed += 1
        print("  FAIL  ", name, "--", detail)

def approx(a, b, tol=1e-3):
    return abs(a - b) <= tol


# -------------------------------------------------------------
# Test 1 - CorpusStats build correctness
# -------------------------------------------------------------
print("Test 1: CorpusStats.build")
docs = [
    {"name": "concrete",        "classification": "masonry"},
    {"name": "concrete c30",    "classification": "concrete masonry"},
    {"name": "wood",            "classification": "building"},
]
stats = CorpusStats.build(docs)

check("N == 3", stats.N == 3, "got N={}".format(stats.N))
check("avgdl_name == 4/3",
      approx(stats.avgdl_name, 4.0 / 3.0),
      "got {}".format(stats.avgdl_name))
check("avgdl_class == 4/3",
      approx(stats.avgdl_class, 4.0 / 3.0),
      "got {}".format(stats.avgdl_class))
check("df[concrete] == 2",
      stats.df.get("concrete") == 2,
      "got {}".format(stats.df.get("concrete")))
check("df[c30] == 1",
      stats.df.get("c30") == 1,
      "got {}".format(stats.df.get("c30")))
check("df[masonry] == 2",
      stats.df.get("masonry") == 2,
      "got {}".format(stats.df.get("masonry")))
check("df[wood] == 1",
      stats.df.get("wood") == 1)
check("vocab contains all 5 tokens",
      set(["concrete", "c30", "masonry", "wood", "building"]).issubset(stats.vocab))


# -------------------------------------------------------------
# Test 2 - IDF formula
# -------------------------------------------------------------
print("Test 2: IDF formula matches log((N - df + 0.5) / (df + 0.5) + 1)")
N = 3.0
expected_idf_concrete = math.log((N - 2 + 0.5) / (2 + 0.5) + 1.0)
expected_idf_c30      = math.log((N - 1 + 0.5) / (1 + 0.5) + 1.0)
check("idf[concrete] == log(1.6)",
      approx(stats.idf["concrete"], expected_idf_concrete),
      "got {}, expected {}".format(stats.idf["concrete"], expected_idf_concrete))
check("idf[c30] == log(8/3)",
      approx(stats.idf["c30"], expected_idf_c30))
check("idf[concrete] < idf[c30] (rarer term has higher IDF)",
      stats.idf["concrete"] < stats.idf["c30"])


# -------------------------------------------------------------
# Test 3 - bm25f_score for hand-calculated example
# -------------------------------------------------------------
print("Test 3: bm25f_score against hand calculation")

# Query "concrete c30" - defaults: k1=1.5, b=0.75, w_name=1.0, w_class=0.5
query = "concrete c30"

# Doc2 (name="concrete c30", dl=2; class="concrete masonry", dl=2)
#   For "concrete": norm_n = 1/(0.25 + 0.75·2/1.333) = 0.7273
#                  norm_c = 0.7273
#                  combined = 1·0.7273 + 0.5·0.7273 = 1.0909
#                  contrib = idf(concrete) · 1.0909 / (1.5 + 1.0909)
#   For "c30":     norm_n = 0.7273, norm_c = 0
#                  combined = 0.7273
#                  contrib = idf(c30) · 0.7273 / (1.5 + 0.7273)
norm_n_concrete_d2 = 1.0 / ((1.0 - 0.75) + 0.75 * 2.0 / (4.0/3.0))
combined_concrete_d2 = 1.0 * norm_n_concrete_d2 + 0.5 * norm_n_concrete_d2
contrib_concrete_d2  = stats.idf["concrete"] * combined_concrete_d2 / (1.5 + combined_concrete_d2)
combined_c30_d2 = 1.0 * norm_n_concrete_d2 + 0.5 * 0.0   # tf_class=0 for c30 in doc2
contrib_c30_d2  = stats.idf["c30"] * combined_c30_d2 / (1.5 + combined_c30_d2)
expected_d2 = contrib_concrete_d2 + contrib_c30_d2

actual_d2 = bm25f_score(query, docs[1], stats)
check("doc2 BM25F == hand calc",
      approx(actual_d2, expected_d2, tol=1e-6),
      "got {}, expected {}".format(actual_d2, expected_d2))

# Doc1 (name="concrete", dl=1; class="masonry", dl=1)
norm_n_concrete_d1 = 1.0 / ((1.0 - 0.75) + 0.75 * 1.0 / (4.0/3.0))
combined_concrete_d1 = 1.0 * norm_n_concrete_d1 + 0.5 * 0.0  # tf_class=0
contrib_concrete_d1  = stats.idf["concrete"] * combined_concrete_d1 / (1.5 + combined_concrete_d1)
# c30 has tf=0 in both fields -> contrib=0
expected_d1 = contrib_concrete_d1
actual_d1 = bm25f_score(query, docs[0], stats)
check("doc1 BM25F == hand calc",
      approx(actual_d1, expected_d1, tol=1e-6),
      "got {}, expected {}".format(actual_d1, expected_d1))

# Doc3 has neither term -> 0
actual_d3 = bm25f_score(query, docs[2], stats)
check("doc3 BM25F == 0", actual_d3 == 0.0, "got {}".format(actual_d3))

# Ranking
check("doc2 > doc1 > doc3", actual_d2 > actual_d1 > actual_d3)


# -------------------------------------------------------------
# Test 4 - Field weight `w_class` matters
# -------------------------------------------------------------
print("Test 4: Field weight changes ranking")
# A document with the query term ONLY in classification scores
# w_class / (w_name + w_class) of what it would with the term in name.
doc_class_only = {"name": "irrelevant", "classification": "concrete"}
doc_name_only  = {"name": "concrete",   "classification": "irrelevant"}
mini = [doc_class_only, doc_name_only]
ms = CorpusStats.build(mini)
score_class = bm25f_score("concrete", doc_class_only, ms)
score_name  = bm25f_score("concrete", doc_name_only, ms)
check("name-field hit > class-field hit (default w_name=1, w_class=0.5)",
      score_name > score_class,
      "name={}, class={}".format(score_name, score_class))
check("equal weights -> equal scores",
      approx(
          bm25f_score("concrete", doc_class_only, ms,
                      params={"w_name": 1.0, "w_class": 1.0}),
          bm25f_score("concrete", doc_name_only, ms,
                      params={"w_name": 1.0, "w_class": 1.0}),
      ))


# -------------------------------------------------------------
# Test 5 - Length normalisation
# -------------------------------------------------------------
print("Test 5: longer field penalised by length normalisation")
short_doc = {"name": "concrete",                "classification": ""}
long_doc  = {"name": "concrete and a lot of other words here", "classification": ""}
ln_stats = CorpusStats.build([short_doc, long_doc])
s_short = bm25f_score("concrete", short_doc, ln_stats)
s_long  = bm25f_score("concrete", long_doc, ln_stats)
check("short doc scores higher with b > 0", s_short > s_long,
      "short={}, long={}".format(s_short, s_long))
# With b = 0 (no length norm), scores tie (same tf, same idf, same combined)
s_short_nob = bm25f_score("concrete", short_doc, ln_stats,
                          params={"b_name": 0.0, "b_class": 0.0})
s_long_nob  = bm25f_score("concrete", long_doc, ln_stats,
                          params={"b_name": 0.0, "b_class": 0.0})
check("b=0 ties short and long", approx(s_short_nob, s_long_nob),
      "short={}, long={}".format(s_short_nob, s_long_nob))


# -------------------------------------------------------------
# Test 6 - Typo-tolerant query expansion
# -------------------------------------------------------------
print("Test 6: expand_query_with_typos")
docs_typo = [
    {"name": "stahl",     "classification": "metals"},
    {"name": "baustahl",  "classification": "metals"},
    {"name": "stuhl",     "classification": "furniture"},
    {"name": "concrete",  "classification": "mineral"},
]
ts = CorpusStats.build(docs_typo)

# Query "staahl" (5 chars, D=2) - expand to vocab tokens within d<=2.
# "stahl" is d=1 (one extra 'a'), weight 1/(1+1)=0.5
# "stuhl" is d=2 (replace 'a'->'u', delete 'a'), weight 1/(1+2)=0.333
# "baustahl" is d=3 -> outside budget D=2 -> not included
expanded = dict(expand_query_with_typos("staahl", ts))
check("'staahl' itself in expansion with weight 1.0",
      approx(expanded.get("staahl", 0), 1.0))
check("'stahl' in expansion (d=1)",
      "stahl" in expanded,
      "expansion keys: {}".format(list(expanded.keys())))
check("'stahl' weight 1/2",
      approx(expanded.get("stahl", 0), 0.5))
check("'baustahl' NOT in expansion (d=3 > D=2)",
      "baustahl" not in expanded)

# Origin tracking
_, origins = _expand_with_origin("staahl", ts)
check("origin of 'stahl' is 'staahl' with d=1",
      origins.get("stahl") == ("staahl", 1),
      "got {}".format(origins.get("stahl")))


# -------------------------------------------------------------
# Test 7 - Trigram BM25 catches compound words
# -------------------------------------------------------------
print("Test 7: trigram BM25F for compound words")
# Word-level tokenisation cannot match "Beton" inside "Stahlbeton"
docs_komp = [
    {"name": "stahlbeton",     "classification": "mineral"},
    {"name": "wood",           "classification": "natural"},
    {"name": "asphalt",        "classification": "mineral"},
]
ks = CorpusStats.build(docs_komp)
# Without alpha_3g, "beton" against "stahlbeton" gives 0 (no word match)
s0 = bm25f_score("beton", docs_komp[0], ks, params={"alpha_3g": 0.0})
check("word BM25F('beton', 'stahlbeton') == 0 (no word match)",
      s0 == 0.0,
      "got {}".format(s0))
# With alpha_3g > 0, trigrams "bet", "eto", "ton" all hit
s1 = bm25f_score("beton", docs_komp[0], ks, params={"alpha_3g": 1.0})
check("alpha_3g>0 raises score above 0", s1 > 0,
      "got {}".format(s1))
# And the trigram score must outrank a doc with no trigram overlap
s_wood = bm25f_score("beton", docs_komp[1], ks, params={"alpha_3g": 1.0})
check("trigram-matching doc outranks non-matching",
      s1 > s_wood,
      "matching={}, non={}".format(s1, s_wood))


# -------------------------------------------------------------
# Test 8 - IsotonicCalibrator interpolation
# -------------------------------------------------------------
print("Test 8: IsotonicCalibrator")
knots = [(0.0, 0.1), (1.0, 0.5), (2.0, 0.9), (4.0, 0.99)]
cal = IsotonicCalibrator(knots)
check("apply below first knot -> first probability",
      approx(cal.apply(-1.0), 0.1))
check("apply above last knot -> last probability",
      approx(cal.apply(10.0), 0.99))
check("apply at knot -> exact probability",
      approx(cal.apply(1.0), 0.5))
check("apply between (1,0.5)-(2,0.9) at 1.5 -> 0.7",
      approx(cal.apply(1.5), 0.7))
# Round-trip
d = cal.to_dict()
cal2 = IsotonicCalibrator.from_dict(d)
check("round-trip via to_dict/from_dict",
      approx(cal.apply(1.5), cal2.apply(1.5)))


# -------------------------------------------------------------
# Test 9 - soft_bound monotone in [0, 1]
# -------------------------------------------------------------
print("Test 9: soft_bound")
check("soft_bound(0) == 0", soft_bound(0) == 0.0)
check("soft_bound(huge) approaches 1",
      approx(soft_bound(50.0), 1.0, tol=1e-6))
check("soft_bound monotone",
      soft_bound(0.5) < soft_bound(1.0) < soft_bound(2.0))


# -------------------------------------------------------------
# Test 10 - explain_score sums correctly
# -------------------------------------------------------------
print("Test 10: explain_score per-term contribs sum to raw")
ex = explain_score("concrete c30", docs[1], stats)
sum_contribs = sum(item["contrib"] for item in ex["per_term"])
trig = ex.get("trigram_contrib", 0.0)
check("Sum per_term.contrib + trigram_contrib == raw (within 1e-3)",
      approx(sum_contribs + trig, ex["raw"], tol=1e-3),
      "Sum contribs={}, raw={}".format(sum_contribs, ex["raw"]))
check("explain_score 'final' is in [0, 1] when soft-bounded",
      0.0 <= ex["final"] <= 1.0,
      "got {}".format(ex["final"]))
check("explain_score 'per_term' has weight + idf for each",
      all("weight" in t and "idf" in t for t in ex["per_term"]))


# -------------------------------------------------------------
# Test 11 - search_bm25f end-to-end ordering
# -------------------------------------------------------------
print("Test 11: search_bm25f ordering")
ranked = search_bm25f("concrete c30", docs, stats)
# Survivors: doc1 (concrete only) and doc2 (concrete + c30). doc3 has
# neither, so its tokens don't pass _fuzzy_match_inner (the binary filter)
# -> not in `ranked`.
ranked_names = [r[0]["name"] for r in ranked]
check("doc2 is first in ranking",
      ranked_names and ranked_names[0] == "concrete c30",
      "ranked names = {}".format(ranked_names))
check("doc1 is in ranking",
      "concrete" in ranked_names,
      "ranked names = {}".format(ranked_names))


# -------------------------------------------------------------
# Test 12 - Round-trip CorpusStats via to_dict/from_dict
# -------------------------------------------------------------
print("Test 12: CorpusStats round-trip")
snapshot = stats.to_dict()
stats2 = CorpusStats.from_dict(snapshot)
check("N equal", stats.N == stats2.N)
check("avgdl_name equal", approx(stats.avgdl_name, stats2.avgdl_name))
check("df equal", stats.df == stats2.df)
check("idf equal for 'concrete'",
      approx(stats.idf["concrete"], stats2.idf["concrete"]))
# Scoring must be identical after round-trip
check("bm25f_score identical after round-trip",
      approx(bm25f_score("concrete c30", docs[1], stats),
             bm25f_score("concrete c30", docs[1], stats2),
             tol=1e-9))


# -------------------------------------------------------------
# Test 13 - ConfidenceCalibrator (mode-agnostic confidence layer)
# -------------------------------------------------------------
print("Test 13: ConfidenceCalibrator")

# bm25f default sigmoid: (midpoint=1.0, spread=1.0)
cc_b = ConfidenceCalibrator(mode="bm25f")
check("bm25f sigmoid at midpoint -> 0.5",
      approx(cc_b.apply(1.0), 0.5))
check("bm25f sigmoid in [0, 1] for raw=0 and raw=100",
      0.0 <= cc_b.apply(0.0) <= 1.0 and 0.0 <= cc_b.apply(100.0) <= 1.0)
check("bm25f sigmoid monotone in raw",
      cc_b.apply(0.5) < cc_b.apply(1.5) < cc_b.apply(3.0) < cc_b.apply(5.0))
check("bm25f sigmoid hand calc at raw=3 -> sigmoid(2) ~ 0.881",
      approx(cc_b.apply(3.0), _sigmoid(2.0), tol=1e-9))

# Custom (mode, sigmoid) pair
cc_x = ConfidenceCalibrator(mode="custom", sigmoid=(2.0, 0.5))
check("custom sigmoid params honoured at midpoint",
      approx(cc_x.apply(2.0), 0.5))

# Isotonic overrides sigmoid
iso = IsotonicCalibrator([(0.0, 0.0), (1.0, 0.9), (5.0, 0.95)])
cc_iso = ConfidenceCalibrator(mode="bm25f", isotonic=iso)
check("isotonic overrides sigmoid when both present (raw=1.0)",
      approx(cc_iso.apply(1.0), 0.9))
check("source() reports isotonic when present",
      "isotonic" in cc_iso.source())
check("source() reports sigmoid when no isotonic",
      "sigmoid" in cc_b.source())

# from_dict: dict-with-knots -> isotonic, dict-with-only-sigmoid -> sigmoid override
cc_d = ConfidenceCalibrator.from_dict("bm25f", {"knots": [(0, 0), (1, 0.8)]})
check("from_dict with knots uses isotonic",
      approx(cc_d.apply(1.0), 0.8))
cc_d2 = ConfidenceCalibrator.from_dict("bm25f", {"sigmoid": [2.0, 0.5]})
check("from_dict with sigmoid override (no knots) uses sigmoid",
      approx(cc_d2.apply(2.0), 0.5))
cc_d3 = ConfidenceCalibrator.from_dict("bm25f", None)
check("from_dict(None) falls back to mode default",
      approx(cc_d3.apply(1.0), 0.5))

# Semantic mode default (midpoint=0.5, spread=0.08)
cc_s = ConfidenceCalibrator(mode="semantic")
check("semantic mode midpoint at raw=0.5 -> 0.5",
      approx(cc_s.apply(0.5), 0.5))
check("semantic mode sharper than bm25f at raw=0.6",
      cc_s.apply(0.6) > 0.7)
check("rank-1-typical bm25f (raw=3.0) lands in [0.7, 0.99]",
      0.7 < cc_b.apply(3.0) < 0.99)
check("rank-1-typical semantic (cos=0.6) lands in [0.7, 0.99]",
      0.7 < cc_s.apply(0.6) < 0.99)


# -------------------------------------------------------------
# Test 14 - MAX_CONFIDENCE global cap (epistemic-honesty guarantee)
# -------------------------------------------------------------
print("Test 14: MAX_CONFIDENCE global cap")

check("MAX_CONFIDENCE constant is 0.97",
      approx(MAX_CONFIDENCE, 0.97, tol=1e-9),
      "got {}".format(MAX_CONFIDENCE))

# Sigmoid path: a huge raw drives sigmoid->1.0, must clamp to cap.
cc_cap_sig = ConfidenceCalibrator(mode="bm25f")
check("sigmoid path caps at MAX_CONFIDENCE for huge raw",
      approx(cc_cap_sig.apply(10_000.0), MAX_CONFIDENCE, tol=1e-9),
      "got {}".format(cc_cap_sig.apply(10_000.0)))
check("sigmoid path never exceeds MAX_CONFIDENCE",
      cc_cap_sig.apply(1e9) <= MAX_CONFIDENCE)

# Sub-cap values pass through unchanged.
check("sigmoid path unchanged below cap (raw=3.0)",
      approx(cc_cap_sig.apply(3.0), _sigmoid(2.0), tol=1e-9))

# Isotonic path with a saturating knot must also clamp.
iso_sat = IsotonicCalibrator([(0.0, 0.0), (1.0, 1.0)])
cc_cap_iso = ConfidenceCalibrator(mode="bm25f", isotonic=iso_sat)
check("isotonic path caps at MAX_CONFIDENCE when knot says 1.0",
      approx(cc_cap_iso.apply(2.0), MAX_CONFIDENCE, tol=1e-9),
      "got {}".format(cc_cap_iso.apply(2.0)))
check("isotonic path unchanged when knot value < cap (knot=0.9, raw=1.0)",
      approx(ConfidenceCalibrator(
          mode="bm25f",
          isotonic=IsotonicCalibrator([(0.0, 0.0), (1.0, 0.9)])
      ).apply(1.0), 0.9, tol=1e-9))

# All four canonical modes obey the cap on extreme input.
for _m in ("bm25f", "semantic", "hybrid", "reranker"):
    _cc = ConfidenceCalibrator(mode=_m)
    check("mode={} sigmoid caps at MAX_CONFIDENCE for raw=10000".format(_m),
          _cc.apply(10_000.0) <= MAX_CONFIDENCE,
          "got {}".format(_cc.apply(10_000.0)))

# source() string surfaces the cap (so tooltips display it).
check("source() mentions cap on sigmoid path",
      "cap=" in cc_cap_sig.source(),
      "source = {}".format(cc_cap_sig.source()))
check("source() mentions cap on isotonic path",
      "cap=" in cc_cap_iso.source(),
      "source = {}".format(cc_cap_iso.source()))

# Backward compatibility: every entry in CONFIDENCE_DEFAULTS is still a
# 2-tuple (midpoint, spread) - the cap is a separate module constant,
# not embedded in the per-mode defaults dict.
for _m, _v in CONFIDENCE_DEFAULTS.items():
    check("CONFIDENCE_DEFAULTS[{}] is a 2-tuple".format(_m),
          isinstance(_v, tuple) and len(_v) == 2,
          "got {}".format(_v))


# -------------------------------------------------------------
# Test 15 - Evaluation metrics (Wilson CI, bootstrap CI, MRR, nDCG, McNemar)
# -------------------------------------------------------------
print("Test 15: Evaluation metrics")

# --- Wilson CI ---
# Hand calculation for (47, 100, z=1.96):
#   p = 0.47
#   centre = (0.47 + 1.96^2/200) / (1 + 1.96^2/100) = 0.489208 / 1.038416 = 0.47111
#   half   = (1.96 / 1.038416) * sqrt(0.47*0.53/100 + 1.96^2/40000) = 0.09600
#   → (0.470, 0.375, 0.567)
_pt, _lo, _hi = wilson_ci(47, 100)
check("wilson_ci(47, 100) point ~ 0.47",
      approx(_pt, 0.470, tol=1e-3), "got {}".format(_pt))
check("wilson_ci(47, 100) lower ~ 0.375",
      approx(_lo, 0.375, tol=5e-3), "got {}".format(_lo))
check("wilson_ci(47, 100) upper ~ 0.567",
      approx(_hi, 0.567, tol=5e-3), "got {}".format(_hi))

_pt, _lo, _hi = wilson_ci(0, 0)
check("wilson_ci(0, 0) returns all zeros (degenerate guard)",
      _pt == 0.0 and _lo == 0.0 and _hi == 0.0,
      "got ({}, {}, {})".format(_pt, _lo, _hi))

_pt, _lo, _hi = wilson_ci(17, 17)
check("wilson_ci(17, 17) point == 1.0",
      _pt == 1.0, "got {}".format(_pt))
check("wilson_ci(17, 17) upper clamped to 1.0",
      _hi <= 1.0 + 1e-12, "got {}".format(_hi))
check("wilson_ci(17, 17) lower < 1.0 (small-N uncertainty)",
      _lo < 1.0, "got {}".format(_lo))

# --- MRR ---
check("mrr([1, 2, 4, None]) == (1 + 0.5 + 0.25 + 0)/4 = 0.4375",
      approx(mrr([1, 2, 4, None]), 0.4375, tol=1e-9),
      "got {}".format(mrr([1, 2, 4, None])))
check("mrr([]) == 0.0", mrr([]) == 0.0)
check("mrr([None, None, None]) == 0.0",
      mrr([None, None, None]) == 0.0)
check("mrr([1]) == 1.0", mrr([1]) == 1.0)

# --- nDCG@k ---
# Test 15a: binary relevance, perfect ranking
#   ranking = [acc1, acc2, acc3], GT = {acc1, acc2, acc3} (3 acceptable, no correct)
#   each contributes (2^1 - 1) / log2(i+1) = 1 / log2(i+1)
#   DCG  = 1 + 1/log2(3) + 1/log2(4)        = 2.131
#   IDCG = same                              = 2.131
#   nDCG = 1.0
_nd = ndcg_at_k(["A1", "A2", "A3"], "", ["A1", "A2", "A3"], k=3)
check("ndcg_at_k binary perfect = 1.0",
      approx(_nd, 1.0, tol=1e-9), "got {}".format(_nd))

# Test 15b: graded relevance, perfect ranking
#   ranking = [C, A, other], GT correct=C acceptable={C, A}
#   gains: C→3 (rel=2 → 2^2-1=3), A→1 (rel=1 → 2^1-1=1), other→0
#   DCG  = 3/log2(2) + 1/log2(3) + 0/log2(4) = 3 + 0.6309 + 0 = 3.6309
#   IDCG = grades sorted [2, 1] → 3 + 0.6309 = 3.6309
#   nDCG = 1.0
_nd = ndcg_at_k(["C", "A", "O"], "C", ["A"], k=3)
check("ndcg_at_k graded perfect = 1.0",
      approx(_nd, 1.0, tol=1e-9), "got {}".format(_nd))

# Test 15c: graded relevance, penalised ranking
#   ranking = [other, C, A], same GT as 15b
#   DCG  = 0 + 3/log2(3) + 1/log2(4) = 1.8929 + 0.5 = 2.3929
#   IDCG = 3.6309
#   nDCG ≈ 0.6591
_nd = ndcg_at_k(["O", "C", "A"], "C", ["A"], k=3)
check("ndcg_at_k graded penalty ~ 0.659",
      approx(_nd, 0.6591, tol=1e-3), "got {}".format(_nd))

# Test 15d: empty ranking → 0
check("ndcg_at_k empty ranking = 0.0",
      ndcg_at_k([], "C", ["A"], k=3) == 0.0)

# Test 15e: no positives in GT (no correct, no acceptable) → 0
check("ndcg_at_k empty GT = 0.0",
      ndcg_at_k(["X", "Y"], "", [], k=3) == 0.0)

# Test 15f: cutoff k cuts off - items at rank > k don't count
#   ranking = [other, C], k=1 → DCG = 0/log2(2) = 0; nDCG = 0.0
_nd = ndcg_at_k(["O", "C"], "C", [], k=1)
check("ndcg_at_k cutoff hides hits beyond k",
      _nd == 0.0, "got {}".format(_nd))

# --- McNemar exact ---
# Hand: mcnemar_pvalue(5, 2):
#   n=7, k=2; cumulative = C(7,0)+C(7,1)+C(7,2) = 1+7+21 = 29
#   p = 2 * 29 * 0.5^7 = 58/128 = 0.453125
_p = mcnemar_pvalue(5, 2)
check("mcnemar_pvalue(5, 2) ~ 0.453",
      approx(_p, 0.453125, tol=1e-6), "got {}".format(_p))

check("mcnemar_pvalue(0, 0) = 1.0 (no discordant pairs)",
      mcnemar_pvalue(0, 0) == 1.0)

# Strong asymmetry: 10 vs 0 → p = 2 * 1 * 0.5^10 = 1/512 ≈ 0.001953
_p = mcnemar_pvalue(10, 0)
check("mcnemar_pvalue(10, 0) ~ 0.00195",
      approx(_p, 1.0 / 512.0, tol=1e-9), "got {}".format(_p))

# Symmetric case: b == c → never significant (p = 1.0)
_p = mcnemar_pvalue(3, 3)
check("mcnemar_pvalue(3, 3) = 1.0 (perfect symmetry)",
      approx(_p, 1.0, tol=1e-9), "got {}".format(_p))

# Order independence: p(b, c) == p(c, b)
check("mcnemar_pvalue symmetric in arguments",
      approx(mcnemar_pvalue(7, 3), mcnemar_pvalue(3, 7), tol=1e-12))

# --- Bootstrap CI ---
# Constant input → constant CI
_pt, _lo, _hi = bootstrap_ci([0.5] * 20)
check("bootstrap_ci on constant returns (c, c, c)",
      _pt == 0.5 and _lo == 0.5 and _hi == 0.5,
      "got ({}, {}, {})".format(_pt, _lo, _hi))

# Empty input → all zeros
_pt, _lo, _hi = bootstrap_ci([])
check("bootstrap_ci on empty returns all zeros",
      _pt == 0.0 and _lo == 0.0 and _hi == 0.0)

# Same seed → deterministic across calls
_a = bootstrap_ci([0.0, 0.5, 1.0] * 10, seed=7)
_b = bootstrap_ci([0.0, 0.5, 1.0] * 10, seed=7)
check("bootstrap_ci deterministic with same seed", _a == _b,
      "got {} vs {}".format(_a, _b))

# Bootstrap point estimate equals plain mean (not a resample mean)
_vals = [1.0, 2.0, 3.0, 4.0, 5.0]
_pt, _lo, _hi = bootstrap_ci(_vals)
check("bootstrap_ci point = unbootstrapped mean",
      approx(_pt, 3.0, tol=1e-9), "got {}".format(_pt))
check("bootstrap_ci CI brackets the point",
      _lo <= _pt <= _hi,
      "got ({}, {}, {})".format(_pt, _lo, _hi))


# -------------------------------------------------------------
# Summary
# -------------------------------------------------------------
print("")
print("-" * 40)
print("{} passed, {} failed".format(_passed, _failed))
if _failed:
    sys.exit(1)
sys.exit(0)
