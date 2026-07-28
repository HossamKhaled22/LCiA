# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: GPL-3.0-or-later
__title__   = "Extract Context"
__version__ = "Version: 3.0"
__doc__     = """Version = 3.0 | Date: 10.03.2026
Extract contextual data for each material in the model and save it to LCA_Context parameter.
Supports Active View, Entire Project, By Category, and Pick Elements scopes.
User selects which properties to export via a property-selector DataGrid.
Output JSON is structured for LLM-based / vector-search ÖKOBAUDAT matching.
"""

import os
import io
import sys
import json
import traceback
import tempfile
import re
from collections import OrderedDict
from System import Guid, Action

from pyrevit import revit, DB, UI
from pyrevit import forms        # must be imported before System.Windows to load WPF assemblies
from pyrevit import script
from Autodesk.Revit.DB import FilteredElementCollector, Transaction

from System.Windows import Visibility, GridLength, Thickness
from System.Windows.Controls import CheckBox as WPFCheckBox, Expander
from System.Windows.Media import SolidColorBrush, Color as WpfColor, VisualTreeHelper
from System.Windows.Data import CollectionViewSource, PropertyGroupDescription

logger = script.get_logger()

uidoc = __revit__.ActiveUIDocument
doc   = uidoc.Document

# ────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────
XAML_FILE        = "ExtractContext.xaml"
PATH_SCRIPT      = os.path.dirname(__file__)
CONTEXT_PARAM    = "LCA_Context"
DATA_DIR         = os.path.join(PATH_SCRIPT, "data")
JSON_EXPORT_PATH = os.path.join(DATA_DIR, "materials_context.json")

# ────────────────────────────────────────────────────────────────
# Property definitions - drives the selector DataGrid
# (key, category, display_name, example_value, description, recommended)
# ────────────────────────────────────────────────────────────────
PROPERTY_DEFINITIONS = [
    # ── Identity ─────────────────────────────────────────────────────────────────────────────
    ("identity.name",         "Identity", "Name",           '"Beton C25/30"',
     "Material name as shown in Revit's Material Browser. The primary identifier of the material throughout the project.",                                   True),
    ("identity.class",        "Identity", "Material Class", '"Concrete"',
     "Top-level material category from the Identity tab (Concrete, Metal, Wood, Plastic, ...). Used to group and filter materials.",                          True),
    ("identity.description",  "Identity", "Description",    '"Structural concrete for slabs"',
     "Free-text description from the Identity tab. Typically records the material's purpose, application, or specification notes.",                           True),
    ("identity.keywords",     "Identity", "Keywords",       '"structural, reinforced"',
     "Comma-separated keyword tags from the Identity tab. Helps search and filter materials within the project.",                                              True),
    ("identity.subclass",     "Identity", "Subclass",       '"Cast-in-place"',
     "Optional sub-classification under the main Material Class (e.g. 'Cast-in-place' beneath Concrete).",                                                    False),
    ("identity.type",         "Identity", "Type",           '"Concrete"',
     "Material type label from the Identity tab. Often mirrors the Material Class field and is used for sorting in schedules.",                               False),
    ("identity.manufacturer", "Identity", "Manufacturer",   '"Holcim AG"',
     "Manufacturer or supplier name from the Identity tab. Used for product specification, schedules, and procurement.",                                       False),
    ("identity.model",        "Identity", "Model",          '"PowerCem 52.5R"',
     "Product model or product code from the Identity tab. Identifies a specific manufacturer SKU or product line.",                                          False),
    ("identity.source",       "Identity", "Source",         '"BS EN 206:2013"',
     "Reference to the standard, datasheet, or document this material was specified from.",                                                                    False),
    ("identity.url",          "Identity", "URL",            '"https://holcim.com/cement"',
     "Hyperlink to the manufacturer's website, technical datasheet, or product page.",                                                                         False),

    # ── Context (host elements) ───────────────────────────────────────────────────────────────
    ("context.host_categories", "Context", "Host Categories", '["Walls", "Floors"]',
     "Revit element categories (Walls, Floors, Roofs, ...) that contain this material. Shows where in the model the material is actually used.",              True),
    ("context.host_types",      "Context", "Host Types",      '["Exterior Wall 300mm"]',
     "Specific element type names that contain this material (e.g. 'Exterior Wall 300mm'). Pinpoints the exact assemblies using this material.",              True),

    # ── Physical ─────────────────────────────────────────────────────────────────────────────
    ("physical.density_kg_m3",           "Physical", "Density",             "2400 kg/m³",
     "Mass per unit volume. Drives self-weight, structural loading, thermal mass, and material quantity take-offs.",                                          True),
    ("physical.structural_class",        "Physical", "Structural Class",    '"Concrete"',
     "Class of the linked Structural asset (Concrete, Metal, Wood, Gas, Liquid ...). Determines which structural properties Revit exposes for the material.", True),
    ("physical.concrete_compression_mpa","Physical", "Compressive Strength","25 MPa",
     "Characteristic compressive strength of concrete (e.g. C25 = 25 MPa). Defines the concrete grade used in structural design.",                            True),
    ("physical.lightweight",             "Physical", "Lightweight",         "false",
     "Flag indicating whether the concrete is classified as lightweight (LC). Affects density assumptions and code-based design rules.",                       True),
    ("physical.youngs_modulus_mpa",      "Physical", "Young's Modulus",     "30000 MPa",
     "Modulus of elasticity - how much the material deforms elastically under axial stress. Used in deflection, stiffness, and FEM analysis.",                False),
    ("physical.poissons_ratio",          "Physical", "Poisson's Ratio",     "0.2",
     "Ratio of lateral to axial strain when the material is loaded. Used in 3D structural and finite-element analysis.",                                       False),
    ("physical.shear_modulus_mpa",       "Physical", "Shear Modulus",       "12500 MPa",
     "Modulus describing the material's resistance to shear deformation. Used in beam torsion and shear analysis.",                                            False),
    ("physical.damping_ratio",           "Physical", "Damping Ratio",       "0.05",
     "Material damping coefficient. Used in dynamic and seismic structural analysis to model energy dissipation.",                                             False),
    ("physical.yield_strength_mpa",      "Physical", "Yield Strength",      "250 MPa",
     "Stress at which the material starts to deform plastically. Defines the design strength of steel and other metals.",                                      False),
    ("physical.tensile_strength_mpa",    "Physical", "Tensile Strength",    "400 MPa",
     "Maximum tensile stress the material can withstand before failure. Used in reinforcement and connection design.",                                         False),
    ("physical.thermal_expansion_inv_C", "Physical", "Thermal Expansion",   "1.2e-5 /\u00b0C",
     "How much the material expands per degree of temperature change. Used for expansion-joint detailing and thermal-stress analysis.",                        False),
    ("physical.shear_strength_reduction","Physical", "Shear Str. Reduction","0.85",
     "Concrete shear-strength reduction factor (per design codes). Used in shear-reinforcement calculations.",                                                 False),
    ("physical.reduction_factor",        "Physical", "Reduction Factor",    "0.9",
     "Metal strength reduction factor (per design codes). Accounts for material variability and code-based safety margins.",                                   False),

    # ── Thermal ──────────────────────────────────────────────────────────────────────────────
    ("thermal.thermal_conductivity_w_mk",    "Thermal", "Thermal Conductivity",  "2.5 W/(m\u00b7K)",
     "Rate at which heat conducts through the material (lower = better insulator). Used in building-envelope U-value and energy analysis.",                    True),
    ("thermal.density_kg_m3",                "Thermal", "Density (Thermal)",     "2400 kg/m\u00b3",
     "Density value stored on the Thermal asset (separate from the Structural asset's density). Used in dynamic thermal-mass simulations.",                    True),
    ("thermal.specific_heat_j_gC",           "Thermal", "Specific Heat",         "0.84 J/(g\u00b7\u00b0C)",
     "Heat required to raise 1 g of material by 1 degree. Used in thermal-mass and energy-storage calculations.",                                              False),
    ("thermal.emissivity",                   "Thermal", "Emissivity",            "0.9",
     "Fraction of thermal radiation the surface emits (0 = perfect reflector, 1 = perfect emitter). Used in radiative heat-exchange analysis.",                False),
    ("thermal.permeability_ng_Pas_m2",       "Thermal", "Permeability",          "0.02 ng/(Pa\u00b7s\u00b7m\u00b2)",
     "Water-vapour permeability - how easily vapour passes through the material. Used in moisture and condensation analysis.",                                 False),
    ("thermal.porosity",                     "Thermal", "Porosity",              "0.3",
     "Fraction of the material's volume that is void space. Affects acoustic, thermal, and moisture-transport behaviour.",                                     False),
    ("thermal.reflectivity",                 "Thermal", "Reflectivity",          "0.05",
     "Fraction of incident light or heat the surface reflects. Used in solar-gain and daylighting analysis.",                                                  False),
    ("thermal.electrical_resistivity_ohm_m", "Thermal", "Electrical Resistivity","1e+10 \u03a9\u00b7m",
     "Resistance the material offers to electric current. Used in electrical engineering and grounding analysis.",                                             False),
    ("thermal.thermal_class",                "Thermal", "Thermal Class",         '"Solid"',
     "Revit's classification of the Thermal asset (Solid / Gas / Liquid). Determines which thermal properties apply.",                                         False),
    ("thermal.transmits_light",              "Thermal", "Transmits Light",       "false",
     "Flag indicating whether the material transmits visible light (e.g. glass). Used in daylighting analysis and rendering.",                                 False),
]

