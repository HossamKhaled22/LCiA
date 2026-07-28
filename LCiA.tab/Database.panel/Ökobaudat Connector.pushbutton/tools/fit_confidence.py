# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Fit a per-mode `ConfidenceCalibrator` from the ground truth.

This is the Phase 0 -> 1 bridge: each search mode (BM25F, semantic,
hybrid, reranker) maps a raw score -> confidence in [0, 1] via this
calibrator. Same `apply(raw)` interface in every mode -> the live UI's
Score% column and the Min Score slider mean the same thing across modes.

Method:
  1. Score every (query, dataset) pair in the candidate set for the mode:
       - bm25f:    survivors of the fuzzy binary filter (Phase 0)
       - semantic: every dataset with a cached embedding (Phase 1)
       - hybrid:   union of BM25F survivors + items with cached embeddings,
                   scored by RRF over the two rankings (Phase 1+)
       - reranker: top-K from hybrid, scored by the cross-encoder logits
                   (Phase 1.7)
  2. Collect (raw_score, is_correct) points.
  3. **Balance negatives** so the base rate is ~1/(K+1) (default K=9 -> 10%).
     This prevents the rank-1 candidate at the strongest raw score from
     calibrating to ~3% (the unbalanced base rate is ~0.7%, which
     squashes all per-row probabilities into the floor of the curve).
  4. Fit isotonic regression (Pool Adjacent Violators) on the balanced set.
  5. Write `confidence_<mode>_<datastock>_<lang>.json`.

The output JSON carries a `max_confidence` field for documentation, but
the cap itself is enforced in `ConfidenceCalibrator.apply` via the
module-level `search_helpers.MAX_CONFIDENCE = 0.97` constant -- the JSON
value is informational only.

Reference for the balanced-negative-sampling trick:
  Owen "Infinitely Imbalanced Logistic Regression" (2007) -- formal study
  of how subsampling negatives shifts the intercept of a probabilistic
  classifier, which is exactly what we need to do to the isotonic curve
  so its outputs span [0, 1] instead of [0, base_rate].

Modes implemented (all use the SAME ground truth via the SAME primitives
as the live UI, so cross-mode calibration is comparable):
  bm25f      uses search_helpers.bm25f_score + typo expansion
  semantic   uses OllamaEmbeddingClient.embed + EmbeddingIndex.get + cosine
  hybrid     fuses BM25F + semantic rankings via reciprocal_rank_fusion
  reranker   top-K from hybrid -> LocalRerankerClient.rerank (auto-spawns
             rerank_service.py if not running)

Usage:
  python tools/fit_confidence.py --mode bm25f    [--lang en|de]
  python tools/fit_confidence.py --mode semantic [--lang en|de]
  python tools/fit_confidence.py --mode hybrid   [--lang en|de]
  python tools/fit_confidence.py --mode reranker [--lang en|de]
  [--neg-per-pos 9] [--rrf-k 60] [--rerank-top-k 20]

Output:
  LCiA_Extension_Cache/confidence_<mode>_a2_sphera_<lang>.json
