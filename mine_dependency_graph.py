"""
mine_dependency_graph.py
─────────────────────────
Mines a dependency graph from the Resolve Actions workflow corpus.

KEY INSIGHT — variable naming in Resolve Actions:
  Most activities: referenced by xName  e.g. %getCellValue1%
  Table activities: referenced by TableName  e.g. %serverList%
                    (NOT the xName of the CreateMemoryTable activity)
  Sessions:        referenced by xName of the Start* activity

We build a combined var_to_type lookup per workflow:
  xName          → CustomTypeName  (all activities)
  TableName      → CustomTypeName  (table-producing activities)

Usage:
    python mine_dependency_graph.py --dry-run
    python mine_dependency_graph.py --min-count 3

Outputs:
    data/dependency_graph.json   — per-activity required input wiring
    data/table_producers.json    — confirmed table-producing activity types
"""

import argparse
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

# ── Constants ────────────────────────────────────────────────────────────────

_XNAME_KEY = "{http://schemas.microsoft.com/winfx/2006/xaml}Name"
_VAR_RE    = re.compile(r"%([^%]+)%")

# Fields that provide table-name output variable (in addition to xName)
# When one of these fields is present on an activity, its value is also
# registered as a variable name in the lookup.
TABLE_NAME_FIELDS = {"TableName", "ResultSetName"}

# Fields we want to track for dependency mining
# (other fields are ignored — credentials, display strings, etc.)
TRACKED_FIELDS = {
    "ResultSet", "ResultSetName", "TableName",   # table consumers
    "RowNumber",                                  # loop counter
    "SessionName",                                # session consumers
    "HostName", "VariableValue", "FilterStatement",
    "Query", "Body", "Subject", "To", "NewValue",
    "Attachments", "HeaderData", "PostData", "Url",
    "ScriptCode", "TheValue", "TheValue2",
    "ADUserName", "ServiceName", "xml", "XMLString",
    "Value", "Counter",
}

# Activity types that consume tables but do NOT produce them.
# These appear as "providers" in the dependency edges only because a corpus
# workflow named its table variable after the GetCellValue (or similar) xName
# that previously read from it — a naming collision, not a real dependency.
# Filtering these out of the provided_by list before classifying ensures
# we don't misclassify string-output activities as table producers.
NON_TABLE_ACTIVITIES = {
    "GetCellValue", "SetCellValue", "GetRowsCount", "GetColumnsCount",
    "GetRows", "GetColumnName", "MemorySet", "MultiMemorySet",
    "DisplayValue", "DisplayMultiValue", "ExitWhile", "WhileActivity",
    "SequenceActivity", "IfElseActivity", "IfElseBranchActivity",
    "ReturnValue", "Continue",
}

TABLE_PRODUCERS = {
    "CreateMemoryTable", "ReadXLS", "ReadCSV",
    "TSQLQuery", "TSQLStatement", "GetRows",
    "ResultSetFilter", "SNGetRecord", "ADListOU",
    "ADListGroup", "GetOpenIncidents", "ProcessList",
    "ServiceList", "FolderList", "GetInstalledSoftware",
    "GetWindowEventLogs", "GetInterfacesStatus",
    "VMList", "VMHostList", "HyperVInfo",
    "ESMGetEvent", "JiraGetIssue", "SortTable",
    "DeleteMemoryTableRows", "DeleteMemoryTableColumns",
    "MemoryTableComparison", "AddMemoryTableRow",
}

FIELD_DEP_TYPE = {
    "ResultSet":    "table",
    "ResultSetName":"table",
    "TableName":    "table",
    "RowNumber":    "loop_counter",
    "Counter":      "loop_counter",
    "SessionName":  "session",
}

# ── XML helpers ───────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag[tag.rfind("}") + 1:] if "}" in tag else tag


def _collect_activities(elem: ET.Element, out: list):
    """Recursively collect all elements as flat attribute dicts."""
    xname = elem.attrib.get(_XNAME_KEY, "")
    local = _local(elem.tag)
    node = {"xName": xname, "CustomTypeName": local}
    # Copy plain (non-namespace) attributes
    for k, v in elem.attrib.items():
        if not k.startswith("{"):
            node[k] = v
    out.append(node)
    for child in elem:
        _collect_activities(child, out)