# Keys that are always written to the output regardless of selection
ALWAYS_EXPORT = {"identity.name"}


# ────────────────────────────────────────────────────────────────
# PropertyItem - row model for the selector DataGrid
# ────────────────────────────────────────────────────────────────
class PropertyItem(object):
    """Plain Python object exposed as DataGrid row via IronPython/WPF binding."""

    _CAT_BG = {
        "Identity": WpfColor.FromRgb(219, 234, 254),   # blue-100
        "Context":  WpfColor.FromRgb(220, 252, 231),   # green-100
        "Physical": WpfColor.FromRgb(254, 243, 199),   # amber-100
        "Thermal":  WpfColor.FromRgb(204, 251, 241),   # teal-100
    }
    _CAT_FG = {
        "Identity": WpfColor.FromRgb(29,  78,  216),   # blue-700
        "Context":  WpfColor.FromRgb(21,  128, 61),    # green-700
        "Physical": WpfColor.FromRgb(180, 83,  9),     # amber-700
        "Thermal":  WpfColor.FromRgb(15,  118, 110),   # teal-700
    }

    def __init__(self, key, category, display_name, example, description, recommended):
        self.Key         = key
        self.Category    = category
        self.DisplayName = display_name
        self.Example     = example
        self.Description = description
        self.Recommended = recommended
        self.IsSelected  = recommended          # default = recommended
        self.StarText    = u"\u2605" if recommended else u""
        self.CategoryBg  = SolidColorBrush(
            self._CAT_BG.get(category, WpfColor.FromRgb(243, 244, 246)))
        self.CategoryFg  = SolidColorBrush(
            self._CAT_FG.get(category, WpfColor.FromRgb(75, 85, 99)))


# ────────────────────────────────────────────────────────────────
# Utility helpers
# ────────────────────────────────────────────────────────────────
def _clean_float(f, places=4):
    if not isinstance(f, float):
        return f
    try:
        s = "{0:.{1}f}".format(f, places).rstrip("0").rstrip(".")
        return float(s) if ("." in s or (s.lstrip("-").isdigit())) else float(f)
    except Exception:
        return f


