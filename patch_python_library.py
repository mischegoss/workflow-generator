"""
patch_pattern_library.py
========================
Patches two classes of problem in pattern_library.json:

  1. TRIGGER KEYWORDS — 11 patterns have empty trigger_keywords, making them
     invisible to the pattern matcher (it skips patterns with no keywords).
     This script adds validated keywords derived from the decomposer output
     vocabulary (intent enum values + common step description words).

  2. SCAFFOLD GAPS — 26 patterns have no scaffold because the mine_corpus.py
     coverage threshold (0.45) is too high for single-leaf patterns.
     This script does NOT generate scaffolds — that requires the full corpus.
     Instead it prints the exact mine_corpus.py command to run, and after you
     run it, re-run this script with --apply-scaffolds to copy the results
     from data/patterns/scaffolds.json into pattern_library.json.

Usage:
    # Step 1: Patch trigger keywords (safe to run anytime, no corpus needed)
    python patch_pattern_library.py

    # Step 2: Run mine_corpus.py to generate missing scaffolds
    #   (printed by this script after the keyword patch)

    # Step 3: Copy scaffolds into pattern_library.json
    python patch_pattern_library.py --apply-scaffolds

    # Dry run to see what would change without writing
    python patch_pattern_library.py --dry-run
"""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

REPO_ROOT    = Path(__file__).parent
PATTERN_FILE = REPO_ROOT / "data" / "patterns" / "pattern_library.json"
SCAFFOLD_FILE = REPO_ROOT / "data" / "patterns" / "scaffolds.json"

# ---------------------------------------------------------------------------
# Keyword patches
# Each entry: pattern_id -> list of trigger keywords
# Keywords validated against score formula: hits/sqrt(n_kw) >= 0.80 threshold
# Single words are preferred; 2-word phrases used only for specificity.
# All keywords are drawn from decomposer intent enum values and common
# step description vocabulary.
# ---------------------------------------------------------------------------

KEYWORD_PATCHES = {

    # Linear: ConvertPasswordToPlaintext + PowerShellScript + StartJsonSession
    "p003": ["powershell", "script", "json", "session", "password", "convert"],

    # Linear: PowerShellScript + StartJsonSession + JsonToTable
    "p004": ["powershell", "script", "json", "session", "table"],

    # Linear: StartCiscoSession + SendCiscoCommand + TerminateCiscoSession
    "p005": ["cisco", "session", "command", "network", "send"],

    # Linear: HTTPRequest × 3
    "p007": ["http", "request", "api", "web", "url"],

    # IfElse: MultiMemorySet × 3
    # Intent "set variable" fires as a keyword; "branch" from control_flow
    "p011": ["set variable", "branch", "when", "condition", "store", "multiple"],

    # IfElse: PowerShellScript + StartJsonSession + JsonToTable
    "p014": ["powershell", "script", "json", "session", "table"],

    # IfElse: MultiMemorySet × 2 + DisplayMultiValue
    "p015": ["set variable", "display", "show", "branch", "when", "multiple"],

    # IfElse: ConvertPasswordToPlaintext + PowerShellScript + StartJsonSession
    "p017": ["powershell", "script", "json", "session", "password", "convert"],

    # While: SetCellValue × 3
    "p024": ["set cell", "loop", "update", "iterate", "for each", "write"],

    # UserGroup: SetCellValue + SetCellValue + ResultSetFilter
    "p043": ["approval", "user", "filter", "set cell", "usergroup", "review"],

    # UserGroup: SetCellValue + ResultSetFilter + Split
    "p044": ["approval", "split", "filter", "set cell", "user", "review"],
}

# Patterns that need scaffolds but don't have them.
# Grouped by the root cause of the failure.
SCAFFOLD_GAPS = {
    "single_leaf_threshold": [
        # These have exactly 1 unique leaf type — coverage gate requires threshold <= 0.33
        "p002", "p009", "p010", "p011", "p013", "p021", "p022",
        "p024", "p027", "p030", "p032", "p035", "p036",
    ],
    "coverage_too_low": [
        # Multiple leaf types but still below 0.45 threshold for real workflows
        "p025", "p026", "p031", "p033", "p034", "p037", "p038",
        "p045", "p046", "p047", "p048",
    ],
    "insufficient_matches_at_3": [
        # Matched 1–2 workflows at threshold 0.45 — lower threshold may find more
        "p008", "p012", "p014", "p015", "p017", "p028", "p029",
        "p039", "p040", "p042",
    ],
}

ALL_SCAFFOLD_GAP_IDS = sorted(
    set(p for ids in SCAFFOLD_GAPS.values() for p in ids)
)