"""
from __future__ import print_function
import argparse
import io
import json
import os
import random
import subprocess
import sys
import time

HERE   = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

import constants
from search_helpers import (
    build_searchable, build_semantic_haystack,
    CorpusStats, bm25f_score,
    expand_query_with_typos, _fuzzy_match_inner, _tokenize,
    IsotonicCalibrator, ConfidenceCalibrator, CONFIDENCE_DEFAULTS,
    MAX_CONFIDENCE,
    cosine_similarity, EmbeddingIndex, OllamaEmbeddingClient,
    LocalRerankerClient, reciprocal_rank_fusion, rrf_max_score,
)


# ---------------------------------------------------------------------
# Pool Adjacent Violators (reused from calibrate_score.py; duplicated
# here to keep this script standalone).
# ---------------------------------------------------------------------

def pav(points):
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    blocks = []
    for x, y in pts:
        if blocks and blocks[-1][2] == x:
            s, c, lo, hi = blocks[-1]
            blocks[-1] = (s + y, c + 1, lo, x)
        else:
            blocks.append((float(y), 1, x, x))
    i = 1
    while i < len(blocks):
        s_prev, c_prev, lo_prev, hi_prev = blocks[i - 1]
        s_cur,  c_cur,  lo_cur,  hi_cur  = blocks[i]
        if s_prev / c_prev > s_cur / c_cur:
            blocks[i - 1] = (s_prev + s_cur, c_prev + c_cur, lo_prev, hi_cur)
            del blocks[i]
            if i > 1:
                i -= 1
        else:
            i += 1
    return [(hi, s / c) for s, c, lo, hi in blocks]


# ---------------------------------------------------------------------
# Per-mode scoring loops -- all take (queries, datasets, ctx) and yield
# (raw_score, is_correct, query, ds_uuid). The ctx dict carries any
# mode-specific resources (corpus stats, embedding index, etc.).
# ---------------------------------------------------------------------

def _score_pairs_bm25f(queries, datasets, ctx):
    """Yield (raw_BM25F, is_correct, query, ds_uuid) for every survivor
    of the binary fuzzy filter across all queries."""
    searchables = ctx["searchables"]
    stats       = ctx["stats"]
    for query, accept in queries:
        nl = (query or "").lower()
        tokens = _tokenize(nl)
        if not nl:
            continue
        expanded = expand_query_with_typos(nl, stats)
        for ds, hay in zip(datasets, searchables):
            if not _fuzzy_match_inner(nl, tokens, hay):
                continue
            raw = bm25f_score(nl, hay, stats, expanded_query=expanded)
            yield (raw, 1 if ds["uuid"] in accept else 0,
                   query, ds["uuid"])


def _score_pairs_semantic(queries, datasets, ctx):
    """Yield (raw_cosine, is_correct, query, ds_uuid) for every dataset
    with a cached embedding. No binary filter -- semantic mode is a pure
    ranker, exactly as in the live UI's `_apply_grid_filters`."""
    ollama = ctx["ollama_client"]
    index  = ctx["embedding_index"]
    for query, accept in queries:
        if not query:
            continue
        qv = ollama.embed(query)
        if qv is None:
            print("  [semantic] skip query={!r}: embed failed ({})".format(
                query, ollama.last_error))
            continue
        for ds in datasets:
            uuid = ds.get("uuid")
            if not uuid:
                continue
            vec = index.get(uuid)
            if vec is None:
                continue
            cos = cosine_similarity(qv, vec)
            yield (cos, 1 if uuid in accept else 0, query, uuid)


def _score_pairs_hybrid(queries, datasets, ctx):
    """Yield (norm_RRF, is_correct, query, ds_uuid) for the union of
    BM25F survivors and items with cached embeddings, scored via
    reciprocal_rank_fusion(k=60). Mirrors `window._compute_rrf_scores`."""
    searchables = ctx["searchables"]
    stats       = ctx["stats"]
    ollama      = ctx["ollama_client"]
    index       = ctx["embedding_index"]
    k           = int(ctx.get("rrf_k", constants.HYBRID_RRF_K))
    max_rrf     = rrf_max_score(2, k)
    for query, accept in queries:
        nl = (query or "").lower()
        if not nl:
            continue
        tokens = _tokenize(nl)
        expanded = expand_query_with_typos(nl, stats)
        # 1) BM25F ranking (filter survivors + scores, sorted descending)
        bm_pairs = []
        for ds, hay in zip(datasets, searchables):
            if not _fuzzy_match_inner(nl, tokens, hay):
                continue
            raw_bm = bm25f_score(nl, hay, stats, expanded_query=expanded)
            uuid = ds.get("uuid")
            if uuid:
                bm_pairs.append((uuid, raw_bm))
        bm_pairs.sort(key=lambda p: p[1], reverse=True)
        bm_ranking = [u for u, _ in bm_pairs]
        # 2) Semantic ranking
        qv = ollama.embed(query)
        if qv is None:
            print("  [hybrid] skip query={!r}: embed failed ({})".format(
                query, ollama.last_error))
            continue
        sem_pairs = []
        for ds in datasets:
            uuid = ds.get("uuid")
            if not uuid:
                continue
            vec = index.get(uuid)
            if vec is None:
                continue
            sem_pairs.append((uuid, cosine_similarity(qv, vec)))
        sem_pairs.sort(key=lambda p: p[1], reverse=True)
        sem_ranking = [u for u, _ in sem_pairs]
        # 3) RRF over the two rankings, normalised
        rrf = reciprocal_rank_fusion([bm_ranking, sem_ranking], k=k)
        for uuid, raw_rrf in rrf.items():
            norm = (raw_rrf / max_rrf) if max_rrf > 0 else raw_rrf
            yield (norm, 1 if uuid in accept else 0, query, uuid)