def _get_param_value(element, builtin_param):
    param = element.get_Parameter(builtin_param)
    if param:
        if param.StorageType == DB.StorageType.String:
            return param.AsString() or ""
        elif param.StorageType == DB.StorageType.Double:
            return param.AsDouble()
        elif param.StorageType == DB.StorageType.Integer:
            return param.AsInteger()
    return ""


def _get_string_by_name(elem, name):
    param = elem.LookupParameter(name)
    if not param:
        params = elem.GetParameters(name)
        if params:
            param = params[0]
    if param and param.StorageType == DB.StorageType.String:
        return param.AsString() or ""
    return ""


def _xyz_to_float(val):
    try:
        if hasattr(val, 'X') and hasattr(val, 'Y') and hasattr(val, 'Z'):
            return float(val.X)
    except Exception:
        pass
    return val


def _safe_asset_prop(asset_obj, prop_name, unit_type=None):
    if not hasattr(asset_obj, prop_name):
        return None
    val = getattr(asset_obj, prop_name)
    if val is None:
        return None
    val = _xyz_to_float(val)
    if unit_type is not None:
        try:
            val = DB.UnitUtils.ConvertFromInternalUnits(float(val), unit_type)
        except Exception:
            pass
    if isinstance(val, float):
        val = _clean_float(val)
    return val


def _json_default(obj):
    try:
        if hasattr(obj, 'X') and hasattr(obj, 'Y') and hasattr(obj, 'Z'):
            return float(obj.X)
    except Exception:
        pass
    try:
        return float(obj)
    except Exception:
        pass
    return str(obj)


def get_all_model_categories(doc):
    cats = []
    for cat in doc.Settings.Categories:
        try:
            if cat.CategoryType == DB.CategoryType.Model:
                cats.append(cat)
        except Exception:
            pass
    return sorted(cats, key=lambda c: c.Name)


# ────────────────────────────────────────────────────────────────
# Shared parameter
# ────────────────────────────────────────────────────────────────
def ensure_context_parameter_exists(doc):
    binding_map = doc.ParameterBindings
    iterator = binding_map.ForwardIterator()
    while iterator.MoveNext():
        if iterator.Key.Name == CONTEXT_PARAM:
            return True

    app = doc.Application
    original_sp = app.SharedParametersFilename
    temp_sp = os.path.join(tempfile.gettempdir(),
                           "LCiA_temp_{}.txt".format(Guid.NewGuid().ToString("N")))
    try:
        with io.open(temp_sp, "w", encoding="utf-8") as f:
            f.write(u"# This is a Revit shared parameter file.\n")
            f.write(u"*META\tVERSION\tMINVERSION\n")
            f.write(u"META\t2\t1\n")
            f.write(u"*GROUP\tID\tNAME\n")
            f.write(u"GROUP\t1\tLCiA_Params\n")
            f.write(u"*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE\tDESCRIPTION\tUSERMODIFIABLE\tHIDEWHENNOVALUE\n")

        app.SharedParametersFilename = temp_sp
        sp_file = app.OpenSharedParameterFile()
        if not sp_file:
            return False

        group = sp_file.Groups.get_Item("LCiA_Params") or sp_file.Groups.Create("LCiA_Params")

        defn = group.Definitions.get_Item(CONTEXT_PARAM)
        if not defn:
            try:
                opt  = DB.ExternalDefinitionCreationOptions(CONTEXT_PARAM, DB.SpecTypeId.String.Text)
            except AttributeError:
                opt  = DB.ExternalDefinitionCreationOptions(CONTEXT_PARAM, DB.ParameterType.Text)
            defn = group.Definitions.Create(opt)

        cat_set = app.Create.NewCategorySet()
        cat_set.Insert(DB.Category.GetCategory(doc, DB.BuiltInCategory.OST_Materials))
        binding = app.Create.NewInstanceBinding(cat_set)

        with Transaction(doc, "Create LCA_Context Parameter") as t:
            t.Start()
            try:
                doc.ParameterBindings.Insert(defn, binding, DB.GroupTypeId.IdentityData)
            except AttributeError:
                try:
                    doc.ParameterBindings.Insert(defn, binding,
                                                  DB.BuiltInParameterGroup.PG_IDENTITY_DATA)
                except Exception:
                    doc.ParameterBindings.Insert(defn, binding)
            t.Commit()
        return True

    except Exception as ex:
        logger.error("Failed to create LCA_Context param: {}".format(ex))
        return False
    finally:
        try:
            app.SharedParametersFilename = original_sp if (original_sp and os.path.exists(original_sp)) else ""
        except Exception:
            pass
        try:
            if os.path.exists(temp_sp):
                os.remove(temp_sp)
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────
# Element collection
# ────────────────────────────────────────────────────────────────
def _collect_elements(scope, cat_ids=None, picked_elements=None):
    if scope == "Pick Elements":
        return list(picked_elements) if picked_elements else []

    if scope == "Active View":
        raw = FilteredElementCollector(doc, doc.ActiveView.Id) \
                  .WhereElementIsNotElementType().ToElements()
    elif scope == "By Category" and cat_ids:
        from System.Collections.Generic import List as NetList
        id_list = NetList[DB.ElementId]()
        for cid in cat_ids:
            id_list.Add(DB.ElementId(cid))
        multi_filter = DB.ElementMulticategoryFilter(id_list)
        raw = FilteredElementCollector(doc).WherePasses(multi_filter) \
                  .WhereElementIsNotElementType().ToElements()
    else:  # Entire Project
        raw = FilteredElementCollector(doc) \
                  .WhereElementIsNotElementType().ToElements()

    rvt_links_int = int(DB.BuiltInCategory.OST_RvtLinks)
    return [e for e in raw
            if e.Category and e.Category.Id.IntegerValue != rvt_links_int]