def load_pattern_library():
    with open(PATTERN_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_pattern_library(patterns, dry_run=False):
    if dry_run:
        print(f"[dry-run] Would write {len(patterns)} patterns to {PATTERN_FILE}")
        return
    # Backup before writing
    backup = PATTERN_FILE.with_suffix(
        f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    shutil.copy(PATTERN_FILE, backup)
    print(f"Backed up to {backup.name}")
    with open(PATTERN_FILE, "w", encoding="utf-8") as f:
        json.dump(patterns, f, indent=2)
    print(f"Written: {PATTERN_FILE}")


def patch_keywords(patterns, dry_run=False):
    changed = 0
    for p in patterns:
        pid = p["pattern_id"]
        if pid not in KEYWORD_PATCHES:
            continue
        new_kw = KEYWORD_PATCHES[pid]
        old_kw = p.get("trigger_keywords", [])
        if old_kw == new_kw:
            continue
        print(f"  {pid} [{p['control_flow']:12}]  "
              f"{old_kw or '(empty)'} -> {new_kw}")
        if not dry_run:
            p["trigger_keywords"] = new_kw
        changed += 1
    return changed


def apply_scaffolds(patterns, dry_run=False):
    if not SCAFFOLD_FILE.exists():
        print(f"ERROR: {SCAFFOLD_FILE} not found. Run mine_corpus.py first.")
        return 0

    with open(SCAFFOLD_FILE, encoding="utf-8") as f:
        scaffold_data = json.load(f)

    scaffolds = scaffold_data.get("scaffolds", {})
    changed = 0
    skipped_no_scaffold = []

    pattern_index = {p["pattern_id"]: p for p in patterns}

    for pid in ALL_SCAFFOLD_GAP_IDS:
        if pid not in scaffolds:
            skipped_no_scaffold.append(pid)
            continue

        entry = scaffolds[pid]
        if entry.get("status") != "ok" or not entry.get("scaffold"):
            print(f"  {pid}  SKIP — status={entry.get('status')}")
            continue

        if pid not in pattern_index:
            print(f"  {pid}  SKIP — not found in pattern_library.json")
            continue

        p = pattern_index[pid]
        match_count = entry.get("match_count", 0)
        print(f"  {pid} [{p['control_flow']:12}]  scaffold generated "
              f"({match_count} corpus matches)")
        if not dry_run:
            p["scaffold"] = entry["scaffold"]
        changed += 1

    if skipped_no_scaffold:
        print(f"\n  Not in scaffolds.json (still need corpus run): "
              f"{skipped_no_scaffold}")

    return changed


def print_mine_corpus_command():
    ids_str = " ".join(ALL_SCAFFOLD_GAP_IDS)
    print()
    print("=" * 70)
    print("NEXT STEP: Generate scaffolds by running mine_corpus.py")
    print("=" * 70)
    print()
    print("The 26 patterns below have no scaffold. They need a lower coverage")
    print("threshold (0.20 vs default 0.45) and min-matches=1 to generate.")
    print()
    print("Run from your repo root:")
    print()
    print("  python mine_corpus.py \\")
    print("    --coverage-threshold 0.20 \\")
    print("    --min-matches 1 \\")
    print(f"    --patterns {ids_str}")
    print()
    print("Then copy scaffolds into pattern_library.json:")
    print()
    print("  python patch_pattern_library.py --apply-scaffolds")
    print()
    print("Root causes:")
    print(f"  {len(SCAFFOLD_GAPS['single_leaf_threshold'])} patterns: single unique leaf type "
          f"— coverage gate needs threshold <= 0.33")
    print(f"  {len(SCAFFOLD_GAPS['coverage_too_low'])} patterns: multiple leaves but real "
          f"workflows have too many distinct types")
    print(f"  {len(SCAFFOLD_GAPS['insufficient_matches_at_3'])} patterns: 1-2 matches at "
          f"threshold 0.45 — lower threshold will find more")
    print()
    print("NOTE: --min-matches 1 means scaffolds are generated from a single")
    print("representative workflow. For patterns with freq >= 10, review the")
    print("generated scaffold manually — a single-workflow scaffold may not")
    print("generalise well. For low-frequency patterns (freq < 5), one match")
    print("is likely fine.")


def main():
    parser = argparse.ArgumentParser(
        description="Patch pattern_library.json with trigger keywords and scaffolds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply-scaffolds", action="store_true",
        help="Copy scaffolds from scaffolds.json into pattern_library.json"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing"
    )
    args = parser.parse_args()

    if not PATTERN_FILE.exists():
        print(f"ERROR: {PATTERN_FILE} not found.")
        raise SystemExit(1)

    patterns = load_pattern_library()
    print(f"Loaded {len(patterns)} patterns from {PATTERN_FILE.name}")
    print()

    if args.apply_scaffolds:
        print("Applying scaffolds from scaffolds.json...")
        changed = apply_scaffolds(patterns, dry_run=args.dry_run)
        print(f"\n{changed} scaffolds applied.")
        if changed > 0:
            save_pattern_library(patterns, dry_run=args.dry_run)
    else:
        print("Patching trigger keywords...")
        changed = patch_keywords(patterns, dry_run=args.dry_run)
        print(f"\n{changed} patterns updated.")
        if changed > 0:
            save_pattern_library(patterns, dry_run=args.dry_run)
        else:
            print("Nothing to update.")

        # Always print the mine_corpus command, whether or not keywords changed
        print_mine_corpus_command()

        # Summary of current pattern library state
        print("=" * 70)
        print("CURRENT PATTERN LIBRARY STATE")
        print("=" * 70)
        has_kw       = sum(1 for p in patterns if p.get("trigger_keywords"))
        has_scaffold  = sum(1 for p in patterns if p.get("scaffold"))
        fully_ready   = sum(1 for p in patterns
                           if p.get("trigger_keywords") and p.get("scaffold"))
        kw_only       = sum(1 for p in patterns
                           if p.get("trigger_keywords") and not p.get("scaffold"))
        print(f"  Total patterns:          {len(patterns)}")
        print(f"  Has trigger keywords:    {has_kw}")
        print(f"  Has scaffold:            {has_scaffold}")
        print(f"  Fully ready (kw+scaffold): {fully_ready}  ← eligible for MODE 1")
        print(f"  Keywords but no scaffold:  {kw_only}  ← will match but fall to MODE 2")
        print()
        print("After running mine_corpus.py + --apply-scaffolds:")
        projected = fully_ready + min(len(ALL_SCAFFOLD_GAP_IDS), 26)
        print(f"  Projected fully-ready patterns: ~{projected} "
              f"(+{projected - fully_ready} from scaffold generation)")


if __name__ == "__main__":
    main()
    