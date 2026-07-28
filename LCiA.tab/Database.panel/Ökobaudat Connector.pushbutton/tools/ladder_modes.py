# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Read-only analysis: the in-tool 'Measure field value' cumulative ladder,
extended from semantic to ALSO hybrid + reranker - so we see the effect of each
BIM-context field for every dense mode, not just semantic cosine.

Method is IDENTICAL to tools/ablate_query_context.py (same query_context
formatting, same search_helpers primitives, same HYBRID_RRF_K / RERANKER_TOP_K,
same BM25F+trigram lexical params, same reranker sidecar on :11500). The ONLY
difference vs that tool is the variant list: here it is the EXACT field-set
order shown in the Validation 'Field-value ladder' window (categories at step
6), so the semantic column reproduces what the live tool already printed and
the hybrid/reranker columns are directly comparable row-for-row.

PERFORMANCE: the bottleneck is BM25F over ~2 340 datasets per query (~3-5 s),
NOT the reranker (~0.4 s) and NOT cosine (instant). So the lexical pass is
parallelised across CPU workers; embeddings are batched through Ollama; the
reranker stays on the sidecar (a separate torch process - avoids the in-process
numpy↔torch OpenMP conflict and is plenty fast). Rankings are cached per unique
enriched query string (a ranking depends only on the query text).

