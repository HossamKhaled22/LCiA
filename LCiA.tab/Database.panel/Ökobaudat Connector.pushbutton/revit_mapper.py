# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: GPL-3.0-or-later
"""
RevitMaterialMapper -- creates shared parameters and writes LCA data
(UUID + UUID_Data) to Revit materials.
"""
#pylint: disable=import-error,invalid-name,broad-except
import os
import io
import json
import tempfile

from System import Guid
from pyrevit import forms, DB
from Autodesk.Revit.DB import Transaction

import constants


class RevitMaterialMapper(object):
    """Handles all Revit API interaction for the MaterialsDatasets tool:
    - auto-creating the UUID / UUID_Data shared parameters if missing
    - batch-writing LCA data from a MaterialDataset to selected materials
    """

    def __init__(self, doc, logger):
        """
        Args:
            doc:    The active Revit Document.
            logger: A PyRevit logger instance for error reporting.
        """
        self._doc    = doc
        self._logger = logger

    # ── Parameter creation ────────────────────────────────────────

    def ensure_parameters_exist(self):
        """Ensure that 'UUID' and 'UUID_Data' exist as project parameters for Materials.

        If either parameter is missing it is created automatically using a
        temporary Revit Shared Parameter file.  The original SharedParametersFilename
        is restored and the temp file is deleted in the finally block.

        Returns True on success, False if creation failed.
        """
        doc = self._doc
        binding_map = doc.ParameterBindings
        iterator = binding_map.ForwardIterator()
        found_uuid = False
        found_data = False
        while iterator.MoveNext():
            definition = iterator.Key
            if definition.Name == constants.UUID_PARAM_NAME:
                found_uuid = True
            elif definition.Name == constants.UUID_DATA_PARAM_NAME:
                found_data = True

        if found_uuid and found_data:
            return True

        app = doc.Application
        original_sp_file = app.SharedParametersFilename
        temp_sp_file = os.path.join(
            tempfile.gettempdir(),
            "LCiA_temp_{}.txt".format(Guid.NewGuid().ToString("N"))
        )

        try:
            with io.open(temp_sp_file, "w", encoding="utf-8") as f:
                f.write(u"# This is a Revit shared parameter file.\n")
                f.write(u"*META\tVERSION\tMINVERSION\n")
                f.write(u"META\t2\t1\n")
                f.write(u"*GROUP\tID\tNAME\n")
                f.write(u"GROUP\t1\tLCiA_Params\n")
                f.write(u"*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE\tDESCRIPTION\tUSERMODIFIABLE\tHIDEWHENNOVALUE\n")
        except Exception as ex:
            self._logger.error("Failed to write temp SP file: {}".format(ex))
            return False

        try:
            app.SharedParametersFilename = temp_sp_file
            sp_file = app.OpenSharedParameterFile()
            if not sp_file:
                return False

            group = sp_file.Groups.get_Item("LCiA_Params")
            if not group:
                group = sp_file.Groups.Create("LCiA_Params")

            success = True
            with Transaction(doc, "Create LCiA Material Parameters") as t:
                t.Start()
                if not found_uuid:
                    if not self._create_param_binding(group, app, constants.UUID_PARAM_NAME):
                        success = False
                if not found_data:
                    if not self._create_param_binding(group, app, constants.UUID_DATA_PARAM_NAME):
                        success = False
                t.Commit()

            return success
        except Exception as ex:
            self._logger.error("Failed to create parameters: {}".format(ex))
            return False
        finally:
            if original_sp_file and os.path.exists(original_sp_file):
                try:
                    app.SharedParametersFilename = original_sp_file
                except Exception:
                    pass
            else:
                try:
                    app.SharedParametersFilename = ""
                except Exception:
                    pass
            try:
                if os.path.exists(temp_sp_file):
                    os.remove(temp_sp_file)
            except Exception:
                pass

    def _create_param_binding(self, group, app, p_name):
        """Create one shared parameter definition and bind it to the Materials category."""
        doc = self._doc
        defn = group.Definitions.get_Item(p_name)
        if not defn:
            try:
                # Revit 2022+
                opt = DB.ExternalDefinitionCreationOptions(p_name, DB.SpecTypeId.String.Text)
            except AttributeError:
                # Older Revit
                opt = DB.ExternalDefinitionCreationOptions(p_name, DB.ParameterType.Text)
            defn = group.Definitions.Create(opt)

        cat_set = app.Create.NewCategorySet()
        cat = DB.Category.GetCategory(doc, DB.BuiltInCategory.OST_Materials)
        cat_set.Insert(cat)
        binding = app.Create.NewInstanceBinding(cat_set)

        try:
            # Revit 2024+
            res = doc.ParameterBindings.Insert(defn, binding, DB.GroupTypeId.IdentityData)
        except AttributeError:
            # Older Revit
            res = doc.ParameterBindings.Insert(defn, binding, DB.BuiltInParameterGroup.PG_IDENTITY_DATA)
        except Exception:
            res = doc.ParameterBindings.Insert(defn, binding)
        return res

    # ── Parameter lookup ──────────────────────────────────────────

    def _get_param(self, mat, pname):
        """Look up a parameter on a material by name; returns None if not found."""
        try:
            p = mat.LookupParameter(pname)
            if p:
                return p
        except Exception:
            pass
        try:
            ps = mat.GetParameters(pname)
            if ps and len(ps) > 0:
                return ps[0]
        except Exception:
            pass
        return None

    # ── Batch mapping ─────────────────────────────────────────────

    def map_dataset(self, materials, dataset):
        """Write UUID and UUID_Data parameters from *dataset* to each material in *materials*.

        Returns:
            (mapped_names, failed_reasons)
            mapped_names:   list of successfully mapped material names
            failed_reasons: list of (name, reason) tuples for failures
        """
        if not materials:
            return [], []
        if not dataset or not dataset.uuid:
            forms.alert("Selected dataset has no UUID.", title=constants.TOOL_TITLE)
            return [], []

        self.ensure_parameters_exist()

        try:
            json_str = json.dumps(dataset.to_dict(), indent=2, ensure_ascii=False)
        except Exception:
            json_str = json.dumps(dataset.to_dict(), indent=2)

        # Write the full ÖKOBAUDAT classification path (translated to English,
        # joined with underscores) to Revit's Material.MaterialClass property.
        # Example: "Mineral Construction_Mortar & Concrete_Concrete".
        # Computed at dataset-load time in window.py via
        # attribute_extractor.format_revit_material_class(); falls back to
        # "Generic" when the classification is empty.
        class_label = getattr(dataset, "RevitMaterialClass", "") or "Generic"
        mapped  = []
        failed  = []

        try:
            with Transaction(self._doc, u"Map \u00d6KOBAUDAT Dataset to Materials") as t:
                t.Start()
                for mat in materials:
                    try:
                        uuid_param = self._get_param(mat, constants.UUID_PARAM_NAME)
                        if not uuid_param or uuid_param.IsReadOnly:
                            failed.append((mat.Name, "No writable '{}' parameter".format(
                                constants.UUID_PARAM_NAME)))
                            continue
                        uuid_param.Set(dataset.uuid)

                        data_param = self._get_param(mat, constants.UUID_DATA_PARAM_NAME)
                        if data_param and not data_param.IsReadOnly:
                            data_param.Set(json_str)
                        else:
                            failed.append((mat.Name, "No writable '{}' parameter".format(
                                constants.UUID_DATA_PARAM_NAME)))

                        try:
                            if hasattr(mat, "MaterialClass"):
                                mat.MaterialClass = class_label
                            else:
                                cp = mat.get_Parameter(DB.BuiltInParameter.MATERIAL_CLASS)
                                if cp and not cp.IsReadOnly:
                                    cp.Set(class_label)
                        except Exception:
                            pass

                        mapped.append(mat.Name)
                    except Exception as ex:
                        failed.append((mat.Name, str(ex)))
                t.Commit()
        except Exception as ex:
            self._logger.error("Batch mapping failed: {}".format(ex))
            forms.alert(
                u"Error mapping materials:\n{}".format(ex),
                title=constants.TOOL_TITLE
            )
            return [], []

        return mapped, failed