def _score_pairs_reranker(queries, datasets, ctx):
    """Yield (raw_logit, is_correct, query, ds_uuid) for the top-K
    hybrid candidates per query, scored by the cross-encoder sidecar.
    Items outside top-K never reach the reranker in production, so they
    don't belong in the fit."""
    reranker = ctx["reranker_client"]
    top_k    = int(ctx.get("rerank_top_k", constants.RERANKER_TOP_K))
    searchables_by_uuid = {ds.get("uuid"): hay
                            for ds, hay in zip(datasets, ctx["searchables"])
                            if ds.get("uuid")}
    ds_by_uuid = {ds.get("uuid"): ds for ds in datasets if ds.get("uuid")}
    for query, accept in queries:
        # Get hybrid scores for this single query
        hyb_items = []  # (uuid, raw_rrf, y)
        for raw_rrf, y, _q, uuid in _score_pairs_hybrid(
                [(query, accept)], datasets, ctx):
            hyb_items.append((uuid, raw_rrf, y))
        hyb_items.sort(key=lambda t: t[1], reverse=True)
        top = hyb_items[:top_k]
        if not top:
            continue
        docs = []
        for uuid, _raw, _y in top:
            hay = searchables_by_uuid.get(uuid, {})
            doc = build_semantic_haystack(hay) or ds_by_uuid[uuid].get("name", "")
            docs.append(doc)
        logits = reranker.rerank(query, docs)
        if not logits or len(logits) != len(top):
            print("  [reranker] skip query={!r}: rerank failed ({})".format(
                query, reranker.last_error))
            continue
        for (uuid, _raw, y), logit in zip(top, logits):
            yield (logit, y, query, uuid)


MODE_SCORERS = {
    "bm25f":    _score_pairs_bm25f,
    "semantic": _score_pairs_semantic,
    "hybrid":   _score_pairs_hybrid,
    "reranker": _score_pairs_reranker,
}


# ---------------------------------------------------------------------
# Calibration diagnostics: ECE + bootstrap CIs
# ---------------------------------------------------------------------

def expected_calibration_error(points, calibrator, n_bins=10):
    """Expected Calibration Error.

    ECE = sum_b (n_b / N) * |observed_b - predicted_b|

    where bins partition the predicted-probability axis [0, 1] into
    `n_bins` equal-width bins. Lower is better; ECE = 0 means perfect
    calibration. Typical "well-calibrated" threshold in ML practice is
    ECE < 0.1; ECE > 0.15 indicates the calibrator is too noisy to
    trust for cross-mode comparison.
    """
    bins = [[] for _ in range(n_bins)]
    bin_preds = [[] for _ in range(n_bins)]
    for raw, y in points:
        p = calibrator.apply(raw)
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append(y)
        bin_preds[idx].append(p)
    total = sum(len(b) for b in bins)
    if total == 0:
        return 0.0
    ece = 0.0
    for b, preds in zip(bins, bin_preds):
        if not b:
            continue
        obs = sum(b) / float(len(b))
        pred = sum(preds) / float(len(preds))
        ece += (len(b) / float(total)) * abs(obs - pred)
    return ece


