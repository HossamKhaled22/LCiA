# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: GPL-3.0-or-later
__title__   = "Ökobaudat\nConnector"
__version__ = "Version: 2.0"
__doc__     = """Version = 2.0 | Date: 03.03.2026
Import ÖKOBAUDAT material data via live API or local CSV and map to Revit Materials.

Features:
- Search the ÖKOBAUDAT database directly via the soda4LCA REST API
- Choose between 4 online databases (A2 Sphera, A1 Sphera, A2 ecoinvent, Project EPDs)
- DataGrid columns adapt automatically to EN 15804+A1 vs +A2 indicator sets
- Create or update Revit Materials with LCA data (UUID + UUID_Data parameters)
"""
#pylint: disable=import-error,invalid-name,broad-except
import sys
import os
import traceback

# Ensure the pushbutton directory is on sys.path so all sub-modules are importable
_script_dir = os.path.dirname(__file__)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from pyrevit import forms, script

logger = script.get_logger()

from window import AssignMaterialWindow


# ────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        AssignMaterialWindow()
    except Exception as ex:
        logger.error("Error: {}".format(ex))
        logger.error(traceback.format_exc())
        forms.alert("An error occurred:\n{}".format(ex), title=__title__)
