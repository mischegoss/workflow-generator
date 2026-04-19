"""
build_activity_data.py
======================
Reads data/REMOVED_SECRET.md and existing data files,
then produces three new/updated data files:

  data/activities_detailed.json
      529 activities with description, mandatory fields, optional fields,
      output type (joined from activity_output_registry), keyed by CustomTypeName.
      Used by: annotation_tools.py (Category 6), validate_activity_schema(),
               _scaffold_node() for auto-generated _sif rules.

  data/activities_controls_extended.json
      Replaces data/activities_controls.json with full 529-activity coverage.
      Same format as activities_controls.json — drop-in replacement.
      Used by: validate_activity_schema() required-field checks.

  data/activity_output_registry_fixed.json
      Replaces data/activity_output_registry.json with all entries keyed
      by CustomTypeName (fixing the display-name mismatch that silently
      disables Category 6 for ~297 activities).
      Same format as activity_output_registry.json — drop-in replacement.

Usage:
    python build_activity_data.py
    python build_activity_data.py --data-dir ./data --dry-run

After verifying the three output files, you can:
  - Replace activities_controls.json with activities_controls_extended.json
  - Replace activity_output_registry.json with activity_output_registry_fixed.json
  - Remove REMOVED_SECRET.md from data/
  - Remove REMOVED_SECRET.docx from data/
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


# REMOVED_SECRET
# Args
# REMOVED_SECRET

parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", default="./data", help="Path to the data directory")
parser.add_argument("--dry-run", action="store_true", help="Print stats, write nothing")
args = parser.parse_args()

DATA_DIR = Path(args.data_dir)

MD_PATH       = DATA_DIR / "REMOVED_SECRET.md"
SYNTAX_PATH   = DATA_DIR / "activity_json_syntax.json"
CONTROLS_PATH = DATA_DIR / "activities_controls.json"
REGISTRY_PATH = DATA_DIR / "activity_output_registry.json"

for p in [MD_PATH, SYNTAX_PATH, CONTROLS_PATH, REGISTRY_PATH]:
    if not p.exists():
        print(f"ERROR: required file not found: {p}")
        sys.exit(1)

print("=" * 60)
print("BUILD ACTIVITY DATA")
print("=" * 60)
print(f"Data dir: {DATA_DIR.resolve()}")
print()


# REMOVED_SECRET
# Step 1: Parse the markdown into structured activity entries
# REMOVED_SECRET

print("[1/5] Parsing REMOVED_SECRET.md...")

def _parse_options(block: str) -> list[str]:
    """Extract 'Options: A, B, C' line into a list."""
    m = re.search(r'- Options:\s+(.+)', block)
    if not m:
        return []
    return [o.strip() for o in m.group(1).split(',') if o.strip()]

def _parse_default(block: str) -> str | None:
    """Extract 'Default Value: X' from a field block."""
    m = re.search(r'- Default Value:\s+`([^`]+)`', block)
    return m.group(1) if m else None

def _parse_fields(section_text: str, header: str) -> list[dict]:
    """Parse either Mandatory or Optional field blocks from a section."""
    # Find the subsection
    idx = section_text.find(f"### {header}")
    if idx == -1:
        return []
    
    # Take text until the next ### or end
    sub = section_text[idx:]
    next_header = sub.find("###", 3)
    if next_header > 0:
        sub = sub[:next_header]
    
    fields = []
    # Each field starts with "- **Field Name**"
    field_blocks = re.split(r'\n- \*\*', sub)
    for block in field_blocks[1:]:  # skip the header block
        lines = block.split('\n')
        label = lines[0].rstrip('*').strip()
        
        field_key_m   = re.search(r'Field Key:\s+`([^`]+)`', block)
        input_type_m  = re.search(r'Input Type:\s+(\w+)', block)
        data_type_m   = re.search(r'Data Type:\s+(.+?)(?:\n|$)', block)
        required_m    = re.search(r'Required:\s+(Yes|No)', block)
        
        if not field_key_m:
            continue
        
        field = {
            "fieldName":  label,
            "fieldKey":   field_key_m.group(1),
            "inputType":  input_type_m.group(1) if input_type_m else "textbox",
            "dataType":   data_type_m.group(1).strip() if data_type_m else "String",
            "required":   required_m.group(1) == "Yes" if required_m else False,
        }
        
        options = _parse_options(block)
        if options:
            field["options"] = options
        
        default = _parse_default(block)
        if default:
            field["defaultValue"] = default
        
        fields.append(field)
    
    return fields

with open(MD_PATH, encoding="utf-8") as f:
    md_content = f.read()

# Normalize line endings — the file may use \r\n (Windows)
md_content = md_content.replace('\r\n', '\n').replace('\r', '\n')

# Split into activity sections on "## " headers
# Use re.split on the pattern "^## " (start of line) to catch all variants
sections = re.split(r'\n## ', md_content)

# Debug: count sections and track skipped ones
skipped_blank = 0
skipped_debug = []

doc_activities: dict[str, dict] = {}   # display_name → parsed entry

for sec in sections[1:]:
    lines = sec.split('\n')
    display_name = lines[0].strip()
    if not display_name:
        skipped_blank += 1
        continue
    # Track sections with unusual content (no mandatory/optional headers at all)
    if 'Mandatory Input Values' not in sec and 'Recommended Optional Values' not in sec:
        skipped_debug.append(f"  No field headers: \"{display_name[:60]}\"")
    
    # Extract description
    desc_m = re.search(r'\*\*Description:\*\*\s+(.+?)(?:\n|$)', sec)
    description = desc_m.group(1).strip() if desc_m else ""
    # Clean up the boilerplate suffix
    description = re.sub(
        r'\s*This activity allows users to configure and execute .+? operations within a workflow\..*',
        '', description
    ).strip()
    
    mandatory = _parse_fields(sec, "Mandatory Input Values")
    optional  = _parse_fields(sec, "Recommended Optional Values")
    
    doc_activities[display_name] = {
        "displayName":              display_name,
        "description":              description,
        "mandatoryInputValues":     mandatory,
        "recommendedOptionalValues": optional,
    }

print(f"  Parsed {len(doc_activities)} activity entries from markdown")
print(f"  Total sections found: {len(sections)-1}  |  Blank names skipped: {skipped_blank}")
if skipped_debug:
    print(f"  Sections with no field headers ({len(skipped_debug)}):") 
    for d in skipped_debug[:10]:
        print(d)
    if len(skipped_debug) > 10:
        print(f"  ... and {len(skipped_debug)-10} more")


# REMOVED_SECRET
# Step 2: Build display_name → CustomTypeName bridge via activity_json_syntax
# REMOVED_SECRET

print("[2/5] Building display name → CustomTypeName bridge...")

with open(SYNTAX_PATH, encoding="utf-8") as f:
    syntax_data = json.load(f)

templates = syntax_data.get("settings", syntax_data) if isinstance(syntax_data, dict) else syntax_data

# Build normalised lookup: stripped_lower → TypeName
# activity_json_syntax uses label == TypeName for most entries
def _normalise(s: str) -> str:
    """Remove spaces, hyphens, underscores, lowercase."""
    return re.sub(r'[\s\-_]', '', s).lower()

ct_by_norm: dict[str, str] = {}
ct_by_label: dict[str, str] = {}  # exact label match
for t in templates:
    ct = t.get("TypeName") or t.get("CustomTypeName") or t.get("name", "")
    if not ct:
        continue
    label = t.get("label", ct).strip()
    ct_by_norm[_normalise(ct)] = ct
    ct_by_norm[_normalise(label)] = ct
    ct_by_label[label] = ct

# Also load existing controls to get activityNames already mapped
with open(CONTROLS_PATH, encoding="utf-8") as f:
    existing_controls = json.load(f)

ctrl_names_set = {e["activityName"] for e in existing_controls}

# Map each doc display name to a CustomTypeName
display_to_ct: dict[str, str] = {}
unmatched: list[str] = []

for display in doc_activities:
    norm = _normalise(display)
    if norm in ct_by_norm:
        display_to_ct[display] = ct_by_norm[norm]
    elif display in ct_by_label:
        display_to_ct[display] = ct_by_label[display]
    else:
        # Try removing common words: "Activity", spaces
        alt = _normalise(display.replace(" Activity", ""))
        if alt in ct_by_norm:
            display_to_ct[display] = ct_by_norm[alt]
        else:
            unmatched.append(display)

print(f"  Matched: {len(display_to_ct)}/{len(doc_activities)}")
if unmatched:
    print(f"  Unmatched ({len(unmatched)}) — will use CamelCase fallback CT:")
    for u in unmatched[:20]:
        # If no spaces: the name is already in its intended form (e.g. Actions_StartWorkflowExecution)
        # Just use it verbatim rather than running capitalize() which would lowercase it.
        if ' ' not in u:
            fallback = u
        else:
            # Strict PascalCase: capitalize every word.
            # Words already all-uppercase are acronyms (AWS, AD, XML, SSM) — preserve as-is.
            fallback = "".join(
                w if w.isupper() and len(w) >= 2 else w.capitalize()
                for w in u.split()
            )
        print(f"    \"{u}\" → {fallback} (FALLBACK)")
        display_to_ct[u] = fallback
    if len(unmatched) > 20:
        print(f"    ... and {len(unmatched)-20} more")

# Invert: ct → display (for registry fix)
ct_to_display: dict[str, str] = {v: k for k, v in display_to_ct.items()}


# REMOVED_SECRET
# Step 3: Load existing output registry
# REMOVED_SECRET

print("[3/5] Loading and fixing activity_output_registry.json...")

with open(REGISTRY_PATH, encoding="utf-8") as f:
    existing_registry = json.load(f)

# Build: ct → {outputType, outputDescription}
# Track collisions — when two display names resolve to the same CT, last-write-wins.
registry_by_ct: dict[str, dict] = {}
collisions: list[str] = []

for entry in existing_registry:
    act_name = entry.get("activityName", "")
    if not act_name:
        continue
    
    out_type = entry.get("outputType", "")
    out_desc = entry.get("outputDescription", "")
    
    norm = _normalise(act_name)
    if norm in ct_by_norm:
        ct = ct_by_norm[norm]
    elif act_name in ctrl_names_set:
        ct = act_name  # already a CustomTypeName
    else:
        ct = act_name  # best effort
    
    if ct in registry_by_ct:
        collisions.append(f"  Collision: \"{act_name}\" → {ct} (already set by another entry)")
    registry_by_ct[ct] = {"outputType": out_type, "outputDescription": out_desc}

print(f"  Registry entries resolved: {len(registry_by_ct)}")
if collisions:
    print(f"  Collisions (same CT from different display names — last-write-wins):")
    for c in collisions:
        print(c)

# Count how many were display names (broken) vs already CT
already_ct = sum(1 for e in existing_registry if e.get("activityName", "") in ctrl_names_set
                 or _normalise(e.get("activityName","")) in ct_by_norm)
print(f"  Previously broken (display names): ~{len(existing_registry) - already_ct}")


# REMOVED_SECRET
# Step 4: Build activities_detailed.json
# REMOVED_SECRET

print("[4/5] Building activities_detailed.json...")

# Combine: doc entry + output registry
detailed: list[dict] = []
no_output_type = []

for display, entry in doc_activities.items():
    ct = display_to_ct.get(display, re.sub(r'\s+', '', display))
    
    registry_info = registry_by_ct.get(ct, {})
    output_type = registry_info.get("outputType", "")
    output_desc = registry_info.get("outputDescription", "")
    
    if not output_type:
        no_output_type.append(ct)
    
    has_enum = any(
        "options" in f
        for f in entry["mandatoryInputValues"] + entry["recommendedOptionalValues"]
    )
    
    detailed.append({
        "activityName":              ct,
        "displayName":               display,
        "description":               entry["description"],
        "outputType":                output_type,
        "outputDescription":         output_desc,
        "mandatoryInputValues":      entry["mandatoryInputValues"],
        "recommendedOptionalValues": entry["recommendedOptionalValues"],
        "hasEnumFields":             has_enum,
        "mandatoryCount":            len(entry["mandatoryInputValues"]),
        "optionalCount":             len(entry["recommendedOptionalValues"]),
    })

# Sort by activityName for stability
detailed.sort(key=lambda x: x["activityName"])

print(f"  Built {len(detailed)} entries")
print(f"  Activities with no output type (not in registry): {len(no_output_type)}")
if no_output_type[:10]:
    print(f"  Sample: {no_output_type[:10]}")


# REMOVED_SECRET
# Step 5: Build activities_controls_extended.json
# REMOVED_SECRET

print("[5/5] Building activities_controls_extended.json...")

# Start with existing controls (already correctly keyed by CustomTypeName)
extended_controls_by_ct: dict[str, list] = {
    e["activityName"]: e["controls"] for e in existing_controls
}

# Add/extend from doc for any activity not already in controls
added = 0
updated = 0
for entry in detailed:
    ct = entry["activityName"]
    
    all_fields = entry["mandatoryInputValues"] + entry["recommendedOptionalValues"]
    if not all_fields:
        continue
    
    # Convert to activities_controls.json format
    controls = [
        {
            "fieldName": f["fieldName"],
            "fieldKey":  f["fieldKey"],
            "inputType": f["inputType"],
            "dataType":  f["dataType"],
            "required":  f["required"],
            **({"options": f["options"]} if "options" in f else {}),
            **({"defaultValue": f["defaultValue"]} if "defaultValue" in f else {}),
        }
        for f in all_fields
    ]
    
    if ct not in extended_controls_by_ct:
        extended_controls_by_ct[ct] = controls
        added += 1
    else:
        # Merge: add fields from doc not already in existing controls
        existing_keys = {c["fieldKey"] for c in extended_controls_by_ct[ct]}
        new_fields = [c for c in controls if c["fieldKey"] not in existing_keys]
        if new_fields:
            extended_controls_by_ct[ct].extend(new_fields)
            updated += 1

extended_controls = [
    {"activityName": ct, "controls": ctrls}
    for ct, ctrls in sorted(extended_controls_by_ct.items())
]

print(f"  Existing entries:  {len(existing_controls)}")
print(f"  New from doc:      {added}")
print(f"  Existing extended: {updated}")
print(f"  Total:             {len(extended_controls)}")


# Build fixed output registry — keep ALL entries including those with no outputType.
# The original filter `if info["outputType"]` was dropping 4 valid entries
# that happen to have an empty or missing outputType field.
registry_fixed = [
    {
        "activityName":      ct,
        "outputType":        info["outputType"],
        "outputDescription": info["outputDescription"],
    }
    for ct, info in sorted(registry_by_ct.items())
]
print(f"  Fixed registry:    {len(registry_fixed)} entries (was {len(existing_registry)})")


# REMOVED_SECRET
# Write outputs
# REMOVED_SECRET

OUT_DETAILED  = DATA_DIR / "activities_detailed.json"
OUT_CONTROLS  = DATA_DIR / "activities_controls_extended.json"
OUT_REGISTRY  = DATA_DIR / "activity_output_registry_fixed.json"

if args.dry_run:
    print()
    print("Dry run — nothing written.")
    print(f"Would write: {OUT_DETAILED}  ({len(detailed)} entries)")
    print(f"Would write: {OUT_CONTROLS}  ({len(extended_controls)} entries)")
    print(f"Would write: {OUT_REGISTRY}  ({len(registry_fixed)} entries)")
    sys.exit(0)

OUT_DETAILED.write_text(json.dumps(detailed, indent=2, ensure_ascii=False), encoding="utf-8")
OUT_CONTROLS.write_text(json.dumps(extended_controls, indent=2, ensure_ascii=False), encoding="utf-8")
OUT_REGISTRY.write_text(json.dumps(registry_fixed, indent=2, ensure_ascii=False), encoding="utf-8")

print()
print("=" * 60)
print("WRITTEN")
print("=" * 60)
print(f"  {OUT_DETAILED}  ({len(detailed)} entries)")
print(f"  {OUT_CONTROLS}  ({len(extended_controls)} entries)")
print(f"  {OUT_REGISTRY}  ({len(registry_fixed)} entries)")
print()
print("NEXT STEPS:")
print("  1. Spot-check activities_detailed.json for a few key activities")
print("     (GetRowsCount, Ping, JsonToTable, MemorySet)")
print("  2. Verify activities_controls_extended.json has correct required fields")
print("  3. Verify activity_output_registry_fixed.json has CustomTypeName keys")
print("  4. When satisfied:")
print("     cp data/activities_controls_extended.json data/activities_controls.json")
print("     cp data/activity_output_registry_fixed.json data/activity_output_registry.json")
print("  5. Remove temporary source files:")
print("     rm data/REMOVED_SECRET.md")
print("     rm 'data/REMOVED_SECRET.docx'")
print()
print("PIPELINE INTEGRATION (after replacing files):")
print("  - validate_activity_schema() gains full 529-activity required-field coverage")
print("  - activity_output_registry Category 6 checks activate for all activities")
print("  - Load activities_detailed.json in annotation_tools._load_detailed_index()")
print("    for description-based retrieval enrichment")