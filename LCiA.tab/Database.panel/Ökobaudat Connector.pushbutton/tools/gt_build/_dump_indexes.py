# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Build compact, searchable indexes for fresh per-material curation (v0.5).

Writes:
  tools/gt_build/cache_index.tsv      uuid<TAB>name<TAB>classification   (sorted by classification, name)
  tools/gt_build/materials_index.tsv  revit_id<TAB>name<TAB>class<TAB>desc<TAB>hosts<TAB>density<TAB>mpa<TAB>lambda
  tools/gt_build/cache_roots.txt       distinct top-level classification roots + counts

Run (CPython 3): python tools/gt_build/_dump_indexes.py
"""
from __future__ import print_function
import io, json, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))           # pushbutton root
LCIA_TAB = os.path.dirname(os.path.dirname(ROOT))       # LCiA.tab
CACHE = os.path.join(ROOT, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_en.json")
MATS = os.path.join(LCIA_TAB, "Context.panel", "ExtractContext.pushbutton", "data", "materials_context - Snowden - En.json")

OUT_CACHE = os.path.join(HERE, "cache_index.tsv")
OUT_MATS = os.path.join(HERE, "materials_index.tsv")
OUT_ROOTS = os.path.join(HERE, "cache_roots.txt")


def load(p):
    with io.open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def clean(s):
    return (u" ".join((s or u"").split())).replace(u"\t", u" ").strip()


def main():
    cache = load(CACHE)
    rows = []
    roots = collections.Counter()
    for r in cache["results"]:
        u = r.get("uuid")
        if not u:
            continue
        name = clean(r.get("name"))
        cls = clean(r.get("classification"))
        rows.append((cls, name, u))
        roots[cls.split("/")[0].strip()] += 1
    rows.sort(key=lambda x: (x[0].lower(), x[1].lower()))
    with io.open(OUT_CACHE, "w", encoding="utf-8", newline="\n") as f:
        for cls, name, u in rows:
            f.write(u"%s\t%s\t%s\n" % (u, name, cls))
    with io.open(OUT_ROOTS, "w", encoding="utf-8", newline="\n") as f:
        for k, v in sorted(roots.items(), key=lambda x: -x[1]):
            f.write(u"%5d  %s\n" % (v, k))
    print("cache_index.tsv:", len(rows), "datasets")

    mats = load(MATS)
    with io.open(OUT_MATS, "w", encoding="utf-8", newline="\n") as f:
        for m in mats:
            pa = m.get("physical_asset") or {}
            ta = m.get("thermal_asset") or {}
            he = m.get("host_elements") or {}
            cats = clean(u",".join(he.get("categories", []) or []))
            dens = pa.get("density_kg_m3", ta.get("density_kg_m3", ""))
            mpa = pa.get("concrete_compression_mpa", "")
            lam = ta.get("thermal_conductivity_w_mk", "")
            f.write(u"%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % (
                m.get("revit_id"), clean(m.get("name")), clean(m.get("class")),
                clean(m.get("description")), cats, dens, mpa, lam))
    print("materials_index.tsv:", len(mats), "materials")
    print("roots written to cache_roots.txt")


if __name__ == "__main__":
    main()