# ────────────────────────────────────────────────────────────────
# Material → host-element mapping
# ────────────────────────────────────────────────────────────────
def _build_mat_host_map(elements):
    mat_id_to_hosts = {}

    with forms.ProgressBar(title="Scanning Elements ({value} of {max_value})",
                           cancellable=True) as pb:
        total = len(elements)
        for i, elem in enumerate(elements):
            if pb.cancelled:
                return None
            if i % 50 == 0:
                pb.update_progress(i, total)
            if not elem:
                continue
            try:
                try:
                    if hasattr(elem, "CurtainGrid") and elem.CurtainGrid is not None:
                        continue
                except Exception:
                    pass

                try:
                    cat = elem.Category
                    cat_name = cat.Name if cat else ""
                except Exception:
                    cat_name = ""
                if not cat_name:
                    continue

                type_name = ""
                try:
                    et = doc.GetElement(elem.GetTypeId())
                    if et:
                        try:
                            type_name = et.Name or ""
                        except Exception:
                            pass
                        if not type_name:
                            try:
                                p = et.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
                                if p:
                                    type_name = p.AsString() or ""
                            except Exception:
                                pass
                        if not type_name:
                            try:
                                p = et.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
                                if p:
                                    type_name = p.AsString() or ""
                            except Exception:
                                pass
                except Exception:
                    pass
                if not type_name:
                    try:
                        p = elem.get_Parameter(DB.BuiltInParameter.ELEM_TYPE_PARAM)
                        if p:
                            te = doc.GetElement(p.AsElementId())
                            if te:
                                type_name = te.Name or ""
                    except Exception:
                        pass

                valid_ids = set()

                try:
                    if hasattr(elem, "GetCompoundStructure"):
                        cs = elem.GetCompoundStructure()
                        if cs:
                            for li in range(cs.LayerCount):
                                mid = cs.GetMaterialId(li)
                                if mid and mid.IntegerValue > 0:
                                    valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass

                try:
                    et2 = doc.GetElement(elem.GetTypeId())
                    if et2 and hasattr(et2, "GetCompoundStructure"):
                        cs2 = et2.GetCompoundStructure()
                        if cs2:
                            for li in range(cs2.LayerCount):
                                mid = cs2.GetMaterialId(li)
                                if mid and mid.IntegerValue > 0:
                                    valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass

                try:
                    sp = elem.get_Parameter(DB.BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                    if sp and sp.AsElementId().IntegerValue > 0:
                        valid_ids.add(int(sp.AsElementId().IntegerValue))
                except Exception:
                    pass

                try:
                    for mid in elem.GetMaterialIds(False):
                        if mid and mid.IntegerValue > 0:
                            valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass

                try:
                    for mid in elem.GetMaterialIds(True):
                        if mid and mid.IntegerValue > 0:
                            valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass

                try:
                    for target in [elem, doc.GetElement(elem.GetTypeId())]:
                        if not target:
                            continue
                        for param in target.Parameters:
                            try:
                                if param.StorageType == DB.StorageType.ElementId:
                                    pid = param.AsElementId()
                                    if pid and pid.IntegerValue > 0:
                                        candidate = doc.GetElement(pid)
                                        if candidate and isinstance(candidate, DB.Material):
                                            valid_ids.add(int(pid.IntegerValue))
                            except Exception:
                                pass
                except Exception:
                    pass

                for mid_int in valid_ids:
                    if mid_int not in mat_id_to_hosts:
                        mat_id_to_hosts[mid_int] = {"categories": set(), "types": set()}
                    mat_id_to_hosts[mid_int]["categories"].add(cat_name)
                    if type_name:
                        mat_id_to_hosts[mid_int]["types"].add(type_name)

            except Exception:
                continue

    return mat_id_to_hosts


# ────────────────────────────────────────────────────────────────
# Filter helper - produces export-ready dict from full material data
# ────────────────────────────────────────────────────────────────
_IDENTITY_MAP = {
    "identity.class":        "class",
    "identity.description":  "description",
    "identity.keywords":     "keywords",
    "identity.subclass":     "subclass",
    "identity.type":         "type",
    "identity.manufacturer": "manufacturer",
    "identity.model":        "model",
    "identity.source":       "source",
    "identity.url":          "url",
}
_PHYSICAL_MAP = {
    "physical.density_kg_m3":           "density_kg_m3",
    "physical.structural_class":         "structural_class",
    "physical.concrete_compression_mpa": "concrete_compression_mpa",
    "physical.lightweight":              "lightweight",
    "physical.youngs_modulus_mpa":       "youngs_modulus_mpa",
    "physical.poissons_ratio":           "poissons_ratio",
    "physical.shear_modulus_mpa":        "shear_modulus_mpa",
    "physical.damping_ratio":            "damping_ratio",
    "physical.yield_strength_mpa":       "yield_strength_mpa",
    "physical.tensile_strength_mpa":     "tensile_strength_mpa",
    "physical.thermal_expansion_inv_C":  "thermal_expansion_inv_C",
    "physical.shear_strength_reduction": "shear_strength_reduction",
    "physical.reduction_factor":         "reduction_factor",
}
_THERMAL_MAP = {
    "thermal.thermal_conductivity_w_mk":    "thermal_conductivity_w_mk",
    "thermal.density_kg_m3":                "density_kg_m3",
    "thermal.specific_heat_j_gC":           "specific_heat_j_gC",
    "thermal.emissivity":                   "emissivity",
    "thermal.permeability_ng_Pas_m2":       "permeability_ng_Pas_m2",
    "thermal.porosity":                     "porosity",
    "thermal.reflectivity":                 "reflectivity",
    "thermal.electrical_resistivity_ohm_m": "electrical_resistivity_ohm_m",
    "thermal.thermal_class":                "thermal_class",
    "thermal.transmits_light":              "transmits_light",
}


def _filter_data(full_data, selected_keys):
    """Return a filtered copy of material data containing only selected fields.

    revit_id and name are always present regardless of selection.
    The full data is still written to the LCA_Context Revit parameter (unfiltered).
    """
    out = OrderedDict()
    out["revit_id"] = full_data["revit_id"]
    out["name"]     = full_data["name"]   # always included

    # Identity scalar fields
    for key, field in _IDENTITY_MAP.items():
        if key in selected_keys:
            v = full_data.get(field, "")
            if v:
                out[field] = v

    # Context / host elements
    host_cats  = "context.host_categories" in selected_keys
    host_types = "context.host_types"      in selected_keys
    if host_cats or host_types:
        raw = full_data.get("host_elements", {})
        host = {}
        if host_cats:
            host["categories"] = raw.get("categories", [])
        if host_types:
            host["types"] = raw.get("types", [])
        out["host_elements"] = host

    # Physical asset
    full_phys = full_data.get("physical_asset", {})
    phys = OrderedDict(
        (field, full_phys[field])
        for key, field in _PHYSICAL_MAP.items()
        if key in selected_keys and field in full_phys
    )
    if phys:
        out["physical_asset"] = phys

    # Thermal asset
    full_therm = full_data.get("thermal_asset", {})
    therm = OrderedDict(
        (field, full_therm[field])
        for key, field in _THERMAL_MAP.items()
        if key in selected_keys and field in full_therm
    )
    if therm:
        out["thermal_asset"] = therm

    return out


# ────────────────────────────────────────────────────────────────
# Material property extraction
# ────────────────────────────────────────────────────────────────
def _extract_materials(mat_id_to_hosts, selected_keys):
    """
    Iterate all materials in the document.
    - Writes FULL context to LCA_Context Revit parameter (always complete).
    - Returns list of dicts filtered to selected_keys for JSON export.
    """
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR)

    ensure_context_parameter_exists(doc)
    all_mats  = FilteredElementCollector(doc).OfClass(DB.Material).ToElements()
    materials = [m for m in all_mats if int(m.Id.IntegerValue) in mat_id_to_hosts]
    all_data_filtered = []

    with Transaction(doc, "Extract Material Context") as t:
        t.Start()
        with forms.ProgressBar(title="Extracting Materials ({value} of {max_value})",
                               cancellable=True) as pb:
            total = len(materials)
            for i, mat in enumerate(materials):
                if pb.cancelled:
                    t.RollBack()
                    return None
                pb.update_progress(i, total)

                mat_int_id = int(mat.Id.IntegerValue)
                _usage     = mat_id_to_hosts.get(mat_int_id, {})

                # ── Build full data dict ─────────────────────────────────────────
                data = OrderedDict()
                data["revit_id"]    = mat_int_id
                data["name"]        = mat.Name
                data["class"]       = mat.MaterialClass or ""
                data["description"] = _get_param_value(mat, DB.BuiltInParameter.ALL_MODEL_DESCRIPTION) or ""
                data["keywords"]    = _get_string_by_name(mat, "Keywords") or ""
                data["manufacturer"]= _get_param_value(mat, DB.BuiltInParameter.ALL_MODEL_MANUFACTURER) or ""
                data["model"]       = _get_param_value(mat, DB.BuiltInParameter.ALL_MODEL_MODEL) or ""
                data["source"]      = _get_string_by_name(mat, "Source") or ""
                data["subclass"]    = _get_string_by_name(mat, "Subclass") or ""
                data["type"]        = _get_string_by_name(mat, "Type") or ""
                _url = _get_string_by_name(mat, "URL") or ""
                if not _url:
                    try:
                        _url = _get_param_value(mat, DB.BuiltInParameter.ALL_MODEL_URL) or ""
                    except Exception:
                        _url = ""
                data["url"] = _url

                if not data["keywords"]:
                    try:
                        pk = mat.get_Parameter(DB.BuiltInParameter.PROPERTY_SET_KEYWORDS)
                        if pk:
                            data["keywords"] = pk.AsString() or ""
                    except Exception:
                        pass

                # Host elements
                data["host_elements"] = {
                    "categories": sorted(_usage.get("categories", [])),
                    "types":      sorted(_usage.get("types", []))
                }

                # ── Physical asset ───────────────────────────────────────────────
                phys = OrderedDict()
                struct_asset_id = mat.StructuralAssetId
                if struct_asset_id != DB.ElementId.InvalidElementId:
                    pse = doc.GetElement(struct_asset_id)
                    if pse:
                        phys["name"]       = pse.Name or ""
                        phys["description"]= _get_string_by_name(pse, "Description") or ""
                        phys["keywords"]   = _get_string_by_name(pse, "Keywords") or ""
                        phys["type"]       = _get_string_by_name(pse, "Type") or ""
                        phys["subclass"]   = _get_string_by_name(pse, "Subclass") or ""
                        phys["source"]     = _get_string_by_name(pse, "Source") or ""
                        phys["source_url"] = _get_string_by_name(pse, "Source URL") or ""

                        asset = pse.GetStructuralAsset()
                        if asset:
                            asset_class_str = ""
                            try:
                                asset_class_str = str(asset.StructuralAssetClass)
                                phys["structural_class"] = asset_class_str
                            except Exception:
                                pass
                            try:
                                phys["behavior"] = str(asset.Behavior)
                            except Exception:
                                pass

                            is_gas_or_liquid = asset_class_str in ("Gas", "Liquid")
                            is_concrete = (asset_class_str == "Concrete")
                            is_metal    = (asset_class_str == "Metal")

                            try:
                                tec = asset.ThermalExpansionCoefficient
                                if tec is not None:
                                    try:
                                        tec = DB.UnitUtils.ConvertFromInternalUnits(
                                            tec, DB.UnitTypeId.InverseDegreesCelsius)
                                    except Exception:
                                        tec = tec * 1.8
                                    phys["thermal_expansion_inv_C"] = _clean_float(tec)
                            except Exception:
                                pass

                            try:
                                d_param = pse.get_Parameter(
                                    DB.BuiltInParameter.PHY_MATERIAL_PARAM_STRUCTURAL_DENSITY)
                                if d_param:
                                    phys["density_kg_m3"] = _clean_float(
                                        DB.UnitUtils.ConvertFromInternalUnits(
                                            d_param.AsDouble(),
                                            DB.UnitTypeId.KilogramsPerCubicMeter))
                                else:
                                    phys["density_kg_m3"] = _clean_float(
                                        float(_xyz_to_float(asset.Density)))
                            except Exception:
                                pass

                            if not is_gas_or_liquid:
                                try:
                                    v = _safe_asset_prop(asset, "YoungModulus", None)
                                    if v is not None:
                                        try:
                                            phys["youngs_modulus_mpa"] = _clean_float(
                                                DB.UnitUtils.ConvertFromInternalUnits(
                                                    float(v), DB.UnitTypeId.Megapascals))
                                        except Exception:
                                            phys["youngs_modulus_mpa"] = _clean_float(float(v))
                                except Exception:
                                    pass
                                try:
                                    pr = asset.PoissonRatio
                                    pr_val = float(pr.X) if hasattr(pr, 'X') else float(pr)
                                    if pr_val:
                                        phys["poissons_ratio"] = _clean_float(pr_val)
                                except Exception:
                                    pass
                                try:
                                    v = _safe_asset_prop(asset, "ShearModulus", None)
                                    if v is not None:
                                        try:
                                            phys["shear_modulus_mpa"] = _clean_float(
                                                DB.UnitUtils.ConvertFromInternalUnits(
                                                    float(v), DB.UnitTypeId.Megapascals))
                                        except Exception:
                                            phys["shear_modulus_mpa"] = _clean_float(float(v))
                                except Exception:
                                    pass
                                try:
                                    d = float(_xyz_to_float(asset.Damping))
                                    if d:
                                        phys["damping_ratio"] = _clean_float(d)
                                except Exception:
                                    pass

                            if is_concrete:
                                try:
                                    phys["concrete_compression_mpa"] = _clean_float(
                                        DB.UnitUtils.ConvertFromInternalUnits(
                                            asset.ConcreteCompression, DB.UnitTypeId.Megapascals))
                                except Exception:
                                    pass
                                try:
                                    phys["lightweight"] = asset.Lightweight
                                except Exception:
                                    pass
                                try:
                                    phys["shear_strength_reduction"] = _clean_float(
                                        asset.ConcreteShearStrengthReduction)
                                except Exception:
                                    pass

                            if not is_gas_or_liquid:
                                try:
                                    phys["yield_strength_mpa"] = _clean_float(
                                        DB.UnitUtils.ConvertFromInternalUnits(
                                            asset.MinimumYieldStress, DB.UnitTypeId.Megapascals))
                                except Exception:
                                    pass
                                try:
                                    phys["tensile_strength_mpa"] = _clean_float(
                                        DB.UnitUtils.ConvertFromInternalUnits(
                                            asset.MinimumTensileStrength, DB.UnitTypeId.Megapascals))
                                except Exception:
                                    pass

                            if is_metal:
                                try:
                                    rf = float(_xyz_to_float(asset.ReductionFactor))
                                    if rf:
                                        phys["reduction_factor"] = _clean_float(rf)
                                except Exception:
                                    pass

                data["physical_asset"] = phys

                # ── Thermal asset ────────────────────────────────────────────────
                therm = OrderedDict()
                thermal_asset_id = mat.ThermalAssetId
                if thermal_asset_id != DB.ElementId.InvalidElementId:
                    pse = doc.GetElement(thermal_asset_id)
                    if pse:
                        therm["name"]        = pse.Name or ""
                        therm["description"] = _get_string_by_name(pse, "Description") or ""
                        therm["keywords"]    = _get_string_by_name(pse, "Keywords") or ""
                        therm["type"]        = _get_string_by_name(pse, "Type") or ""
                        therm["subclass"]    = _get_string_by_name(pse, "Subclass") or ""
                        therm["source"]      = _get_string_by_name(pse, "Source") or ""
                        therm["source_url"]  = _get_string_by_name(pse, "Source URL") or ""
                        asset = pse.GetThermalAsset()
                        if asset:
                            try:
                                therm["thermal_class"] = str(asset.ThermalMaterialType)
                            except Exception:
                                pass
                            try:
                                therm["behavior"] = ("Isotropic" if asset.IsIsotropic
                                                     else "Orthotropic")
                            except Exception:
                                pass
                            try:
                                therm["transmits_light"] = asset.TransmitsLight
                            except Exception:
                                pass

                            def _tconv(prop, key, unit=None):
                                v = _safe_asset_prop(asset, prop, unit)
                                if v is not None:
                                    therm[key] = v

                            _tconv("ThermalConductivity", "thermal_conductivity_w_mk",
                                   DB.UnitTypeId.WattsPerMeterKelvin)
                            _tconv("SpecificHeat",        "specific_heat_j_gC",
                                   DB.UnitTypeId.JoulesPerGramDegreeCelsius)
                            _tconv("Density",             "density_kg_m3",
                                   DB.UnitTypeId.KilogramsPerCubicMeter)
                            _tconv("Emissivity",          "emissivity")
                            _tconv("Permeability",        "permeability_ng_Pas_m2",
                                   DB.UnitTypeId.NanogramsPerPascalSecondSquareMeter)
                            _tconv("Porosity",            "porosity")
                            if "porosity" in therm and isinstance(therm["porosity"], float):
                                therm["porosity"] = _clean_float(therm["porosity"] * 10.0)
                            _tconv("Reflectivity",        "reflectivity")
                            _tconv("ElectricalResistivity","electrical_resistivity_ohm_m",
                                   DB.UnitTypeId.OhmMeters)
                data["thermal_asset"] = therm

                # ── Filter to user-selected properties ──────────────────────────
                filtered = _filter_data(data, selected_keys)

                # ── Write filtered data to LCA_Context parameter ─────────────────
                try:
                    raw_str   = json.dumps(filtered, ensure_ascii=False, default=_json_default)
                    clean_str = re.sub(
                        r"(-?\d+\.\d+)",
                        lambda m: "{0:.4f}".format(round(float(m.group(1)), 4)
                                                   ).rstrip("0").rstrip(".") or "0",
                        raw_str)
                    param = mat.LookupParameter(CONTEXT_PARAM)
                    if param and not param.IsReadOnly:
                        param.Set(clean_str)
                except Exception:
                    pass

                # ── Filtered dict for JSON export ────────────────────────────────
                all_data_filtered.append(filtered)

        t.Commit()
    return all_data_filtered


