# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Pure-Python ILCD JSON parsing utilities.

No .NET imports - works in both IronPython 2.7 and CPython 3.x.
Used by:
  - api_client.py       (IronPython, inside Revit)
  - indicator_prefetcher.py  (CPython 3, offline CLI)

All functions operate on plain Python dicts parsed from ILCD JSON.
"""
from __future__ import print_function
import re as _re
import xml.etree.ElementTree as _ET


# ────────────────────────────────────────────────────────────────
# ILCD XML → dict adapter
# ────────────────────────────────────────────────────────────────
#
# OEKOBAUDAT's `?format=json` per-process endpoint started returning HTTP 500
# (server-side java.lang.NullPointerException) in May 2026. The same data is
# fully accessible via `?format=xml`, which is the canonical ILCD format.
# This adapter converts ILCD XML into the dict shape Soda4LCA's JSON
# serializer used to produce, so parse_full / parse_indicators / parse_name /
# parse_category / parse_technology_description / parse_ref_unit (defined
# below) run unchanged on the new XML-derived data.
#
# Lives in ilcd_parser.py so both api_client.py (IronPython, inside Revit)
# and indicator_prefetcher.py (CPython 3, offline tooling) can import it
# without duplication.

# Element names that should always be wrapped in a list. The legacy JSON
# always represented these as arrays; the parser iterates them as such.
_FORCE_LIST = frozenset([
    # Multilingual text arrays
    "baseName", "shortDescription",
    "generalComment", "useAdviceForDataSet",
    "technologyDescriptionAndIncludedProcesses",
    "technologicalApplicability",
    "timeRepresentativenessDescription",
    "descriptionOfRestrictions",
    "otherReviewDetails",
    # Repeated elements iterated as lists by the parser
    "classification", "class",
    "exchange", "LCIAResult",
    "compliance",
    "referenceToDataSource",
    "referenceToReferenceFlow",
    "review",
    "referenceToNameOfReviewerAndInstitution",
])


def _strip_ns(tag):
    """Drop the `{namespace}` prefix that ElementTree puts on tags/attribs."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _xml_elem_to_dict(elem):
    """Recursive ILCD XML element → dict / list / string converter.

    * Tag names and attribute keys have their XML namespace stripped.
    * Text-only leaves return a plain string.
    * Elements with attributes only (no children/text) return a dict of attribs.
    * Elements with text plus attributes/children store the text under "value".
    * Repeated children with the same tag collapse to a list. Elements in
      `_FORCE_LIST` are always wrapped in a list even when only one occurs.
    * The `<other>` ILCD extension wrapper is rewritten to the legacy JSON
      shape: `{"anies": [{"name": "amount", "module": "A1-A3", "value": "X"},
      {"name": "referenceToUnitGroupDataSet", "value": {...}}, ...]}` - what
      parse_indicators / parse_ref_unit consume.
    """
    tag = _strip_ns(elem.tag)

    if tag == "other":
        anies = []
        for child in elem:
            ctag = _strip_ns(child.tag)
            a = {"name": ctag}
            # Strip namespaces from attribute keys (ILCD attributes like
            # `module` on <amount> live in the EPD-2013 namespace).
            child_attribs = {_strip_ns(k): v for k, v in child.attrib.items()}
            if "module" in child_attribs:
                a["module"] = child_attribs["module"]
            other_attribs = {k: v for k, v in child_attribs.items()
                             if k != "module"}
            child_text = (child.text or "").strip()
            sub_children = list(child)
            if not sub_children and not other_attribs:
                a["value"] = child_text
            else:
                value_dict = dict(other_attribs)
                if child_text:
                    value_dict["value"] = child_text
                grouped = {}
                for sub in sub_children:
                    sub_tag = _strip_ns(sub.tag)
                    sub_val = _xml_elem_to_dict(sub)
                    if sub_tag in grouped:
                        if not isinstance(grouped[sub_tag], list):
                            grouped[sub_tag] = [grouped[sub_tag]]
                        grouped[sub_tag].append(sub_val)
                    elif sub_tag in _FORCE_LIST:
                        grouped[sub_tag] = [sub_val]
                    else:
                        grouped[sub_tag] = sub_val
                value_dict.update(grouped)
                a["value"] = value_dict
            anies.append(a)
        return {"anies": anies}

    # Generic path
    result = {_strip_ns(k): v for k, v in elem.attrib.items()}
    text = (elem.text or "").strip()
    grouped = {}
    for child in elem:
        ctag = _strip_ns(child.tag)
        cval = _xml_elem_to_dict(child)
        if ctag in grouped:
            if not isinstance(grouped[ctag], list):
                grouped[ctag] = [grouped[ctag]]
            grouped[ctag].append(cval)
        elif ctag in _FORCE_LIST:
            grouped[ctag] = [cval]
        else:
            grouped[ctag] = cval

    if text and not result and not grouped:
        return text
    if text:
        result["value"] = text
    result.update(grouped)
    return result