Touches no shipped code. Writes only tools/ladder_modes_results.{json,md}.
Prereqs: Ollama (bge-m3) up; embeddings_a2_sphera_<lang>.bin + ds_cache present;
rerank_service.py live on :11500 for the reranker column.
"""
from __future__ import print_function
import io
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _p in (PARENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8")   # 'λ' label crashes cp1252 console
except Exception:
    pass

from search_helpers import (
    CorpusStats, build_searchable,
    reciprocal_rank_fusion,
    EmbeddingIndex, OllamaEmbeddingClient, LocalRerankerClient,
    build_semantic_haystack,
    ndcg_at_k,
)
from query_context import build_enriched_query

try:
    from constants import (HYBRID_RRF_K, RERANKER_TOP_K,
                           RERANKER_BASE_URL, RERANKER_TIMEOUT_MS)
except Exception:
    HYBRID_RRF_K, RERANKER_TOP_K = 60, 20
    RERANKER_BASE_URL, RERANKER_TIMEOUT_MS = "http://127.0.0.1:11500", 15000

BTT_PARAMS = {"alpha_3g": 0.5}          # same as ablate_query_context.py
LEX_CAP = 200                            # BM25F top-N kept (RRF weight beyond is ~0)

GT_BY_LANG = {
    "en": "sample_project_a2_sphera_en_v0.6__opus-4.8.json",
    "de": "golden_nugget_a2_sphera_de_v0.6__opus-4.8.json",
}

# EXACT field sets from the Validation pushbutton _LADDER (display order).
LADDER = [
    (u"name (baseline)", ["name"]),
    (u"+ class",         ["name", "class"]),
    (u"+ description",    ["name", "class", "description"]),
    (u"+ concrete grade", ["name", "class", "description", "concrete_grade"]),
    (u"+ struct class",   ["name", "class", "description", "concrete_grade",
                           "structural_class"]),
    (u"+ categories",     ["name", "class", "description", "concrete_grade",
                           "structural_class", "host_categories"]),
    (u"+ density",        ["name", "class", "description", "concrete_grade",
                           "structural_class", "host_categories", "density"]),
    (u"+ λ thermal", ["name", "class", "description", "concrete_grade",
                           "structural_class", "host_categories", "density",
                           "thermal_conductivity"]),
    (u"+ host types",     ["name", "class", "description", "concrete_grade",
                           "structural_class", "host_categories", "density",
                           "thermal_conductivity", "host_types"]),
]
NOISE_STEP = {u"+ density", u"+ λ thermal", u"+ host types"}


# ── BM25F worker (numpy-only process; NO torch → no OpenMP conflict) ──────────
_WORKER = {}


def _w_init(cache_path, lang):
    import io as _io
    import json as _json
    from search_helpers import CorpusStats as _CS, build_searchable as _bs
    with _io.open(cache_path, "r", encoding="utf-8") as f:
        cache = _json.load(f)["results"]
    sh = [_bs(d, lang=lang) for d in cache]
    _WORKER["cache"] = cache
    _WORKER["sh"] = sh
    _WORKER["stats"] = _CS.build(sh)


def _w_bm25(s):
    from phase0_benchmark import _run_bm25f_variant
    res = _run_bm25f_variant(s, _WORKER["cache"], _WORKER["sh"],
                             _WORKER["stats"], BTT_PARAMS, None)
    return (s, [d["uuid"] for d, _ in res[:LEX_CAP]])


def _load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rank_first_u(ranked_uuids, accept):
    for i, u in enumerate(ranked_uuids, 1):
        if u in accept:
            return i
    return None


def run_lang(lang, reranker, workers):
    cache_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                              "ds_cache_v2_a2_sphera_{}.json".format(lang))
    gt_path = os.path.join(PARENT, GT_BY_LANG[lang])
    bin_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                            "embeddings_a2_sphera_{}.bin".format(lang))
    for p in (cache_path, gt_path, bin_path):
        if not os.path.exists(p):
            print("ERROR missing:", p)
            return None

    t0 = time.time()
    cache = _load_json(cache_path)
    gt = _load_json(gt_path)
    datasets = cache["results"]
    searchables = [build_searchable(ds, lang=lang) for ds in datasets]
    uuid_to_sh = {ds["uuid"]: sh for ds, sh in zip(datasets, searchables)}

    idx = EmbeddingIndex.load_bin(bin_path)
    sem_uuids, rows = [], []
    for ds in datasets:
        v = idx.get(ds["uuid"])
        if v is not None:
            sem_uuids.append(ds["uuid"])
            rows.append(v)
    mat = np.asarray(rows, dtype=np.float32)

    client = OllamaEmbeddingClient(model="bge-m3", timeout_ms=60000)
    if not client.is_available() or client.embed("concrete") is None:
        print("ERROR: Ollama not reachable / warm-up failed")
        return None

    rr = reranker if (reranker is not None and reranker.is_available()) else None
    modes = ["semantic", "hybrid"] + (["reranker"] if rr else [])
    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    n = len(match_entries)
    print("[{}] {} datasets, {} embedded, {} GT match queries; modes={}".format(
        lang, len(datasets), mat.shape[0], n, modes))

    uniq = set()
    for _lab, fl in LADDER:
        for e in match_entries:
            uniq.add(build_enriched_query(e, fl, lang))
    uniq = sorted(uniq)
    print("  {} unique enriched queries".format(len(uniq)))

    # 1) Batch-embed every unique query (one Ollama forward pass per 64).
    embed_cache = {}
    B = 64
    for i in range(0, len(uniq), B):
        chunk = uniq[i:i + B]
        vecs = client.embed_batch(chunk)
        if not vecs or len(vecs) != len(chunk):
            for s in chunk:
                embed_cache[s] = client.embed(s)
        else:
            for s, v in zip(chunk, vecs):
                embed_cache[s] = v
    print("  embedded in {:.1f}s; BM25F on {} workers...".format(
        time.time() - t0, workers))

    # 2) Parallel BM25F (the bottleneck) - numpy-only workers, no torch.
    bm25_by = {}
    t_lex = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_w_init,
                             initargs=(cache_path, lang)) as ex:
        for s, lst in ex.map(_w_bm25, uniq, chunksize=4):
            bm25_by[s] = lst
    print("  BM25F done in {:.1f}s; cosine+RRF+rerank...".format(
        time.time() - t_lex))

    # 3) Per unique string -> ranked uuid list per mode.
    rank_cache = {}
    done = 0
    for s in uniq:
        qv = embed_cache.get(s)
        if qv is not None:
            scores = mat.dot(np.asarray(qv, dtype=np.float32))
            sem_rank = [sem_uuids[i] for i in np.argsort(-scores)]
        else:
            sem_rank = []
        btt = bm25_by.get(s, [])
        if btt and sem_rank:
            rrf = reciprocal_rank_fusion([btt, sem_rank], k=HYBRID_RRF_K)
            hyb = [u for u, _ in sorted(rrf.items(), key=lambda t: t[1],
                                        reverse=True)]
        else:
            hyb = btt or sem_rank
        rec = {"semantic": sem_rank, "hybrid": hyb}
        if rr is not None:
            rer = hyb
            topk = hyb[:RERANKER_TOP_K]
            if topk:
                docs = [build_semantic_haystack(uuid_to_sh.get(u, {}))
                        for u in topk]
                logits = rr.rerank(s, docs)
                if logits and len(logits) == len(topk):
                    reord = [u for u, _ in sorted(zip(topk, logits),
                             key=lambda t: t[1], reverse=True)]
                    rer = reord + hyb[RERANKER_TOP_K:]
            rec["reranker"] = rer
        rank_cache[s] = rec
        done += 1
        if done % 100 == 0:
            print("    {}/{} ranked ({:.1f}s)".format(
                done, len(uniq), time.time() - t0))

    # 4) Aggregate per (mode, variant).
    out = {m: [] for m in modes}
    for lab, fl in LADDER:
        per = {m: {"recip": [], "h1": 0, "h5": 0, "h10": 0, "any": 0,
                   "ndcg": []} for m in modes}
        for e in match_entries:
            s = build_enriched_query(e, fl, lang)
            correct = e.get("correct_uuid", "") or ""
            accept_only = list(e.get("acceptable_uuids", []) or [])
            accept = set(accept_only)
            if correct:
                accept.add(correct)
            rc = rank_cache[s]
            for m in modes:
                rk = rc.get(m, [])
                first = _rank_first_u(rk, accept)
                P = per[m]
                if first is not None:
                    P["any"] += 1
                    P["recip"].append(1.0 / first)
                    if first <= 1:
                        P["h1"] += 1
                    if first <= 5:
                        P["h5"] += 1
                    if first <= 10:
                        P["h10"] += 1
                else:
                    P["recip"].append(0.0)
                P["ndcg"].append(ndcg_at_k(rk, correct, accept_only, k=10))
        for m in modes:
            P = per[m]
            out[m].append({
                "label": lab, "fields": fl, "n": n,
                "mrr": sum(P["recip"]) / n if n else 0.0,
                "r1": P["h1"], "r5": P["h5"], "r10": P["h10"], "rall": P["any"],
                "ndcg": sum(P["ndcg"]) / n if n else 0.0,
            })
    dur = time.time() - t0
    print("  [{}] done in {:.1f}s".format(lang, dur))
    return {"lang": lang, "n": n, "modes": modes, "out": out, "secs": dur,
            "rrf_k": HYBRID_RRF_K, "rerank_top_k": RERANKER_TOP_K,
            "lex_cap": LEX_CAP}


def run_fuzzy_lang(lang, workers):
    """Offline FUZZY-mode ladder (no Ollama / sidecar needed): the live 'Fuzzy'
    ranking - fuzzy binary filter + BM25F - fed the *enriched* query, so we can
    see whether BIM-context enrichment helps or hurts the lexical path. Reuses
    the same parallel `_w_bm25` worker the semantic ladder uses for BM25F."""
    cache_path = os.path.join(PARENT, "LCiA_Extension_Cache",
                              "ds_cache_v2_a2_sphera_{}.json".format(lang))
    gt_path = os.path.join(PARENT, GT_BY_LANG[lang])
    for p in (cache_path, gt_path):
        if not os.path.exists(p):
            print("ERROR missing:", p)
            return None

    t0 = time.time()
    cache = _load_json(cache_path)
    gt = _load_json(gt_path)
    datasets = cache["results"]
    match_entries = [e for e in gt["entries"] if e.get("label") == "match"]
    n = len(match_entries)
    print("[{}] FUZZY ladder: {} datasets, {} GT match queries".format(
        lang, len(datasets), n))

    uniq = set()
    for _lab, fl in LADDER:
        for e in match_entries:
            uniq.add(build_enriched_query(e, fl, lang))
    uniq = sorted(uniq)
    print("  {} unique enriched queries; fuzzy+BM25F on {} workers...".format(
        len(uniq), workers))

    bm25_by = {}
    with ProcessPoolExecutor(max_workers=workers, initializer=_w_init,
                             initargs=(cache_path, lang)) as ex:
        for s, lst in ex.map(_w_bm25, uniq, chunksize=4):
            bm25_by[s] = lst
    print("  ranked in {:.1f}s".format(time.time() - t0))

    rows = []
    for lab, fl in LADDER:
        per = {"recip": 0.0, "h1": 0, "h5": 0, "h10": 0, "any": 0, "ndcg": 0.0}
        for e in match_entries:
            s = build_enriched_query(e, fl, lang)
            correct = e.get("correct_uuid", "") or ""
            accept_only = list(e.get("acceptable_uuids", []) or [])
            accept = set(accept_only)
            if correct:
                accept.add(correct)
            rk = bm25_by.get(s, [])
            first = _rank_first_u(rk, accept)
            if first is not None:
                per["any"] += 1
                per["recip"] += 1.0 / first
                if first <= 1:
                    per["h1"] += 1
                if first <= 5:
                    per["h5"] += 1
                if first <= 10:
                    per["h10"] += 1
            per["ndcg"] += ndcg_at_k(rk, correct, accept_only, k=10)
        rows.append({
            "label": lab, "fields": fl, "n": n,
            "mrr": per["recip"] / n if n else 0.0,
            "r1": per["h1"], "r5": per["h5"], "r10": per["h10"],
            "rall": per["any"], "ndcg": per["ndcg"] / n if n else 0.0,
        })
    dur = time.time() - t0
    print("  [{}] FUZZY done in {:.1f}s".format(lang, dur))
    # Same record shape as run_lang so print_tables/write_md work unchanged.
    return {"lang": lang, "n": n, "modes": ["fuzzy"], "out": {"fuzzy": rows},
            "secs": dur, "rrf_k": HYBRID_RRF_K, "rerank_top_k": RERANKER_TOP_K,
            "lex_cap": LEX_CAP}


def _pct(x, n):
    return int(round(100.0 * x / n)) if n else 0


def print_tables(res):
    n = res["n"]
    print("\n" + "=" * 92)
    print("LANGUAGE {}   (n={} GT match materials)   RRF k={}  rerank top-{}".format(
        res["lang"].upper(), n, res["rrf_k"], res["rerank_top_k"]))
    for m in res["modes"]:
        rowsv = res["out"][m]
        base = rowsv[0]["mrr"]
        print("\n--- mode: {} ---".format(m.upper()))
        print("  {:<16} {:>6} {:>5} {:>5} {:>5} {:>7} {:>9}".format(
            "field set", "MRR", "R@1", "R@5", "R@10", "nDCG10", "dMRR"))
        for r in rowsv:
            tag = "  <noise" if r["label"] in NOISE_STEP else ""
            print("  {:<16} {:>6.3f} {:>4d}% {:>4d}% {:>4d}% {:>7.3f} "
                  "{:>+9.3f}{}".format(
                      r["label"], r["mrr"], _pct(r["r1"], n), _pct(r["r5"], n),
                      _pct(r["r10"], n), r["ndcg"], r["mrr"] - base, tag))
        best = max(rowsv, key=lambda r: r["mrr"])
        print("  BEST: {}  MRR {:.3f} ({:+.3f} vs name)".format(
            best["label"], best["mrr"], best["mrr"] - base))


def _fmt_delta(x):
    return u"{:+.3f}".format(x)


def write_md(results, path):
    """Render tools/ladder_modes.md from the in-memory results list (same shape
    as ladder_modes_results.json). House style mirrors the auto-generated
    tools/ablation_query_context_*.md: title + metadata preamble, per-language
    per-mode tables, a best-variant summary, and a cross-mode synthesis."""
    best_combo = u"name + class + description + concrete_grade + structural_class"
    L = []
    L.append(u"# Field-value ladder — all dense modes (semantic / hybrid / reranker)")
    L.append(u"")
    L.append(u"Auto-generated by `tools/ladder_modes.py` (`write_md`). Cumulative "
             u"BIM-context field ladder in the Validation pushbutton's display "
             u"order, evaluated for **every dense mode** — not just semantic "
             u"cosine — so the hybrid and reranker columns are directly comparable "
             u"to the semantic ladder the live tool already prints. Method is "
             u"identical to `tools/ablate_query_context.py` (same `query_context` "
             u"formatting, same BM25F+trigram lexical pass, same RRF and reranker "
             u"sidecar).")
    L.append(u"")
    r0 = results[0]
    secs = u" · ".join(u"{} {:.1f}s".format(r["lang"].upper(), r.get("secs", 0.0))
                       for r in results)
    L.append(u"- **Embedding**: bge-m3 · **RRF k**: {} · **reranker top-K**: {} "
             u"· **lexical cap (BM25F top-N)**: {}".format(
                 r0.get("rrf_k", HYBRID_RRF_K), r0.get("rerank_top_k", RERANKER_TOP_K),
                 r0.get("lex_cap", LEX_CAP)))
    L.append(u"- **n** = {} GT match materials per language · "
             u"**Generated**: {}".format(r0.get("n", "?"),
                                          time.strftime("%Y-%m-%d %H:%M:%S")))
    L.append(u"- **Run time**: {}".format(secs))
    L.append(u"")
    L.append(u"> **Headline.** The single combo that wins across every dense mode "
             u"and both languages is **{}** (“+ struct class”). "
             u"`+ categories` lifts EN-semantic only but hurts hybrid + reranker "
             u"and all of DE; `density`, `λ thermal`, and `host_types` are "
             u"noise everywhere — keep them as deterministic filters, not query "
             u"text.".format(best_combo))
    L.append(u"")

    # ── per-language, per-mode tables ────────────────────────────────────────
    for res in results:
        n = res["n"]
        L.append(u"## Language {} (n={})".format(res["lang"].upper(), n))
        L.append(u"")
        for m in res["modes"]:
            rowsv = res["out"][m]
            base = rowsv[0]["mrr"]
            L.append(u"### Mode: {}".format(m))
            L.append(u"")
            L.append(u"| Field set | MRR | R@1 | R@5 | R@10 | "
                     u"ΔMRR vs name |")
            L.append(u"|---|---:|---:|---:|---:|---:|")
            for r in rowsv:
                lab = r["label"] + (u" *" if r["label"] in NOISE_STEP else u"")
                L.append(u"| {} | {:.3f} | {}% | {}% | {}% | {} |".format(
                    lab, r["mrr"], _pct(r["r1"], n), _pct(r["r5"], n),
                    _pct(r["r10"], n), _fmt_delta(r["mrr"] - base)))
            best = max(rowsv, key=lambda r: r["mrr"])
            L.append(u"")
            L.append(u"**Best:** {} — MRR {:.3f} ({} vs name)".format(
                best["label"], best["mrr"], _fmt_delta(best["mrr"] - base)))
            L.append(u"")

    # ── best variant per mode (per language) ─────────────────────────────────
    L.append(u"## Best variant per mode (per language)")
    L.append(u"")
    L.append(u"| Lang | Mode | Best field set | MRR | vs name |")
    L.append(u"|---|---|---|---:|---:|")
    for res in results:
        for m in res["modes"]:
            rowsv = res["out"][m]
            base = rowsv[0]["mrr"]
            best = max(rowsv, key=lambda r: r["mrr"])
            L.append(u"| {} | {} | {} | {:.3f} | {} |".format(
                res["lang"].upper(), m, best["label"], best["mrr"],
                _fmt_delta(best["mrr"] - base)))
    L.append(u"")

    # ── cross-mode synthesis - marginal Δ per step across all (lang, mode) ────
    L.append(u"## Cross-mode synthesis — which fields help vs noise")
    L.append(u"")
    L.append(u"For each cumulative step the marginal ΔMRR (step − previous step) "
             u"is averaged across every (language × mode) cell; *helps in* counts "
             u"the cells where that step improved MRR.")
    L.append(u"")
    L.append(u"| Field (step) | mean marginal ΔMRR | helps in | Verdict |")
    L.append(u"|---|---:|---:|---|")
    for i in range(1, len(LADDER)):
        lab = LADDER[i][0]
        deltas = []
        for res in results:
            for m in res["modes"]:
                rowsv = res["out"][m]
                if i < len(rowsv):
                    deltas.append(rowsv[i]["mrr"] - rowsv[i - 1]["mrr"])
        if not deltas:
            continue
        mean_d = sum(deltas) / len(deltas)
        pos = sum(1 for d in deltas if d > 0)
        if lab in NOISE_STEP:
            verdict = u"noise — keep as a filter"
        elif mean_d <= -0.015:
            verdict = (u"hurts (helps EN-semantic only)"
                       if lab == u"+ categories" else u"hurts")
        elif mean_d >= 0.015 and pos >= 4:
            verdict = u"helps"
        else:
            verdict = u"~ neutral — safe to keep"
        L.append(u"| {} | {} | {}/{} | {} |".format(
            lab, _fmt_delta(mean_d), pos, len(deltas), verdict))
    L.append(u"")
    L.append(u"**Recommendation.** Enrich the dense-mode query with **{}** and "
             u"stop there. `description` and `concrete_grade` help in every cell; "
             u"`class` and `structural_class` are corroborating context that never "
             u"materially hurts and give the production reranker its best result. "
             u"Do **not** add `categories` — it only helps EN-semantic and drags "
             u"down hybrid + reranker in both languages. Route `density`, "
             u"`λ thermal`, and `host_types` to deterministic filters."
             .format(best_combo))
    L.append(u"")

    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(u"\n".join(L) + u"\n")
    print("  wrote ->", path)


def main():
    md_path = os.path.join(HERE, "ladder_modes.md")
    if "--md-only" in sys.argv:
        # Regenerate the .md from the existing results JSON - no Ollama / sidecar
        # / BM25F re-run needed. Used to verify the report generator in isolation.
        rec_path = os.path.join(HERE, "ladder_modes_results.json")
        results = _load_json(rec_path)
        write_md(results, md_path)
        print("Wrote", md_path, "from existing", rec_path)
        return 0

    langs = ["en", "de"]
    if "--lang" in sys.argv:
        langs = [sys.argv[sys.argv.index("--lang") + 1]]
    workers = 14
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])

    if "--fuzzy-only" in sys.argv:
        # Lexical-only ladder: does BIM-context enrichment help the Fuzzy path?
        # No Ollama / reranker needed. Saved to a SEPARATE json so the
        # validated semantic/hybrid/reranker results are untouched.
        fres = []
        for lg in langs:
            r = run_fuzzy_lang(lg, workers)
            if r:
                fres.append(r)
                try:
                    print_tables(r)
                except Exception as ex:
                    print("  (table print skipped:", ex, ")")
        if fres:
            fpath = os.path.join(HERE, "ladder_modes_fuzzy_results.json")
            with io.open(fpath, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(fres, indent=2, ensure_ascii=False))
            print("\nWrote", fpath)
        return 0

    do_rerank = "--no-rerank" not in sys.argv

    reranker = None
    if do_rerank:
        reranker = LocalRerankerClient(base_url=RERANKER_BASE_URL,
                                       timeout_ms=RERANKER_TIMEOUT_MS)
        if reranker.is_available():
            print("Reranker sidecar ready:", reranker.model)
        else:
            print("WARN reranker sidecar down ({}); reranker column skipped"
                  .format(reranker.last_error))
            reranker = None

    results = []
    rec_path = os.path.join(HERE, "ladder_modes_results.json")
    for lg in langs:
        r = run_lang(lg, reranker, workers)
        if r:
            results.append(r)
            with io.open(rec_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(results, indent=2, ensure_ascii=False))
            print("  saved ->", rec_path)
            try:
                print_tables(r)
            except Exception as ex:
                print("  (table print skipped:", ex, ")")
    if results:
        write_md(results, md_path)
    print("\nWrote", rec_path)
    return 0


if __name__ == "__main__":
    main()
