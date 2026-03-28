"""
Run from repo root: python find_tsqlquery.py
Mines TSQLQuery XOML patterns from raw corpus files.
Focus: connection string field name and variable reference used in practice.
"""

import os, re

CORPUS_DIR = "workflows_raw/xml"
TARGET = "TSQLQuery"

results = []
files_checked = 0
raw_hits = 0

for fname in sorted(os.listdir(CORPUS_DIR)):
    if not fname.endswith(".xml"):
        continue
    files_checked += 1
    with open(os.path.join(CORPUS_DIR, fname), encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    if TARGET not in raw:
        continue
    raw_hits += 1

    pattern = r'<(?:\w+:)?' + TARGET + r'\b.*?(?:/>|</(?:\w+:)?' + TARGET + r'>)'
    for match in re.finditer(pattern, raw, re.DOTALL):
        fragment = re.sub(r'\s+', ' ', match.group(0)).strip()
        results.append((fname, fragment))

print(f"Files checked : {files_checked}")
print(f"Files with TSQLQuery: {raw_hits}")
print(f"Instances extracted : {len(results)}")
print()

# Deduplicate structurally
seen = {}
for fname, fragment in results:
    struct_key = re.sub(r'="[^"]*"', '=""', fragment)
    struct_key = re.sub(r'<\w+:', '<', struct_key)
    if struct_key not in seen:
        seen[struct_key] = (fname, fragment)

print(f"Unique structural patterns: {len(seen)}")
print()

for i, (sk, (fname, fragment)) in enumerate(seen.items(), 1):
    print(f"--- Pattern {i} (from: {fname}) ---")
    print(fragment)
    print()

# Also: extract just connection string field values across all instances
print("=" * 70)
print("Connection string field survey (all instances):")
print("=" * 70)
conn_fields = ["ConnectionStringTextBox", "ConnectionString", "SiteName", "SiteId"]
conn_values = {f: set() for f in conn_fields}
for fname, fragment in results:
    for field in conn_fields:
        m = re.search(field + r'="([^"]*)"', fragment)
        if m:
            conn_values[field].add(m.group(1))

for field, values in conn_values.items():
    if values:
        print(f"\n{field} values seen ({len(values)} unique):")
        for v in sorted(values):
            print(f"  {v!r}")