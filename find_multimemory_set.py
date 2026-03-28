"""
Run from repo root: python find_multimemoryset.py
Searches workflows_raw/xml for MultiMemorySet and prints each unique XOML fragment.
"""

import os
import re
import html

CORPUS_DIR = "workflows_raw/xml"
TARGET = "MultiMemorySet"

found = []

for fname in sorted(os.listdir(CORPUS_DIR)):
    if not fname.endswith(".xml"):
        continue
    path = os.path.join(CORPUS_DIR, fname)
    with open(path, encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    if TARGET not in raw:
        continue

    # Extract the Xoml attribute value
    m = re.search(r'Xoml="(.*?)" XomlStatus', raw, re.DOTALL)
    if not m:
        continue

    xoml = html.unescape(m.group(1))
    xoml = xoml.replace("&#xD;&#xA;", "\n").replace("\r\n", "\n")

    # Find every MultiMemorySet tag (self-closing or with children)
    # Match from opening tag to either /> or </MultiMemorySet>
    for match in re.finditer(
        r'<MultiMemorySet\b.*?(?:/>|</MultiMemorySet>)', xoml, re.DOTALL
    ):
        fragment = match.group(0)
        # Normalize whitespace for deduplication key
        key = re.sub(r'\s+', ' ', fragment).strip()
        found.append((fname, fragment, key))

if not found:
    print("No MultiMemorySet activities found in corpus.")
else:
    # Deduplicate by structural pattern (strip variable-specific values)
    seen_keys = {}
    for fname, fragment, key in found:
        # Structural key: strip attribute values, keep attr names only
        struct_key = re.sub(r'="[^"]*"', '=""', key)
        if struct_key not in seen_keys:
            seen_keys[struct_key] = (fname, fragment)

    print(f"Found MultiMemorySet in {len(set(f for f,_,_ in found))} workflows.")
    print(f"Unique structural patterns: {len(seen_keys)}\n")
    print("=" * 70)

    for i, (struct_key, (fname, fragment)) in enumerate(seen_keys.items(), 1):
        print(f"\n--- Pattern {i} (first seen in: {fname}) ---")
        print(fragment)
        print()

    # Also print all filenames that contain MultiMemorySet
    print("=" * 70)
    print(f"\nAll files containing MultiMemorySet:")
    for fname in sorted(set(f for f, _, _ in found)):
        print(f"  {fname}")