def ilcd_xml_to_dict(xml_str):
    """Convert an ILCD XML payload (bytes or str) into the dict shape that
    parse_full / parse_indicators / parse_name / parse_category /
    parse_technology_description / parse_ref_unit consume."""
    if isinstance(xml_str, bytes):
        root = _ET.fromstring(xml_str)
    else:
        root = _ET.fromstring(xml_str.encode("utf-8"))
    return _xml_elem_to_dict(root)


# ────────────────────────────────────────────────────────────────
# ILCD indicator name resolution
# ────────────────────────────────────────────────────────────────

# Duplicate of constants.LCIA_NAME_MAP so this module has no project imports.
# If the canonical map in constants.py changes, update this copy.
LCIA_NAME_MAP = {
    "GWP-total":        "GWP_total",
    "GWP-fossil":       "GWP_fossil",
    "GWP-biogenic":     "GWP_biogenic",
    "GWP-luluc":        "GWP_luluc",
    "ODP":              "ODP",
    "AP":               "AP",
    "EP-freshwater":    "EP_freshwater",
    "EP-marine":        "EP_marine",
    "EP-terrestrial":   "EP_terrestrial",
    "POCP":             "POCP",
    "ADPE":             "ADP_elem",
    "ADP-minerals&metals": "ADP_elem",
    "ADPF":             "ADP_fossil",
    "ADP-fossil":       "ADP_fossil",
    "WDP":              "WDP",
    "PENRT":            "PENRT",
    "PENRM":            "PENRM",
    "PENRE":            "PENRE",
    "PERT":             "PERT",
    "PERM":             "PERM",
    "PERE":             "PERE",
    "SM":               "SM",
    "SF":               "SF",
    "NRSF":             "NRSF",
    "FW":               "FW",
    "HWD":              "HWD",
    "NHWD":             "NHWD",
    "RWD":              "RWD",
    "CRU":              "CRU",
    "MFR":              "MFR",
    "MER":              "MER",
    "EEE":              "EEE",
    "EET":              "EET",
    # A1 single GWP / EP
    "GWP":              "GWP_total",
    "EP":               "EP_freshwater",
    # English descriptive names
    "Climate change":                               "GWP_total",
    "Climate change-Fossil":                        "GWP_fossil",
    "Climate change-Biogenic":                      "GWP_biogenic",
    "Climate change-Land use and land use change":  "GWP_luluc",
    "Eutrophication, freshwater":                   "EP_freshwater",
    "Eutrophication marine":                        "EP_marine",
    "Eutrophication, terrestrial":                  "EP_terrestrial",
    "Resource use, minerals and metals":            "ADP_elem",
    "Resource use, fossils":                        "ADP_fossil",
    "Water use":                                    "WDP",
}

# All 31 indicator prop names (for iteration)
ALL_INDICATOR_PROPS = [
    "GWP_total", "GWP_fossil", "GWP_biogenic", "GWP_luluc",
    "ODP", "AP", "EP_freshwater", "EP_marine", "EP_terrestrial",
    "POCP", "ADP_elem", "ADP_fossil", "WDP",
    "PENRE", "PENRM", "PENRT", "PERE", "PERM", "PERT",
    "SM", "SF", "NRSF", "FW", "HWD", "NHWD", "RWD",
    "CRU", "MFR", "MER", "EEE", "EET",
]

# Production-phase modules to sum for A1-A3 aggregate
_A1A3_MODULES = {"A1", "A2", "A3", "A1-A3"}


# ────────────────────────────────────────────────────────────────
# Low-level helpers
# ────────────────────────────────────────────────────────────────

def extract_short_name(desc_list, preferred_lang=None):
    """Pull a short-description text from an ILCD multilingual array.

    Tries *preferred_lang* first, then 'en', then the first entry.
    """
    if preferred_lang:
        for d in (desc_list or []):
            if d.get("lang") == preferred_lang:
                return d.get("value", "")
    for d in (desc_list or []):
        if d.get("lang") == "en":
            return d.get("value", "")
    if desc_list:
        return desc_list[0].get("value", "")
    return ""


def extract_indicator_tag(full_name):
    """From 'Global Warming Potential - total (GWP-total)' extract 'GWP-total'."""
    m = _re.search(r'\(([^)]+)\)\s*$', full_name or "")
    if m:
        return m.group(1).strip()
    return full_name.strip() if full_name else ""


