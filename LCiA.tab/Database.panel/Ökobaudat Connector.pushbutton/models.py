# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Data model classes for the MaterialsDatasets tool.

Classes:
    IndicatorRow    -- one row in the transposed LCA details grid
    MaterialDataset -- represents one EPD dataset (stub + optional full detail)
    ModuleDataRow   -- one row per life-cycle module (legacy grid, kept for compatibility)
"""
#pylint: disable=invalid-name,broad-except
from collections import OrderedDict

import constants


# ────────────────────────────────────────────────────────────────
# IndicatorRow
# ────────────────────────────────────────────────────────────────
class IndicatorRow(object):
    """A row in the transposed details grid: one indicator, values per module."""

    def __init__(self, prop_name, display_name, unit):
        self.Indicator = display_name
        self.Unit = unit
        self._prop_name = prop_name

    def set_module_value(self, col_key, formatted_str):
        setattr(self, col_key, formatted_str)

    def __getattr__(self, name):
        # Guard: let Python's own attribute machinery raise for private names
        # so WPF DataGrid binding does not loop infinitely.
        if name.startswith("_"):
            raise AttributeError(name)
        return u""


# ────────────────────────────────────────────────────────────────
# MaterialDataset
# ────────────────────────────────────────────────────────────────
class MaterialDataset(object):
    """Represents a material dataset from the ÖKOBAUDAT API."""

    def __init__(self, dataset_key, name, reference_unit, category=None, uuid=None,
                 classification="", location="", valid_until="",
                 dataset_type="", owner="", compliance="",
                 program_operator="", epd_no=""):
        self.dataset_key = dataset_key
        self.uuid = uuid
        self.name = name
        self.reference_unit = reference_unit
        self.category = category or "Other"
        self.modules = {}          # {module_name: {prop_name: float}}
        self.DisplayName = name
        self._details_fetched = False  # whether full ILCD data has been loaded
        # Rich metadata columns (matching ÖKOBAUDAT website columns)
        self.Classification   = classification.strip()   if classification   else ""
        # Display-only localized mirror: in EN mode this holds the
        # English-translated path; in DE mode it equals Classification.
        # Filtering, sorting and prefix-matching always use the canonical
        # German `Classification` - set at load time in window._do_api_search.
        self.ClassificationDisplay = self.Classification
        self.Location         = location.strip()         if location         else ""
        self.ValidUntil       = str(valid_until).strip() if valid_until      else ""
        self.DatasetType      = dataset_type.strip()     if dataset_type     else ""
        self.Owner            = owner.strip()            if owner            else ""
        self.Compliance       = compliance.strip()       if compliance       else ""
        self.ProgramOperator  = program_operator.strip() if program_operator else ""
        self.EpdNo            = epd_no.strip()           if epd_no           else ""
        
        # Summary indicators per lifecycle phase (for advanced filtering)
        # A1-A3 (manufacturing core), A1-A5 (production + construction),
        # B1-B7 (use stage), C1-C4 (end-of-life), D (beyond system boundary)
        _PHASE_PROPS = [
            "GWP_total", "GWP_fossil", "GWP_biogenic", "GWP_luluc",
            "ODP", "AP", "EP_freshwater", "EP_marine", "EP_terrestrial",
            "POCP", "ADP_elem", "ADP_fossil", "WDP",
            "PENRE", "PENRM", "PENRT", "PERE", "PERM", "PERT",
            "SM", "SF", "NRSF", "FW", "HWD", "NHWD", "RWD",
            "CRU", "MFR", "MER", "EEE", "EET",
        ]
        for _p in _PHASE_PROPS:
            setattr(self, _p + "_A1A3", None)
            setattr(self, _p + "_A1A5", None)
            setattr(self, _p + "_B1B7", None)
            setattr(self, _p + "_C1C4", None)
            setattr(self, _p + "_D",    None)

        # Extracted material attributes (populated from indicator cache)
        # All deterministic - driven by structured ILCD fields or LCIA indicator values.
        self.UsesSecondaryMaterial = False  # SM indicator > 0 (structured LCIA value)
        self.MaterialCategory = ""          # "mineral", "wood", "metal", etc. (classification field)
        self.ConcreteStrength = ""          # "Low", "Medium", "High" (C-class regex - deterministic)
        self.ElementFunction = ""           # populated by attribute_extractor; not a UI filter
        self.ModulesAvailable = []          # list of module names present (e.g. ["A1-A3", "C1-C4", "D"])

        # BM25F ranking output (set by window._apply_grid_filters when a
        # global query is present; None / empty when no query is active).
        # WPF binds the Score% column to MatchScorePct + MatchScoreTooltip.
        self.MatchScore        = None       # calibrated [0,1] or None
        self.MatchScoreRaw     = None       # raw BM25F
        self.MatchScorePct     = u""        # formatted "85%" or "" when no query
        self.MatchScoreTooltip = u""        # per-term explanation string for cell ToolTip
        self.ReferenceYear = None           # int from processInformation.time.referenceYear
        self.TechnologyDescription = ""     # ILCD "Technology description including background system"
        # English classification path joined with underscores, written to Revit
        # `Material.MaterialClass` by revit_mapper. Populated at runtime in
        # window.py via attribute_extractor.format_revit_material_class().
        # e.g. "Mineral Construction_Mortar & Concrete_Concrete"
        self.RevitMaterialClass = ""

    def load_indicator_cache(self, cache_entry):
        """Populate summary indicators and attributes from a prefetched cache entry.

        Args:
            cache_entry: dict from indicators_{source}_{lang}.json with keys
                         like 'GWP_total', 'PENRT', 'attributes', etc.
        """
        if not cache_entry:
            return
        # Set per-phase indicator values (A1-A3, C1-C4, D)
        _INDICATOR_PROPS = [
            "GWP_total", "GWP_fossil", "GWP_biogenic", "GWP_luluc",
            "ODP", "AP", "EP_freshwater", "EP_marine", "EP_terrestrial",
            "POCP", "ADP_elem", "ADP_fossil", "WDP",
            "PENRE", "PENRM", "PENRT", "PERE", "PERM", "PERT",
            "SM", "SF", "NRSF", "FW", "HWD", "NHWD", "RWD",
            "CRU", "MFR", "MER", "EEE", "EET",
        ]
        for prop in _INDICATOR_PROPS:
            val = cache_entry.get(prop)
            if val is not None:
                setattr(self, prop + "_A1A3", val)
            val_a5 = cache_entry.get(prop + "_A1A5")
            if val_a5 is not None:
                setattr(self, prop + "_A1A5", val_a5)
            val_b = cache_entry.get(prop + "_B1B7")
            if val_b is not None:
                setattr(self, prop + "_B1B7", val_b)
            val_c = cache_entry.get(prop + "_C1C4")
            if val_c is not None:
                setattr(self, prop + "_C1C4", val_c)
            val_d = cache_entry.get(prop + "_D")
            if val_d is not None:
                setattr(self, prop + "_D", val_d)

        # Set material attributes (all deterministic)
        attrs = cache_entry.get("attributes", {})
        # Support both old cache key ("is_recycled") and new key ("uses_secondary_material")
        # so existing caches continue to work before the next prefetcher run.
        self.UsesSecondaryMaterial = (
            attrs.get("uses_secondary_material") or attrs.get("is_recycled", False)
        )
        self.MaterialCategory = attrs.get("material_category", "")
        self.ConcreteStrength = attrs.get("concrete_strength", "")
        self.ElementFunction  = attrs.get("element_function", "")
        self.ModulesAvailable = cache_entry.get("modules_available", [])
        self.ReferenceYear    = cache_entry.get("reference_year")  # int or None
        self.TechnologyDescription = cache_entry.get("tech_description", "")

    def add_module_data(self, module, indicator, value):
        """Store a single indicator value for a given life-cycle module."""
        if module not in self.modules:
            self.modules[module] = {}
        try:
            self.modules[module][indicator] = float(value)
        except (ValueError, TypeError):
            pass

    def to_dict(self):
        """Serialize to an OrderedDict suitable for JSON storage in Revit."""
        sorted_mods = constants._sort_modules(list(self.modules.keys()))
        indicators = []
        for prop_name, en_name, _de, unit, _grp in constants.INDICATOR_META:
            row = OrderedDict()
            row["indicator"] = en_name
            row["unit"] = unit
            has_any = False
            for mod in sorted_mods:
                val = self.modules.get(mod, {}).get(prop_name)
                if val is not None:
                    row[mod] = val
                    has_any = True
            if has_any:
                indicators.append(row)
        result = OrderedDict()
        result["datasetKey"]     = self.dataset_key
        result["uuid"]           = self.uuid
        result["name"]           = self.name
        result["referenceUnit"]  = self.reference_unit
        result["category"]       = self.category
        result["phases"]         = sorted_mods
        result["indicators"]     = indicators
        return result


# ────────────────────────────────────────────────────────────────
# ModuleDataRow
# ────────────────────────────────────────────────────────────────
class ModuleDataRow(object):
    """Row for a DataGrid showing module impact values (one row per life-cycle phase)."""

    # Maps display-name variants → attribute names (used by set_value)
    _INDICATOR_MAP = {
        'ODP': 'ODP', 'AP': 'AP',
        'PENRT': 'PENRT', 'PENRM': 'PENRM', 'PENRE': 'PENRE',
        'PERT': 'PERT', 'PERM': 'PERM', 'PERE': 'PERE',
        'ADP elem.': 'ADP_elem', 'ADP elem': 'ADP_elem', 'ADP_elem': 'ADP_elem',
        'ADP fossil': 'ADP_fossil', 'ADP_fossil': 'ADP_fossil',
        'SM': 'SM', 'SF': 'SF', 'NRSF': 'NRSF', 'FW': 'FW',
        'HWD': 'HWD', 'NHWD': 'NHWD', 'RWD': 'RWD',
        'CRU': 'CRU', 'MFR': 'MFR', 'MER': 'MER', 'EEE': 'EEE', 'EET': 'EET',
        'GWP total': 'GWP_total', 'GWP_total': 'GWP_total',
        'GWP-biogenic': 'GWP_biogenic', 'GWP biogenic': 'GWP_biogenic', 'GWP_biogenic': 'GWP_biogenic',
        'GWP-fossil': 'GWP_fossil', 'GWP fossil': 'GWP_fossil', 'GWP_fossil': 'GWP_fossil',
        'GWP-luluc': 'GWP_luluc', 'GWP luluc': 'GWP_luluc', 'GWP_luluc': 'GWP_luluc',
        'EP-marine': 'EP_marine', 'EP marine': 'EP_marine', 'EP_marine': 'EP_marine',
        'EP-freshwater': 'EP_freshwater', 'EP freshwater': 'EP_freshwater', 'EP_freshwater': 'EP_freshwater',
        'EP-terrestrial': 'EP_terrestrial', 'EP terrestrial': 'EP_terrestrial', 'EP_terrestrial': 'EP_terrestrial',
        'POCP': 'POCP', 'WDP': 'WDP',
        'GWP': 'GWP_total', 'EP': 'EP_freshwater',
    }

    def __init__(self, module):
        self.Module         = module
        self.ODP            = None
        self.AP             = None
        self.PENRT          = None
        self.PENRM          = None
        self.PENRE          = None
        self.PERT           = None
        self.PERM           = None
        self.PERE           = None
        self.ADP_elem       = None
        self.ADP_fossil     = None
        self.SM             = None
        self.SF             = None
        self.NRSF           = None
        self.FW             = None
        self.HWD            = None
        self.NHWD           = None
        self.RWD            = None
        self.CRU            = None
        self.MFR            = None
        self.MER            = None
        self.EEE            = None
        self.EET            = None
        self.GWP_total      = None
        self.GWP_biogenic   = None
        self.GWP_fossil     = None
        self.GWP_luluc      = None
        self.EP_marine      = None
        self.EP_freshwater  = None
        self.EP_terrestrial = None
        self.POCP           = None
        self.WDP            = None

    def _fmt(self, v):
        if v is None:
            return ""
        try:
            return "{:.4g}".format(v)
        except:
            return str(v) if v else ""

    # ── Formatted properties ──────────────────────────────────
    @property
    def ODP_Formatted(self):           return self._fmt(self.ODP)
    @property
    def AP_Formatted(self):            return self._fmt(self.AP)
    @property
    def PENRT_Formatted(self):         return self._fmt(self.PENRT)
    @property
    def PENRM_Formatted(self):         return self._fmt(self.PENRM)
    @property
    def PENRE_Formatted(self):         return self._fmt(self.PENRE)
    @property
    def PERT_Formatted(self):          return self._fmt(self.PERT)
    @property
    def PERM_Formatted(self):          return self._fmt(self.PERM)
    @property
    def PERE_Formatted(self):          return self._fmt(self.PERE)
    @property
    def ADP_elem_Formatted(self):      return self._fmt(self.ADP_elem)
    @property
    def ADP_fossil_Formatted(self):    return self._fmt(self.ADP_fossil)
    @property
    def SM_Formatted(self):            return self._fmt(self.SM)
    @property
    def SF_Formatted(self):            return self._fmt(self.SF)
    @property
    def NRSF_Formatted(self):          return self._fmt(self.NRSF)
    @property
    def FW_Formatted(self):            return self._fmt(self.FW)
    @property
    def HWD_Formatted(self):           return self._fmt(self.HWD)
    @property
    def NHWD_Formatted(self):          return self._fmt(self.NHWD)
    @property
    def RWD_Formatted(self):           return self._fmt(self.RWD)
    @property
    def CRU_Formatted(self):           return self._fmt(self.CRU)
    @property
    def MFR_Formatted(self):           return self._fmt(self.MFR)
    @property
    def MER_Formatted(self):           return self._fmt(self.MER)
    @property
    def EEE_Formatted(self):           return self._fmt(self.EEE)
    @property
    def EET_Formatted(self):           return self._fmt(self.EET)
    @property
    def GWP_total_Formatted(self):     return self._fmt(self.GWP_total)
    @property
    def GWP_biogenic_Formatted(self):  return self._fmt(self.GWP_biogenic)
    @property
    def GWP_fossil_Formatted(self):    return self._fmt(self.GWP_fossil)
    @property
    def GWP_luluc_Formatted(self):     return self._fmt(self.GWP_luluc)
    @property
    def EP_marine_Formatted(self):     return self._fmt(self.EP_marine)
    @property
    def EP_freshwater_Formatted(self): return self._fmt(self.EP_freshwater)
    @property
    def EP_terrestrial_Formatted(self):return self._fmt(self.EP_terrestrial)
    @property
    def POCP_Formatted(self):          return self._fmt(self.POCP)
    @property
    def WDP_Formatted(self):           return self._fmt(self.WDP)

    def set_value(self, indicator, value):
        """Set an indicator value by display name (with fuzzy key matching)."""
        prop_name = self._INDICATOR_MAP.get(indicator) or self._INDICATOR_MAP.get(indicator.strip())
        if prop_name and hasattr(self, prop_name):
            try:
                if isinstance(value, (int, float)):
                    setattr(self, prop_name, float(value))
                else:
                    setattr(self, prop_name, float(str(value).replace(',', '.')))
            except (ValueError, TypeError):
                pass


