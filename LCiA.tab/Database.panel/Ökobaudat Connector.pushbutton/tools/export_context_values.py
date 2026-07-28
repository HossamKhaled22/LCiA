# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Export per-field BIM-context *value* annotations for the live UI chips.

Reads `tools/ablation_query_context_{lang}.json` (produced by
`ablate_query_context.py`) and writes
`LCiA_Extension_Cache/query_context_values_a2_sphera_{lang}.json` - the small
file the Connector and the Validation tool load to label each context-field
chip with its measured leave-one-out marginal MRR, coverage, helps/neutral/
noise verdict, and whether it is recommended / default-on.

When a run's `loo_marginal_mrr` is empty (e.g. the EN run computed only the
4-variant cross-mode subset), we fall back to the PUBLISHED study values from
the field-ladder study so the chips still carry the authoritative
numbers. Re-run a full ablation (all 17 variants) and this exporter to refresh
from live data:

    python tools/ablate_query_context.py --lang en           # full ladder + LOO
    python tools/export_context_values.py

Pure CPython 3 (offline). The output JSON is consumed by IronPython 2.7.
"""
from __future__ import print_function
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
CACHE = os.path.join(PARENT, "LCiA_Extension_Cache")

# Cross-language verdicts (the authoritative conclusion of the study - the
# *direction* is what is consistent across the two independent language sets;
# per-field magnitudes are not individually significant at n=55).
VERDICT = {
    "class":                "helps",
    "description":          "helps",
    "concrete_grade":       "helps",
    "structural_class":     "neutral",
    "host_categories":      "mixed",
    "thermal_conductivity": "noise",
    "density":              "noise",
    "host_types":           "noise",
}
# Fields recommended to ADD beyond the Name+Class default (all "helps").
RECOMMENDED = set(["class", "description", "concrete_grade"])
# Fields ON by default (the user-chosen minimal default).
DEFAULT_ON = set(["name", "class"])
# Stable display order (matches query_context.FIELD_KEYS minus name).
FIELD_ORDER = ["class", "description", "concrete_grade", "structural_class",
               "host_categories", "density", "thermal_conductivity",
               "host_types"]

# Published EN leave-one-out marginal MRR (field-ladder study,
# semantic; used only when the run's loo_marginal_mrr is empty).
PUBLISHED_LOO_EN = {
    "class": 0.021, "description": 0.059, "concrete_grade": 0.014,
    "structural_class": 0.005, "host_categories": -0.015,
    "thermal_conductivity": -0.024, "density": -0.079, "host_types": 0.007,
}

# Headline best-combination numbers per language (for the "highest value"
# readout). EN = reranker V3 vs name-only (the study headline); DE = best
# semantic cumulative variant vs name-only.
HEADLINE = {
    "en": {"best_mode": "reranker", "best_mrr": 0.562, "baseline_mrr": 0.490},
    "de": {"best_mode": "semantic", "best_mrr": 0.372, "baseline_mrr": 0.298},
}


def _loo_for(lang, abl):
    """Return {field: loo_mrr} for the given language."""
    raw = abl.get("loo_marginal_mrr") or {}
    out = {}
    for field in FIELD_ORDER:
        rec = raw.get(field)
        if isinstance(rec, dict):
            # Prefer semantic; else first available mode.
            val = rec.get("semantic")
            if val is None and rec:
                val = list(rec.values())[0]
            if val is not None:
                out[field] = float(val)
    if not out and lang == "en":
        out = dict(PUBLISHED_LOO_EN)
    return out


def _build(lang):
    abl_path = os.path.join(HERE, "ablation_query_context_{}.json".format(lang))
    if not os.path.exists(abl_path):
        print("WARN: missing", abl_path, "- skipping", lang)
        return None
    abl = json.load(io.open(abl_path, encoding="utf-8"))
    coverage = abl.get("coverage", {})
    loo = _loo_for(lang, abl)
    n = abl.get("n_match", 55)

    fields = {}
    for field in FIELD_ORDER:
        fields[field] = {
            "loo_mrr":     round(loo.get(field, 0.0), 4),
            "coverage":    int(coverage.get(field, 0)),
            "verdict":     VERDICT.get(field, "neutral"),
            "recommended": field in RECOMMENDED,
            "default_on":  field in DEFAULT_ON,
        }

    head = HEADLINE.get(lang, {})
    payload = {
        "_schema":      "query_context_values_v1",
        "datastock":    "a2_sphera",
        "lang":         lang,
        "n_match":      n,
        "source_run":   "tools/ablation_query_context_{}.json".format(lang),
        "name_default_on": True,
        "fields":       fields,
        "best_combination": ["name", "class", "description", "concrete_grade"],
        "best_mode":    head.get("best_mode", "reranker"),
        "best_mrr":     head.get("best_mrr"),
        "baseline_mrr": head.get("baseline_mrr"),
        "note": "Per-field measured value for the Connector/Validation context "
                "chips. loo_mrr = leave-one-out marginal MRR (positive helps, "
                "negative adds noise). Enriches the dense (semantic/reranker) "
                "query only; numeric fields belong in deterministic filters.",
    }
    out_path = os.path.join(
        CACHE, "query_context_values_a2_sphera_{}.json".format(lang))
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
    print("Wrote", out_path)
    return out_path


def main():
    for lang in ("en", "de"):
        _build(lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
