#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Offline indicator pre-fetcher for Oekobaudat datasets.

Fetches full ILCD JSON for all datasets and caches aggregated A1-A3,
C1-C4, and D indicator values plus extracted material attributes.
This enables advanced filtering (GWP tier buttons, phase selection,
etc.) at runtime without needing to call the API for every dataset.

Run this script once (from CPython 3, outside Revit) after the dataset
list cache has been populated by opening the tool in Revit.

Usage
-----
# Single source + language (fastest, ~6-10 min with parallel workers):
    python indicator_prefetcher.py --source a2_sphera --lang de

# All sources, de only (recommended first run, ~15-20 min):
    python indicator_prefetcher.py --all-de

# Copy de results to en instantly (no network calls):
    python indicator_prefetcher.py --copy-to-en

# All sources, both languages (combines the above two):
    python indicator_prefetcher.py --all

# Re-fetch only entries missing C1-C4/D data (after adding phase support):
    python indicator_prefetcher.py --all-de --update-phases

# List available sources:
    python indicator_prefetcher.py --list

Prerequisites
-------------
1. Python 3.x (does NOT run inside Revit / IronPython)
2. The PyRevit plugin's cache must already be populated -- open the
   Oekobaudat Connector tool in Revit at least once so the dataset
   lists are downloaded and saved to LCiA_Extension_Cache/.

The script saves results to:
    LCiA_Extension_Cache/indicators_{source}_{lang}.json

Speed notes
-----------
- Uses up to MAX_WORKERS parallel HTTP connections (default 5).
- --all-de + --copy-to-en is ~2x faster than --all because indicator
  values are language-independent (same ILCD JSON regardless of lang).