# ────────────────────────────────────────────────────────────────
# Pick elements helper
# ────────────────────────────────────────────────────────────────
def _pick_elements():
    try:
        refs = uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            "Select elements to extract material context from")
        if not refs:
            return None
        return [doc.GetElement(r.ElementId) for r in refs]
    except Exception as ex:
        msg = str(ex).lower()
        if "cancel" in msg or "abort" in msg or "OperationCanceledException" in str(type(ex)):
            return None
        raise


# ────────────────────────────────────────────────────────────────
# JSON export
# ────────────────────────────────────────────────────────────────
def _do_export(all_data):
    try:
        raw = json.dumps(all_data, ensure_ascii=False, indent=4, default=_json_default)
        clean = re.sub(
            r"(-?\d+\.\d+)",
            lambda m: "{0:.4f}".format(round(float(m.group(1)), 4)
                                       ).rstrip("0").rstrip(".") or "0",
            raw)
        with io.open(JSON_EXPORT_PATH, "w", encoding="utf-8") as f:
            f.write(unicode(clean))
        return JSON_EXPORT_PATH
    except Exception as ex:
        logger.error(traceback.format_exc())
        return None


# ────────────────────────────────────────────────────────────────
# Category filter helpers
# ────────────────────────────────────────────────────────────────
def _fuzzy_match(query, text):
    q = query.lower()
    t = text.lower()
    return all(c in t for c in q.split()) or q in t


