"""
patch_output_registry.py

Remaps activity_output_registry.json entries from display names to CustomTypeNames
so that annotation_tools.py Category 6 lookups resolve correctly.

Resolution order per entry:
  1. Already a CustomTypeName — leave it
  2. Exact display name match from syntax file
  3. Space removal (e.g. 'Get Rows Count' -> 'GetRowsCount')
  4. Case-insensitive match against all CustomTypeNames
  5. Unresolved — leave as-is and report

Run from repo root:
  python3 patch_output_registry.py

Writes output to data/activity_output_registry.json (backs up original first).
"""

import json
import shutil
from pathlib import Path

DATA_DIR = Path("data")
SYNTAX_FILE  = DATA_DIR / "activity_json_syntax.json"
REGISTRY_FILE = DATA_DIR / "activity_output_registry.json"
BACKUP_FILE  = DATA_DIR / "activity_output_registry.json.bak"

# ---------------------------------------------------------------------------
# Appendix A — 30 high-frequency activities missing from registry entirely.
# These are ADDED if not already present after remapping.
# ---------------------------------------------------------------------------
APPENDIX_A = [
    {"activityName": "GetCellValue",            "outputType": "Scalar",    "outputDescription": "Returns single cell value (string)"},
    {"activityName": "MemorySet",               "outputType": "None",      "outputDescription": "Stores value, produces no output variable"},
    {"activityName": "DisplayValue",            "outputType": "None",      "outputDescription": "Writes to log, produces no output variable"},
    {"activityName": "GetRowsCount",            "outputType": "Scalar",    "outputDescription": "Returns integer count"},
    {"activityName": "CreateMemoryTable",       "outputType": "DataTable", "outputDescription": "Creates and returns a ResultSet"},
    {"activityName": "ResultSetFilter",         "outputType": "DataTable", "outputDescription": "Returns filtered ResultSet"},
    {"activityName": "MultiMemorySet",          "outputType": "None",      "outputDescription": "Stores multiple values, no output"},
    {"activityName": "RunWorkflow",             "outputType": "None",      "outputDescription": "Executes child workflow, no return"},
    {"activityName": "TSQLQuery",               "outputType": "DataTable", "outputDescription": "Returns query ResultSet"},
    {"activityName": "TSQLStatement",           "outputType": "None",      "outputDescription": "Executes INSERT/UPDATE, no ResultSet"},
    {"activityName": "GetDate",                 "outputType": "Scalar",    "outputDescription": "Returns date string"},
    {"activityName": "JsonToTable",             "outputType": "DataTable", "outputDescription": "Returns parsed ResultSet"},
    {"activityName": "HTTPRequest",             "outputType": "DataTable", "outputDescription": "Returns 3-column table (Status Code, Body, Request)"},
    {"activityName": "SendEmail",               "outputType": "None",      "outputDescription": "Sends email, no output variable"},
    {"activityName": "PowerShellScript",        "outputType": "Status",    "outputDescription": "Returns Success/Failure"},
    {"activityName": "PowerShell",              "outputType": "Status",    "outputDescription": "Returns Success/Failure"},
    {"activityName": "DateDifference",          "outputType": "Scalar",    "outputDescription": "Returns numeric difference"},
    {"activityName": "IsEmpty",                 "outputType": "Boolean",   "outputDescription": "Returns True/False"},
    {"activityName": "ConvertToHTMLTable",      "outputType": "Scalar",    "outputDescription": "Returns HTML string"},
    {"activityName": "MemoryTableUnion",        "outputType": "DataTable", "outputDescription": "Returns merged ResultSet"},
    {"activityName": "DeleteMemoryTableRows",   "outputType": "DataTable", "outputDescription": "Returns modified ResultSet"},
    {"activityName": "AddMemoryTableRow",       "outputType": "DataTable", "outputDescription": "Returns modified ResultSet"},
    {"activityName": "SetCellValue",            "outputType": "None",      "outputDescription": "Modifies table in place, no output"},
    {"activityName": "GoTo",                    "outputType": "None",      "outputDescription": "Flow control, no output"},
    {"activityName": "Wait",                    "outputType": "None",      "outputDescription": "Pauses execution, no output"},
    {"activityName": "TerminateWorkflow",       "outputType": "None",      "outputDescription": "Stops execution, no output"},
    {"activityName": "Split",                   "outputType": "DataTable", "outputDescription": "Returns table of split segments"},
    {"activityName": "MemoryClean",             "outputType": "None",      "outputDescription": "Clears variable, no output"},
    {"activityName": "StartJsonSession",        "outputType": "None",      "outputDescription": "Opens session, no output variable"},
    {"activityName": "ConvertPasswordToPlaintext", "outputType": "Scalar", "outputDescription": "Returns plaintext string"},
]


