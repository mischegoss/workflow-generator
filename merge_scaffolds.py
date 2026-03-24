"""
merge_scaffolds.py  —  run from project root

Merges confirmed scaffolds from data/patterns/scaffolds.json into
data/patterns/pattern_library.json.

Usage:
    python merge_scaffolds.py           # dry run — shows what would change
    python merge_scaffolds.py --write   # writes merged pattern_library.json

What it does:
  - For each pattern in scaffolds.json with status == "ok",
    adds a "scaffold" field to the matching entry in pattern_library.json.
  - Patterns with insufficient_matches are skipped.
  - Patterns already having a "scaffold" field are skipped (idempotent).
  - Writes to pattern_library.json in place (original is backed up to
    pattern_library.json.bak).

Flags p003/p004 duplication warning before writing.
"""

import argparse
import json
import pathlib
import sys

PATTERN_LIB_PATH = pathlib.Path("data/patterns/pattern_library.json")
SCAFFOLDS_PATH   = pathlib.Path("data/patterns/scaffolds.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="Write merged file. Without this flag, dry run only.")
    args = parser.parse_args()

    # ── Load files ──────────────────────────────────────────────────────────
    if not PATTERN_LIB_PATH.exists():
        print(f"ERROR: {PATTERN_LIB_PATH} not found", file=sys.stderr)
        sys.exit(1)
    if not SCAFFOLDS_PATH.exists():
        print(f"ERROR: {SCAFFOLDS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    with open(PATTERN_LIB_PATH, encoding="utf-8") as f:
        library = json.load(f)
    with open(SCAFFOLDS_PATH, encoding="utf-8") as f:
        scaffolds_data = json.load(f)

    scaffolds = scaffolds_data.get("scaffolds", {})

    # Build lookup: pattern_id → index in library list
    lib_index = {p["pattern_id"]: i for i, p in enumerate(library)}

    # ── Duplication check: p003 vs p004 ─────────────────────────────────────
    p003_scaffold = scaffolds.get("p003", {}).get("scaffold")
    p004_scaffold = scaffolds.get("p004", {}).get("scaffold")
    if p003_scaffold and p004_scaffold and p003_scaffold == p004_scaffold:
        print("⚠  WARNING: p003 and p004 scaffolds are IDENTICAL.")
        print("   Both patterns have different sequence_fragments in pattern_library.json")
        print("   but the mined scaffold is the same for both.")
        print("   Merging both as-is. Review whether one should be removed from the library.")
        print()

    # ── Merge ───────────────────────────────────────────────────────────────
    merged = 0
    skipped_no_scaffold = 0
    skipped_already_present = 0

    for pid, scaffold_entry in scaffolds.items():
        if scaffold_entry.get("status") != "ok":
            skipped_no_scaffold += 1
            continue

        scaffold = scaffold_entry.get("scaffold")
        if not scaffold:
            skipped_no_scaffold += 1
            continue

        if pid not in lib_index:
            print(f"  SKIP {pid} — not found in pattern_library.json")
            continue

        pattern = library[lib_index[pid]]

        if "scaffold" in pattern:
            print(f"  SKIP {pid} — scaffold already present")
            skipped_already_present += 1
            continue

        print(f"  MERGE {pid} [{pattern['control_flow']}] "
              f"({len(scaffold)} activities in scaffold)")
        if args.write:
            pattern["scaffold"] = scaffold
        merged += 1

    # ── Summary ─────────────────────────────────────────────────────────────
    print()
    print(f"Patterns to merge:          {merged}")
    print(f"Skipped (no scaffold):      {skipped_no_scaffold}")
    print(f"Skipped (already present):  {skipped_already_present}")

    if not args.write:
        print()
        print("Dry run — nothing written. Run with --write to apply.")
        return

    # ── Write ────────────────────────────────────────────────────────────────
    backup_path = PATTERN_LIB_PATH.with_suffix(".json.bak")
    backup_path.write_text(
        PATTERN_LIB_PATH.read_text(encoding="utf-8"),
        encoding="utf-8"
    )
    print(f"Backup written: {backup_path}")

    PATTERN_LIB_PATH.write_text(
        json.dumps(library, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"Written: {PATTERN_LIB_PATH}")
    print()
    print("Done. Run test_pipeline.py with a ReadXLS+loop prompt to confirm MODE 1 triggers.")


if __name__ == "__main__":
    main()