def bootstrap_knot_cis(balanced_points, base_knots, n_resamples=200, seed=42):
    """Bootstrap 95% CI for the calibrated probability at each base
    knot's x-position.

    On a small ground truth (18 queries) the PAV-fit isotonic curve is
    noisy -- a single mislabelled positive can swing a high-bin knot
    from 0.9 to 0.7. The CI quantifies that uncertainty so the user
    can decide whether a mode's calibration is precise enough to trust
    for cross-mode comparison or needs more ground truth.

    Method: resample the balanced points with replacement (preserving
    sample size), refit PAV, evaluate the new calibrator at every base
    knot's x-position. Repeat `n_resamples` times; report the 2.5 / 50 /
    97.5 percentiles of the evaluated probability per knot.
    """
    if not base_knots or not balanced_points:
        return []
    rng = random.Random(seed)
    base_xs = [x for x, _p in base_knots]
    per_x_preds = dict((x, []) for x in base_xs)
    n = len(balanced_points)
    for _ in range(n_resamples):
        sample = [balanced_points[rng.randrange(n)] for _ in range(n)]
        sample_knots = pav(sample)
        sample_cal = IsotonicCalibrator(sample_knots)
        for x in base_xs:
            per_x_preds[x].append(sample_cal.apply(x))
    cis = []
    for x in base_xs:
        ps = sorted(per_x_preds[x])
        m = len(ps)
        lo_idx = max(0, int(0.025 * m))
        hi_idx = min(m - 1, int(0.975 * m))
        med_idx = m // 2
        cis.append({
            "x":        float(x),
            "p_2_5":    float(ps[lo_idx]),
            "p_median": float(ps[med_idx]),
            "p_97_5":   float(ps[hi_idx]),
            "ci_width": float(ps[hi_idx] - ps[lo_idx]),
        })
    return cis


# ---------------------------------------------------------------------
# Balanced subsampling
# ---------------------------------------------------------------------

def balance(points, neg_per_pos, seed=42):
    """Keep all positives; randomly subsample negatives so the base rate
    becomes 1 / (1 + neg_per_pos). Returns the kept (raw, y) list."""
    rng = random.Random(seed)
    pos = [p for p in points if p[1] == 1]
    neg = [p for p in points if p[1] == 0]
    target_neg = neg_per_pos * len(pos)
    if len(neg) > target_neg:
        rng.shuffle(neg)
        neg = neg[:target_neg]
    return pos + neg


# ---------------------------------------------------------------------
# Reranker sidecar auto-spawn
# ---------------------------------------------------------------------

