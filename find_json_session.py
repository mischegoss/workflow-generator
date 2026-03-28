"""
Run from repo root: python find_json_session.py
Finds StartJsonSession and JsonToTable in workflows_raw/xml and prints
the exact XOML fragments including all field values.
"""

import os
import re
import html

CORPUS_DIR = "workflows_raw/xml"
TARGETS = ["StartJsonSession", "JsonToTable"]

results = {t: [] for t in TARGETS}
files_checked = 0
files_containing = 0

for fname in sorted(os.listdir(CORPUS_DIR)):
    if not fname.endswith(".xml"):
        continue
    path = os.path.join(CORPUS_DIR, fname)
    files_checked += 1
    with open(path, encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    if not any(t in raw for t in TARGETS):
        continue
    files_containing += 1

    m = re.search(r'Xoml="(.*?)" XomlStatus', raw, re.DOTALL)
    if not m:
        print(f"  [!] {fname}: no Xoml= attr found")
        continue

    xoml = html.unescape(m.group(1))
    xoml = xoml.replace("&#xD;&#xA;", "\n").replace("&#xA;", "\n").replace("\r\n", "\n")

    for target in TARGETS:
        if target not in xoml:
            continue
        pattern = r'<(?:\w+:)?' + target + r'\b.*?(?:/>|</(?:\w+:)?' + target + r'>)'
        for match in re.finditer(pattern, xoml, re.DOTALL):
            fragment = match.group(0)
            key = re.sub(r'\s+', ' ', fragment).strip()
            results[target].append((fname, fragment, key))

print(f"Files checked : {files_checked}")
print(f"Files with hit: {files_containing}")
print()

for target in TARGETS:
    hits = results[target]
    print(f"{'='*70}")
    print(f"{target}: {len(hits)} instance(s)")
    print(f"{'='*70}")

    if not hits:
        print("  Not found in XOML bodies.\n")
        continue

    # Deduplicate structurally
    seen = {}
    for fname, fragment, key in hits:
        struct_key = re.sub(r'="[^"]*"', '=""', key)
        if struct_key not in seen:
            seen[struct_key] = (fname, fragment)

    print(f"Unique structural patterns: {len(seen)}\n")
    for i, (sk, (fname, fragment)) in enumerate(seen.items(), 1):
        print(f"--- Pattern {i} (from: {fname}) ---")
        print(fragment)
        print()

    print(f"All files containing {target}:")
    for fname in sorted(set(f for f, _, _ in hits)):
        print(f"  {fname}")
    print()