def resolve_indicator_prop(desc_lists):
    """Resolve an indicator property name from ILCD description lists.

    Tries DE first (reliably includes abbreviation in parentheses),
    then EN, then first available, then substring fallback.

    Args:
        desc_lists: The ``shortDescription`` array from an ILCD
                    referenceToLCIAMethodDataSet or referenceToFlowDataSet.

    Returns:
        The internal property name (e.g. ``"GWP_total"``) or ``None``.
    """
    for preferred in ("de", "en", None):
        method_desc = extract_short_name(desc_lists, preferred_lang=preferred)
        indicator_tag = extract_indicator_tag(method_desc)
        prop_name = LCIA_NAME_MAP.get(indicator_tag)
        if prop_name:
            return prop_name
        prop_name = LCIA_NAME_MAP.get(method_desc)
        if prop_name:
            return prop_name

    # Last resort: partial substring match
    indicator_tag = extract_indicator_tag(
        extract_short_name(desc_lists, preferred_lang="de")
    )
    for key in LCIA_NAME_MAP:
        if key.lower() in (indicator_tag or "").lower():
            return LCIA_NAME_MAP[key]

    return None


# ────────────────────────────────────────────────────────────────
# High-level ILCD → dict parsing
# ────────────────────────────────────────────────────────────────

def parse_ref_unit(process_json):
    """Extract the reference unit string (e.g. 'kg', 'm3', 'm2') from ILCD JSON."""
    try:
        pi = process_json.get("processInformation", {})
        qr = pi.get("quantitativeReference", {})
        ref_ids = qr.get("referenceToReferenceFlow", [])
        ref_id = ref_ids[0] if ref_ids else 0
        exchanges = process_json.get("exchanges", {}).get("exchange", [])
        for ex in exchanges:
            if ex.get("dataSetInternalID") == ref_id:
                anies = ex.get("other", {}).get("anies", [])
                for a in anies:
                    if a.get("name") == "referenceToUnitGroupDataSet":
                        unit_desc = extract_short_name(
                            a.get("value", {}).get("shortDescription", [])
                        )
                        if unit_desc:
                            return unit_desc
                return "m3"
        return "m3"
    except Exception:
        return "m3"


def parse_category(process_json):
    """Extract the classification / category string from ILCD JSON."""
    try:
        pi = process_json.get("processInformation", {})
        dsi = pi.get("dataSetInformation", {})
        ci = dsi.get("classificationInformation", {})
        classes = ci.get("classification", [])
        for c in classes:
            cats = c.get("class", [])
            if cats:
                return " / ".join([cl.get("value", "") for cl in cats if cl.get("value")])
        return "Other"
    except Exception:
        return "Other"


def parse_name(process_json, lang=None):
    """Extract the dataset name from ILCD JSON."""
    pi = process_json.get("processInformation", {})
    dsi = pi.get("dataSetInformation", {})
    name_list = dsi.get("name", {}).get("baseName", [])
    return extract_short_name(name_list, preferred_lang=lang)


def parse_technology_description(process_json, lang=None):
    """Extract the 'Technology description' text from ILCD JSON."""
    try:
        pi = process_json.get("processInformation", {})
        tech = pi.get("technology", {})
        desc_list = tech.get("technologyDescriptionAndIncludedProcesses", [])
        return extract_short_name(desc_list, preferred_lang=lang)
    except Exception:
        return ""


def parse_indicators(process_json):
    """Parse all indicator values from ILCD JSON.

    Returns:
        dict of ``{module_name: {prop_name: float_value}}``.
        Modules are strings like ``"A1"``, ``"A1-A3"``, ``"C3"``, ``"D"``.
    """
    modules = {}

    def _add(module, prop_name, value):
        if module not in modules:
            modules[module] = {}
        try:
            modules[module][prop_name] = float(value)
        except (ValueError, TypeError):
            pass

    def _parse_anies(desc_lists, anies_source):
        prop_name = resolve_indicator_prop(desc_lists)
        if not prop_name:
            return
        anies = anies_source.get("other", {}).get("anies", [])
        for a in anies:
            module = a.get("module")
            val_str = a.get("value")
            if module and val_str is not None and not isinstance(val_str, dict):
                _add(module, prop_name, val_str)

    # 1) LCIAResults (GWP, ODP, AP, EP, POCP, ADP, WDP)
    lcia_results = process_json.get("LCIAResults", {}).get("LCIAResult", [])
    for result in lcia_results:
        desc_lists = result.get("referenceToLCIAMethodDataSet", {}).get("shortDescription", [])
        _parse_anies(desc_lists, result)

    # 2) Exchanges (PENRT, PENRM, PENRE, PERT, PERM, PERE, SM, FW, etc.)
    pi = process_json.get("processInformation", {})
    ref_ids = set(pi.get("quantitativeReference", {}).get("referenceToReferenceFlow", []))
    exchanges = process_json.get("exchanges", {}).get("exchange", [])
    for ex in exchanges:
        if ex.get("dataSetInternalID") in ref_ids:
            continue  # skip the reference product flow
        desc_lists = ex.get("referenceToFlowDataSet", {}).get("shortDescription", [])
        _parse_anies(desc_lists, ex)

    return modules


