"""
Run from repo root: python find_row_activities.py
Corpus files are raw XOML (no TotalExport wrapper).
Activities appear with dynamic namespace prefixes: ns0:SetCellValue, ns3:AddMemoryTableRow, etc.
"""

import os, re

CORPUS_DIR = "workflows_raw/xml"
TARGETS = ["AddMemoryTableRow", "DeleteMemoryTableRows", "SetCellValue"]

results = {t: [] for t in TARGETS}
files_checked = 0
raw_hits = {t: 0 for t in TARGETS}

for fname in sorted(os.listdir(CORPUS_DIR)):
    if not fname.endswith(".xml"):
        continue
    files_checked += 1
    with open(os.path.join(CORPUS_DIR, fname), encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    for target in TARGETS:
        if target not in raw:
            continue
        raw_hits[target] += 1

        # Match <ns0:SetCellValue .../> or <SetCellValue .../> — any prefix or none
        pattern = r'<(?:\w+:)?' + target + r'\b.*?(?:/>|</(?:\w+:)?' + target + r'>)'
        for match in re.finditer(pattern, raw, re.DOTALL):
            fragment = re.sub(r'\s+', ' ', match.group(0)).strip()
            results[target].append((fname, fragment))

print(f"Files checked: {files_checked}\n")
for t in TARGETS:
    print(f"{t}: raw hits in {raw_hits[t]} files, {len(results[t])} instances extracted")

print()
for target in TARGETS:
    hits = results[target]
    print(f"{'='*70}")
    print(f"{target}: {len(hits)} instance(s)")
    print(f"{'='*70}")
    if not hits:
        print("  Not found.\n")
        continue

    # Deduplicate by structure (strip values, keep attr names, strip ns prefix)
    seen = {}
    for fname, fragment in hits:
        struct_key = re.sub(r'="[^"]*"', '=""', fragment)
        struct_key = re.sub(r'<\w+:', '<', struct_key)
        if struct_key not in seen:
            seen[struct_key] = (fname, fragment)

    print(f"Unique structural patterns: {len(seen)}\n")
    for i, (sk, (fname, fragment)) in enumerate(seen.items(), 1):
        print(f"--- Pattern {i} (from: {fname}) ---")
        print(fragment)
        print()