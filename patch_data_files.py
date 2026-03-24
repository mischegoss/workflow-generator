"""
patch_data_files.py
───────────────────
Applies four targeted fixes identified during the data files review.
Run from the repo root:

    python3 patch_data_files.py
    python3 patch_data_files.py --dry-run   # preview only, write nothing

Fixes applied
─────────────
1. data/wiring_map.json
   Remove three WhileActivity-as-source wiring entries that are wrong on the
   platform. WhileActivity produces no output variables and cannot be a valid
   wire source. These are corpus artefacts where the xName resolution picked up
   the WhileActivity xName instead of ExitWhile.

   Entries removed:
     WhileActivity → GetRows.RowNumber          (7 wf, 100%)
     WhileActivity → ADListGroup.GroupName       (4 wf, 80%)
     WhileActivity → GetColumnName.ColumnNumber  (3 wf, 100%)

2. data/enum_values.json
   Remove StartJsonSession.SessionName from enum_values. The values observed
   in the corpus ("fsDataSession", "jsonSessionCurrUserData", etc.) are
   environment-specific session identifiers, not valid platform enum choices.
   SessionName is a free-text connection name, not a dropdown field. Having
   it in enum_values would cause load_activity_template() to seed the field
   with a customer-specific value as the default.

3. mine_corpus.py
   Add the three WhileActivity wiring suppressions to WIRING_SUPPRESSIONS so
   future runs of the miner do not regenerate the bad entries.

4. agents/structure_builder_agent.py
   Two additions to the INSTRUCTION string:
   a) Update the activity_manifest CONTEXT description to document the new
      pre_filled_fields key that run_retrieval() will add in the pipeline redesign.
   b) Add a PRE-FILLED FIELDS rule block in the PLATFORM RULES section so
      StructureBuilder treats those values as authoritative and does not override
      them during assembly.

Dependencies: Python stdlib only. No ADK or third-party imports.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
#  Repo root detection
# ─────────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent

# Expected file paths (relative to repo root)
WIRING_MAP_PATH      = REPO_ROOT / "data" / "wiring_map.json"
ENUM_VALUES_PATH     = REPO_ROOT / "data" / "enum_values.json"
MINE_CORPUS_PATH     = REPO_ROOT / "mine_corpus.py"
STRUCT_BUILDER_PATH  = REPO_ROOT / "agents" / "structure_builder_agent.py"


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _backup(path: Path) -> Path:
    """Write a .bak copy alongside the original. Returns backup path."""
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


def _check_path(path: Path, label: str) -> bool:
    if not path.exists():
        print(f"  [SKIP] {label}: file not found at {path}")
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 1 — wiring_map.json: remove bad WhileActivity entries
# ─────────────────────────────────────────────────────────────────────────────

# These (source, target, field) triples are wrong: WhileActivity cannot be a
# wire source because it produces no output variables. The corpus artefact
# occurs because some workflows use the WhileActivity xName in a RowNumber
# or similar field instead of the correct ExitWhile xName.
WIRING_REMOVE = {
    ("WhileActivity", "GetRows",       "RowNumber"),
    ("WhileActivity", "ADListGroup",   "GroupName"),
    ("WhileActivity", "GetColumnName", "ColumnNumber"),
}


def patch_wiring_map(dry_run: bool) -> bool:
    label = "wiring_map.json"
    if not _check_path(WIRING_MAP_PATH, label):
        return False

    with open(WIRING_MAP_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    before = len(entries)
    kept    = []
    removed = []

    for entry in entries:
        key = (
            entry.get("source_activity", ""),
            entry.get("target_activity", ""),
            entry.get("target_field", ""),
        )
        if key in WIRING_REMOVE:
            removed.append(entry)
        else:
            kept.append(entry)

    after = len(kept)

    if not removed:
        print(f"  [OK]   {label}: all 3 bad entries already absent — nothing to do")
        return True

    for e in removed:
        print(
            f"  [RM]   {label}: {e['source_activity']} -> "
            f"{e['target_activity']}.{e['target_field']} "
            f"({e.get('workflow_count', '?')} wf, {e.get('pct_of_target', '?')}%)"
        )

    if not dry_run:
        bak = _backup(WIRING_MAP_PATH)
        print(f"  [BAK]  {label}: backup written to {bak.name}")
        with open(WIRING_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2)
        print(f"  [DONE] {label}: {before} entries -> {after} entries ({len(removed)} removed)")
    else:
        print(f"  [DRY]  {label}: would remove {len(removed)} entries ({before} -> {after})")

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 2 — enum_values.json: remove StartJsonSession.SessionName
# ─────────────────────────────────────────────────────────────────────────────

# SessionName on StartJsonSession holds environment-specific connection
# identifiers (e.g. "fsDataSession", "UserJobsSession"). These are not
# valid platform enum choices — they're free-text names chosen by the
# workflow developer. Having them in enum_values would cause
# load_activity_template() to seed the field with a customer-specific value.
ENUM_REMOVE = {
    "StartJsonSession": {"SessionName"},
}


def patch_enum_values(dry_run: bool) -> bool:
    label = "enum_values.json"
    if not _check_path(ENUM_VALUES_PATH, label):
        return False

    with open(ENUM_VALUES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    patched = False
    for activity, fields_to_remove in ENUM_REMOVE.items():
        if activity not in data:
            print(f"  [OK]   {label}: {activity} not present — nothing to do")
            continue
        for field in fields_to_remove:
            if field not in data[activity]:
                print(f"  [OK]   {label}: {activity}.{field} already absent — nothing to do")
                continue
            obs = data[activity][field].get("total_observations", "?")
            vals = [v["value"] for v in data[activity][field].get("values", [])]
            print(
                f"  [RM]   {label}: {activity}.{field} "
                f"({obs} observations, values: {vals})"
            )
            if not dry_run:
                del data[activity][field]
                patched = True
            else:
                print(f"  [DRY]  {label}: would remove {activity}.{field}")

        # If the activity has no remaining fields after removal, remove the
        # activity entry entirely to keep the file clean.
        if not dry_run and activity in data and not data[activity]:
            del data[activity]
            print(f"  [RM]   {label}: {activity} entry now empty — removed entirely")
            patched = True

    if patched and not dry_run:
        bak = _backup(ENUM_VALUES_PATH)
        print(f"  [BAK]  {label}: backup written to {bak.name}")
        with open(ENUM_VALUES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"  [DONE] {label}: patch applied")

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 3 — mine_corpus.py: add new WIRING_SUPPRESSIONS entries
# ─────────────────────────────────────────────────────────────────────────────

# The three entries added to WIRING_REMOVE above must also be suppressed in
# the miner so they are not regenerated on future corpus runs.

OLD_SUPPRESSIONS = """\
WIRING_SUPPRESSIONS = {
    ("WhileActivity", "GetCellValue", "RowNumber"),
    ("WhileActivity", "SetCellValue", "RowNumber"),
}"""

NEW_SUPPRESSIONS = """\
WIRING_SUPPRESSIONS = {
    # Confirmed wrong: WhileActivity produces no output variables.
    # ExitWhile is the correct source for all row-index wires.
    ("WhileActivity", "GetCellValue",  "RowNumber"),
    ("WhileActivity", "SetCellValue",  "RowNumber"),
    ("WhileActivity", "GetRows",       "RowNumber"),
    ("WhileActivity", "ADListGroup",   "GroupName"),
    ("WhileActivity", "GetColumnName", "ColumnNumber"),
}"""


def patch_mine_corpus(dry_run: bool) -> bool:
    label = "mine_corpus.py"
    if not _check_path(MINE_CORPUS_PATH, label):
        return False

    text = MINE_CORPUS_PATH.read_text(encoding="utf-8")

    if NEW_SUPPRESSIONS in text:
        print(f"  [OK]   {label}: WIRING_SUPPRESSIONS already up to date — nothing to do")
        return True

    if OLD_SUPPRESSIONS not in text:
        # Try to find partial match for diagnostics
        if "WIRING_SUPPRESSIONS" in text:
            print(
                f"  [WARN] {label}: WIRING_SUPPRESSIONS found but does not match expected "
                f"format — manual edit required"
            )
        else:
            print(f"  [WARN] {label}: WIRING_SUPPRESSIONS not found — manual edit required")
        return False

    updated = text.replace(OLD_SUPPRESSIONS, NEW_SUPPRESSIONS, 1)

    if not dry_run:
        bak = _backup(MINE_CORPUS_PATH)
        print(f"  [BAK]  {label}: backup written to {bak.name}")
        MINE_CORPUS_PATH.write_text(updated, encoding="utf-8")
        print(f"  [DONE] {label}: added 3 entries to WIRING_SUPPRESSIONS")
    else:
        print(f"  [DRY]  {label}: would add 3 entries to WIRING_SUPPRESSIONS")

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Fix 4 — agents/structure_builder_agent.py: add pre_filled_fields support
# ─────────────────────────────────────────────────────────────────────────────

# Change 4a: update the activity_manifest CONTEXT description to document
# the pre_filled_fields key that run_retrieval() will attach.
OLD_MANIFEST_DESC = (
    "- 'activity_manifest': compact list — each entry has step_id, selected_activity, status,\n"
    "  and frequency_tier only. No candidates array. (from ActivityRetrieverAgent)"
)

NEW_MANIFEST_DESC = (
    "- 'activity_manifest': compact list — each entry has step_id, selected_activity, status,\n"
    "  and frequency_tier. Some entries also include pre_filled_fields: a dict of\n"
    "  field_key -> value pairs confirmed by corpus analysis. Treat these as authoritative."
)

# Change 4b: insert a PRE-FILLED FIELDS rule block into the PLATFORM RULES section.
# Inserted immediately before the ROW ACCESS block so it appears in logical order
# (output variables -> field wiring -> row access).
PRE_FILLED_ANCHOR = "ROW ACCESS — MOST CRITICAL RULE:"

PRE_FILLED_BLOCK = """\
PRE-FILLED FIELDS — treat as authoritative
If a manifest entry contains a pre_filled_fields dict, those field values have been
confirmed by analysis of 609 real workflows. Use them exactly as provided.
Do NOT override, ignore, or second-guess pre_filled_fields values.
Fields not covered by pre_filled_fields are filled using your normal assembly logic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def patch_structure_builder(dry_run: bool) -> bool:
    label = "agents/structure_builder_agent.py"
    if not _check_path(STRUCT_BUILDER_PATH, label):
        return False

    text = STRUCT_BUILDER_PATH.read_text(encoding="utf-8")
    original = text
    changes = []

    # ── Change 4a: update CONTEXT manifest description ───────────────────────
    if NEW_MANIFEST_DESC in text:
        print(f"  [OK]   {label}: activity_manifest CONTEXT already updated — skipping 4a")
    elif OLD_MANIFEST_DESC in text:
        text = text.replace(OLD_MANIFEST_DESC, NEW_MANIFEST_DESC, 1)
        changes.append("4a: updated activity_manifest CONTEXT description")
    else:
        print(
            f"  [WARN] {label}: could not find expected activity_manifest CONTEXT string.\n"
            f"         Manual edit required for change 4a.\n"
            f"         Expected line:\n"
            f"           {OLD_MANIFEST_DESC.splitlines()[0]}"
        )

    # ── Change 4b: insert PRE-FILLED FIELDS rule block ───────────────────────
    if PRE_FILLED_BLOCK.strip() in text:
        print(f"  [OK]   {label}: PRE-FILLED FIELDS rule already present — skipping 4b")
    elif PRE_FILLED_ANCHOR in text:
        # Insert the new block immediately before the ROW ACCESS section header
        text = text.replace(
            PRE_FILLED_ANCHOR,
            PRE_FILLED_BLOCK + PRE_FILLED_ANCHOR,
            1,
        )
        changes.append("4b: inserted PRE-FILLED FIELDS rule block before ROW ACCESS")
    else:
        print(
            f"  [WARN] {label}: could not find ROW ACCESS anchor in PLATFORM RULES.\n"
            f"         Manual edit required for change 4b."
        )

    if not changes:
        return True

    if not dry_run:
        if text == original:
            print(f"  [OK]   {label}: no changes needed")
            return True
        bak = _backup(STRUCT_BUILDER_PATH)
        print(f"  [BAK]  {label}: backup written to {bak.name}")
        STRUCT_BUILDER_PATH.write_text(text, encoding="utf-8")
        for c in changes:
            print(f"  [DONE] {label}: {c}")
    else:
        for c in changes:
            print(f"  [DRY]  {label}: would apply {c}")

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Apply data file fixes identified in the pipeline redesign review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing anything.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — no files will be modified\n")

    results = {}

    print("[1/4] Patching data/wiring_map.json — remove bad WhileActivity wires")
    results["wiring_map"]      = patch_wiring_map(args.dry_run)

    print("\n[2/4] Patching data/enum_values.json — remove StartJsonSession.SessionName")
    results["enum_values"]     = patch_enum_values(args.dry_run)

    print("\n[3/4] Patching mine_corpus.py — add 3 entries to WIRING_SUPPRESSIONS")
    results["mine_corpus"]     = patch_mine_corpus(args.dry_run)

    print("\n[4/4] Patching agents/structure_builder_agent.py — pre_filled_fields support")
    results["struct_builder"]  = patch_structure_builder(args.dry_run)

    # ── Summary ───────────────────────────────────────────────────────────────
    ok      = sum(1 for v in results.values() if v)
    skipped = sum(1 for v in results.values() if not v)
    print(f"\n{'DRY RUN ' if args.dry_run else ''}Results: {ok}/4 patches applied"
          + (f", {skipped} skipped/warned" if skipped else ""))

    if skipped:
        print("\nFiles marked [WARN] require a manual edit. See messages above for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()