"""
mine_returnvalue_context.py
===========================
Mines Resolve Actions workflow XMLs to answer one question:

    For each activity type that immediately precedes an IfElseActivity,
    what are the ReturnValue.Type, ConditionType, and Value distributions
    across all branches that test that activity's output?

This tells the pipeline when to use Type="UserDefinedValue" vs
Type="StoredValue" — a distinction that cannot be inferred from a single
workflow and must be derived from corpus frequency.

Output: data/returnvalue_context.json

Usage:
    python mine_returnvalue_context.py
    python mine_returnvalue_context.py --xml-dir /path/to/workflows_raw
    python mine_returnvalue_context.py --min-count 3 --dry-run

Output format:
    {
      "generated_from": "...",
      "total_workflows": 609,
      "total_ifelse_instances": 1842,
      "preceding_activity": {
        "SendEmail": {
          "total_condition_branches": 94,
          "type_distribution": {
            "UserDefinedValue": {"count": 73, "pct": 78},
            "StoredValue":      {"count": 21, "pct": 22}
          },
          "conditiontype_by_type": {
            "UserDefinedValue": {"Contains": 58, "Not Contains": 15},
            "StoredValue":      {"": 18, "Equals": 3}
          },
          "top_values_by_type": {
            "UserDefinedValue": [{"value": "yes", "count": 31}, ...],
            "StoredValue":      [{"value": "Running", "count": 9}, ...]
          }
        },
        "ServiceStatus": { ... },
        ...
      }
    }

Methodology notes:
  - "preceding activity" = the last non-container sibling before the
    IfElseActivity in parent element order. This is the activity whose
    result the IfElse is testing.
  - Only CONDITION branches are counted (UseBranchWhenTimeout != "True" or
    ConditionType != ""). Default/timeout branches are excluded because
    they don't reflect the preceding activity's output type.
  - Counts are per-branch-instance (not per-workflow) because multiple
    branches in one IfElse are genuinely independent observations.
  - IfElse nodes that have no identifiable preceding activity (e.g. at
    the top of a nested branch) are recorded under key "_no_preceding".
    These are informational only and excluded from pipeline rules.
  - min_count: preceding activity types with fewer than this many total
    condition branches are excluded from output (noise floor).
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT       = Path(__file__).parent
_DEFAULT_XML_DIR = _REPO_ROOT / "workflows_raw"
_DEFAULT_OUT     = _REPO_ROOT / "data" / "returnvalue_context.json"

_XAML_NS   = "http://schemas.microsoft.com/winfx/2006/xaml"
_XNAME_KEY = f"{{{_XAML_NS}}}Name"

# Container types — skipped when looking for the "preceding activity"
# because they don't produce a scalar result the IfElse can test.
CONTAINER_TYPES = {
    "SequentialWorkflowActivity", "SequenceActivity",
    "IfElseActivity", "IfElseBranchActivity",
    "WhileActivity", "ForEachActivity",
    "ParallelActivity", "UserGroup",
    "ExitWhile",
}

# ReturnValue and Continue are not "preceding activities"
SKIP_AS_PRECEDING = CONTAINER_TYPES | {"ReturnValue", "Continue"}


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _local_name(tag: str) -> str:
    if not tag.startswith("{"):
        return tag
    return tag[tag.index("}") + 1:]


def _activity_type(elem: ET.Element) -> str:
    """Return the platform activity type name for an element."""
    return _local_name(elem.tag)


def _sanitise_xoml(xoml: str) -> str:
    """
    Fix known non-well-formed constructs in Resolve Actions XOML before parsing.

    &&&  — platform literal used in Formula attribute values (e.g. =Equals(&&&,Running)).
           Not valid XML. Replace with a safe placeholder before parsing; we don't need
           the Formula field value, only the Type/ConditionType/Value fields.
    """
    return xoml.replace("&&&", "___TRIPLE_AMP___")


def parse_workflow_file(path: Path) -> ET.Element | None:
    """
    Return the root SequentialWorkflowActivity element from a file,
    handling both raw XOML files and TotalExport wrappers.

    TotalExport: ET.fromstring decodes the outer XML entities, giving us the
    Xoml attribute value as a (mostly) well-formed XML string. We apply
    _sanitise_xoml() before the inner parse to handle &&& literals.

    Raw XOML: the file is already a SequentialWorkflowActivity — parse directly.

    Returns None on any parse failure.
    """
    try:
        raw_bytes = path.read_bytes()
        root      = ET.fromstring(raw_bytes)
    except ET.ParseError as e:
        print(f"  [SKIP] {path.name}: outer parse error — {e}")
        return None

    tag = _local_name(root.tag)

    if tag == "SequentialWorkflowActivity":
        return root

    if tag == "TotalExport":
        wf_info = root.find(".//WorkflowInfo")
        if wf_info is None:
            return None
        # ET has already decoded outer XML entities in the attribute value.
        # We just need to sanitise &&& before the inner parse.
        xoml_raw = wf_info.get("Xoml", "")
        if not xoml_raw:
            return None
        try:
            inner = ET.fromstring(_sanitise_xoml(xoml_raw))
        except ET.ParseError as e:
            print(f"  [SKIP] {path.name}: inner XOML parse error — {e}")
            return None
        return inner

    return None


# ---------------------------------------------------------------------------
# Core walk: find every IfElseActivity and its preceding activity type
# ---------------------------------------------------------------------------

def _find_preceding_activity(parent: ET.Element, ifelse_elem: ET.Element) -> str:
    """
    Walk parent's children in order. Return the CustomTypeName of the last
    non-container, non-skip sibling that appears before ifelse_elem.
    Returns "_no_preceding" if none found.
    """
    preceding = "_no_preceding"
    for child in parent:
        if child is ifelse_elem:
            break
        atype = _activity_type(child)
        if atype not in SKIP_AS_PRECEDING:
            preceding = atype
    return preceding


def _extract_returnvalue_fields(branch_elem: ET.Element) -> dict | None:
    """
    Find the ReturnValue child of an IfElseBranchActivity and return its
    key fields. Returns None if no ReturnValue found.
    """
    for child in branch_elem:
        if _activity_type(child) == "ReturnValue":
            return {
                "Type":                child.get("Type", ""),
                "ConditionType":       child.get("ConditionType", ""),
                "Value":               child.get("Value", ""),
                "UseStoredValue":      child.get("UseStoredValue", ""),
                "UseBranchWhenTimeout": child.get("UseBranchWhenTimeout", ""),
            }
    return None


def _is_default_branch(rv: dict) -> bool:
    """
    Return True if this ReturnValue represents a default/timeout branch.
    Default branches carry UseBranchWhenTimeout="True" AND an empty or
    missing ConditionType. We exclude them: they don't reflect the
    preceding activity's output type.
    """
    return rv.get("UseBranchWhenTimeout", "") == "True" and rv.get("ConditionType", "") == ""


def walk_ifelse_nodes(
    elem: ET.Element,
    parent: ET.Element | None,
    results: list,
) -> None:
    """
    Recursively walk the element tree. For each IfElseActivity found:
      1. Identify the preceding activity type from parent context.
      2. For each IfElseBranchActivity child, extract the ReturnValue fields.
      3. Append a record for every CONDITION branch (not default/timeout).
    """
    atype = _activity_type(elem)

    if atype == "IfElseActivity":
        preceding = (
            _find_preceding_activity(parent, elem)
            if parent is not None
            else "_no_preceding"
        )

        for branch in elem:
            if _activity_type(branch) != "IfElseBranchActivity":
                continue
            rv = _extract_returnvalue_fields(branch)
            if rv is None:
                continue
            if _is_default_branch(rv):
                continue  # skip default/timeout branches
            results.append({
                "preceding":      preceding,
                "Type":           rv["Type"],
                "ConditionType":  rv["ConditionType"],
                "Value":          rv["Value"],
                "UseStoredValue": rv["UseStoredValue"],
            })

        # Recurse into branches (catches nested IfElse)
        for branch in elem:
            walk_ifelse_nodes(branch, elem, results)

    else:
        # Recurse into all children, passing current elem as parent
        for child in elem:
            walk_ifelse_nodes(child, elem, results)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(records: list, min_count: int) -> dict:
    """
    Group records by preceding activity type and build the output structure.
    Excludes types with fewer than min_count total condition branches.
    """
    # Group raw records
    by_preceding: dict[str, list] = defaultdict(list)
    for r in records:
        by_preceding[r["preceding"]].append(r)

    total_ifelse = len(records)
    result = {}

    for preceding, rows in sorted(by_preceding.items(), key=lambda x: -len(x[1])):
        total = len(rows)
        if total < min_count:
            continue

        # Type distribution
        type_counter: Counter = Counter(r["Type"] for r in rows)
        type_dist = {
            t: {"count": c, "pct": round(100 * c / total)}
            for t, c in type_counter.most_common()
        }

        # ConditionType breakdown per Type
        cond_by_type: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            cond_by_type[r["Type"]][r["ConditionType"]] += 1
        cond_dist = {
            t: dict(counter.most_common())
            for t, counter in cond_by_type.items()
        }

        # Top Values per Type (non-empty, non-variable)
        values_by_type: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            v = r["Value"].strip()
            if v and not v.startswith("%") and v not in ("{x:Null}", ""):
                values_by_type[r["Type"]][v] += 1
        top_values = {
            t: [{"value": v, "count": c} for v, c in counter.most_common(10)]
            for t, counter in values_by_type.items()
        }

        result[preceding] = {
            "total_condition_branches": total,
            "type_distribution":        type_dist,
            "conditiontype_by_type":    cond_dist,
            "top_values_by_type":       top_values,
        }

    return result, total_ifelse


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Mine ReturnValue Type/ConditionType distributions by preceding activity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--xml-dir", default=str(_DEFAULT_XML_DIR),
        help=f"Directory of workflow XML files (default: {_DEFAULT_XML_DIR})"
    )
    parser.add_argument(
        "--output", default=str(_DEFAULT_OUT),
        help=f"Output JSON path (default: {_DEFAULT_OUT})"
    )
    parser.add_argument(
        "--min-count", type=int, default=3,
        help="Min condition branches for a preceding-activity entry to be included (default: 3)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print stats, write nothing"
    )
    args = parser.parse_args()

    xml_dir    = Path(args.xml_dir)
    output     = Path(args.output)
    min_count  = args.min_count

    if not xml_dir.exists():
        print(f"ERROR: {xml_dir} not found.")
        print( "       Pass --xml-dir /path/to/your/workflow/xml/files")
        raise SystemExit(1)

    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files found under {xml_dir}")
        raise SystemExit(1)

    print(f"Parsing {len(xml_files)} files...")

    all_records   = []
    n_ok          = 0
    n_fail        = 0
    n_ifelse_wf   = 0  # workflows that contain at least one IfElse

    for path in xml_files:
        root = parse_workflow_file(path)
        if root is None:
            n_fail += 1
            continue
        n_ok += 1

        records_before = len(all_records)
        walk_ifelse_nodes(root, parent=None, results=all_records)
        if len(all_records) > records_before:
            n_ifelse_wf += 1

    print(f"  Parsed OK: {n_ok}  |  Failed: {n_fail}")
    print(f"  Workflows with IfElse: {n_ifelse_wf}")
    print(f"  Total condition branches collected: {len(all_records)}")

    agg, total_ifelse = aggregate(all_records, min_count)

    # Print summary table
    print(f"\nReturnValue Type distribution by preceding activity (min_count={min_count}):\n")
    header = f"  {'Preceding Activity':<32}  {'Branches':>8}  {'StoredValue':>12}  {'UserDefined':>12}  {'Other':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for preceding, data in sorted(agg.items(), key=lambda x: -x[1]["total_condition_branches"]):
        if preceding.startswith("_"):
            continue
        total   = data["total_condition_branches"]
        td      = data["type_distribution"]
        sv_pct  = td.get("StoredValue",      {}).get("pct", 0)
        udv_pct = td.get("UserDefinedValue", {}).get("pct", 0)
        other   = 100 - sv_pct - udv_pct
        print(f"  {preceding:<32}  {total:>8}  {sv_pct:>11}%  {udv_pct:>11}%  {other:>7}%")

    # Show top ConditionType/Value breakdown for the highest-signal entries
    print("\nTop condition patterns per preceding activity:\n")
    for preceding, data in sorted(agg.items(), key=lambda x: -x[1]["total_condition_branches"])[:15]:
        if preceding.startswith("_"):
            continue
        print(f"  {preceding}  ({data['total_condition_branches']} branches)")
        for rv_type, cond_counts in sorted(data["conditiontype_by_type"].items()):
            top_vals = data["top_values_by_type"].get(rv_type, [])[:3]
            vals_str = ", ".join(f'"{v["value"]}"({v["count"]})' for v in top_vals) or "(empty/variable)"
            cond_str = ", ".join(f'{ct or "(empty)"}:{n}' for ct, n in list(cond_counts.items())[:3])
            print(f"    Type={rv_type:<20}  ConditionType: {cond_str}")
            print(f"    {'':20}  Top values:    {vals_str}")
        print()

    # No-preceding summary (informational)
    no_prec = agg.get("_no_preceding")
    if no_prec:
        print(f"  _no_preceding: {no_prec['total_condition_branches']} branches "
              f"(IfElse at top of branch — no sibling activity context)")
        print()

    output_data = {
        "generated_from":        str(xml_dir),
        "total_workflows_parsed": n_ok,
        "workflows_with_ifelse":  n_ifelse_wf,
        "total_condition_branches": total_ifelse,
        "min_count_threshold":    min_count,
        "preceding_activity":     agg,
    }

    if args.dry_run:
        print("Dry run — nothing written.")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
    print(f"Written to {output}")
    print()
    print("Next steps:")
    print("  1. Review type_distribution for SendEmail, UserInput, and any other")
    print("     activities where UserDefinedValue pct is >= 50%.")
    print("  2. Update the StructureBuilder 'ACTIVITY OUTPUT VALUES' table in")
    print("     agents/structure_builder_agent.py with corpus-backed Type values.")
    print("  3. For activities where both types appear at significant frequency,")
    print("     add a disambiguation rule (e.g. if ConditionType=Contains → UserDefinedValue).")


if __name__ == "__main__":
    main()