def build_lookup(syntax_file: Path) -> tuple[set, dict, dict]:
    """Returns (custom_names, display_to_custom, lower_to_custom)."""
    with open(syntax_file) as f:
        syntax = json.load(f)

    custom_names = set()
    display_to_custom = {}
    lower_to_custom = {}

    for act in syntax.get("settings", []):
        custom = act.get("CustomTypeName") or act.get("name")
        display = act.get("DisplayName", "")
        if not custom:
            continue
        custom_names.add(custom)
        lower_to_custom[custom.lower()] = custom
        if display and display != custom:
            display_to_custom[display] = custom

    return custom_names, display_to_custom, lower_to_custom


def remap_entry(name: str, custom_names: set, display_to_custom: dict,
                lower_to_custom: dict) -> tuple[str, str]:
    """
    Returns (resolved_name, method) where method describes how it was resolved.
    """
    # 1. Already correct
    if name in custom_names:
        return name, "already_correct"

    # 2. Exact display name match
    if name in display_to_custom:
        return display_to_custom[name], "display_match"

    # 3. Space removal
    camel = "".join(name.split())
    if camel in custom_names:
        return camel, "space_removal"

    # 4. Case-insensitive match
    lower = camel.lower()
    if lower in lower_to_custom:
        return lower_to_custom[lower], "case_insensitive"

    # 5. Unresolved
    return name, "unresolved"


def main():
    print("Loading data files...")
    custom_names, display_to_custom, lower_to_custom = build_lookup(SYNTAX_FILE)

    with open(REGISTRY_FILE) as f:
        registry = json.load(f)

    print(f"Registry entries: {len(registry)}")

    # Back up original
    shutil.copy(REGISTRY_FILE, BACKUP_FILE)
    print(f"Backup written to {BACKUP_FILE}")

    # Remap
    counts = {"already_correct": 0, "display_match": 0,
              "space_removal": 0, "case_insensitive": 0, "unresolved": 0}
    unresolved = []

    for entry in registry:
        old_name = entry["activityName"]
        new_name, method = remap_entry(old_name, custom_names, display_to_custom, lower_to_custom)
        entry["activityName"] = new_name
        counts[method] += 1
        if method == "unresolved":
            unresolved.append(old_name)

    # Add Appendix A entries (skip any already present after remapping)
    existing_names = {e["activityName"] for e in registry}
    added = []
    for entry in APPENDIX_A:
        if entry["activityName"] not in existing_names:
            registry.append(entry)
            added.append(entry["activityName"])

    # Write output
    with open(REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)

    # Report
    print()
    print("=== Results ===")
    print(f"  Already correct:      {counts['already_correct']}")
    print(f"  Display name match:   {counts['display_match']}")
    print(f"  Space removal:        {counts['space_removal']}")
    print(f"  Case-insensitive:     {counts['case_insensitive']}")
    print(f"  Unresolved (skipped): {counts['unresolved']}")
    print(f"  Appendix A added:     {len(added)}")
    print(f"  Total entries now:    {len(registry)}")
    print()

    if unresolved:
        print(f"=== {len(unresolved)} Unresolved (Category 6 still disabled for these) ===")
        for name in unresolved:
            print(f"  {name}")

    if added:
        print()
        print(f"=== Appendix A entries added ===")
        for name in added:
            print(f"  {name}")

    print()
    print("Done. Written to", REGISTRY_FILE)


if __name__ == "__main__":
    main()