- --update-phases skips entries that already have C1-C4 data.
"""
from __future__ import print_function, division
import os
import sys
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.request as _urllib_req

# Local imports (pure Python, no .NET)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ilcd_parser
import attribute_extractor

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR   = os.path.join(SCRIPT_DIR, "LCiA_Extension_Cache")
API_BASE    = "https://www.oekobaudat.de/OEKOBAU.DAT/resource"

REQUEST_PAUSE = 0.0   # seconds between API calls (parallel mode; latency is natural throttle)
RETRY_PAUSE   = 2.0   # seconds after an error before retrying
MAX_RETRIES   = 2      # retry count per dataset on transient errors
MAX_WORKERS   = 5     # parallel HTTP connections (increase to 8 on fast connections)

CACHE_VERSION = 1

ALL_SOURCES = ["a2_sphera", "a1_sphera", "a2_ecoinvent", "project_epds"]
ALL_LANGS   = ["de", "en"]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get_json(url):
    """HTTP GET *url* and return parsed JSON dict."""
    req = _urllib_req.Request(url)
    req.add_header("Accept", "application/json")
    resp = _urllib_req.urlopen(req, timeout=30)
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _get_xml(url):
    """HTTP GET *url* and return raw XML body as a unicode string."""
    req = _urllib_req.Request(url)
    req.add_header("Accept", "application/xml")
    resp = _urllib_req.urlopen(req, timeout=30)
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return raw


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_dataset_cache(source, lang):
    """Return the list of dataset dicts from ds_cache_v2_{source}_{lang}.json."""
    path = os.path.join(CACHE_DIR, "ds_cache_v2_{}_{}.json".format(source, lang))
    if not os.path.exists(path):
        return None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                raw = json.load(f)
            return raw.get("results", [])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def _load_existing_indicators(source, lang):
    """Return existing indicator cache dict, or empty structure."""
    path = os.path.join(CACHE_DIR, "indicators_{}_{}.json".format(source, lang))
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("datasets", {})
    except Exception:
        return {}


def _save_indicators(source, lang, datasets_dict):
    """Write indicator cache to disk."""
    path = os.path.join(CACHE_DIR, "indicators_{}_{}.json".format(source, lang))
    data = {
        "version": CACHE_VERSION,
        "timestamp": time.time(),
        "datasets": datasets_dict,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=None)


# Curated UUID -> Revit-aligned category map. Loaded once on first use.
# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def _process_one_dataset(uuid, lang):
    """Fetch and parse ILCD data for one dataset UUID.

    Returns:
        dict with indicator values, ref_unit, modules_available, attributes.
        Or None on failure.
    """
    # Fetch ILCD XML (the canonical format). OEKOBAUDAT's `?format=json`
    # per-process endpoint returns HTTP 500 due to a server-side regression
    # (java.lang.NullPointerException). The XML endpoint is healthy and serves
    # the same underlying data - XML is also the standard ILCD exchange format
    # used by every conforming LCA tool. Convert to dict locally so the
    # downstream parser keeps the same signature.
    url = "{}/processes/{}?format=xml".format(API_BASE, uuid)

    for attempt in range(MAX_RETRIES + 1):
        try:
            xml_str = _get_xml(url)
            process_json = ilcd_parser.ilcd_xml_to_dict(xml_str)
            break
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_PAUSE)
                continue
            return None

    try:
        parsed = ilcd_parser.parse_full(process_json, lang=lang)
    except Exception:
        return None

    a1a3 = parsed["indicators_a1a3"]
    a1a5 = parsed.get("indicators_a1a5", {}) or {}
    b1b7 = parsed.get("indicators_b1b7", {}) or {}
    c1c4 = ilcd_parser.aggregate_c1c4(parsed["modules"])
    d_vals = ilcd_parser.aggregate_d(parsed["modules"])
    name = parsed["name"]
    category = parsed["category"]

    # Extract material attributes from the classification path. The
    # `material_category` field in the result is now a passive cache value
    # (the Revit MaterialClass write-back uses ds.RevitMaterialClass instead).
    sm_value = a1a3.get("SM")
    attrs = attribute_extractor.extract_all(
        name, category, sm_value=sm_value,
        tech_description=parsed.get("tech_description", ""),
    )

    # Build result - store A1-A3, A1-A5, B1-B7, C1-C4 and D values per indicator
    result = {}
    for prop in ilcd_parser.ALL_INDICATOR_PROPS:
        val = a1a3.get(prop)
        if val is not None:
            result[prop] = val                      # A1-A3 (existing key, bare for back-compat)
        val_a5 = a1a5.get(prop)
        if val_a5 is not None:
            result[prop + "_A1A5"] = val_a5
        val_b = b1b7.get(prop)
        if val_b is not None:
            result[prop + "_B1B7"] = val_b
        val_c = c1c4.get(prop)
        if val_c is not None:
            result[prop + "_C1C4"] = val_c
        val_d = d_vals.get(prop)
        if val_d is not None:
            result[prop + "_D"] = val_d

    result["ref_unit"] = parsed["ref_unit"]
    result["modules_available"] = parsed["modules_available"]
    result["attributes"] = attrs
    result["tech_description"] = parsed.get("tech_description", "")
    result["reference_year"] = parsed.get("reference_year")
    # Schema version - bumped when cache shape changes so older entries
    # can be detected and re-fetched by _needs_phase_update().
    # v2 = adds _A1A5 / _B1B7 aggregates (may legitimately be absent for
    # datasets without A4/A5/B-stage data; the marker distinguishes
    # "processed under v2 but no data" from "never processed under v2").
    result["_schema_version"] = 2

    return result


def _needs_phase_update(entry):
    """Return True if the cache entry predates the current schema version."""
    if entry is None:
        return True
    # v2 adds _A1A5 / _B1B7 aggregates. Entries without _schema_version are
    # pre-v2 and need a full re-fetch. Entries at v2+ are current, even if
    # they lack _A1A5/_B1B7 (dataset simply has no A4/A5/B-stage data).
    return int(entry.get("_schema_version", 1)) < 2


def build(source, lang, update_phases=False):
    """Build (or incrementally update) indicator cache for *source* / *lang*.

    Args:
        update_phases: If True, also re-fetch entries that exist but are
                       missing C1-C4 / D indicator data (added in v2).
    """
    print("\n  Loading dataset cache: {} / {}".format(source, lang))
    datasets = _load_dataset_cache(source, lang)
    if datasets is None:
        print("  [SKIP] Cache file not found.")
        print("         Open the Oekobaudat Connector in Revit first so the")
        print("         dataset list is downloaded.")
        return
    if not datasets:
        print("  [SKIP] Cache is empty.")
        return
    print("  {} datasets in cache.".format(len(datasets)))

    existing = _load_existing_indicators(source, lang)
    if existing:
        print("  {} datasets already cached.".format(len(existing)))

    if update_phases:
        to_fetch = [ds for ds in datasets
                    if ds.get("uuid") and (
                        ds["uuid"] not in existing or
                        _needs_phase_update(existing.get(ds["uuid"]))
                    )]
        if not to_fetch:
            print("  All entries already have C1-C4/D data and reference_year -- nothing to do.")
            return
        print("  {} datasets need refresh (--update-phases: missing C1-C4/D or reference_year).".format(len(to_fetch)))
    else:
        to_fetch = [ds for ds in datasets
                    if ds.get("uuid") and ds["uuid"] not in existing]
        if not to_fetch:
            print("  All indicators are up to date -- nothing to do.")
            return
        print("  {} datasets need indicator data.".format(len(to_fetch)))

    indicators = dict(existing)
    total         = len(to_fetch)
    lock          = threading.Lock()
    done_count    = [0]
    error_count   = [0]
    save_interval = 50   # save every N completed datasets

    def _fetch_and_store(ds):
        uuid   = ds["uuid"]
        result = _process_one_dataset(uuid, lang)
        with lock:
            if result is not None:
                indicators[uuid] = result
                done_count[0] += 1
                n = done_count[0]
                if n % 100 == 0 or n == total:
                    pct = 100 * n // total
                    print("  {}/{} done ({}%)...".format(n, total, pct))
                if n % save_interval == 0:
                    _save_indicators(source, lang, dict(indicators))
            else:
                error_count[0] += 1
                print("    [WARN] Failed: {} (will retry on next run)".format(uuid[:12]))

    print("  Fetching with {} parallel workers...".format(MAX_WORKERS))
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_and_store, ds): ds for ds in to_fetch}
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                with lock:
                    error_count[0] += 1

    elapsed = time.time() - t_start
    # Final save
    _save_indicators(source, lang, indicators)

    print("  Saved {} indicator records to indicators_{}_{}.json  ({:.0f}s elapsed)".format(
        len(indicators), source, lang, elapsed))
    if error_count[0]:
        print("  WARNING: {} datasets failed (run again to retry).".format(error_count[0]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Pre-fetch ILCD indicator data for Oekobaudat datasets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Recommended: fetch all sources + both languages, update C1-C4/D data:
  python indicator_prefetcher.py --all --update-phases

  # Single source + language:
  python indicator_prefetcher.py --source a2_sphera --lang de

  # All sources, de only (half the network calls — en has different text):
  python indicator_prefetcher.py --all-de --update-phases

  python indicator_prefetcher.py --list

The script is incremental: already-fetched UUIDs are skipped unless
--update-phases is given.  Uses {} parallel workers by default.
        """.format(MAX_WORKERS),
    )
    p.add_argument("--source", choices=ALL_SOURCES,
                   help="Data source key (mutually exclusive with --all / --all-de)")
    p.add_argument("--lang", choices=ALL_LANGS, default="de",
                   help="Language: de (default) or en")
    p.add_argument("--all", dest="build_all", action="store_true",
                   help="Fetch indicators for ALL sources and BOTH languages")
    p.add_argument("--all-de", dest="build_all_de", action="store_true",
                   help="Fetch indicators for ALL sources, de language only (~2x faster)")
    p.add_argument("--update-phases", dest="update_phases", action="store_true",
                   help="Re-fetch entries missing C1-C4/D data (use after adding phase support)")
    p.add_argument("--workers", type=int, default=MAX_WORKERS,
                   help="Number of parallel HTTP workers (default {})".format(MAX_WORKERS))
    p.add_argument("--list", dest="list_sources", action="store_true",
                   help="List available sources and exit")
    return p.parse_args()


