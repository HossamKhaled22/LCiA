# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Constants, configuration, LCIA mappings, indicator metadata,
and small utility functions shared across all modules.
"""
#pylint: disable=invalid-name

TOOL_TITLE   = "Ökobaudat Connector"
TOOL_VERSION = "Version: 2.0"

# ────────────────────────────────────────────────────────────────
# Revit shared parameter names
# ────────────────────────────────────────────────────────────────
UUID_PARAM_NAME      = "UUID"
UUID_DATA_PARAM_NAME = "UUID_Data"

# ────────────────────────────────────────────────────────────────
# ÖKOBAUDAT API
# ────────────────────────────────────────────────────────────────
API_BASE = "https://www.oekobaudat.de/OEKOBAU.DAT/resource"

# ────────────────────────────────────────────────────────────────
# Phase 1 - semantic search (Ollama + bge-m3)
# ────────────────────────────────────────────────────────────────
# Local Ollama server hosting the embedding model. Reachable from
# IronPython 2.7 via System.Net.WebClient and from CPython 3 via urllib.
OLLAMA_BASE_URL     = "http://localhost:11434"
EMBEDDING_MODEL     = "bge-m3"
# Per-query embed timeout (ms). A *warm* bge-m3 embed is ~0.5s, but a
# *cold* load - model evicted from RAM/VRAM under memory pressure from
# other Ollama models - can take tens of seconds (the code budgets ~60s).
# The healthcheck shares this client but fails fast on connection-refused
# when Ollama is down, so a high ceiling here only ever helps the one call
# that needs it: the cold-load embed POST. Was 10000, which timed out on
# any cold load and dropped semantic mode to the BM25F fallback.
OLLAMA_TIMEOUT_MS   = 60000
EMBEDDING_DIM       = 1024
EMBEDDING_CACHE_VER = 1
# Sidecar binary format magic. Bumped if the on-disk layout changes.
EMBEDDING_BIN_MAGIC = b"EMB1"

# ────────────────────────────────────────────────────────────────
# Phase 1.5 - auto-build feature flags
# ────────────────────────────────────────────────────────────────
# When True, the Connector spawns `embedding_prefetcher.py` in the
# background on launch if the `.bin` sidecar is missing or older than
# the dataset cache for the current (source, lang). Set to False to
# require manual prefetcher runs.
EMBEDDING_AUTO_BUILD_ENABLED = True
# Parallel HTTP workers for the auto-build subprocess. Lower than the
# manual default (5) to keep CPU/network impact gentle while the user
# is actively working in Revit.
EMBEDDING_AUTO_BUILD_WORKERS = 3
# Absolute path to a CPython 3 executable with `lancedb` + `pyarrow`
# AND (for the cross-encoder reranker) `sentence-transformers` + `torch`.
# Blank means auto-detect: try `python.exe` on PATH, then `py.exe -3`
# (Windows Python launcher). Set an explicit absolute path here if the
# auto-detect picks an interpreter that lacks the required packages.
EMBEDDING_PYTHON_EXE = ""

# ────────────────────────────────────────────────────────────────
# Phase 1+ - Hybrid retrieval (Reciprocal Rank Fusion)
# ────────────────────────────────────────────────────────────────
# RRF (Cormack, Clarke & Buttcher 2009) fuses BM25F + semantic
# rankings by summing 1 / (k + rank) over both rankers. k = 60 is
# the original paper's "simple but effective" default and remains
# the standard in modern hybrid retrieval pipelines (e.g. the
# daveebbelaar/ai-cookbook reference).
HYBRID_RRF_K = 60
# Whether the "Hybrid" checkbox starts checked. False is friendlier
# for benchmarking - users opt in once they want fused ranking.
HYBRID_RRF_ENABLED_DEFAULT = False

# ────────────────────────────────────────────────────────────────
# Phase 1+ - Cross-encoder reranker
# ────────────────────────────────────────────────────────────────
# A locally-hosted Python sidecar (`rerank_service.py`) wraps
# `sentence_transformers.CrossEncoder` and serves cross-encoder
# rerank scores over HTTP. Ollama does not expose a true
# `/api/rerank` endpoint, so we don't try to reuse it for this stage.
# The reranker post-processes whatever first-pass ranker is active
# (cosine OR RRF), re-scoring the top-K candidates by attending
# over the (query, dataset) pair together - sharper top-10 ordering.
RERANKER_MODEL              = "BAAI/bge-reranker-v2-m3"
RERANKER_PORT               = 11500
RERANKER_BASE_URL           = "http://127.0.0.1:11500"
RERANKER_TOP_K              = 20      # rerank only the top-K from the first pass
RERANKER_TIMEOUT_MS         = 15000   # rerank can be 1-3s for top-20 on CPU
RERANKER_AUTO_SPAWN_ENABLED = True    # spawn rerank_service.py on Connector launch

ECOINVENT_DATASOURCE_UUID = "b497a91f-e14b-4b69-8f28-f50eb1576766"
COMPLIANCE_A2       = "c0016b33-8cf7-415c-ac6e-deba0d21440d"
COMPLIANCE_A2_EF31  = "d4aa3ec7-b1d7-4a4a-a6cb-37af88dcc902"
COMPLIANCE_A1       = "b00f9ec0-7874-11e3-981f-0800200c9a66"

DATASTOCKS = {
    "a2_sphera": {
        "uuid": "ca70a7e6-0ea4-4e90-a947-d44585783626",
        "label": u"ÖKOBAUDAT \u2013 EN 15804+A2 (Sphera MLC)",
        "compliance": "A2",
        "extra_params": (
            "&dataSource={ds}&dataSourceMode=NOT"
            "&compliance={c1}&compliance={c2}&complianceMode=OR"
        ).format(
            ds=ECOINVENT_DATASOURCE_UUID,
            c1=COMPLIANCE_A2,
            c2=COMPLIANCE_A2_EF31,
        ),
    },
    "a1_sphera": {
        "uuid": "c391de0f-2cfd-47ea-8883-c661d294e2ba",
        "label": u"ÖKOBAUDAT \u2013 EN 15804+A1 (Sphera MLC)",
        "compliance": "A1",
        "extra_params": (
            "&dataSource={ds}&dataSourceMode=NOT"
            "&compliance={c1}"
        ).format(
            ds=ECOINVENT_DATASOURCE_UUID,
            c1=COMPLIANCE_A1,
        ),
    },
    "a2_ecoinvent": {
        "uuid": "ca70a7e6-0ea4-4e90-a947-d44585783626",
        "label": u"ÖKOBAUDAT \u2013 EN 15804+A2 (ecoinvent)",
        "compliance": "A2",
        "extra_params": "&dataSource={}".format(ECOINVENT_DATASOURCE_UUID),
    },
    "project_epds": {
        "uuid": "413cab35-5d20-4915-93d9-691b43724302",
        "label": u"Project EPDs \u2013 EN 15804+A2 (Sphera MLC)",
        "compliance": "A2",
        "extra_params": (
            "&dataSource={ds}&dataSourceMode=NOT"
        ).format(ds=ECOINVENT_DATASOURCE_UUID),
    },
}

# ────────────────────────────────────────────────────────────────
# Indicator column definitions per compliance version
# Each entry: (display_header, property_name, width)
# ────────────────────────────────────────────────────────────────
A2_COLUMNS = [
    ("Module",           "Module",                80),
    ("GWP total",        "GWP_total_Formatted",   100),
    ("GWP-fossil",       "GWP_fossil_Formatted",  100),
    ("GWP-biogenic",     "GWP_biogenic_Formatted", 110),
    ("GWP-luluc",        "GWP_luluc_Formatted",   100),
    ("ODP",              "ODP_Formatted",          90),
    ("AP",               "AP_Formatted",           90),
    ("EP-freshwater",    "EP_freshwater_Formatted", 110),
    ("EP-marine",        "EP_marine_Formatted",    100),
    ("EP-terrestrial",   "EP_terrestrial_Formatted", 120),
    ("POCP",             "POCP_Formatted",          90),
    ("ADP elem.",        "ADP_elem_Formatted",      90),
    ("ADP fossil",       "ADP_fossil_Formatted",    90),
    ("WDP",              "WDP_Formatted",            90),
    ("PENRT",            "PENRT_Formatted",          90),
    ("PENRM",            "PENRM_Formatted",          90),
    ("PENRE",            "PENRE_Formatted",          90),
    ("PERT",             "PERT_Formatted",           90),
    ("PERM",             "PERM_Formatted",           90),
    ("PERE",             "PERE_Formatted",           90),
    ("SM",               "SM_Formatted",             90),
    ("SF",               "SF_Formatted",             90),
    ("NRSF",             "NRSF_Formatted",           90),
    ("FW",               "FW_Formatted",             90),
    ("HWD",              "HWD_Formatted",            90),
    ("NHWD",             "NHWD_Formatted",           90),
    ("RWD",              "RWD_Formatted",            90),
    ("CRU",              "CRU_Formatted",            90),
    ("MFR",              "MFR_Formatted",            90),
    ("MER",              "MER_Formatted",            90),
    ("EEE",              "EEE_Formatted",            90),
    ("EET",              "EET_Formatted",            90),
]

A1_COLUMNS = [
    ("Module",         "Module",              80),
    ("GWP",            "GWP_total_Formatted", 100),
    ("ODP",            "ODP_Formatted",        90),
    ("AP",             "AP_Formatted",         90),
    ("EP",             "EP_freshwater_Formatted", 90),
    ("POCP",           "POCP_Formatted",        90),
    ("ADP elem.",      "ADP_elem_Formatted",    90),
    ("ADP fossil",     "ADP_fossil_Formatted",  90),
    ("PENRT",          "PENRT_Formatted",        90),
    ("PENRM",          "PENRM_Formatted",        90),
    ("PENRE",          "PENRE_Formatted",        90),
    ("PERT",           "PERT_Formatted",          90),
    ("PERM",           "PERM_Formatted",          90),
    ("PERE",           "PERE_Formatted",          90),
    ("SM",             "SM_Formatted",            90),
    ("SF",             "SF_Formatted",            90),
    ("NRSF",           "NRSF_Formatted",          90),
    ("FW",             "FW_Formatted",            90),
    ("HWD",            "HWD_Formatted",           90),
    ("NHWD",           "NHWD_Formatted",          90),
    ("RWD",            "RWD_Formatted",           90),
    ("CRU",            "CRU_Formatted",           90),
    ("MFR",            "MFR_Formatted",           90),
    ("MER",            "MER_Formatted",           90),
    ("EEE",            "EEE_Formatted",           90),
    ("EET",            "EET_Formatted",           90),
]

# ────────────────────────────────────────────────────────────────
# ILCD indicator short-description → internal property name
# ────────────────────────────────────────────────────────────────
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
    # English descriptive names (EN API responses omit abbreviation tag for GWP indicators)
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

# ────────────────────────────────────────────────────────────────
# Transposed details layout: indicator rows, module columns
# ────────────────────────────────────────────────────────────────
MODULE_ORDER = [
    "A1", "A2", "A3", "A1-A3", "A4", "A5",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "C1", "C2", "C3", "C4", "D",
]

# (prop_name, en_label, de_label, unit, group)
# group: "env" = Environmental Impact, "res" = Resource Use / Output
INDICATOR_META = [
    # Environmental Impact Indicators
    ("GWP_total",
     u"Climate change, total (GWP total)",
     u"Globales Erw\u00e4rmungspotenzial - total (GWP-total)",
     u"kg CO\u2082 eq.", "env"),
    ("GWP_fossil",
     u"Climate change, fossil (GWP-fossil)",
     u"Globales Erw\u00e4rmungspotenzial - fossil (GWP-fossil)",
     u"kg CO\u2082 eq.", "env"),
    ("GWP_biogenic",
     u"Climate change, biogenic (GWP-biogenic)",
     u"Globales Erw\u00e4rmungspotenzial - biogen (GWP-biogenic)",
     u"kg CO\u2082 eq.", "env"),
    ("GWP_luluc",
     u"Climate change, land use and land use change (GWP-luluc)",
     u"Globales Erw\u00e4rmungspotenzial - Landnutzung und Landnutzungs\u00e4nderung (GWP-luluc)",
     u"kg CO\u2082 eq.", "env"),
    ("ODP",
     u"Depletion of the stratospheric ozone layer (ODP)",
     u"Abbaupotenzial der stratosph\u00e4rischen Ozonschicht (ODP)",
     u"kg CFC-11 eq.", "env"),
    ("AP",
     u"Acidification (AP)",
     u"Versauerungspotenzial, kumulierte \u00dcberschreitung (AP)",
     u"mol H\u207a eq.", "env"),
    ("EP_freshwater",
     u"Eutrophication, freshwater (EP-freshwater)",
     u"Eutrophierungspotenzial - S\u00fc\u00dfwasser (EP-S\u00fc\u00dfwasser)",
     u"kg P eq.", "env"),
    ("EP_marine",
     u"Eutrophication, marine (EP-marine)",
     u"Eutrophierungspotenzial - Meerwasser (EP-Meerwasser)",
     u"kg N eq.", "env"),
    ("EP_terrestrial",
     u"Eutrophication, terrestrial (EP-terrestrial)",
     u"Eutrophierungspotenzial - terrestrisch (EP-terrestrisch)",
     u"mol N eq.", "env"),
    ("POCP",
     u"Formation of ozone (POCP)",
     u"Bildungspotenzial f\u00fcr troposph\u00e4risches Ozon (POCP)",
     u"kg NMVOC eq.", "env"),
    ("ADP_elem",
     u"Depletion of abiotic resources, minerals and metals (ADP elem.)",
     u"Abbaupotenzial f\u00fcr abiotische Ressourcen - Mineralien und Metalle (ADP-Mineralien)",
     u"kg Sb eq.", "env"),
    ("ADP_fossil",
     u"Depletion of abiotic resources, fossil (ADP fossil)",
     u"Abbaupotenzial f\u00fcr abiotische Ressourcen - fossile Energietr\u00e4ger (ADP-fossil)",
     u"MJ", "env"),
    ("WDP",
     u"Water use (WDP)",
     u"Wassernutzungspotenzial (WDP)",
     u"m\u00b3", "env"),
    # Resource Use / Life Cycle Indicators
    ("PENRE",
     u"Use of non-renewable primary energy excl. feedstock (PENRE)",
     u"Nicht erneuerbare Prim\u00e4renergie, exkl. Rohstoffnutzung (PENRE)",
     u"MJ", "res"),
    ("PENRM",
     u"Use of non-renewable primary energy as feedstock (PENRM)",
     u"Nicht erneuerbare Prim\u00e4renergie, inkl. Rohstoffnutzung (PENRM)",
     u"MJ", "res"),
    ("PENRT",
     u"Total use of non-renewable primary energy (PENRT)",
     u"Nicht erneuerbare Prim\u00e4renergie, gesamt (PENRT)",
     u"MJ", "res"),
    ("PERE",
     u"Use of renewable primary energy excl. feedstock (PERE)",
     u"Erneuerbare Prim\u00e4renergie, exkl. Rohstoffnutzung (PERE)",
     u"MJ", "res"),
    ("PERM",
     u"Use of renewable primary energy as feedstock (PERM)",
     u"Erneuerbare Prim\u00e4renergie, inkl. Rohstoffnutzung (PERM)",
     u"MJ", "res"),
    ("PERT",
     u"Total use of renewable primary energy (PERT)",
     u"Erneuerbare Prim\u00e4renergie, gesamt (PERT)",
     u"MJ", "res"),
    ("SM",
     u"Secondary materials (SM)",
     u"Einsatz von Sekund\u00e4rrohstoffen (SM)",
     u"kg", "res"),
    ("SF",
     u"Use of renewable secondary fuels (SF)",
     u"Erneuerbare Sekund\u00e4rbrennstoffe (SF)",
     u"MJ", "res"),
    ("NRSF",
     u"Use of non-renewable secondary fuels (NRSF)",
     u"Nicht erneuerbare Sekund\u00e4rbrennstoffe (NRSF)",
     u"MJ", "res"),
    ("FW",
     u"Use of net fresh water (FW)",
     u"Einsatz von S\u00fc\u00dfwasserressourcen (FW)",
     u"m\u00b3", "res"),
    ("HWD",
     u"Hazardous waste disposed (HWD)",
     u"Gef\u00e4hrliche Abf\u00e4lle zur Beseitigung (HWD)",
     u"kg", "res"),
    ("NHWD",
     u"Non-hazardous waste disposed (NHWD)",
     u"Nicht gef\u00e4hrliche Abf\u00e4lle zur Beseitigung (NHWD)",
     u"kg", "res"),
    ("RWD",
     u"Radioactive waste disposed (RWD)",
     u"Radioaktive Abf\u00e4lle zur Beseitigung (RWD)",
     u"kg", "res"),
    ("CRU",
     u"Components for re-use (CRU)",
     u"Komponenten zur Wiederverwendung (CRU)",
     u"kg", "res"),
    ("MFR",
     u"Materials for recycling (MFR)",
     u"Materialien zum Recycling (MFR)",
     u"kg", "res"),
    ("MER",
     u"Materials for energy recovery (MER)",
     u"Materialien zur Energier\u00fcckgewinnung (MER)",
     u"kg", "res"),
    ("EEE",
     u"Exported electrical energy (EEE)",
     u"Exportierter Strom (EEE)",
     u"MJ", "res"),
    ("EET",
     u"Exported thermal energy (EET)",
     u"Exportierte W\u00e4rme (EET)",
     u"MJ", "res"),
]

# Indicator prop names valid for EN 15804+A1 (subset)
A1_INDICATORS = {
    "GWP_total", "ODP", "AP", "EP_freshwater", "POCP", "ADP_elem", "ADP_fossil",
    "PENRT", "PENRM", "PENRE", "PERT", "PERM", "PERE",
    "SM", "SF", "NRSF", "FW", "HWD", "NHWD", "RWD", "CRU", "MFR", "MER", "EEE", "EET",
}


# ────────────────────────────────────────────────────────────────
# Utility functions
# ────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────
# Advanced query defaults
# ────────────────────────────────────────────────────────────────
# Per-indicator slider range defaults (min, max, decimal_places)
# Used as fallback when no indicator cache is loaded yet.
# Note: GWP_biogenic can be negative (carbon storage in wood/biobased materials).
INDICATOR_RANGE_DEFAULTS = {
    "GWP_total":       (0,    2000,   0),
    "GWP_fossil":      (0,    2000,   0),
    "GWP_biogenic":    (-500,  500,   1),  # can be negative (carbon storage)
    "GWP_luluc":       (-10,   10,    2),
    "ODP":             (0,    0.001,  6),  # kg CFC-11 eq. -- tiny values
    "AP":              (0,    5,      2),
    "EP_freshwater":   (0,    0.5,    3),
    "EP_marine":       (0,    2,      2),
    "EP_terrestrial":  (0,    20,     1),
    "POCP":            (0,    5,      2),
    "ADP_elem":        (0,    0.01,   5),
    "ADP_fossil":      (0,    5000,   0),
    "WDP":             (0,    100,    1),
    "PENRT":  (0, 10000, 0),
    "PENRE":  (0,  5000, 0),
    "PENRM":  (0,  5000, 0),
    "PERT":   (0,  5000, 0),
    "PERE":   (0,  5000, 0),
    "PERM":   (0,  5000, 0),
    "SM":     (0,  1000, 0),
    "SF":     (0,  1000, 0),
    "NRSF":   (0,  1000, 0),
    "FW":     (0,    50, 1),
    "HWD":    (0,   100, 1),
    "NHWD":   (0,   500, 0),
    "RWD":    (0,    10, 2),
    "CRU":    (0,   100, 1),
    "MFR":    (0,   500, 0),
    "MER":    (0,   100, 1),
    "EEE":    (0,   500, 0),
    "EET":    (0,   500, 0),
}

# Env indicators available for slider selection (prop_name, short_label, unit)
ENV_FILTER_INDICATORS = [
    ("GWP_total",      "GWP-total",      u"kg CO\u2082 eq."),
    ("GWP_fossil",     "GWP-fossil",     u"kg CO\u2082 eq."),
    ("GWP_biogenic",   "GWP-biogenic",   u"kg CO\u2082 eq."),
    ("GWP_luluc",      "GWP-luluc",      u"kg CO\u2082 eq."),
    ("ODP",            "ODP",            u"kg CFC-11 eq."),
    ("AP",             "AP",             u"mol H\u207a eq."),
    ("EP_freshwater",  "EP-freshwater",  u"kg P eq."),
    ("EP_marine",      "EP-marine",      u"kg N eq."),
    ("EP_terrestrial", "EP-terrestrial", u"mol N eq."),
    ("POCP",           "POCP",           u"kg NMVOC eq."),
    ("ADP_elem",       "ADP elem.",      u"kg Sb eq."),
    ("ADP_fossil",     "ADP fossil",     u"MJ"),
    ("WDP",            "WDP",            u"m\u00b3"),
]

# Resource indicators available for slider selection
RES_FILTER_INDICATORS = [
    ("PENRT",  "PENRT",  "MJ"),
    ("PENRE",  "PENRE",  "MJ"),
    ("PENRM",  "PENRM",  "MJ"),
    ("PERT",   "PERT",   "MJ"),
    ("PERE",   "PERE",   "MJ"),
    ("PERM",   "PERM",   "MJ"),
    ("SM",     "SM",     "kg"),
    ("SF",     "SF",     "MJ"),
    ("NRSF",   "NRSF",   "MJ"),
    ("FW",     "FW",     u"m\u00b3"),
    ("HWD",    "HWD",    "kg"),
    ("NHWD",   "NHWD",   "kg"),
    ("RWD",    "RWD",    "kg"),
    ("CRU",    "CRU",    "kg"),
    ("MFR",    "MFR",    "kg"),
    ("MER",    "MER",    "kg"),
    ("EEE",    "EEE",    "MJ"),
    ("EET",    "EET",    "MJ"),
]


def _module_to_col_key(module):
    """Convert a module label (e.g. 'A1-A3') to a safe Python attribute name."""
    return "M_" + module.replace("-", "_").replace(" ", "_").replace(".", "_")


def _sort_modules(modules):
    """Sort module labels in EN 15804 canonical order."""
    def _key(m):
        try:
            return MODULE_ORDER.index(m)
        except ValueError:
            return len(MODULE_ORDER)
    return sorted(modules, key=_key)
