# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Material attribute extraction from dataset names and classifications.

Pure-Python - no project imports, no .NET dependencies.
Works in both IronPython 2.7 and CPython 3.x.

Only deterministic extractors are included:
- Material category  (from structured classification field - cannot misfire)
- Concrete strength  (C-class regex on name - structurally deterministic)
- Element function   (regex on name - populated in the indicator cache but
                      NOT exposed as a UI filter)
- Uses secondary material (SM indicator value > 0 - structured LCIA field)

Removed (all heuristic / non-deterministic):
- Recycled keyword detection (regex on free-text names)
- Declared unit inference   (text pattern on name + tech description)
- Concrete weight class     (substring "beton" - too broad)
- Wood / Steel / Insulation sub-type patterns (1–4 % coverage)
- Thermal conductivity extraction (0 % reliable coverage)
"""
from __future__ import print_function
import re as _re


# ────────────────────────────────────────────────────────────────
# Material category mapping (first classification segment -> category)
# ────────────────────────────────────────────────────────────────
_CLASSIFICATION_CATEGORIES = {
    # German classification segments from OKOBAUDAT
    # Values are Revit MaterialClass strings (used as fallback when the
    # curated UUID map lacks an entry \u2014 e.g. for newly-published datasets).
    "Mineralische Baustoffe":       "Concrete",
    "Holz":                         "Wood",
    "Holzwerkstoffe":               "Wood",
    "Metalle":                      "Metal",
    "Kunststoffe":                  "Plastic",
    "Beschichtungen":               "Paint/Coating",
    "Dichtungsbahnen":              "Plastic",
    "Daemmstoffe":                  "Insulation",
    u"D\u00e4mmstoffe":             "Insulation",
    "Bauchemie":                    "Paint/Coating",
    "Fenster und Fassaden":         "System",
    u"Komponenten von Fenstern und Vorhangfassaden": "System",
    "Gebaeudetechnik":              "Building Services",
    u"Geb\u00e4udetechnik":         "Building Services",
    "Komposite":                    "Composite",
    "Sonstige":                     "Generic",
    "End of Life":                  "Miscellaneous",
    # English equivalents
    "Mineral construction materials": "Concrete",
    "Wood":                          "Wood",
    "Metals":                        "Metal",
    "Plastics":                      "Plastic",
    "Coatings":                      "Paint/Coating",
    "Insulation materials":          "Insulation",
    "Building services":             "Building Services",
    "Composites":                    "Composite",
}

# Legacy lowercase tokens written by the previous prefetcher version.
# Mapped to Revit-aligned strings so old caches still work until the
# user runs `python indicator_prefetcher.py --all-de --update-phases`.
_LEGACY_CATEGORY_UPGRADE = {
    "mineral":           "Concrete",
    "wood":              "Wood",
    "metal":             "Metal",
    "plastic":           "Plastic",
    "coating":           "Paint/Coating",
    "sealing":           "Plastic",
    "insulation":        "Insulation",
    "chemistry":         "Paint/Coating",
    "windows":           "System",
    "building_services": "Building Services",
    "composite":         "Composite",
}


def upgrade_legacy_category(value):
    """Convert a legacy lowercase category token to the new Revit-aligned string.

    If the value is already a Revit-aligned string (or empty), return as-is.
    """
    if not value:
        return ""
    return _LEGACY_CATEGORY_UPGRADE.get(value, value)

# ────────────────────────────────────────────────────────────────
# Concrete strength class boundaries (first C-number in MPa)
# ────────────────────────────────────────────────────────────────
_CONCRETE_STRENGTH_RE = _re.compile(r'\bC\s*(\d+)\s*/\s*(\d+)\b')

_CONCRETE_LOW_MAX    = 25   # C8/10 through C20/25
_CONCRETE_HIGH_MIN   = 45   # C45/55 and above


# ────────────────────────────────────────────────────────────────
# Element function classification (structural role of the dataset)
# Populated in the indicator cache; NOT exposed as a UI filter.
# ────────────────────────────────────────────────────────────────
_ELEMENT_FUNCTION_PATTERNS = [
    # (compiled_regex, function_label)
    # Order matters - first match wins, so put specific patterns before broad ones.

    # Circulation elements
    (_re.compile(
        r'\b(?:Treppe|Treppen|Stufe|Stufen|Podest'
        r'|stairs|stair|ramp|Rampe|steps|step)\b', _re.IGNORECASE),
     "circulation"),

    # Horizontal structural elements (floor / ceiling / slab / deck)
    (_re.compile(
        r'\b(?:Decke|Decken|Bodenplatte|Geschossdecke|Massivdecke'
        r'|Hohlk.rperdecke|Filigrandecke|Spannbetondecke'
        r'|slab|ceiling|floor\s*slab|floor\s*element|deck)\b', _re.IGNORECASE),
     "horizontal-structural"),

    # Vertical structural elements (wall / column / pillar)
    (_re.compile(
        r'\b(?:Wand|W.nde|Wandelement|Mauer|Mauerwerk|Pfeiler'
        r'|St.tze|S.ule|Fertigteilwand'
        r'|wall|column|pillar|masonry)\b', _re.IGNORECASE),
     "vertical-structural"),

    # Foundation elements
    (_re.compile(
        r'\b(?:Fundament|Gr.ndung|Bohrpfahl|Pfahlgr.ndung|Sohle'
        r'|footing|foundation|pile|piling)\b', _re.IGNORECASE),
     "foundation"),

    # Roof elements
    (_re.compile(
        r'\b(?:Dach|Dachelemente?|Dachziegel|Dachstein|Dachplatte'
        r'|roof|roofing)\b', _re.IGNORECASE),
     "roof"),

    # Opening elements (windows, doors)
    (_re.compile(
        r'\b(?:Fenster|T.r|T.ren|Fensterprofil|Fensterglas'
        r'|window|door|glazing)\b', _re.IGNORECASE),
     "opening"),

    # Finishing / surface layers (plaster, screed, paint, coating)
    (_re.compile(
        r'\b(?:Putz|Estrich|Anstrich|Farbe|Beschichtung|Spachtel'
        r'|Fliese|Fliesen|Tapete|Bodenbelag|Parkett|Laminat|Linoleum'
        r'|plaster|screed|paint|coating|render|tile|tiles'
        r'|flooring|laminate|parquet|linoleum|wallpaper)\b', _re.IGNORECASE),
     "finishing"),
]


# ────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────

def extract_element_function(name, classification=""):
    """Classify the structural function of a dataset from its name.

    Returns one of: "horizontal-structural", "vertical-structural",
    "circulation", "foundation", "roof", "opening", "finishing",
    or "" (empty string) if the dataset appears to be a raw material
    or cannot be classified.

    Stored in the indicator cache for downstream use. Not a UI filter.
    """
    combined = (name or "") + " " + (classification or "")
    for pattern, label in _ELEMENT_FUNCTION_PATTERNS:
        if pattern.search(combined):
            return label
    return ""


def extract_query_element_function(query_text, translated_queries=None):
    """Infer the structural function the user is searching for.

    Runs extract_element_function over the original query and every
    translated variant (German multi-query expansion). Returns the
    first non-empty label, or "" if no structural function is evident.

    This matters because English query words like "floor" don't match
    the German-first regex, but the translated "Decke" does.
    """
    fn = extract_element_function(query_text or "")
    if fn:
        return fn
    if translated_queries:
        for q in translated_queries:
            if not q:
                continue
            fn = extract_element_function(q)
            if fn:
                return fn
    return ""


# Compatibility matrix: query_fn -> {dataset_fn: weight}
# weight == 1.0 means fully compatible (no penalty)
# weight == 0.5 means soft mismatch (half penalty)
# weight == 0.0 means hard mismatch (full penalty)
# Missing entries default to 0.0 (hard mismatch).
# Either side being "" short-circuits to 1.0 in function_compatibility.
_FUNCTION_COMPATIBILITY = {
    "horizontal-structural": {
        "horizontal-structural": 1.0,
        "foundation":            0.5,
        "circulation":           0.5,
    },
    "vertical-structural": {
        "vertical-structural": 1.0,
        "foundation":          0.5,
    },
    "foundation": {
        "foundation":            1.0,
        "horizontal-structural": 1.0,
        "vertical-structural":   0.5,
    },
    "roof": {
        "roof":                  1.0,
        "horizontal-structural": 0.5,
    },
    "circulation": {
        "circulation":           1.0,
        "horizontal-structural": 0.5,
    },
    "opening": {
        "opening":    1.0,
        "finishing":  0.5,
    },
    "finishing": {
        "finishing": 1.0,
    },
}


def function_compatibility(query_fn, dataset_fn):
    """Return compatibility weight between query and dataset element functions.

    Returns:
        1.0 -> compatible (no penalty applied)
        0.5 -> soft mismatch (half penalty)
        0.0 -> hard mismatch (full penalty)

    Either side being "" (raw material or undetected function) yields 1.0,
    so raw inputs like "Beton C30/37" are never penalised.
    """
    if not query_fn or not dataset_fn:
        return 1.0
    row = _FUNCTION_COMPATIBILITY.get(query_fn)
    if row is None:
        return 1.0
    return row.get(dataset_fn, 0.0)


def extract_material_category(classification):
    """Determine the Revit-aligned material category for a dataset.

    Maps the FIRST segment of the German classification path to a coarse
    Revit-style category label (one of the keys in `_CLASSIFICATION_CATEGORIES`,
    e.g. "Concrete", "Wood", "Insulation", "Building Services").

    The previous version also accepted an optional `curated_map` overlay
    loaded from `material_categories_curated.json`; that overlay was retired
    in the Phase 0 cleanup along with the curated map itself. The Revit
    `MaterialClass` write-back is now driven by `format_revit_material_class()`
    (full classification path joined with underscores), and the chip filter
    is driven by the hierarchical Materials Classification chips.

    `MaterialCategory` is therefore a passive cache field today: it ends up
    inside the `to_dict()` JSON written to Revit materials' `UUID_Data`
    parameter for backward compatibility, but no UI / filter / write-back
    consumes it.

    Args:
        classification: Full German classification path, e.g.
            "Mineralische Baustoffe / Steine und Elemente / Betonfertigteile"

    Returns:
        Revit MaterialClass string (e.g. "Concrete", "Wood", "Building Services")
        or "" when no root matches.
    """
    if not classification:
        return ""
    first_seg = classification.split("/")[0].strip()
    cat = _CLASSIFICATION_CATEGORIES.get(first_seg, "")
    if cat:
        return cat
    cl = classification.lower()
    for key, val in _CLASSIFICATION_CATEGORIES.items():
        if key.lower() in cl:
            return val
    return ""


def format_revit_material_class(classification_de, translations=None):
    """Convert a German OEKOBAUDAT classification path into an English
    underscore-joined label suitable for Revit's `MaterialClass` property.

    Example:
        "Mineralische Baustoffe / Moertel und Beton / Beton"
        ->  "Mineral Construction_Mortar & Concrete_Concrete"

    Each path segment is looked up in `translations` (typically
    `_DE_TO_EN_LABELS` from window.py). Segments missing from the dict
    fall through to their German form (so the label is never empty just
    because one segment lacks a translation).

    Args:
        classification_de: German classification path (with " / " separators).
        translations: dict {de_string: en_string}. May be None or empty,
            in which case the German segments are used verbatim.

    Returns:
        Underscore-joined English (or German-fallback) path. Returns
        "Generic" when the input is empty/falsy so that Revit's
        MaterialClass always gets a non-empty value.
    """
    if not classification_de:
        return "Generic"
    # Normalize whitespace (collapse OEKOBAUDAT's known double-space typos)
    path = u" ".join(classification_de.split())
    parts = [p.strip() for p in path.split(u"/") if p.strip()]
    if not parts:
        return "Generic"
    table = translations or {}
    en_parts = [table.get(p, p) for p in parts]
    return u"_".join(en_parts)


def extract_concrete_strength(name):
    """Extract concrete strength class from a dataset name.

    Pattern: C<fck>/<fck,cube> - e.g. "Beton C30/37"

    This regex is structurally deterministic: if the pattern matches, the
    dataset demonstrably carries a C-class concrete designation.

    Returns:
        "Low" (fck ≤ 25), "Medium" (26–44), "High" (≥ 45), or "" if absent.
    """
    m = _CONCRETE_STRENGTH_RE.search(name or "")
    if not m:
        return ""
    c_num = int(m.group(1))
    if c_num <= _CONCRETE_LOW_MAX:
        return "Low"
    elif c_num >= _CONCRETE_HIGH_MIN:
        return "High"
    else:
        return "Medium"


def uses_secondary_material(sm_value):
    """Return True when the SM (Secondary Materials) LCIA indicator > 0.

    SM is a structured LCIA field - the result is 100 % deterministic.
    No regex, no keyword matching.

    Args:
        sm_value: Float or None from the SM indicator for A1-A3.

    Returns:
        True if sm_value is a positive number, False otherwise.
    """
    return sm_value is not None and sm_value > 0


def extract_all(name, classification="", sm_value=None, tech_description=""):
    """Extract all material attributes from name, classification and indicators.

    Returns:
        dict with keys: material_category, uses_secondary_material,
        concrete_strength, element_function.

    Args:
        name: Dataset name.
        classification: Full classification path.
        sm_value: SM (Secondary Materials) LCIA indicator value (A1-A3).
        tech_description: Accepted for API compatibility but not used.
    """
    cat = extract_material_category(classification)
    return {
        "material_category":       cat,
        "uses_secondary_material": uses_secondary_material(sm_value),
        "concrete_strength":       extract_concrete_strength(name) if cat in ("Concrete", "Masonry") else "",
        "element_function":        extract_element_function(name, classification),
    }