def _ensure_reranker_ready(reranker, wait_seconds=120):
    """If the sidecar isn't already running, spawn it and poll /health
    until ready. Mirrors the Connector's auto-spawn pattern but uses
    `subprocess.Popen` (we're in CPython 3, no .NET Process available).
    Returns True on success, False on failure (with a printed reason)."""
    if reranker.is_available():
        print("Reranker sidecar already running ({}).".format(reranker.model))
        return True
    script_path = os.path.join(PARENT, "rerank_service.py")
    if not os.path.exists(script_path):
        print("ERROR: rerank_service.py not found at {}".format(script_path))
        return False
    print("Reranker sidecar not running. Spawning rerank_service.py "
          "(model={}, port={})...".format(
              constants.RERANKER_MODEL, constants.RERANKER_PORT))
    try:
        # The sidecar's PID lock prevents duplicate spawns if the
        # Connector is also open; this Popen will exit cleanly if so.
        subprocess.Popen(
            [sys.executable, "-u", script_path,
             "--port", str(constants.RERANKER_PORT),
             "--model", constants.RERANKER_MODEL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=PARENT,
        )
    except Exception as ex:
        print("ERROR: spawn failed: {!r}".format(ex))
        return False
    print("Waiting up to {}s for the model to load...".format(wait_seconds))
    for i in range(wait_seconds):
        if reranker.is_available():
            print("Reranker ready after {}s.".format(i + 1))
            return True
        time.sleep(1.0)
    print("ERROR: reranker did not report ready within {}s ({})".format(
        wait_seconds, reranker.last_error))
    return False


# ---------------------------------------------------------------------
# Mode-specific resource setup
# ---------------------------------------------------------------------

def _setup_ctx(args, datasets, searchables, stats):
    """Build the per-mode context dict with the resources required."""
    ctx = {
        "datasets":     datasets,
        "searchables":  searchables,
        "stats":        stats,
        "lang":         args.lang,
        "rrf_k":        args.rrf_k,
        "rerank_top_k": args.rerank_top_k,
    }
    if args.mode in ("semantic", "hybrid", "reranker"):
        bin_path = os.path.join(
            PARENT, "LCiA_Extension_Cache",
            "embeddings_a2_sphera_{}.bin".format(args.lang))
        if not os.path.exists(bin_path):
            print("ERROR: embedding cache not found at {}".format(bin_path))
            print("       Run: python embedding_prefetcher.py "
                  "--source a2_sphera --lang {}".format(args.lang))
            sys.exit(2)
        ctx["embedding_index"] = EmbeddingIndex.load_bin(bin_path)
        print("Loaded {} embeddings (model={}, dim={}) from {}".format(
            len(ctx["embedding_index"]),
            ctx["embedding_index"].model,
            ctx["embedding_index"].dim,
            os.path.basename(bin_path)))
        ctx["ollama_client"] = OllamaEmbeddingClient(
            base_url=constants.OLLAMA_BASE_URL,
            model=constants.EMBEDDING_MODEL,
            timeout_ms=10000,
        )
        if not ctx["ollama_client"].is_available():
            print("ERROR: Ollama not reachable at {}".format(
                constants.OLLAMA_BASE_URL))
            print("       Start: ollama serve")
            sys.exit(2)
        print("Ollama reachable at {} (model={}).".format(
            constants.OLLAMA_BASE_URL, constants.EMBEDDING_MODEL))
    if args.mode == "reranker":
        ctx["reranker_client"] = LocalRerankerClient(
            base_url=constants.RERANKER_BASE_URL,
            timeout_ms=constants.RERANKER_TIMEOUT_MS,
        )
        if not _ensure_reranker_ready(ctx["reranker_client"]):
            sys.exit(2)
    return ctx


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode",
                    choices=("bm25f", "semantic", "hybrid", "reranker"),
                    default="bm25f")
    ap.add_argument("--lang", choices=("en", "de"), default="en")
    ap.add_argument("--neg-per-pos", type=int, default=9,
                    help="Negatives to keep per positive (default 9 -> 10%% base rate)")
    ap.add_argument("--rrf-k", type=int, default=constants.HYBRID_RRF_K,
                    help="RRF k constant for hybrid/reranker modes (default 60)")
    ap.add_argument("--rerank-top-k", type=int, default=constants.RERANKER_TOP_K,
                    help="Top-K hybrid candidates to rerank (default 20)")
    ap.add_argument("--n-bootstrap", type=int, default=200,
                    help="Bootstrap resamples for knot CIs (default 200; 0 to skip)")
    ap.add_argument("--ece-warn-threshold", type=float, default=0.15,
                    help="Print a warning if ECE exceeds this (default 0.15)")
    args = ap.parse_args()

    cache_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "ds_cache_v2_a2_sphera_{}.json".format(args.lang))
    gt_path = os.path.join(
        PARENT, "sample_project_a2_sphera_{}.json".format(args.lang))
    out_path = os.path.join(
        PARENT, "LCiA_Extension_Cache",
        "confidence_{}_a2_sphera_{}.json".format(args.mode, args.lang))

    with io.open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    with io.open(gt_path, "r", encoding="utf-8") as f:
        gt = json.load(f)
    datasets = cache["results"]
    searchables = [build_searchable(d, lang=args.lang) for d in datasets]
    stats = CorpusStats.build(searchables)

    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    queries = []
    for e in match_entries:
        accept = set(e.get("acceptable_uuids", []) or [])
        c = e.get("correct_uuid", "")
        if c:
            accept.add(c)
        queries.append((e["name"], accept))

    print("Mode = {} | lang = {} | {} queries | neg_per_pos = {}".format(
        args.mode, args.lang, len(queries), args.neg_per_pos))

    ctx = _setup_ctx(args, datasets, searchables, stats)

    scorer = MODE_SCORERS[args.mode]
    raw_points = [(raw, y) for (raw, y, _q, _u) in
                  scorer(queries, datasets, ctx)]
    pos_n = sum(1 for _, y in raw_points if y == 1)
    neg_n = sum(1 for _, y in raw_points if y == 0)
    print("  Raw points: {} ({} positive, {} negative; "
          "unbalanced base rate = {:.3f}%)".format(
              len(raw_points), pos_n, neg_n,
              100.0 * pos_n / max(1, len(raw_points))))

    balanced_points = balance(raw_points, args.neg_per_pos)
    bp_pos = sum(1 for _, y in balanced_points if y == 1)
    bp_neg = sum(1 for _, y in balanced_points if y == 0)
    print("  Balanced: {} ({} pos, {} neg; base rate = {:.1f}%)".format(
          len(balanced_points), bp_pos, bp_neg,
          100.0 * bp_pos / max(1, len(balanced_points))))

    knots = pav(balanced_points)
    print("  PAV reduced to {} knots".format(len(knots)))
    if knots:
        print("  first knots:", [(round(x, 3), round(p, 3)) for x, p in knots[:3]])
        print("  last  knots:", [(round(x, 3), round(p, 3)) for x, p in knots[-3:]])

    # Quick reliability check
    cal = IsotonicCalibrator(knots)
    bins = [[] for _ in range(10)]
    for raw, y in balanced_points:
        idx = min(9, int(cal.apply(raw) * 10))
        bins[idx].append(y)
    print()
    print("  Reliability check (10 bins of predicted probability):")
    for i, b in enumerate(bins):
        if not b:
            continue
        obs = sum(b) / float(len(b))
        print("    [{:.1f},{:.1f}]   observed {:.3f} over n={}".format(
            i / 10.0, (i + 1) / 10.0, obs, len(b)))

    # ECE -- weighted mean gap between predicted and observed across bins.
    ece = expected_calibration_error(balanced_points, cal, n_bins=10)
    print()
    print("  Expected Calibration Error (ECE, 10 bins): {:.4f}".format(ece))
    if ece > args.ece_warn_threshold:
        print("  WARNING: ECE > {} -- this calibration is noisy. "
              "Consider expanding ground truth or skipping this JSON "
              "(the live UI will fall back to the sigmoid default).".format(
                  args.ece_warn_threshold))

    # Bootstrap CIs at each base knot's x. Quantifies isotonic
    # uncertainty under small-N. Skipped when --n-bootstrap 0.
    knot_cis = []
    if args.n_bootstrap > 0:
        print()
        print("  Bootstrapping {} resamples for per-knot 95% CIs...".format(
            args.n_bootstrap))
        knot_cis = bootstrap_knot_cis(
            balanced_points, knots,
            n_resamples=args.n_bootstrap)
        if knot_cis:
            mean_w = sum(c["ci_width"] for c in knot_cis) / len(knot_cis)
            max_w = max(c["ci_width"] for c in knot_cis)
            print("  Per-knot CI width: mean={:.3f}  max={:.3f}".format(
                mean_w, max_w))

    payload = {
        "_schema":         "confidence_v2",
        "mode":            args.mode,
        "datastock":       "a2_sphera_{}".format(args.lang),
        "neg_per_pos":     args.neg_per_pos,
        "n_pos":           pos_n,
        "n_neg":           neg_n,
        "n_balanced":      len(balanced_points),
        "sigmoid":         list(CONFIDENCE_DEFAULTS.get(args.mode, (1.0, 1.0))),
        # `max_confidence` here is informational: the cap is enforced by
        # search_helpers.ConfidenceCalibrator.apply via the module
        # constant MAX_CONFIDENCE. Stored alongside for documentation
        # and for any future loader that wants to read it explicitly.
        "max_confidence":  MAX_CONFIDENCE,
        "ece":             float(ece),
        "n_bootstrap":     int(args.n_bootstrap),
        "knot_cis":        knot_cis,
        "knots":           [[float(x), float(p)] for x, p in knots],
    }
    if args.mode in ("hybrid", "reranker"):
        payload["rrf_k"] = args.rrf_k
    if args.mode == "reranker":
        payload["rerank_top_k"] = args.rerank_top_k
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
    print()
    print("Wrote", out_path)
    print("Live UI / Validation will pick this up on next dataset reload.")


if __name__ == "__main__":
    main()
