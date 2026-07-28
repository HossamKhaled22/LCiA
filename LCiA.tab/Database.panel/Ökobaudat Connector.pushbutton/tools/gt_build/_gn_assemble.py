# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Assemble golden_nugget_a2_sphera_de_v0.6__opus-4.8.json from _gn_decisions.py.

Pulls exact German name/classification from the DE cache by UUID (R6/R8), expands
near-identical families (R9), validates every invariant (R7), writes the JSON to BOTH
the Connector pushbutton folder and the Validation.pushbutton folder, and prints a
self-check report. CPython 3.
"""
import json, io, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PB = os.path.abspath(os.path.join(HERE, "..", ".."))                       # Connector pushbutton root
EXT = os.path.abspath(os.path.join(PB, "..", "..", ".."))                  # LCiA.extension root
CACHE = os.path.join(PB, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_de.json")
MATS = os.path.join(PB, "..", "..", "Context.panel", "ExtractContext.pushbutton", "data", "materials_context - Nugget - De.json")
VALID = os.path.join(EXT, "LCiA.tab", "Validation.panel", "Validation.pushbutton")
OUT1 = os.path.join(PB, "golden_nugget_a2_sphera_de_v0.6__opus-4.8.json")
OUT2 = os.path.join(VALID, "golden_nugget_a2_sphera_de_v0.6__opus-4.8.json")

sys.path.insert(0, HERE)
from _gn_decisions import DECISIONS

TYPE_ORDER = {"generic":0, "average":1, "representative":2, "template":3, "specific":4}

def load_json(p):
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)

def main():
    cache_rows = load_json(CACHE)["results"]
    by_uuid = {r["uuid"]: r for r in cache_rows}
    mats = load_json(MATS)

    errors = []; entries = []; matched = skipped = review_cnt = 0; review_list = []

    for m in mats:
        rid = m["revit_id"]
        d = DECISIONS.get(rid)
        if d is None:
            errors.append("NO DECISION for revit_id %s (%s)" % (rid, m.get("name"))); continue
        he = m.get("host_elements", {}) or {}
        base = {
            "revit_id": rid, "name": m.get("name"), "description": m.get("description", ""),
            "class": m.get("class", ""),
            "host_categories": he.get("categories", []), "host_types": he.get("types", []),
            "physical_asset": m.get("physical_asset", {}), "thermal_asset": m.get("thermal_asset", {}),
        }
        if d["label"] == "skip":
            skipped += 1
            sr = d["skip_reason"]
            if not re.match(r"^\[(not a material|negligible appearance finish|outside building-fabric boundary)\]", sr):
                errors.append("BAD skip tag rid %s: %s" % (rid, sr[:50]))
            base.update({"label":"skip","skip_reason":sr,"expected_classification_path":None,
                         "correct_uuid":None,"acceptable_uuids":[],"top_10":[]})
            entries.append(base); continue

        matched += 1
        picks = d["picks"]
        for (u,g,rat) in picks:
            if u not in by_uuid: errors.append("MISSING uuid %s in rid %s" % (u, rid))
        if picks[0][1] != 3: errors.append("rid %s rank-1 grade != 3" % rid)
        gr = [g for (_,g,_) in picks]
        if any(b > a for a,b in zip(gr, gr[1:])): errors.append("rid %s picks not grade-ordered: %s" % (rid, gr))
        for (u,g,rat) in picks:
            if g not in (1,2,3): errors.append("rid %s bad grade %s" % (rid, g))

        acceptable = []; seen = set()
        for (u,g,rat) in picks:
            if u not in seen and u in by_uuid: acceptable.append(u); seen.add(u)
        for rule in d.get("expand", []):
            leaf = rule["leaf"]; rgx = re.compile(rule["name_regex"], re.I); allow = set(rule.get("types", []))
            ms = []
            for r in cache_rows:
                if r.get("classification","") != leaf: continue
                dt = (r.get("dataset_type","") or "").replace(" dataset","").strip()
                if allow and dt not in allow: continue
                if not rgx.search(r.get("name","")): continue
                ms.append(r)
            ms.sort(key=lambda r: (TYPE_ORDER.get((r.get("dataset_type","") or "").replace(" dataset","").strip(),9), r.get("name","")))
            for r in ms:
                if r["uuid"] not in seen: acceptable.append(r["uuid"]); seen.add(r["uuid"])

        correct = picks[0][0]
        top = []
        for i,(u,g,rat) in enumerate(picks[:10]):
            r = by_uuid[u]
            top.append({"rank":i+1,"uuid":u,"name":r["name"],"classification":r.get("classification",""),
                        "relevance":g,"rationale":rat})
        if not (correct == top[0]["uuid"] == acceptable[0]):
            errors.append("rid %s invariant broken" % rid)
        if len(top) > 10: errors.append("rid %s top_10>10" % rid)
        rev = bool(d.get("review", False))
        if rev: review_cnt += 1; review_list.append((rid, m.get("name")))
        base.update({
            "label":"match","expected_classification_path":by_uuid[correct].get("classification",""),
            "lca_reasoning":d["lca_reasoning"],"confidence":d["confidence"],"needs_human_review":rev,
            "correct_uuid":correct,"acceptable_uuids":acceptable,"top_10":top})
        entries.append(base)

    if matched + skipped != len(mats):
        errors.append("COUNT mismatch %d != %d" % (matched+skipped, len(mats)))

    out = {
        "version":"0.6-golden-nugget-expert","annotator_model":"claude-opus-4-8","annotation_date":"2026-06-01",
        "source_bim":"LCiA.tab/Context.panel/ExtractContext.pushbutton/data/materials_context.json",
        "source_revit_model":"BIM_Projekt_Golden_Nugget.rvt",
        "datastock":"a2_sphera","language":"de","dataset_count":len(cache_rows),
        "matched_count":matched,"skipped_count":skipped,"needs_human_review_count":review_cnt,
        "system_boundary":"IN = Gebäude-Fabric (Tragwerk, Hülle, Trennwände, Boden-/Dach-/Wandaufbauten, Verglasung, Dämmung, Abdichtung/Bahnen, Mauerwerk, Estrich, Putz, Fliesen, hüllenschließende Türen) PLUS Sanitärkeramik; OUT = TGA/Gebäudetechnik, lose Geräte/Ausstattung, Möbel/Einbauten, Außenanlagen/Landschaft, Photovoltaik, reine Anstrich-/Erscheinungs-Finishes, Kontext-Massenmodelle der Umgebung und Revit-/Software-Platzhalter.",
        "methodology":"Jedes der 88 BIM-Materialien wurde gegen die explizite Systemgrenze als MAP oder SKIP beurteilt, ausschliesslich aus dem lokalen BIM-Kontext und dem 2341-Datensatz-Cache a2_sphera DE. Kandidatenpools wurden deterministisch nach OEKOBAUDAT-Klassifikationsfamilie aufgezaehlt (taxonomie-getrieben, unabhaengig von der BM25F/semantischen Suche des Plugins) und von einem LCA/AEC-Materialspezialisten gerankt. correct_uuid = das namens-/funktionsnaechste EPD (generisch/repraesentativ bevorzugt, R5); acceptable_uuids = alle echten funktionalen Substitute in Rangfolge (R2/R3), Note 3=exakt, 2=stark, 1=lockerer. Nahezu identische Familien (z. B. werksspezifischer Transportbeton der passenden Festigkeitsklasse, alle Mineralwolle-Varianten) sind vollstaendig gelistet (R9). Alle UUID/Name/Klassifikation wurden zur Assemblierzeit byte-genau per UUID aus dem DE-Cache gezogen.",
        "ranking_basis":[
            "Rang 1 = namens-/funktionsnaechstes EPD; Relevanz 3.",
            "acceptable_uuids = correct zuerst, dann alle echten Substitute nach Note und Naehe.",
            "Generische / repraesentative / Durchschnitts-EPDs vor Einzelhersteller-EPDs fuer Rang 1 (R5); deutscher Name moeglichst direkt passend.",
            "Tie-breaker: Beton-Festigkeitsklasse (MPa->C-Klasse), Rohdichte, Waermeleitfaehigkeit, Bauteilfunktion, Steifigkeit (hart->Hartschaum, weich->Matte).",
            "Funktionale Aequivalenz entscheidet ueber Akzeptanz: Kandidaten duerfen Klassifikationsblaetter nur kreuzen, wenn ein LCA-Pruefer sie als austauschbar ansieht; nie ueber Funktion/Form hinweg (R3, Kreuzfamilie <= Note 2).",
        ],
        "metric_target":"Recall@K (K=1,3,5,10), Exact-correct R@1 / MRR gegen correct_uuid, sowie any-acceptable Recall@K gegen acceptable_uuids.",
        "scope_notes":"Belagsflaechen (Asphalt, Gelaendearbeiten) werden als Bauwerksflaeche bewertet, jedoch als Grenzfall (review) markiert; Umgebungs-Massenmodelle, elektrische Ausstattung und reine RGB-Farb-/Lack-Kunststoffe werden uebersprungen. Finish-benannte Metalle/Hoelzer werden auf ihre Bulk-Substanz abgebildet, wenn der Host zur Fabric gehoert (z. B. 'Metall - Stahl schwarz' an Geschossdecken -> Stahlprofil; Holz-RGB an Fassadenpfosten/Fenstern -> Konstruktions-/Schnittholz), und uebersprungen bei Geraete-/Ausstattungs-Hosts oder reinen Farb-/Lackschichten ohne Bulk. Die charakteristische 'Goldfassade Brass' wird auf Messing/Kupferlegierung abgebildet (review). 'Dämmung - hart' -> EPS-Hartschaum (Hartschaum-Familie), 'Dämmung - weich' -> Mineralwolle (volle Variantenreihe, R9). Kaminrohr (Abgasrohr) = TGA -> SKIP, waehrend Kamin-Aussenbekleidung/-Abdeckung als Fabric-Mauerwerk gemappt werden (review).",
        "entries":entries,
    }

    for OUT in (OUT1, OUT2):
        with io.open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("written:", OUT)

    print("\nmaterials:", len(mats), " matched:", matched, " skipped:", skipped, " needs_review:", review_cnt)
    sizes = [len(e["acceptable_uuids"]) for e in entries if e["label"]=="match"]
    if sizes: print("acceptable size: min %d max %d mean %.1f" % (min(sizes), max(sizes), sum(sizes)/len(sizes)))
    if errors:
        print("\n!!! %d ERRORS !!!" % len(errors))
        for e in errors: print("  -", e)
    else:
        print("\nALL INVARIANTS OK (correct==top_10[0]==acceptable[0]; UUIDs exist; rank-1 grade 3; grade-ordered; counts==88).")
    print("\nneeds_human_review (%d):" % review_cnt)
    for rid, nm in review_list: print("  %-8s %s" % (rid, nm))

if __name__ == "__main__":
    main()