def main():
    args = _parse_args()

    # Allow overriding MAX_WORKERS from CLI
    global MAX_WORKERS
    MAX_WORKERS = args.workers

    if args.list_sources:
        print("Available sources:")
        for s in ALL_SOURCES:
            print("  {}".format(s))
        return

    if args.build_all:
        print("=" * 60)
        print("Building indicator cache for ALL sources x BOTH languages")
        print("  Workers: {}  |  update-phases: {}".format(MAX_WORKERS, args.update_phases))
        print("=" * 60)
        for source in ALL_SOURCES:
            for lang in ALL_LANGS:
                build(source, lang, update_phases=args.update_phases)
        print("\nDone.")
    elif args.build_all_de:
        print("=" * 60)
        print("Building indicator cache for ALL sources (de only)")
        print("  Workers: {}  |  update-phases: {}".format(MAX_WORKERS, args.update_phases))
        print("  Note: run --all to also populate en caches (different text/attributes).")
        print("=" * 60)
        for source in ALL_SOURCES:
            build(source, "de", update_phases=args.update_phases)
        print("\nDone.")
    elif args.source:
        print("=" * 60)
        print("Building indicator cache: {} / {}  (workers={}, update-phases={})".format(
            args.source, args.lang, MAX_WORKERS, args.update_phases))
        print("=" * 60)
        build(args.source, args.lang, update_phases=args.update_phases)
        print("\nDone.")
    else:
        print("Please specify --source, --all, or --all-de.")
        print("Use --help for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
