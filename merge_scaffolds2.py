"""
merge_scaffolds.py
==================
Merges generated scaffolds from data/patterns/scaffolds.json into
data/patterns/pattern_library.json.

This enables MODE 1 (scaffold-fill) for 12 patterns. Currently every run
goes through MODE 2 (example-guided) because no pattern has a scaffold field.

Usage:
    cd ~/Documents/workflow_generator
    python merge_scaffolds.py               # dry run — preview changes
    python merge_scaffolds.py --apply       # write the merged file

The original pattern_library.json is backed up as pattern_library.json.bak
before any write.
"""

import argparse
import json
import os
import shutil
from pathlib import Path

DATA_DIR          = Path(os.getenv("DATA_DIR", "data"))
PATTERN_LIB_PATH  = DATA_DIR / "patterns" / "pattern_library.json"
SCAFFOLDS_PATH    = DATA_DIR / "patterns" / "scaffolds.json"


def load_json(path: Path) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Merge scaffolds into pattern_library.json")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the merged file. Without this flag, runs as a dry run.",
    )
    args = parser.parse_args()

    # ── Load files ────────────────────────────────────────────────────────────
    if not PATTERN_LIB_PATH.exists():
        print(f"[ERROR] pattern_library.json not found at {PATTERN_LIB_PATH}")
        return
    if not SCAFFOLDS_PATH.exists():
        print(f"[ERROR] scaffolds.json not found at {SCAFFOLDS_PATH}")
        return

    library  = load_json(PATTERN_LIB_PATH)
    scaffolds_file = load_json(SCAFFOLDS_PATH)
    scaffolds = scaffolds_file.get("scaffolds", {})

    # ── Determine format ──────────────────────────────────────────────────────
    # pattern_library.json may be a list or a dict with a "patterns" key
    if isinstance(library, list):
        patterns = library
        is_wrapped = False
    elif isinstance(library, dict) and "patterns" in library:
        patterns = library["patterns"]
        is_wrapped = True
    else:
        print(f"[ERROR] Unexpected pattern_library.json format: {type(library)}")
        return

    print(f"Loaded {len(patterns)} patterns from pattern_library.json")
    print(f"Loaded {len(scaffolds)} scaffold entries from scaffolds.json")
    print()

    # ── Check for duplicates (p003 / p004 warning) ────────────────────────────
    # The data_files_integration_guide noted p003 and p004 produced identical
    # activity lists. Surface this before merging.
    p003 = scaffolds.get("p003", {})
    p004 = scaffolds.get("p004", {})
    if (p003.get("scaffold") and p004.get("scaffold") and
            p003.get("sequence_fragment") == p004.get("sequence_fragment")):
        print("[WARN] p003 and p004 have identical sequence_fragment — likely duplicates.")
        print("       Both scaffolds will be merged. Review pattern_library.json entries")
        print("       for p003 and p004 to confirm they are distinct patterns.")
        print()

    # ── Merge ─────────────────────────────────────────────────────────────────
    merged_count   = 0
    skipped_count  = 0
    notfound_count = 0
    null_count     = 0

    for pattern in patterns:
        pid = pattern.get("pattern_id")
        if not pid:
            continue

        if pid not in scaffolds:
            continue

        scaffold_entry = scaffolds[pid]
        scaffold_data  = scaffold_entry.get("scaffold")
        status         = scaffold_entry.get("status", "")

        if scaffold_data is None:
            # insufficient_matches — skip silently
            null_count += 1
            continue

        if "scaffold" in pattern:
            print(f"  [SKIP]   {pid}: already has scaffold field — not overwriting")
            skipped_count += 1
            continue

        # Merge
        pattern["scaffold"] = scaffold_data
        merged_count += 1

        cf    = scaffold_entry.get("control_flow", "")
        count = scaffold_entry.get("match_count", 0)
        types = list(scaffold_data.keys())[:4]
        print(f"  [MERGE]  {pid} [{cf}]  {count} corpus matches  →  {types}")

    print()
    print(f"Summary: {merged_count} merged, {skipped_count} skipped (already present), "
          f"{null_count} skipped (insufficient_matches), "
          f"{notfound_count} scaffold IDs not found in library")
    print()

    if merged_count == 0:
        print("Nothing to write.")
        return

    # ── Write ─────────────────────────────────────────────────────────────────
    if args.apply:
        bak_path = PATTERN_LIB_PATH.with_suffix(".json.bak")
        shutil.copy2(PATTERN_LIB_PATH, bak_path)
        print(f"Backed up original to {bak_path}")

        output = library if is_wrapped else patterns
        with open(PATTERN_LIB_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        print(f"Written: {PATTERN_LIB_PATH}")
        print()
        print("Next step: run test_pipeline.py with a prompt that matches p019")
        print("(ReadXLS + loop over rows) and confirm match_status == 'MATCHED'")
        print("and MODE 1 is triggered in StructureBuilder.")
    else:
        print("[DRY RUN] No files written. Run with --apply to write changes.")
        print()
        print("Merged library preview (first merged pattern):")
        for p in patterns:
            if "scaffold" in p and p.get("pattern_id") in scaffolds:
                scaffold_keys = list(p["scaffold"].keys())
                print(f"  pattern_id: {p['pattern_id']}")
                print(f"  scaffold top-level keys: {scaffold_keys}")
                break


if __name__ == "__main__":
    main()