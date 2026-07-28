# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""BIM-context query enrichment - single source of truth.

Builds the *query* string that the semantic / hybrid / reranker pipeline
embeds, from a chosen set of BIM metadata fields. Today the live tool and
the benchmark both query with the bare Revit material name; this module is
the one place that assembles a richer query (name + class + description +
physical/thermal properties + …) so the offline ablation
(`tools/ablate_query_context.py`) and - later - the live Connector and the
Validation pushbutton all construct the query identically (no drift), exactly
as `search_helpers.build_searchable` / `build_semantic_haystack` are the one
place that builds the *document* haystack.

Pure Python - no project imports, no numpy/.NET. Imports cleanly under both
IronPython 2.7 (inside Revit) and CPython 3 (offline tools).

Input shape tolerance - `build_enriched_query` accepts an entry/material dict
in EITHER of the two shapes the project produces:

  * Ground-truth entry  (sample_project_*_v0.6__opus-4.8.json):
        {name, description, class, host_categories[], host_types[],
         physical_asset:{structural_class, density_kg_m3,
                         concrete_compression_mpa, lightweight},
         thermal_asset:{thermal_conductivity_w_mk, density_kg_m3}}
  * ExtractContext output (materials_context.json / LCA_Context param):
        {name, description, class, host_elements:{categories[], types[]},
         physical_asset:{...}, thermal_asset:{...}}

