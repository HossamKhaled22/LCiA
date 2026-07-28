# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Deterministic taxonomy query helper for GT building (CPython 3).

Reads the raw a2_sphera_en cache and supports:
  tree   <substr>     : distinct classification paths whose path contains <substr> (case-insensitive), with counts
  cls    <substr>     : every dataset whose classification path contains <substr>
  name   <substr>     : every dataset whose NAME contains <substr>
  both   <cls> :: <nm>: datasets whose classification contains <cls> AND name contains <nm>
  uuid   <uuid>       : show the single dataset with this uuid (verify existence)
  leaf   <exact path> : datasets whose classification path EQUALS <exact path>

Output columns: uuid \t dataset_type \t name \t classification
"""
import json, io, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CACHE = os.path.join(ROOT, "LCiA_Extension_Cache", "ds_cache_v2_a2_sphera_en.json")

def load():
    with io.open(CACHE, encoding="utf-8") as f:
        return json.load(f)["results"]

def norm(s):
    return " ".join((s or "").split()).lower()

def main():
    rows = load()
    if len(sys.argv) < 2:
        print("usage: _query.py {tree|cls|name|both|uuid|leaf} <arg>")
        return
    cmd = sys.argv[1]
    # optional filters: --type=a,b (include dataset_type prefixes), --xtype=a,b (exclude),
    # --nh=substr (name contains), --xnh=substr (name excludes)
    inc_t = exc_t = nh = xnh = None
    rest = []
    for a in sys.argv[2:]:
        if a.startswith("--type="): inc_t = [x.strip() for x in a[7:].split(",") if x.strip()]
        elif a.startswith("--xtype="): exc_t = [x.strip() for x in a[8:].split(",") if x.strip()]
        elif a.startswith("--nh="): nh = norm(a[5:])
        elif a.startswith("--xnh="): xnh = norm(a[6:])
        else: rest.append(a)
    arg = " ".join(rest)
    argn = norm(arg)

    def passes(r):
        dt = (r.get("dataset_type","") or "").replace(" dataset","").strip()
        if inc_t is not None and not any(dt.startswith(t) for t in inc_t): return False
        if exc_t is not None and any(dt.startswith(t) for t in exc_t): return False
        nm = norm(r.get("name",""))
        if nh is not None and nh not in nm: return False
        if xnh is not None and xnh in nm: return False
        return True

    if cmd == "tree":
        from collections import Counter, defaultdict
        c = Counter()
        dt = defaultdict(Counter)
        for r in rows:
            p = r.get("classification", "")
            if argn in norm(p):
                c[p] += 1
                dt[p][r.get("dataset_type", "")] += 1
        for p in sorted(c):
            types = ", ".join("%s:%d" % (k.replace(" dataset", ""), v) for k, v in sorted(dt[p].items()))
            print("%4d  %s   [%s]" % (c[p], p, types))
        print("--- %d paths, %d datasets ---" % (len(c), sum(c.values())))
        return

    if cmd == "uuid":
        for r in rows:
            if r.get("uuid") == arg:
                print("%s\t%s\t%s\t%s" % (r["uuid"], r.get("dataset_type",""), r["name"], r.get("classification","")))
                return
        print("NOT FOUND:", arg)
        return

    out = []
    for r in rows:
        if not passes(r):
            continue
        nm = norm(r.get("name", ""))
        cl = norm(r.get("classification", ""))
        if cmd == "cls" and argn in cl:
            out.append(r)
        elif cmd == "name" and argn in nm:
            out.append(r)
        elif cmd == "leaf" and argn == cl:
            out.append(r)
        elif cmd == "both":
            parts = arg.split("::")
            ca = norm(parts[0]); na = norm(parts[1]) if len(parts) > 1 else ""
            if ca in cl and na in nm:
                out.append(r)
    # sort by classification then name for contiguous family blocks
    out.sort(key=lambda r: (r.get("classification",""), r.get("name","")))
    for r in out:
        print("%s\t%s\t%s\t%s" % (r["uuid"], r.get("dataset_type","").replace(" dataset",""), r["name"], r.get("classification","")))
    print("--- %d datasets ---" % len(out))

if __name__ == "__main__":
    main()
