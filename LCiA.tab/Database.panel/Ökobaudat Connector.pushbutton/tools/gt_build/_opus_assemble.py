# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Assemble sample_project_a2_sphera_en_v0.6__opus-4.8.json from _opus_decisions.py.

Pulls exact name/classification/dataset_type from the cache by UUID (guarantees R7),
expands near-identical families (R9), validates every invariant, writes the JSON, and
prints a self-check report. CPython 3.
"""
import json, io, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PB = os.path.abspath(os.path.join(HERE, "..", ".."))           # pushbutton root
CACHE = os.path.join(PB, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_en.json")
MATS = os.path.join(PB, "..", "..", "Context.panel", "ExtractContext.pushbutton", "data", "materials_context - Snowden - En.json")
OUT = os.path.join(PB, "sample_project_a2_sphera_en_v0.6__opus-4.8.json")

sys.path.insert(0, HERE)
from _opus_decisions import DECISIONS

TYPE_ORDER = {"generic":0, "average":1, "representative":2, "template":3, "specific":4}

def load_json(p):
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)

def main():
    cache_rows = load_json(CACHE)["results"]
    by_uuid = {}
    for r in cache_rows:
        by_uuid[r["uuid"]] = r
    mats = load_json(MATS)

    errors = []
    entries = []
    matched = skipped = review_cnt = 0
    review_list = []

    def dtype(u):
        return (by_uuid[u].get("dataset_type","") or "").replace(" dataset","").strip()

    for m in mats:
        rid = m["revit_id"]
        d = DECISIONS.get(rid)
        if d is None:
            errors.append("NO DECISION for revit_id %s (%s)" % (rid, m.get("name")))
            continue
        he = m.get("host_elements", {}) or {}
        base = {
            "revit_id": rid,
            "name": m.get("name"),
            "description": m.get("description", ""),
            "class": m.get("class", ""),
            "host_categories": he.get("categories", []),
            "host_types": he.get("types", []),
            "physical_asset": m.get("physical_asset", {}),
            "thermal_asset": m.get("thermal_asset", {}),
        }
        if d["label"] == "skip":
            skipped += 1
            sr = d["skip_reason"]
            if not re.match(r"^\[(not a material|negligible appearance finish|outside building-fabric boundary)\]", sr):
                errors.append("BAD skip category tag rid %s: %s" % (rid, sr[:40]))
            base.update({
                "label": "skip",
                "skip_reason": sr,
                "expected_classification_path": None,
                "correct_uuid": None,
                "acceptable_uuids": [],
                "top_10": [],
            })
            entries.append(base)
            continue

        # ---- MATCH ----
        matched += 1
        picks = d["picks"]
        # validate uuids exist
        for (u, g, rat) in picks:
            if u not in by_uuid:
                errors.append("MISSING uuid %s in rid %s picks" % (u, rid))
        # rank-1 grade must be 3
        if picks[0][1] != 3:
            errors.append("rid %s rank-1 grade != 3 (%s)" % (rid, picks[0][1]))
        # grades must be non-increasing (rank by grade)
        grfor = [g for (_,g,_) in picks]
        if any(grform > grprev for grprev, grform in zip(grfor, grfor[1:])):
            errors.append("rid %s picks not grade-ordered: %s" % (rid, grfor))
        # all grades 1..3
        for (u,g,rat) in picks:
            if g not in (1,2,3):
                errors.append("rid %s bad grade %s for %s" % (rid, g, u))

        acceptable = []
        seen = set()
        for (u, g, rat) in picks:
            if u not in seen and u in by_uuid:
                acceptable.append(u); seen.add(u)

        # ---- expansions (append to acceptable only) ----
        for rule in d.get("expand", []):
            leaf = rule["leaf"]; rgx = re.compile(rule["name_regex"], re.I)
            allow = set(rule.get("types", []))
            matches = []
            for r in cache_rows:
                if r.get("classification","") != leaf:
                    continue
                dt = (r.get("dataset_type","") or "").replace(" dataset","").strip()
                if allow and dt not in allow:
                    continue
                if not rgx.search(r.get("name","")):
                    continue
                matches.append(r)
            matches.sort(key=lambda r: (TYPE_ORDER.get((r.get("dataset_type","") or "").replace(" dataset","").strip(), 9), r.get("name","")))
            for r in matches:
                u = r["uuid"]
                if u not in seen:
                    acceptable.append(u); seen.add(u)

        correct = picks[0][0]
        # build top_10 (<=10) from picks
        top = []
        for i, (u, g, rat) in enumerate(picks[:10]):
            r = by_uuid[u]
            top.append({
                "rank": i+1,
                "uuid": u,
                "name": r["name"],
                "classification": r.get("classification",""),
                "relevance": g,
                "rationale": rat,
            })
        # invariant: correct == top_10[0] == acceptable[0]
        if not (correct == top[0]["uuid"] == acceptable[0]):
            errors.append("rid %s invariant broken: correct=%s top0=%s acc0=%s" % (rid, correct, top[0]["uuid"], acceptable[0]))
        if len(top) > 10:
            errors.append("rid %s top_10 > 10" % rid)

        rev = bool(d.get("review", False))
        if rev:
            review_cnt += 1
            review_list.append((rid, m.get("name")))
        base.update({
            "label": "match",
            "expected_classification_path": by_uuid[correct].get("classification",""),
            "lca_reasoning": d["lca_reasoning"],
            "confidence": d["confidence"],
            "needs_human_review": rev,
            "correct_uuid": correct,
            "acceptable_uuids": acceptable,
            "top_10": top,
        })
        entries.append(base)

    # coverage check
    if matched + skipped != len(mats):
        errors.append("COUNT mismatch matched+skipped=%d != %d" % (matched+skipped, len(mats)))

    out = {
        "version": "0.6-snowdon-expert",
        "annotator_model": "claude-opus-4-8",
        "annotation_date": "2026-06-01",
        "source_bim": "LCiA.tab/Context.panel/ExtractContext.pushbutton/data/materials_context.json",
        "source_revit_model": "Snowdon Towers Sample Architectural.rvt",
        "datastock": "a2_sphera",
        "language": "en",
        "dataset_count": 2383,
        "matched_count": matched,
        "skipped_count": skipped,
        "needs_human_review_count": review_cnt,
        "system_boundary": "IN = building fabric (structure, envelope, partitions, floor/roof/wall build-ups, glazing, insulation, waterproofing/membranes, masonry, screed, tile, doors that close the envelope/partitions) PLUS sanitary ceramic ware; OUT = MEP/building services, loose equipment/appliances, furniture/casework, site & landscape furnishings, photovoltaics, pure appearance/paint finishes, and Revit placeholders.",
        "methodology": "Each of the 145 BIM materials was judged MAP or SKIP against the explicit system boundary, working only from the local BIM context and the 2383-dataset a2_sphera EN cache. Candidate pools were enumerated deterministically by OEKOBAUDAT classification family (taxonomy-driven, independent of the plugin's BM25F/semantic search), then ranked by an LCA/AEC-materials specialist. correct_uuid = the single closest-named / closest-function EPD (generic/representative preferred, R5); acceptable_uuids = every genuine functional substitute in ranked order (R2/R3), graded 3=exact, 2=strong, 1=looser. Near-identical families (e.g. plant-specific ready-mix concrete of the matching strength class) are listed in full in acceptable_uuids (R9). All UUID/name/classification strings were pulled verbatim from the cache by UUID at assembly time.",
        "ranking_basis": [
            "rank 1 = closest-named / closest-function EPD; relevance grade 3.",
            "acceptable_uuids = correct first, then all genuine substitutes ranked by grade then closeness.",
            "Generic / representative / category-average EPDs preferred over single-vendor EPDs for rank 1, and English-named where a real choice exists (R5).",
            "Tie-breakers: concrete strength class (MPa->C-class), bulk density, thermal conductivity, host-element function.",
            "Functional equivalence governs acceptance: candidates may span classification leaves only when an LCA reviewer would treat them as interchangeable, never across function/form (R3).",
        ],
        "metric_target": "Recall@K (K=1,3,5,10), exact-correct R@1 / MRR against correct_uuid, and any-acceptable Recall@K against acceptable_uuids.",
        "scope_notes": "Paving surfaces (asphalt, clay-brick paving, precast-concrete paving) are treated as building fabric (horizontal construction) and MAPPED; planter boxes, tree grates, soil, plants, turf and living walls are site/landscape furnishings and SKIPPED. Finish-named metals are mapped to their bulk substance when the host is within the fabric boundary (e.g. 'Steel, Paint Finish, Dark Gray' on stairs -> steel section) and skipped when the host is a fixture/appliance/furniture/site item or when no bulk substance exists (Paint class). Doors that close the envelope/partitions are fabric, so door-leaf substances (wood, steel, aluminium) are mapped while pure paint/colour door finishes are skipped. Sanitary ceramic ware (WCs, washbasins) is MAPPED with needs_human_review=true. Wood-look ceramic tile carries a mislabelled BIM class 'Wood' and is mapped to glazed stoneware tile (R3).",
        "entries": entries,
    }

    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # ---------- report ----------
    print("written:", OUT)
    print("materials:", len(mats), " matched:", matched, " skipped:", skipped, " needs_review:", review_cnt)
    print("entries:", len(entries))
    # acceptable-size distribution
    sizes = [len(e["acceptable_uuids"]) for e in entries if e["label"]=="match"]
    if sizes:
        print("acceptable_uuids size: min %d  max %d  mean %.1f" % (min(sizes), max(sizes), sum(sizes)/len(sizes)))
    if errors:
        print("\n!!! %d ERRORS !!!" % len(errors))
        for e in errors:
            print("  -", e)
    else:
        print("\nALL INVARIANTS OK (correct==top_10[0]==acceptable[0]; all UUIDs exist; rank-1 grade 3; grade-ordered; counts==145).")
    print("\nneeds_human_review (%d):" % review_cnt)
    for rid, nm in review_list:
        print("  %-8s %s" % (rid, nm))

if __name__ == "__main__":
    main()
