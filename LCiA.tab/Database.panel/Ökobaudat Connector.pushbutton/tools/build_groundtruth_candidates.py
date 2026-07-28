# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Stage 1 of the fresh Snowdon ground-truth build (v0.4).

Deterministic, no LLM, no network. Reads the full Snowdon Towers BIM material
extraction + the A2 Sphera EN ÖKOBAUDAT caches and produces, per *matchable*
construction material, an INDEPENDENT high-recall candidate pool for the expert
matching workflow to rank.

"Independent" = the pool is assembled from (a) the ÖKOBAUDAT classification
taxonomy and (b) a simple token-overlap (Jaccard) recall booster - deliberately
NOT the BM25F / semantic / hybrid scoring formulae that the benchmark later puts
under test. This keeps the gold standard methodologically decoupled from the
systems it measures.

Outputs (under tools/gt_build/):
  candidates/<revit_id>.json   {material, candidates[], tech{uuid:desc}}
  skips.json                   list of v0.3-format skip entries (the ~84 non-products)
  manifest.json                {datastock, lang, dataset_count, match_count,
                                skip_count, matchable:[{revit_id,name,candidate_file,...}]}

Run (CPython 3, no Revit needed):
    python tools/build_groundtruth_candidates.py
"""
from __future__ import print_function
import io
import json
import os
import re
import sys

HERE     = os.path.dirname(os.path.abspath(__file__))
PARENT   = os.path.dirname(HERE)                       # ...\Ökobaudat Connector.pushbutton
LCIA_TAB = os.path.dirname(os.path.dirname(PARENT))    # ...\LCiA.tab

# Reuse the single-source-of-truth German->English classification translator.
sys.path.insert(0, PARENT)
from classification_labels import translate_path       # noqa: E402

BIM_CTX  = os.path.join(LCIA_TAB, "Context.panel", "ExtractContext.pushbutton",
                        "data", "materials_context - Snowden - En.json")
CACHE_DS  = os.path.join(PARENT, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_en.json")
CACHE_IND = os.path.join(PARENT, "LCiA_Extension_Cache", "indicators_a2_sphera_en.json")

OUT_DIR  = os.path.join(HERE, "gt_build")
CAND_DIR = os.path.join(OUT_DIR, "candidates")

DATASTOCK = "a2_sphera"
LANG      = "en"
POOL_CAP  = 100     # max candidates per material (records are compact)
LEX_TOPN  = 45      # cross-family lexical recall booster size
TECH_TOPN = 25      # how many leading candidates get a tech_description (verifier use)
TECH_CHARS = 480    # truncate tech descriptions to keep files small

# --------------------------------------------------------------------------
# Partition rules: matchable construction material vs skip.
# --------------------------------------------------------------------------
CONSTRUCTION_CATS = set([
    u"Walls", u"Floors", u"Roofs", u"Ceilings", u"Columns", u"Structural Columns",
    u"Stairs", u"Runs", u"Ramps", u"Landings", u"Handrails", u"Top Rails",
    u"Doors", u"Windows", u"Curtain Panels", u"Curtain Wall Mullions",
    u"Wall Sweeps", u"Slab Edges", u"Site", u"Hardscape", u"Parking", u"Foundation",
])

# Hard skip if the material NAME contains any of these (case-insensitive),
# regardless of host category - clear non-products / assemblies / landscape.
NAME_SKIP = [
    u"poche", u"default", u"cabinet", u"counter top", u"mirror", u"appliance",
    u"elevator", u"umbrella", u"sculpture", u"planting", u"living wall",
    u"tree ", u"grass", u"soil", u"solar", u"leather", u"textile", u"linen",
    u"gasket", u"dispenser", u"supports", u"sheet metal",
]
# Hard skip by Revit material class.
CLASS_SKIP = set([u"Paint", u"Gas", u"System", u"Fabric"])

# Materials deliberately promoted from skip -> matched. Their bulk
# substance is a genuine construction material (steel, stainless, glass,
# sanitary ceramic), but the BIM model files them under a fixture/finish host
# - a common real-world data-quality issue. Documented in the v0.4 scope_notes.
ALLOW_PROMOTE = set([
    # Sanitary ceramics (plumbing fixtures, real vitreous ceramic)
    468384, 469172, 870414,
    # Fixture steel & stainless (bulk steel/stainless, fixture finish)
    35341, 450622, 778536, 1420526, 1420527, 1106372, 1106373, 870415, 1660388,
    # Lighting / decorative glass (bulk glass, fixture lens)
    465404, 466340, 1419906,
])
PROMOTE_NOTE = (u"Promoted from skip to matched: the BIM model files this material "
                u"under a fixture/finish host (lighting, plumbing, food-service or "
                u"specialty equipment), but its bulk substance is a genuine "
                u"construction material with an ÖKOBAUDAT EPD. Inconsistent BIM "
                u"naming/categorisation is a common real-world data-quality issue; "
                u"included to raise ground-truth N. See scope_notes.")


def _norm_ws(s):
    return re.sub(r"\s+", u" ", (s or u"")).strip()


def classify_skip(mat):
    """Return (is_skip, reason_key) for a BIM material dict."""
    name = (mat.get("name") or u"").strip()
    low  = name.lower()
    cls  = (mat.get("class") or u"").strip()
    cats = set(mat.get("host_elements", {}).get("categories", []) or [])

    if low == u"air" or cls == u"Gas":
        return True, "air"
    if cls in (u"System",) or low in (u"poche", u"default", u"default wall",
                                      u"default light source"):
        return True, "system"
    if cls == u"Paint":
        return True, "paint"
    for tok in NAME_SKIP:
        if tok in low:
            # landscape/site living vs furniture/appliance vs system buckets
            if tok in (u"tree ", u"grass", u"soil", u"planting", u"sculpture",
                       u"living wall", u"umbrella", u"supports"):
                return True, "landscape"
            if tok in (u"solar",):
                return True, "equipment"
            return True, "furniture"
    if cls == u"Fabric":
        return True, "furniture"
    # No construction host at all -> furniture / fixture / equipment finish.
    if cats and not (cats & CONSTRUCTION_CATS):
        return True, "fixture"
    return False, None


SKIP_REASON = {
    "air":       (u"Air gap / cavity has no associated material EPD in ÖKOBAUDAT. "
                  u"LCA practice treats unfilled cavities as zero embodied impact "
                  u"(no product is consumed), so this layer is excluded from "
                  u"BIM→EPD mapping rather than matched."),
    "system":    (u"Revit system / placeholder material (no physical construction "
                  u"product behind it). Excluded from LCA mapping."),
    "paint":     (u"Pure paint / colour finish — a coating appearance only, with no "
                  u"bulk construction-product layer to declare. Out of the "
                  u"construction-material scope of this ground truth."),
    "landscape": (u"Living / landscape or site element (vegetation, soil, site "
                  u"furniture). No manufactured construction-product EPD applies; "
                  u"out of scope."),
    "equipment": (u"Building-services / equipment item (e.g. PV module). Out of the "
                  u"construction-only scope chosen for this ground truth."),
    "furniture": (u"Furniture / casework / appliance / fixture component, not a "
                  u"building-fabric construction material. Out of the "
                  u"construction-only scope chosen for this ground truth."),
    "fixture":   (u"Used only on furniture / fixtures / equipment (no construction "
                  u"host element). The bulk substance may be a real material, but as "
                  u"a fixture finish it is out of the construction-only scope."),
}

# --------------------------------------------------------------------------
# Classification-family targeting: BIM material -> ÖKOBAUDAT German path prefixes
# whose datasets are pulled into the pool wholesale (high recall within family).
# --------------------------------------------------------------------------
MIN = u"Mineralische Baustoffe"


def family_targets(mat):
    """Return (family_key, [german_path_prefixes]) for a matchable material."""
    name = (mat.get("name") or u"").lower()
    cls  = (mat.get("class") or u"").lower()
    desc = (mat.get("description") or u"").lower()
    blob = u" ".join([name, cls, desc])

    def has(*ks):
        return any(k in blob for k in ks)

    # Order matters: most specific first.
    if has(u"gypsum", u"shaft liner", u"drywall", u"plasterboard", u"gwb"):
        return "gypsum", [MIN + u" / Steine und Elemente / Gipsplatten",
                          MIN + u" / Bindemittel / Gips",
                          MIN + u" / Steine und Elemente / Faserzement"]
    if has(u"densdeck", u"cover board", u"roof cover", u"roof board"):
        # e.g. DensDeck = glass-mat gypsum / fibre-cement roof cover board.
        return "mineral_board", [MIN + u" / Steine und Elemente / Gipsplatten",
                                 MIN + u" / Steine und Elemente / Faserzement",
                                 MIN + u" / Steine und Elemente / Brandschutzplatten",
                                 MIN + u" / Bindemittel / Gips"]
    if has(u"porcelain", u"sanitary", u"vitreous", u"washbasin", u"lavatory",
           u"toilet") and not has(u"tile", u"floor", u"pattern"):
        return "sanitary", [u"Gebäudetechnik / Sanitär",
                            MIN + u" / Steine und Elemente / Steinzeug",
                            MIN + u" / Steine und Elemente / Fliesen und Platten"]
    if has(u"asphalt"):
        return "asphalt", [MIN + u" / Asphalt"]
    if has(u"terrazzo"):
        return "terrazzo", [MIN + u" / Steine und Elemente",
                            MIN + u" / Mörtel und Beton"]
    if has(u"pea stone", u"gravel", u"aggregate"):
        return "aggregate", [MIN + u" / Zuschläge",
                             MIN + u" / Steine und Elemente / Naturwerkstein"]
    if has(u"terrazzo", u"stone tile", u"porcelain tile", u"ceramic", u"stone tiles",
           u"tile"):
        return "tile_stone", [MIN + u" / Steine und Elemente / Fliesen und Platten",
                              MIN + u" / Steine und Elemente / Steinzeug",
                              MIN + u" / Steine und Elemente / Naturwerkstein"]
    if has(u"concrete", u"cmu", u"masonry", u"brick", u"cast-in-place", u"precast",
           u"lightweight"):
        return "mineral_struct", [MIN + u" / Mörtel und Beton",
                                  MIN + u" / Steine und Elemente"]
    if has(u"epdm", u"tpo", u"membrane", u"vapor", u"vapour", u"roofing"):
        return "membrane", [u"Kunststoffe / Dachbahnen",
                            u"Kunststoffe / Folien und Vliese",
                            u"Kunststoffe / Dichtmassen",
                            u"Dämmstoffe / Polyethylen"]
    if has(u"eifs", u"insulation", u"rigid insulation"):
        return "insulation", [u"Dämmstoffe"]
    if has(u"glass", u"glazing", u"glazed"):
        return "glass", [u"Komponenten von Fenstern und Vorhangfassaden",
                         MIN + u" / Steine und Elemente / Glasbausteine"]
    if has(u"aluminum", u"aluminium", u"anodized"):
        return "aluminium", [u"Metalle / Aluminium",
                             u"Komponenten von Fenstern und Vorhangfassaden"]
    if has(u"bronze", u"brass", u"copper"):
        return "copper", [u"Metalle / Kupfer", u"Metalle / Stahl und Eisen"]
    if has(u"steel", u"iron", u"metal deck", u"metal stud", u"metal furring",
           u"metal panel", u"metal parapet", u"stud", u"rebar", u"stainless",
           u"sheet metal") or cls == u"metal":
        return "steel", [u"Metalle / Stahl und Eisen", u"Metalle / Edelstahl",
                         u"Metalle / Aluminium"]
    if has(u"osb", u"plywood", u"oriented strand", u"particle", u"mdf", u"sheathing") and has(u"wood", u"osb", u"plywood", u"strand", u"sheathing"):
        return "wood_panel", [u"Holz / Holzwerkstoffe", u"Holz"]
    if cls == u"wood" or has(u"oak", u"birch", u"pine", u"teak", u"timber", u"wood",
                             u"decking", u"clad", u"plywood", u"osb"):
        return "wood", [u"Holz",
                        u"Komponenten von Fenstern und Vorhangfassaden / Türen und Tore",
                        u"Komponenten von Fenstern und Vorhangfassaden / Rahmen"]
    # Fallback by Revit class -> L1 root.
    root = {
        u"concrete": MIN, u"masonry": MIN, u"ceramic": MIN, u"generic": MIN,
        u"metal": u"Metalle", u"plastic": u"Kunststoffe",
    }.get(cls, MIN)
    return "fallback", [root]


# --------------------------------------------------------------------------
# Tokenisation for the lexical recall booster.
# --------------------------------------------------------------------------
_STOP = set([u"and", u"the", u"for", u"with", u"von", u"und", u"mit", u"der",
             u"die", u"das", u"aus", u"per", u"low", u"high", u"grey", u"gray",
             u"white", u"black", u"brown", u"ivory", u"light", u"dark"])


def toks(text):
    return set(t for t in re.findall(r"[a-z0-9]+", (text or u"").lower())
               if len(t) >= 2 and t not in _STOP)


def main():
    with io.open(CACHE_DS, "r", encoding="utf-8") as f:
        ds_rows = json.load(f)["results"]
    with io.open(CACHE_IND, "r", encoding="utf-8") as f:
        ind = json.load(f).get("datasets", {})
    with io.open(BIM_CTX, "r", encoding="utf-8") as f:
        bim = json.load(f)

    # Precompute per-dataset search material once.
    cand_index = []
    for r in ds_rows:
        uu = r.get("uuid") or u""
        if not uu:
            continue
        cls_de = _norm_ws(r.get("classification"))
        cls_en = translate_path(cls_de, u"en")
        name   = (r.get("name") or u"").strip()
        attrs  = (ind.get(uu) or {}).get("attributes", {}) or {}
        rec = {
            "uuid": uu,
            "name": name,
            "classification": cls_de,
            "classification_en": cls_en,
            "material_category": attrs.get("material_category", u""),
            "concrete_strength": attrs.get("concrete_strength", u""),
            "gwp_total": (ind.get(uu) or {}).get("GWP_total"),
            "modules_available": (ind.get(uu) or {}).get("modules_available", []),
            "_tokens": toks(name + u" " + cls_en + u" " + cls_de),
            "_clsnorm": cls_de.lower(),
        }
        cand_index.append(rec)

    if not os.path.isdir(CAND_DIR):
        os.makedirs(CAND_DIR)
    else:
        for fn in os.listdir(CAND_DIR):          # wipe stale per-material files
            if fn.endswith(".json"):
                os.remove(os.path.join(CAND_DIR, fn))

    matchable, skips = [], []
    fam_counter = {}

    n_promoted = 0
    for mat in bim:
        is_skip, reason = classify_skip(mat)
        promoted = False
        if is_skip and mat.get("revit_id") in ALLOW_PROMOTE:
            is_skip, reason, promoted = False, None, True
            n_promoted += 1
        he = mat.get("host_elements", {}) or {}
        base = {
            "revit_id": mat.get("revit_id"),
            "name": mat.get("name"),
            "description": mat.get("description", u"") or u"",
            "class": mat.get("class", u"") or u"",
            "host_categories": sorted(he.get("categories", []) or []),
            "host_types": sorted(he.get("types", []) or []),
            "physical_asset": mat.get("physical_asset"),
            "thermal_asset": mat.get("thermal_asset"),
            "promoted": promoted,
        }
        if promoted:
            base["promote_note"] = PROMOTE_NOTE
        if is_skip:
            entry = dict(base)
            entry.update({
                "label": "skip",
                "skip_reason": SKIP_REASON[reason],
                "expected_classification_path": None,
                "correct_uuid": None,
                "acceptable_uuids": [],
                "top_10": [],
            })
            skips.append(entry)
            continue

        fam_key, prefixes = family_targets(mat)
        fam_counter[fam_key] = fam_counter.get(fam_key, 0) + 1
        pref_norm = [_norm_ws(p).lower() for p in prefixes]

        mtok = toks(u" ".join([base["name"], base["description"], base["class"]]))

        scored = []
        for rec in cand_index:
            in_family = any(rec["_clsnorm"].startswith(p) for p in pref_norm)
            inter = len(mtok & rec["_tokens"])
            union = len(mtok | rec["_tokens"]) or 1
            jac = inter / float(union)
            # family members get a recall guarantee via a large base bonus
            score = (1000 if in_family else 0) + inter * 10 + jac
            if in_family or inter > 0:
                scored.append((score, jac, inter, in_family, rec))

        scored.sort(key=lambda x: x[0], reverse=True)
        chosen = scored[:POOL_CAP]
        # Ensure at least the lexical top-N are present even if family is huge.
        if len(scored) > POOL_CAP:
            lex_only = sorted(scored, key=lambda x: (x[2], x[1]), reverse=True)[:LEX_TOPN]
            have = set(id(c[4]) for c in chosen)
            for c in lex_only:
                if id(c[4]) not in have and len(chosen) < POOL_CAP + LEX_TOPN:
                    chosen.append(c)

        candidates, tech = [], {}
        for i, (_score, _jac, _inter, _fam, rec) in enumerate(chosen):
            gwp = rec["gwp_total"]
            candidates.append({
                "uuid": rec["uuid"],
                "name": rec["name"],
                "classification": rec["classification"],
                "classification_en": rec["classification_en"],
                "material_category": rec["material_category"],
                "concrete_strength": rec["concrete_strength"],
                "gwp_total": (round(gwp, 2) if isinstance(gwp, (int, float)) else None),
                "modules_available": rec["modules_available"],
            })
            if i < TECH_TOPN:
                td = (ind.get(rec["uuid"]) or {}).get("tech_description") or u""
                if td:
                    tech[rec["uuid"]] = td[:TECH_CHARS]

        rid = base["revit_id"]
        cand_path = os.path.join(CAND_DIR, "%s.json" % rid)
        with io.open(cand_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"material": base, "family": fam_key,
                                "candidates": candidates, "tech": tech},
                               indent=2, ensure_ascii=False))
        matchable.append({
            "revit_id": rid,
            "name": base["name"],
            "class": base["class"],
            "family": fam_key,
            "promoted": promoted,
            "candidate_file": os.path.relpath(cand_path, PARENT).replace("\\", "/"),
            "candidate_count": len(candidates),
        })

    with io.open(os.path.join(OUT_DIR, "skips.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(skips, indent=2, ensure_ascii=False))
    manifest = {
        "datastock": DATASTOCK, "lang": LANG, "dataset_count": len(ds_rows),
        "match_count": len(matchable), "skip_count": len(skips),
        "matchable": matchable,
    }
    with io.open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, indent=2, ensure_ascii=False))

    manifest["promoted_count"] = n_promoted

    # ---- report ----
    print("BIM materials: %d   ->   matchable: %d (incl. %d promoted)   skip: %d"
          % (len(bim), len(matchable), n_promoted, len(skips)))
    print("\nMatchable by family:")
    for k in sorted(fam_counter):
        print("  %-16s %d" % (k, fam_counter[k]))
    print("\nMATCHABLE (%d):" % len(matchable))
    for m in matchable:
        print("  [%-13s] %-34s (%d cand)%s" % (m["family"], (m["name"] or "")[:34],
                                               m["candidate_count"],
                                               "  <promoted>" if m["promoted"] else ""))
    print("\nSKIP (%d):" % len(skips))
    for s in skips:
        print("  %-34s :: %s" % ((s["name"] or "")[:34], s["skip_reason"][:46]))
    print("\nWrote: %s" % CAND_DIR)
    print("Wrote: %s" % os.path.join(OUT_DIR, "skips.json"))
    print("Wrote: %s" % os.path.join(OUT_DIR, "manifest.json"))


if __name__ == "__main__":
    main()