Missing fields are elided gracefully, so the same call works for a sparse
"Default" material and a fully-specified concrete.
"""
from __future__ import print_function

# Separator mirrors build_semantic_haystack's " | " so the query text is
# formatted the same way the document side was at prefetch time.
SEP = u" | "

# ── Standard EN 206 concrete strength classes (cylinder fck → "Cxx/yy") ──
# Used to turn a raw compressive-strength number (MPa) into the class token
# that actually appears in ÖKOBAUDAT EPD names ("Beton C25/30").
_CONCRETE_CLASSES = [
    (8, u"C8/10"), (12, u"C12/15"), (16, u"C16/20"), (20, u"C20/25"),
    (25, u"C25/30"), (30, u"C30/37"), (35, u"C35/45"), (40, u"C40/50"),
    (45, u"C45/55"), (50, u"C50/60"), (55, u"C55/67"), (60, u"C60/75"),
]

# Structural-class tokens that carry no disambiguating signal (Revit's
# generic placeholders) - emitted as "" so they never pollute the query.
_UNINFORMATIVE_STRUCT = set([u"basic", u"generic", u""])


def mpa_to_strength_class(mpa):
    """Map a cylinder compressive strength (MPa) to the nearest standard
    EN 206 class token at or below it (C20/25 for 24.1 MPa, matching the
    GT's own '24.1 MPa -> C20/25' reasoning). Returns "" on bad input."""
    try:
        v = float(mpa)
    except (TypeError, ValueError):
        return u""
    if v <= 0:
        return u""
    chosen = _CONCRETE_CLASSES[0][1]
    for fck, label in _CONCRETE_CLASSES:
        # +0.5 tolerance so 24.6 rounds up to C25/30 but 24.1 stays C20/25.
        if v + 0.5 >= fck:
            chosen = label
        else:
            break
    return chosen


def _u(x):
    """Coerce to unicode, IronPython-2.7 / CPython-3 safe."""
    if x is None:
        return u""
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", "replace")
    except Exception:
        pass
    try:
        return u"{0}".format(x)
    except Exception:
        return u""


def _num(x, decimals=0):
    """Format a number compactly (drop trailing zeros)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return u""
    if decimals <= 0 and abs(v - round(v)) < 1e-9:
        return u"{0:d}".format(int(round(v)))
    s = (u"{0:.%df}" % decimals).format(v)
    if u"." in s:
        s = s.rstrip(u"0").rstrip(u".")
    return s


def _phys(entry):
    p = entry.get("physical_asset")
    return p if isinstance(p, dict) else {}


def _therm(entry):
    t = entry.get("thermal_asset")
    return t if isinstance(t, dict) else {}


def _host(entry, which):
    """Return the host categories/types list under either input shape."""
    flat = entry.get("host_" + which)        # GT shape: host_categories/types
    if isinstance(flat, list):
        return flat
    he = entry.get("host_elements")          # ExtractContext shape
    if isinstance(he, dict) and isinstance(he.get(which), list):
        return he.get(which)
    return []


def _density(entry):
    p, t = _phys(entry), _therm(entry)
    for src in (p.get("density_kg_m3"), t.get("density_kg_m3")):
        if src is not None:
            return src
    return None


# ── Field extractors: each returns a formatted query *piece* (or "") ─────────
# A field that is absent/empty for a given material yields "" and is dropped,
# so the field-coverage analysis in the ablation = count of non-"" pieces.

def _f_name(e, lang):
    return _u(e.get("name")).strip()


def _f_class(e, lang):
    return _u(e.get("class")).strip()


def _f_description(e, lang):
    desc = _u(e.get("description")).strip()
    name = _u(e.get("name")).strip()
    # Drop description when it merely repeats the name (very common in the GT).
    if not desc or desc.lower() == name.lower():
        return u""
    return desc


def _f_structural_class(e, lang):
    sc = _u(_phys(e).get("structural_class")).strip()
    if sc.lower() in _UNINFORMATIVE_STRUCT:
        return u""
    return sc


def _f_density(e, lang):
    d = _density(e)
    s = _num(d, 0)
    return (u"density " + s + u" kg/m3") if s else u""


def _f_thermal_conductivity(e, lang):
    lam = _therm(e).get("thermal_conductivity_w_mk")
    s = _num(lam, 3)
    return (u"thermal conductivity " + s + u" W/(m.K)") if s else u""


def _f_concrete_grade(e, lang):
    return mpa_to_strength_class(_phys(e).get("concrete_compression_mpa"))


def _f_host_categories(e, lang):
    cats = [_u(c).strip() for c in _host(e, "categories") if _u(c).strip()]
    return (u"used in " + u", ".join(cats)) if cats else u""


def _f_host_types(e, lang):
    types = [_u(t).strip() for t in _host(e, "types") if _u(t).strip()]
    return u", ".join(types) if types else u""


# Registry: ordered so build_enriched_query emits fields in a stable order.
FIELD_EXTRACTORS = [
    ("name",                 _f_name),
    ("class",                _f_class),
    ("description",          _f_description),
    ("structural_class",     _f_structural_class),
    ("density",              _f_density),
    ("thermal_conductivity", _f_thermal_conductivity),
    ("concrete_grade",       _f_concrete_grade),
    ("host_categories",      _f_host_categories),
    ("host_types",           _f_host_types),
]
_EXTRACTOR_BY_KEY = dict(FIELD_EXTRACTORS)
FIELD_KEYS = [k for k, _ in FIELD_EXTRACTORS]


def get_field_piece(entry, field, lang="en"):
    """Return the formatted query piece for one field (or "" if absent).
    Exposed so the ablation can compute per-field coverage."""
    fn = _EXTRACTOR_BY_KEY.get(field)
    if fn is None:
        return u""
    try:
        return fn(entry, lang) or u""
    except Exception:
        return u""


def build_enriched_query(entry, fields=None, lang="en"):
    """Assemble the query string from the selected `fields` (in registry
    order, deduplicated, joined by ' | '). `fields=None` ⇒ name only
    (today's behaviour). Always returns at least the name when present."""
    if fields is None:
        fields = ["name"]
    wanted = set(fields)
    pieces, seen = [], set()
    for key in FIELD_KEYS:                      # stable registry order
        if key not in wanted:
            continue
        piece = get_field_piece(entry, key, lang)
        if piece and piece.lower() not in seen:
            seen.add(piece.lower())
            pieces.append(piece)
    if not pieces:                              # never emit an empty query
        return _u(entry.get("name")).strip()
    return SEP.join(pieces)


# ── Ablation matrix: cumulative ladder + leave-one-out from ALL ──────────────
# The cumulative ladder answers "does adding field X help?"; the leave-one-out
# block isolates each field's *marginal* value/noise against the full set.
ALL_FIELDS = list(FIELD_KEYS)

_CUMULATIVE = [
    ("V0_name",            ["name"]),
    ("V1_+class",          ["name", "class"]),
    ("V2_+description",    ["name", "class", "description"]),
    ("V3_+struct_class",   ["name", "class", "description", "structural_class"]),
    ("V4_+density",        ["name", "class", "description", "structural_class",
                            "density"]),
    ("V5_+lambda",         ["name", "class", "description", "structural_class",
                            "density", "thermal_conductivity"]),
    ("V6_+concrete_grade", ["name", "class", "description", "structural_class",
                            "density", "thermal_conductivity", "concrete_grade"]),
    ("V7_+host_cat",       ["name", "class", "description", "structural_class",
                            "density", "thermal_conductivity", "concrete_grade",
                            "host_categories"]),
    ("V8_ALL_+host_types", list(ALL_FIELDS)),
]

# Leave-one-out: ALL minus each non-name field (name is the irreducible base).
_LOO = [("LOO_-" + f, [x for x in ALL_FIELDS if x != f])
        for f in ALL_FIELDS if f != "name"]

# Public ablation matrix: list of (variant_name, [field_keys]).
VARIANTS = _CUMULATIVE + _LOO


def variant_fields(variant_name):
    """Look up the field list for a named variant (or None)."""
    for name, fields in VARIANTS:
        if name == variant_name:
            return fields
    return None