def parse_workflow(xml_path: pathlib.Path, verbose: bool = False) -> list[dict] | None:
    """
    Parse a workflow XML file. Handles two formats:

    Format A — TotalExport wrapper (fresh exports):
      <TotalExport> <Workflows> <WorkflowInfo Xoml="..."> → parse Xoml attribute

    Format B — Raw XOML (corpus files):
      <SequentialWorkflowActivity ...> → parse directly as XOML root
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        local_root = _local(root.tag)

        # Format A: TotalExport wrapper
        if local_root == "TotalExport":
            wi = root.find(".//WorkflowInfo")
            if wi is None:
                if verbose:
                    print(f"  [skip] {xml_path.name} — TotalExport but no WorkflowInfo")
                return None
            xoml = wi.get("Xoml", "")
            if not xoml:
                if verbose:
                    print(f"  [skip] {xml_path.name} — empty Xoml attribute")
                return None
            try:
                xoml_root = ET.fromstring(xoml)
            except ET.ParseError:
                # Some corpus exports double-escape — try html.unescape
                import html as _html
                try:
                    xoml_root = ET.fromstring(_html.unescape(xoml))
                except ET.ParseError as e:
                    if verbose:
                        print(f"  [fail] {xml_path.name} — Xoml parse error: {e}")
                    return None

        # Format B: Raw XOML root
        elif local_root in ("SequentialWorkflowActivity", "CustomWorkflow"):
            xoml_root = root

        else:
            if verbose:
                print(f"  [skip] {xml_path.name} — unknown root tag: {local_root}")
            return None

        activities: list[dict] = []
        for child in xoml_root:
            _collect_activities(child, activities)
        return activities or None

    except ET.ParseError as e:
        if verbose:
            print(f"  [fail] {xml_path.name} — XML parse error: {e}")
        return None
    except Exception as e:
        if verbose:
            print(f"  [fail] {xml_path.name} — {type(e).__name__}: {e}")
        return None


# ── Variable resolution ───────────────────────────────────────────────────────

def build_var_lookup(activities: list[dict]) -> dict[str, str]:
    """
    Build variable_name → CustomTypeName for one workflow.

    Two sources:
      1. xName of every activity  (standard case)
      2. TableName / ResultSetName fields on table-producing activities
         because %serverList% references the TableName, not the xName
         of the CreateMemoryTable activity.
    """
    lookup: dict[str, str] = {}
    for act in activities:
        ct    = act.get("CustomTypeName", "")
        xname = act.get("xName", "")
        if xname and ct:
            lookup[xname] = ct
        # Register TableName as a variable name too
        for fname in TABLE_NAME_FIELDS:
            tname = act.get(fname, "")
            if tname and ct and not tname.startswith("%"):
                lookup[tname] = ct
    return lookup


# ── Main mining ──────────────────────────────────────────────────────────────

def mine(xml_dir: pathlib.Path, min_count: int, verbose: bool = False) -> tuple[dict, dict]:
    # (consumer_type, field, producer_type) → set of workflow IDs
    edges: dict[tuple, set] = defaultdict(set)
    # (consumer_type, field) → set of workflow IDs that have any wired value
    wired: dict[tuple, set] = defaultdict(set)

    total = failures = 0
    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"[ERROR] No XML files in {xml_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(xml_files)} XML files...")

    for xml_path in xml_files:
        activities = parse_workflow(xml_path, verbose=verbose)
        if not activities:
            failures += 1
            continue
        total += 1
        wf_id = xml_path.stem

        var_to_type = build_var_lookup(activities)

        for act in activities:
            consumer = act.get("CustomTypeName", "")
            if not consumer:
                continue

            for field in TRACKED_FIELDS:
                val = act.get(field, "")
                if not val or not isinstance(val, str):
                    continue

                refs = _VAR_RE.findall(val)
                for ref in refs:
                    producer = var_to_type.get(ref)
                    if not producer:
                        continue
                    edges[(consumer, field, producer)].add(wf_id)
                    wired[(consumer, field)].add(wf_id)

    print(f"  Parsed: {total} workflows  |  Failed: {failures}")

    # ── Build output ─────────────────────────────────────────────────────────
    grouped: dict[tuple, dict[str, int]] = defaultdict(dict)
    for (consumer, field, producer), wf_ids in edges.items():
        count = len(wf_ids)
        if count >= min_count:
            grouped[(consumer, field)][producer] = count

    graph: dict = {}
    for (consumer, field), providers in sorted(grouped.items()):
        if not providers:
            continue
        total_wired = len(wired[(consumer, field)])
        sorted_p    = sorted(providers.items(), key=lambda x: -x[1])
        dep_type    = _dep_type(field, [p for p, _ in sorted_p])

        # For table dependencies, strip non-table activities from the output.
        # They only appear due to corpus variable naming collisions.
        if dep_type == "table":
            sorted_p = [(p, c) for p, c in sorted_p if p not in NON_TABLE_ACTIVITIES]
            if not sorted_p:
                continue  # Nothing real left — skip this edge entirely

        note        = _note(consumer, field, dep_type, sorted_p[0][0])

        graph.setdefault(consumer, {"required_inputs": []})
        graph[consumer]["required_inputs"].append({
            "field":           field,
            "dependency_type": dep_type,
            "provided_by":     [p for p, _ in sorted_p],
            "provider_counts": {p: c for p, c in sorted_p},
            "total_wired":     total_wired,
            "notes":           note,
        })

    stats = {
        "total_workflows":  total,
        "parse_failures":   failures,
        "consumer_types":   len(graph),
        "total_dep_edges":  sum(len(v["required_inputs"]) for v in graph.values()),
    }
    return graph, stats


def _dep_type(field: str, providers: list[str]) -> str:
    # Filter out activities that consume tables but don't produce them.
    # These appear as providers only due to variable naming collisions in the
    # corpus (e.g. a table variable named after the GetCellValue xName that
    # reads from it). Strip them before checking if providers are table types.
    real_providers = [p for p in providers if p not in NON_TABLE_ACTIVITIES]

    if real_providers and all(p in TABLE_PRODUCERS for p in real_providers[:3]):
        return "table"
    if field == "RowNumber" and "WhileActivity" in providers:
        return "loop_counter"
    return FIELD_DEP_TYPE.get(field, "string")


def _note(consumer: str, field: str, dep_type: str, top: str) -> str:
    if dep_type == "table":
        return (
            f"{consumer}.{field} requires a table variable produced by {top} "
            f"(or similar). If no table-creating activity precedes this in the "
            f"workflow, the table must be populated externally before execution."
        )
    if dep_type == "loop_counter":
        return (
            f"{consumer}.{field} must reference the WhileActivity xName to get "
            f"the current row index. {consumer} requires an enclosing WhileActivity."
        )
    if dep_type == "session":
        return (
            f"{consumer}.{field} requires an active session from a preceding "
            f"{top} activity."
        )
    return (
        f"{consumer}.{field} is most commonly wired from {top}. "
        f"Ensure the providing activity precedes this one in the workflow."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Mine activity dependency graph from Resolve Actions corpus"
    )
    parser.add_argument("--xml-dir",    default="./workflows_raw")
    parser.add_argument("--output-dir", default="./data")
    parser.add_argument("--min-count",  type=int, default=3,
                        help="Min workflow count for an edge to be included (default: 3)")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--verbose",    action="store_true",
                        help="Show per-file skip/fail reasons")
    args = parser.parse_args()

    xml_dir    = pathlib.Path(args.xml_dir)
    output_dir = pathlib.Path(args.output_dir)

    if not xml_dir.exists():
        print(f"[ERROR] Not found: {xml_dir}", file=sys.stderr)
        sys.exit(1)

    graph, stats = mine(xml_dir, args.min_count, verbose=args.verbose)

    print(f"\nResults:")
    print(f"  Consumer types with dependencies: {stats['consumer_types']}")
    print(f"  Total dependency edges:           {stats['total_dep_edges']}")

    # Print all table + loop_counter + session dependencies
    print(f"\nTable / loop / session dependencies (min_count={args.min_count}):")
    for consumer, data in sorted(graph.items()):
        for inp in data["required_inputs"]:
            if inp["dependency_type"] in ("table", "loop_counter", "session"):
                top3 = ", ".join(
                    f"{p}({c})" for p, c in
                    list(inp["provider_counts"].items())[:3]
                )
                print(f"  {consumer}.{inp['field']:<20} [{inp['dependency_type']:<12}] ← {top3}")

    if args.dry_run:
        print("\nDry run — nothing written.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # dependency_graph.json
    dep_path = output_dir / "dependency_graph.json"
    dep_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    print(f"\nWritten: {dep_path}  ({stats['total_dep_edges']} edges)")

    # table_producers.json — corpus-confirmed + hardcoded set
    corpus_producers: set[str] = set()
    for data in graph.values():
        for inp in data["required_inputs"]:
            if inp["dependency_type"] == "table":
                corpus_producers.update(inp["provided_by"])
    all_producers = sorted(TABLE_PRODUCERS | corpus_producers)

    tp_path = output_dir / "table_producers.json"
    tp_path.write_text(
        json.dumps({
            "description": (
                "Activity types that produce table (ResultSet) outputs. "
                "Used by annotation_tools to detect missing table creation dependencies."
            ),
            "producers": all_producers,
        }, indent=2),
        encoding="utf-8",
    )
    print(f"Written: {tp_path}  ({len(all_producers)} table-producing types)")
    print(f"""
Next steps:
  dependency_graph.json
    → Wire into annotation_tools.py: new Category 6 check walks each activity's
      required_inputs and flags where a table/session provider is missing.
    → Wire into pipeline_stages.run_retrieval(): attach missing_dependencies
      to manifest entries so StructureBuilder knows what to add.

  table_producers.json
    → Replace hardcoded TABLE_PRODUCING_ACTIVITIES set in annotation_tools.py
      with a load from this file.
""")


if __name__ == "__main__":
    main()