# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
OekobaudatClient -- HTTP access, local JSON caching, and ILCD parsing
for the ÖKOBAUDAT REST API.

All network I/O uses System.Net.WebClient (IronPython/.NET, works inside Revit).
"""
#pylint: disable=import-error,invalid-name,broad-except
import os
import json
import time
import re as _re

import clr
clr.AddReference("System")
clr.AddReference("System.Net")
from System.Net import WebClient
from System.Text import Encoding
from System import Uri

import constants
from models import MaterialDataset
from ilcd_parser import ilcd_xml_to_dict


class OekobaudatClient(object):
    """Handles all communication with the ÖKOBAUDAT soda4LCA REST API
    and the local JSON dataset cache."""

    CACHE_TTL = 7 * 24 * 60 * 60  # 7 days in seconds

    def __init__(self, cache_dir):
        """
        Args:
            cache_dir: Absolute path to the directory used for JSON cache files.
        """
        self._cache_dir = cache_dir
        if not os.path.exists(cache_dir):
            try:
                os.makedirs(cache_dir)
            except Exception:
                pass

    # ── Cache helpers ─────────────────────────────────────────────

    def get_cache_path(self, cache_key, lang):
        """Return the path to the local JSON cache file for this source and language."""
        filename = "ds_cache_v2_{}_{}.json".format(cache_key, lang)
        return os.path.join(self._cache_dir, filename)

    def is_cache_fresh(self, cache_path):
        """Return True if the cache file exists and is younger than CACHE_TTL."""
        if not os.path.exists(cache_path):
            return False
        return (time.time() - os.path.getmtime(cache_path)) < self.CACHE_TTL

    def load_cache(self, cache_path):
        """Load and return the results list from a cache file.
        Returns an empty list if the file cannot be read."""
        try:
            with open(cache_path, "rb") as f:
                raw = f.read()
                # Decode bytes → unicode so German characters render correctly
                data = json.loads(raw.decode("utf-8"))
                return data.get("results", [])
        except Exception:
            return []

    def save_cache(self, cache_path, results):
        """Write results list to a cache file (UTF-8, with timestamp)."""
        try:
            json_str = json.dumps(
                {"timestamp": time.time(), "results": results},
                ensure_ascii=False
            )
            with open(cache_path, "wb") as f:
                # IronPython 2.7: json.dumps returns unicode when ensure_ascii=False
                if isinstance(json_str, unicode):
                    f.write(json_str.encode("utf-8"))
                else:
                    f.write(json_str)
        except Exception:
            pass

    # ── HTTP ──────────────────────────────────────────────────────

    def get_json(self, url):
        """HTTP GET *url* and return the parsed JSON dict. Raises on error."""
        wc = WebClient()
        wc.Encoding = Encoding.UTF8
        wc.Headers.Add("Accept", "application/json")
        raw = wc.DownloadString(Uri(url))
        return json.loads(raw)

    def get_text(self, url, accept="application/xml"):
        """HTTP GET *url* and return the raw response body as a string.
        Used for fetching ILCD XML payloads. Raises on error."""
        wc = WebClient()
        wc.Encoding = Encoding.UTF8
        wc.Headers.Add("Accept", accept)
        return wc.DownloadString(Uri(url))

    # ── Public API ────────────────────────────────────────────────

    def search_processes(self, datastock_uuid, name_query,
                         page_size=500, extra_params="", lang="de"):
        """Search processes in a datastock by name.

        Automatically paginates to fetch ALL matching results.

        Returns:
            (all_results, total_count) where all_results is a list of dicts
            each containing uuid, name, and metadata fields.
        """
        all_results = []
        start_index = 0
        total_count = 0
        lang_param = "&lang={}".format(lang) if lang else ""

        while True:
            url = (
                "{}/datastocks/{}/processes"
                "?format=json&search=true&name={}&pageSize={}&startIndex={}{}{}"
            ).format(
                constants.API_BASE, datastock_uuid,
                name_query, page_size, start_index,
                extra_params, lang_param
            )
            data = self.get_json(url)
            total_count = data.get("totalCount", 0)

            items = data.get("data", [])
            if not items:
                break

            for item in items:
                comp_list = item.get("compliance", [])
                comp_str = (
                    " / ".join([c.get("name", "") for c in comp_list if c.get("name")])
                    if comp_list else ""
                )
                reg_auth = item.get("regAuthority", {})
                prog_op = reg_auth.get("name", "") if isinstance(reg_auth, dict) else ""
                all_results.append({
                    "uuid":             item.get("uuid", ""),
                    "name":             item.get("name", ""),
                    "classification":   item.get("classific", ""),
                    "location":         item.get("geo", ""),
                    "valid_until":      item.get("validUntil", ""),
                    "dataset_type":     item.get("subType", ""),
                    "owner":            item.get("owner", ""),
                    "compliance":       comp_str,
                    "program_operator": prog_op,
                    "epd_no":           item.get("regNo", ""),
                })

            if len(all_results) >= total_count or len(items) < page_size:
                break
            start_index += page_size

        return all_results, total_count

    def get_process_detail(self, process_uuid):
        """Fetch the full ILCD data for a single process. Returns a dict.

        Source format: ILCD XML (the canonical exchange format). We convert
        XML → dict locally via `ilcd_xml_to_dict` so downstream parsers
        (ilcd_parser.parse_full and friends) run unchanged.

        Why XML and not JSON: OEKOBAUDAT's `?format=json` per-process endpoint
        returns HTTP 500 with a server-side java.lang.NullPointerException
        (regression in their JSON serializer, May 2026). The XML endpoint is
        healthy and serves the same underlying data - XML is also the format
        every ILCD-compliant LCA tool uses, so this is the more robust choice
        long-term regardless of when (or if) their JSON endpoint is fixed.
        """
        url = "{}/processes/{}?format=xml".format(constants.API_BASE, process_uuid)
        xml_str = self.get_text(url)
        return ilcd_xml_to_dict(xml_str)

    # ── ILCD parsing (private helpers) ───────────────────────────

    def _extract_short_name(self, desc_list, preferred_lang=None):
        """Pull a short-description text from an ILCD multilingual array.
        Tries *preferred_lang* first, then 'en', then the first entry."""
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

    def _extract_indicator_tag(self, full_name):
        """From 'Global Warming Potential - total (GWP-total)' extract 'GWP-total'."""
        m = _re.search(r'\(([^)]+)\)\s*$', full_name or "")
        if m:
            return m.group(1).strip()
        return full_name.strip() if full_name else ""

    def _parse_ilcd_ref_unit_string(self, process_json):
        """Extract the reference unit string (e.g. 'kg', 'm3', 'm2') from ILCD exchanges."""
        try:
            pi = process_json.get("processInformation", {})
            qr = pi.get("quantitativeReference", {})
            ref_ids = qr.get("referenceToReferenceFlow", [])
            ref_id = ref_ids[0] if ref_ids else 0
            exchanges = process_json.get("exchanges", {}).get("exchange", [])
            for ex in exchanges:
                if ex.get("dataSetInternalID") == ref_id:
                    other = ex.get("other", {})
                    anies = other.get("anies", [])
                    for a in anies:
                        if a.get("name") == "referenceToUnitGroupDataSet":
                            unit_desc = self._extract_short_name(
                                a.get("value", {}).get("shortDescription", [])
                            )
                            if unit_desc:
                                return unit_desc
                    return "m3"
            return "m3"
        except Exception:
            return "m3"

    def _parse_ilcd_category(self, process_json):
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

    # ── ILCD → MaterialDataset ────────────────────────────────────

    def parse_ilcd_to_dataset(self, process_json, process_uuid, lang=None):
        """Convert a full ILCD process JSON dict into a MaterialDataset object."""
        pi = process_json.get("processInformation", {})
        dsi = pi.get("dataSetInformation", {})
        name_list = dsi.get("name", {}).get("baseName", [])
        name = self._extract_short_name(name_list, preferred_lang=lang) or process_uuid

        ref_unit = self._parse_ilcd_ref_unit_string(process_json)
        category = self._parse_ilcd_category(process_json)

        ds = MaterialDataset(
            dataset_key=process_uuid,
            name=name,
            reference_unit=ref_unit,
            category=category,
            uuid=process_uuid,
        )

        def _parse_indicator_anies(desc_lists, anies_source):
            """Resolve indicator name from *desc_lists*, then populate ds.modules
            from the anies list in *anies_source*."""
            prop_name = None
            indicator_tag = ""

            # Try DE first (DE descriptions reliably include the abbreviation in parentheses),
            # then EN, then first available.
            for preferred in ("de", "en", None):
                method_desc = self._extract_short_name(desc_lists, preferred_lang=preferred)
                indicator_tag = self._extract_indicator_tag(method_desc)
                prop_name = constants.LCIA_NAME_MAP.get(indicator_tag)
                if prop_name:
                    break
                # Also try the full description string directly (English names without parens)
                prop_name = constants.LCIA_NAME_MAP.get(method_desc)
                if prop_name:
                    indicator_tag = method_desc
                    break
            else:
                prop_name = None

            if not prop_name:
                # Last resort: partial substring match
                for key in constants.LCIA_NAME_MAP:
                    if key.lower() in (indicator_tag or "").lower():
                        prop_name = constants.LCIA_NAME_MAP[key]
                        break

            if not prop_name:
                return

            anies = anies_source.get("other", {}).get("anies", [])
            for a in anies:
                module = a.get("module")
                val_str = a.get("value")
                if module and val_str is not None and not isinstance(val_str, dict):
                    try:
                        ds.add_module_data(module, prop_name, float(val_str))
                    except (ValueError, TypeError):
                        pass

        # Parse LCIA results section (GWP, ODP, AP, EP, POCP, ADP, WDP …)
        lcia_results = process_json.get("LCIAResults", {}).get("LCIAResult", [])
        for result in lcia_results:
            desc_lists = result.get("referenceToLCIAMethodDataSet", {}).get("shortDescription", [])
            _parse_indicator_anies(desc_lists, result)

        # Parse exchanges section for energy/material indicators
        # (PENRT, PENRM, PENRE, PERT, PERM, PERE, SM, FW …)
        pi2 = process_json.get("processInformation", {})
        ref_ids = set(pi2.get("quantitativeReference", {}).get("referenceToReferenceFlow", []))
        exchanges = process_json.get("exchanges", {}).get("exchange", [])
        for ex in exchanges:
            if ex.get("dataSetInternalID") in ref_ids:
                continue  # skip the reference product flow itself
            desc_lists = ex.get("referenceToFlowDataSet", {}).get("shortDescription", [])
            _parse_indicator_anies(desc_lists, ex)

        ds._details_fetched = True
        return ds