def _regex_match(query, text):
    try:
        return bool(re.search(query, text, re.IGNORECASE))
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────
# WPF Window
# ────────────────────────────────────────────────────────────────
class ExtractContextWindow(forms.WPFWindow):
    def __init__(self):
        self._init_complete   = False
        self._pick_requested  = False
        self._should_extract  = False
        self._scope           = "Active View"
        self._cat_ids         = []
        self._search_mode     = "fuzzy"
        self._all_category_checkboxes = []
        self._property_items  = []
        self._selected_keys   = set()

        forms.WPFWindow.__init__(self, os.path.join(PATH_SCRIPT, XAML_FILE))
        self.Title = __title__
        self.text_output_path.Text = "Output: {}".format(JSON_EXPORT_PATH)
        self._load_categories()
        self._load_properties()
        self._update_count_label()
        self._init_complete = True

    # ── Property selector grid ────────────────────────────────────
    def _load_properties(self):
        """Build PropertyItem list from PROPERTY_DEFINITIONS and set as DataGrid ItemsSource."""
        self._property_items = [
            PropertyItem(key, cat, name, example, desc, rec)
            for key, cat, name, example, desc, rec in PROPERTY_DEFINITIONS
        ]
        self.dg_properties.ItemsSource = self._property_items
        self._apply_grouping()

    def _apply_grouping(self):
        """Group the DataGrid rows by Category."""
        try:
            view = CollectionViewSource.GetDefaultView(self.dg_properties.ItemsSource)
            if view:
                view.GroupDescriptions.Clear()
                view.GroupDescriptions.Add(PropertyGroupDescription("Category"))
        except Exception:
            pass

    def _update_count_label(self):
        total    = len(self._property_items)
        selected = sum(1 for item in self._property_items if item.IsSelected)
        self.text_selected_count.Text = "({} / {} selected)".format(selected, total)

    def _refresh_grid(self):
        """Reset ItemsSource to force DataGrid to re-read all IsSelected states."""
        self.dg_properties.ItemsSource = None
        self.dg_properties.ItemsSource = self._property_items
        self._apply_grouping()
        self._update_count_label()

    def on_checkbox_changed(self, sender, e):
        """Called when any export checkbox is toggled - update the count label."""
        self._update_count_label()

    def on_recommended_click(self, sender, e):
        for item in self._property_items:
            item.IsSelected = item.Recommended
        self._refresh_grid()

    def on_select_all_click(self, sender, e):
        for item in self._property_items:
            item.IsSelected = True
        self._refresh_grid()

    def on_deselect_all_click(self, sender, e):
        for item in self._property_items:
            item.IsSelected = False
        self._refresh_grid()

    def _set_all_groups_expanded(self, is_expanded):
        """Walk the DataGrid visual tree and set all Expander controls."""
        try:
            def walk(element):
                if isinstance(element, Expander):
                    element.IsExpanded = is_expanded
                count = VisualTreeHelper.GetChildrenCount(element)
                for i in range(count):
                    walk(VisualTreeHelper.GetChild(element, i))
            walk(self.dg_properties)
        except Exception:
            pass

    def on_expand_all(self, _sender, _e):
        self._set_all_groups_expanded(True)

    def on_collapse_all(self, _sender, _e):
        self._set_all_groups_expanded(False)

    # ── Category panel ────────────────────────────────────────────
    def _load_categories(self):
        cats = get_all_model_categories(doc)
        for cat in cats:
            cb = WPFCheckBox()
            cb.Content = cat.Name
            cb.Tag     = cat.Id.IntegerValue
            cb.Margin  = Thickness(20, 0, 0, 4)
            cb.Checked   += self.checkbox_category_checked
            cb.Unchecked += self.checkbox_category_checked
            self._all_category_checkboxes.append(cb)
            self.stackpanel_categories.Children.Add(cb)

    def toggle_search_mode(self, sender, e):
        self._search_mode = "regex" if self._search_mode == "fuzzy" else "fuzzy"
        self.btn_searchmode.Content = "AZ" if self._search_mode == "fuzzy" else ".*"
        self.textbox_category_filter_updated(None, None)

    def textbox_category_filter_updated(self, sender, e):
        query = self.textbox_category_filter.Text or ""
        for cb in self._all_category_checkboxes:
            if not query:
                cb.Visibility = Visibility.Visible
            else:
                match = (_fuzzy_match(query, cb.Content)
                         if self._search_mode == "fuzzy"
                         else _regex_match(query, cb.Content))
                cb.Visibility = Visibility.Visible if match else Visibility.Collapsed

    def checkbox_all_categories_changed(self, sender, e):
        checked = self.checkbox_all_categories.IsChecked
        for cb in self._all_category_checkboxes:
            if cb.Visibility == Visibility.Visible:
                cb.IsChecked = checked

    def checkbox_category_checked(self, sender, e):
        pass  # read at extract time

    # ── Scope ─────────────────────────────────────────────────────
    def radio_scope_changed(self, sender, e):
        if not self._init_complete:
            return
        if self.rb_by_category.IsChecked:
            self.col_categories.Width = GridLength(320)
            self.col_splitter.Width   = GridLength(6)
            self.grid_category_filter.Visibility      = Visibility.Visible
            self.grid_categories_splitter.Visibility  = Visibility.Visible
        else:
            self.col_categories.Width = GridLength(0)
            self.col_splitter.Width   = GridLength(0)
            self.grid_category_filter.Visibility      = Visibility.Collapsed
            self.grid_categories_splitter.Visibility  = Visibility.Collapsed

        if self.rb_pick_elements.IsChecked:
            self._trigger_pick()

    def _trigger_pick(self):
        """Collect selected keys then close for pick-elements flow."""
        self._selected_keys = set(
            item.Key for item in self._property_items if item.IsSelected
        ) | ALWAYS_EXPORT
        if not self._selected_keys - ALWAYS_EXPORT:
            forms.alert("Please select at least one property to export.",
                        title=__title__)
            self.rb_active_view.IsChecked = True
            return
        self._pick_requested = True
        self.Close()

    # ── Extract button ────────────────────────────────────────────
    def on_extract_click(self, sender, e):
        # Collect selected property keys from grid
        self._selected_keys = set(
            item.Key for item in self._property_items if item.IsSelected
        ) | ALWAYS_EXPORT   # name is always included

        if not self._selected_keys - ALWAYS_EXPORT:
            forms.alert("Please select at least one property to export.",
                        title=__title__)
            return

        if self.rb_active_view.IsChecked:
            self._scope = "Active View"
        elif self.rb_entire_project.IsChecked:
            self._scope = "Entire Project"
        elif self.rb_by_category.IsChecked:
            self._scope = "By Category"
            self._cat_ids = [cb.Tag for cb in self._all_category_checkboxes
                             if cb.IsChecked]
            if not self._cat_ids:
                forms.alert("Please select at least one category.", title=__title__)
                return
        elif self.rb_pick_elements.IsChecked:
            self._pick_requested = True
            self.Close()
            return

        self._should_extract = True
        self.Close()


