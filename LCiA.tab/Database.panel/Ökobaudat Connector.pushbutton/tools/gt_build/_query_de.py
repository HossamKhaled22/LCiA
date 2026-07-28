# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""DE-cache variant of _query.py (ds_cache_v2_a2_sphera_de.json). Same CLI."""
import os, sys, runpy
HERE = os.path.dirname(os.path.abspath(__file__))
import _query
_query.CACHE = os.path.join(_query.ROOT, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_de.json")
if __name__ == "__main__":
    _query.main()