def aggregate_a1a3(modules):
    """Compute A1-A3 aggregated indicator values.

    If the dataset has an ``"A1-A3"`` module, use its values directly.
    Otherwise, sum A1 + A2 + A3 where available.

    Returns:
        dict of ``{prop_name: float}`` for the production phase.
    """
    # Prefer the pre-aggregated A1-A3 module
    if "A1-A3" in modules:
        return dict(modules["A1-A3"])

    # Sum individual modules
    agg = {}
    for mod_key in ("A1", "A2", "A3"):
        mod_data = modules.get(mod_key, {})
        for prop, val in mod_data.items():
            if val is not None:
                agg[prop] = agg.get(prop, 0.0) + val
    return agg


def aggregate_c1c4(modules):
    """Compute C1-C4 aggregated indicator values.

    If the dataset has a ``"C1-C4"`` module, use its values directly.
    Otherwise, sum C1 + C2 + C3 + C4 where available.

    Returns:
        dict of ``{prop_name: float}`` for the end-of-life phase.
    """
    if "C1-C4" in modules:
        return dict(modules["C1-C4"])

    agg = {}
    for mod_key in ("C1", "C2", "C3", "C4"):
        mod_data = modules.get(mod_key, {})
        for prop, val in mod_data.items():
            if val is not None:
                agg[prop] = agg.get(prop, 0.0) + val
    return agg


def aggregate_d(modules):
    """Return indicator values for module D (beyond system boundary).

    Returns:
        dict of ``{prop_name: float}``, empty if module D is absent.
    """
    return dict(modules.get("D", {}))


def aggregate_a1a5(modules):
    """Compute A1-A5 aggregated indicator values (production + construction).

    Prefer a pre-aggregated ``"A1-A5"`` module when present. Otherwise take
    the A1-A3 aggregate (pre-aggregated ``"A1-A3"`` or sum of A1+A2+A3) and
    add A4 and A5 on top when available.
    """
    if "A1-A5" in modules:
        return dict(modules["A1-A5"])

    agg = dict(aggregate_a1a3(modules))
    for mod_key in ("A4", "A5"):
        for prop, val in modules.get(mod_key, {}).items():
            if val is not None:
                agg[prop] = agg.get(prop, 0.0) + val
    return agg


def aggregate_b1b7(modules):
    """Compute B1-B7 aggregated indicator values (use stage).

    Prefer a pre-aggregated ``"B1-B7"`` module when present. Otherwise sum
    B1..B7 where available. Returns ``{}`` if no B modules are present.
    """
    if "B1-B7" in modules:
        return dict(modules["B1-B7"])

    agg = {}
    for mod_key in ("B1", "B2", "B3", "B4", "B5", "B6", "B7"):
        for prop, val in modules.get(mod_key, {}).items():
            if val is not None:
                agg[prop] = agg.get(prop, 0.0) + val
    return agg


def parse_reference_year(process_json):
    """Extract the reference year integer from ILCD JSON.

    Path: processInformation.time.referenceYear

    Returns:
        int or None if the field is absent or not an integer.
    """
    try:
        pi = process_json.get("processInformation", {})
        time_block = pi.get("time", {})
        val = time_block.get("referenceYear")
        if val is None:
            return None
        return int(val)
    except (ValueError, TypeError):
        return None


def parse_full(process_json, lang=None):
    """Parse an ILCD process JSON into a flat result dict.

    Returns a dict with keys:
        name, ref_unit, category, modules (raw), indicators_a1a3 (aggregated),
        modules_available (list of module names), tech_description,
        reference_year.
    """
    name = parse_name(process_json, lang=lang)
    ref_unit = parse_ref_unit(process_json)
    category = parse_category(process_json)
    modules = parse_indicators(process_json)
    a1a3 = aggregate_a1a3(modules)
    a1a5 = aggregate_a1a5(modules)
    b1b7 = aggregate_b1b7(modules)

    return {
        "name": name,
        "ref_unit": ref_unit,
        "category": category,
        "modules": modules,
        "indicators_a1a3": a1a3,
        "indicators_a1a5": a1a5,
        "indicators_b1b7": b1b7,
        "modules_available": sorted(modules.keys()),
        "tech_description": parse_technology_description(process_json, lang=lang),
        "reference_year": parse_reference_year(process_json),
    }