# ────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────
def main():
    picked_elements = None

    while True:
        win = ExtractContextWindow()
        win.ShowDialog()

        if win._pick_requested:
            picked_elements = _pick_elements()
            if picked_elements is None:
                continue
            mat_id_to_hosts = _build_mat_host_map(picked_elements)
            if mat_id_to_hosts is None:
                break
            all_data = _extract_materials(mat_id_to_hosts, win._selected_keys)
            if all_data is None:
                break
            path = _do_export(all_data)
            if path:
                forms.alert(
                    "Extracted {} materials from {} picked elements.\n"
                    "Saved to:\n{}".format(len(all_data), len(picked_elements), path),
                    title=__title__)
            else:
                forms.alert("Material context written to parameters but JSON export failed.",
                            title=__title__)
            break

        elif win._should_extract:
            elements = _collect_elements(win._scope, cat_ids=win._cat_ids)
            if not elements:
                forms.alert("No elements found for the selected scope.", title=__title__)
                continue

            mat_id_to_hosts = _build_mat_host_map(elements)
            if mat_id_to_hosts is None:
                break
            all_data = _extract_materials(mat_id_to_hosts, win._selected_keys)
            if all_data is None:
                break
            path = _do_export(all_data)
            if path:
                forms.alert(
                    "Extracted {} materials.\nSaved to:\n{}".format(len(all_data), path),
                    title=__title__)
            else:
                forms.alert("Material context written to parameters but JSON export failed.",
                            title=__title__)
            break

        else:
            break


if __name__ == "__main__":
    main()
