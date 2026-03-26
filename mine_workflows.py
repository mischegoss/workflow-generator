"""
mine_workflows.py
=================
Mines Resolve Actions XML workflow files from workflows_raw/xml.

Produces 5 output files in the same directory as this script:

  1. mined_activity_sequences.json     — flat activity-type sequence per workflow
  2. mined_prefixspan_clusters.json    — top recurring activity clusters (PrefixSpan)
  3. mined_column_conventions.json     — DataTable column names per producer activity
  4. mined_memoryset_patterns.json     — MemorySet/MultiMemorySet variable patterns
  5. mined_summary.txt                 — human-readable summary of all findings

Usage:
    python mine_workflows.py --xml_dir /path/to/workflows_raw/xml

Requires: mlxtend (pip install mlxtend)
"""

import argparse
import html
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# ACTIVITY CLASSIFICATION
# ---------------------------------------------------------------------------

# Activities that are structural controls — excluded from sequence mining
# because they are container/flow nodes, not discrete operations
CONTROL_TYPES = {
    # Structural containers — not discrete operations
    "SequentialWorkflowActivity", "SequenceActivity", "IfElseBranchActivity",
    "WhileActivity", "IfElseActivity", "ParallelActivity", "UserGroup",
    "ForEachActivity",
    # Metadata / condition nodes — not operations
    "WorkflowInfo", "ReturnValue",
    # Exit/flow control — not operations
    "ExitWhile",
}

# Activities known to produce DataTables — we mine their output column names
DATATABLE_PRODUCERS = {
    "TSQLQuery", "MySQLQuery", "OracleQuery", "DB2Query",
    "HttpRequest", "HTTPRequest",
    "CreateMemoryTable",
    "RemoveEmptyRowsAndColumnsFromTable",
    "GetRows",
    "ReadExcel", "ReadFile",
    "ListFolder",
    "MatchRegularExpression",
    "ConvertTextToTable",
    "SNGetRecord",
    "JSONtoTable", "JsonToTable",
    "XMLtoTable", "XmlToTable",
    "WMIQuery",
}

# Canonical name normalisation — XML TypeName -> clean display name
DISPLAY_NAMES = {
    "TSQLQuery": "TSQL Query",
    "MySQLQuery": "MySQL Query",
    "OracleQuery": "Oracle Query",
    "DB2Query": "DB2 Query",
    "HttpRequest": "HTTP Request",
    "HTTPRequest": "HTTP Request",
    "CreateMemoryTable": "Create Memory Table",
    "RemoveEmptyRowsAndColumnsFromTable": "Remove Empty Rows And Columns From Table",
    "GetRows": "Get Rows",
    "ReadExcel": "Read Excel",
    "ReadFile": "Read File",
    "ListFolder": "List Folder",
    "MatchRegularExpression": "Match Regular Expression",
    "ConvertTextToTable": "Convert Text To Table",
    "SNGetRecord": "SN Get Record",
    "JSONtoTable": "JSON to Table",
    "JsonToTable": "JSON to Table",
    "XMLtoTable": "XML to Table",
    "XmlToTable": "XML to Table",
    "WMIQuery": "WMI Query",
    "GetRowsCount": "Get Rows Count",
    "GetCellValue": "Get Cell Value",
    "GetColumnsCount": "Get Columns Count",
    "MemorySet": "Memory Set",
    "MultiMemorySet": "Multi Memory Set",
    "DisplayValue": "Display Value",
    "ExitWhile": "Exit While",
    "Ping": "Ping",
    "ServiceStatus": "Service Status",
    "ServiceStart": "Service Start",
    "ServiceStop": "Service Stop",
    "SendEmail": "Send Email",
    "SendSMTPEmail": "Send SMTP Email",
    "DateDifference": "Date Difference",
    "GetDate": "Get Date",
}


def display(type_name: str) -> str:
    return DISPLAY_NAMES.get(type_name, type_name)


# ---------------------------------------------------------------------------
# XML PARSING
# ---------------------------------------------------------------------------

def load_xoml(path: str) -> str:
    """Load an XML file and double-unescape its HTML entities."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    return html.unescape(html.unescape(raw))


def extract_activity_sequence(xoml: str) -> list[dict]:
    """
    Extract every activity tag in document order.
    Returns list of dicts: {type, xname, tag_text}
    Excludes SequentialWorkflowActivity (root node).
    """
    activities = []
    for m in re.finditer(r'<(\w+)\s([^>]*x:Name="[^"]*"[^>]*)/?>', xoml):
        type_name = m.group(1)
        if type_name == "SequentialWorkflowActivity":
            continue
        attrs = m.group(2)
        xname_m = re.search(r'x:Name="([^"]+)"', attrs)
        xname = xname_m.group(1) if xname_m else ""
        activities.append({
            "type": type_name,
            "xname": xname,
            "tag": m.group(0),
        })
    return activities


def get_attr(tag: str, attr: str) -> str:
    m = re.search(rf'{attr}="([^"]*)"', tag)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# MINER 1 — Activity sequences (flat, controls stripped)
# ---------------------------------------------------------------------------

def mine_sequences(workflows: list[dict]) -> list[list[str]]:
    """
    Returns one sequence per workflow — list of activity TypeNames,
    with pure structural controls removed.
    """
    sequences = []
    for wf in workflows:
        seq = [
            a["type"] for a in wf["activities"]
            if a["type"] not in CONTROL_TYPES
        ]
        if seq:
            sequences.append(seq)
    return sequences


# ---------------------------------------------------------------------------
# MINER 2 — PrefixSpan frequent subsequence mining
# ---------------------------------------------------------------------------

def run_prefixspan(sequences: list[list[str]], min_support: float = 0.05, max_len: int = 6):
    """
    Runs PrefixSpan on activity sequences.
    min_support: fraction of workflows that must contain the pattern.
    Returns list of (support_count, pattern) sorted descending.
    """
    try:
        from mlxtend.frequent_patterns import fpgrowth
        from mlxtend.preprocessing import TransactionEncoder
    except ImportError:
        pass

    # PrefixSpan via manual implementation (mlxtend doesn't ship PrefixSpan,
    # only FP-Growth on itemsets). We implement a simple prefix-projected
    # database approach.

    n = len(sequences)
    min_count = max(2, int(min_support * n))

    def project(db, prefix_item):
        projected = []
        for seq in db:
            for i, item in enumerate(seq):
                if item == prefix_item:
                    projected.append(seq[i + 1:])
                    break
        return projected

    def prefixspan(db, prefix, min_count, max_len, results):
        if len(prefix) >= max_len:
            return
        # Count all items in projected db
        counts = Counter(item for seq in db for item in set(seq))
        for item, count in counts.items():
            if count >= min_count:
                new_prefix = prefix + [item]
                results.append((count, new_prefix))
                new_db = project(db, item)
                prefixspan(new_db, new_prefix, min_count, max_len, results)

    results = []
    prefixspan(sequences, [], min_count, max_len, results)

    # Deduplicate and sort
    seen = set()
    unique = []
    for count, pattern in sorted(results, key=lambda x: -x[0]):
        key = tuple(pattern)
        if key not in seen:
            seen.add(key)
            unique.append({
                "support_count": count,
                "support_pct": round(count / n * 100, 1),
                "pattern": pattern,
                "pattern_display": [display(p) for p in pattern],
                "length": len(pattern),
            })

    # Filter to patterns of length >= 2 (single items are trivial)
    unique = [x for x in unique if x["length"] >= 2]

    # Return top 40 by support
    return unique[:40]


# ---------------------------------------------------------------------------
# MINER 3 — Column name conventions per DataTable producer
# ---------------------------------------------------------------------------

def mine_column_conventions(workflows: list[dict]) -> dict:
    """
    For each DataTable-producing activity type, collect all column names
    observed in GetCellValue calls that reference that table.

    Also extracts columns embedded in CreateMemoryTable.TableAsString.

    Returns dict: producer_type -> {
        column_names: Counter,
        query_samples: list (for SQL types),
        http_column_note: str,
    }
    """
    # Build map: xname -> type for DataTable producers in each workflow
    conventions = defaultdict(lambda: {
        "column_names": Counter(),
        "query_samples": [],
        "table_name_samples": [],
        "total_workflows": 0,
    })

    for wf in workflows:
        xoml = wf["xoml"]

        # Map xname -> type for all DataTable producers
        producer_map = {}  # xname -> type
        table_name_map = {}  # tableName (from CreateMemoryTable) -> type

        for act in wf["activities"]:
            if act["type"] in DATATABLE_PRODUCERS:
                producer_map[act["xname"]] = act["type"]

                # CreateMemoryTable: also map by TableName field
                if act["type"] == "CreateMemoryTable":
                    tname = get_attr(act["tag"], "TableName")
                    if tname:
                        table_name_map[tname] = "CreateMemoryTable"

                    # Extract inline column names from TableAsString
                    ts_m = re.search(r'TableAsString="([^"]+)"', act["tag"])
                    if ts_m:
                        ts = html.unescape(ts_m.group(1))
                        cols = re.findall(r"xs:element name='([^']+)'", ts)
                        for col in cols:
                            if col not in ("NewDataSet", "resultSet"):
                                conventions["CreateMemoryTable"]["column_names"][col] += 1

                # SQL queries: extract SELECT column names from Query field
                if act["type"] in ("TSQLQuery", "MySQLQuery", "OracleQuery", "DB2Query"):
                    query = get_attr(act["tag"], "Query")
                    if query:
                        # Normalise: strip newlines, collapse whitespace
                        q_clean = re.sub(r'\s+', ' ', query.strip())
                        if q_clean not in conventions[act["type"]]["query_samples"]:
                            conventions[act["type"]]["query_samples"].append(q_clean[:200])

                        # Extract explicit column names from SELECT col1, col2 FROM ...
                        sel_m = re.match(r'SELECT\s+(.+?)\s+FROM', q_clean, re.IGNORECASE)
                        if sel_m:
                            cols_raw = sel_m.group(1)
                            if cols_raw.strip() != "*":
                                cols = [c.strip().split()[-1].strip('[]`"') for c in cols_raw.split(",")]
                                for col in cols:
                                    if col:
                                        conventions[act["type"]]["column_names"][col] += 1

        # Now mine GetCellValue for column names, linking back to producer
        for m in re.finditer(r'<GetCellValue\s([^>]+)>', xoml):
            tag_attrs = m.group(1)
            rs_name = re.search(r'ResultSetName="([^"]+)"', tag_attrs)
            col_num = re.search(r'ColumnNumber="([^"]+)"', tag_attrs)

            if not rs_name or not col_num:
                continue

            table_ref = rs_name.group(1)
            col = col_num.group(1)

            # Skip numeric column references (ColumnType=Number pattern)
            if col.isdigit():
                continue

            # Look up the producer
            producer_type = producer_map.get(table_ref) or table_name_map.get(table_ref)
            if producer_type:
                conventions[producer_type]["column_names"][col] += 1

        # Track HTTP Request columns (always Status Code, Body, Request)
        for act in wf["activities"]:
            if act["type"] in ("HttpRequest", "HTTPRequest"):
                conventions["HttpRequest"]["total_workflows"] += 1

    # Add HTTP Request hardcoded columns (platform-defined, not in XML)
    conventions["HttpRequest"]["column_names"].update({
        "Status Code": 999,
        "Body": 999,
        "Request": 999,
    })
    conventions["HttpRequest"]["http_column_note"] = (
        "HTTP Request always returns exactly these 3 columns. "
        "They are platform-defined and do not vary."
    )

    # Serialise Counters
    output = {}
    for producer, data in conventions.items():
        col_counts = data["column_names"]
        if not col_counts:
            continue
        output[display(producer)] = {
            "top_columns": [
                {"column": col, "frequency": count}
                for col, count in col_counts.most_common(20)
            ],
            "query_samples": data.get("query_samples", [])[:5],
            "note": data.get("http_column_note", ""),
        }

    return output


# ---------------------------------------------------------------------------
# MINER 4 — MemorySet / MultiMemorySet patterns at workflow top
# ---------------------------------------------------------------------------

def mine_memoryset_patterns(workflows: list[dict]) -> dict:
    """
    Extracts MemorySet and MultiMemorySet usage patterns, focusing on
    activities that appear in the first N positions of a workflow
    (the 'header' initialisation block).

    Returns:
      - common_variable_names: Counter of VariableName values
      - scope_distribution: Workflow vs Global counts
      - header_patterns: most common sequences of the first 3-5 activities
      - value_patterns: what kinds of values are set (literal, %var%, empty)
    """
    common_var_names = Counter()
    scope_dist = Counter()
    header_sequences = Counter()
    value_patterns = Counter()
    memoryset_details = []

    for wf in workflows:
        xoml = wf["xoml"]

        # Extract all MemorySet instances
        for m in re.finditer(r'<(?:MemorySet|MultiMemorySet)\s([^>]+)>', xoml):
            tag = m.group(0)
            type_name = "MemorySet" if "<MemorySet" in tag else "MultiMemorySet"
            xname = get_attr(tag, "x:Name")
            var_name = get_attr(tag, "VariableName")
            var_value = get_attr(tag, "VariableValue")
            scope = get_attr(tag, "VariableScope") or "Workflow"

            if var_name:
                common_var_names[var_name] += 1
            scope_dist[scope] += 1

            # Classify value type
            if not var_value:
                vtype = "empty"
            elif re.match(r'^%[^%]+%$', var_value):
                vtype = "variable_reference"
            elif var_value.startswith("PLACEHOLDER_"):
                vtype = "placeholder"
            else:
                vtype = "literal"
            value_patterns[vtype] += 1

            memoryset_details.append({
                "type": type_name,
                "xname": xname,
                "variableName": var_name,
                "variableValue": var_value[:80] if var_value else "",
                "scope": scope,
                "valueType": vtype,
            })

        # Header sequence: first 5 non-control activity types
        non_control_seq = [
            a["type"] for a in wf["activities"]
            if a["type"] not in CONTROL_TYPES
        ][:5]
        if non_control_seq:
            header_sequences[tuple(non_control_seq)] += 1

    # Top header patterns
    top_headers = [
        {
            "pattern": [display(t) for t in seq],
            "count": count,
        }
        for seq, count in header_sequences.most_common(15)
    ]

    return {
        "top_variable_names": [
            {"name": n, "count": c}
            for n, c in common_var_names.most_common(30)
        ],
        "scope_distribution": dict(scope_dist),
        "value_type_distribution": dict(value_patterns),
        "top_header_patterns": top_headers,
        "total_memoryset_instances": len(memoryset_details),
        "sample_details": memoryset_details[:50],
    }


# ---------------------------------------------------------------------------
# MINER 5 — Additional: IfElse condition values actually used
# ---------------------------------------------------------------------------

def mine_ifelse_conditions(workflows: list[dict]) -> list[dict]:
    """
    Extracts all ReturnValue condition patterns actually used in real workflows.
    Gives ground truth on ConditionType + Value combinations per upstream activity.
    """
    conditions = []

    for wf in workflows:
        xoml = wf["xoml"]

        # Build xname->type map
        xname_type = {a["xname"]: a["type"] for a in wf["activities"]}

        for m in re.finditer(r'<ReturnValue\s([^>]+)>', xoml):
            attrs = m.group(1)
            ctype = re.search(r'ConditionType="([^"]*)"', attrs)
            value = re.search(r'\bValue="([^"]*)"', attrs)
            use_stored = re.search(r'UseStoredValue="([^"]*)"', attrs)
            timeout = re.search(r'UseBranchWhenTimeout="([^"]*)"', attrs)
            is_valid = re.search(r'IsValid="([^"]*)"', attrs)

            conditions.append({
                "conditionType": ctype.group(1) if ctype else "",
                "value": value.group(1) if value else "",
                "useStoredValue": use_stored.group(1) if use_stored else "",
                "useBranchWhenTimeout": timeout.group(1) if timeout else "",
                "isValid": is_valid.group(1) if is_valid else "",
            })

    # Aggregate
    combo_counter = Counter(
        (c["conditionType"], c["value"], c["useBranchWhenTimeout"])
        for c in conditions
    )

    return [
        {
            "conditionType": ct,
            "value": val,
            "useBranchWhenTimeout": timeout,
            "count": count,
        }
        for (ct, val, timeout), count in combo_counter.most_common(40)
    ]


# ---------------------------------------------------------------------------
# MINER 6 — ExitWhile counter sources
# ---------------------------------------------------------------------------

def mine_exitwhile_patterns(workflows: list[dict]) -> dict:
    """
    For every ExitWhile, records what activity type provided the Counter value.
    Confirms GetRowsCount as dominant source.
    """
    counter_sources = Counter()
    row_number_sources = Counter()

    for wf in workflows:
        xoml = wf["xoml"]
        xname_type = {a["xname"]: a["type"] for a in wf["activities"]}

        # ExitWhile counter source
        for m in re.finditer(r'<ExitWhile\s([^>]+)>', xoml):
            attrs = m.group(1)
            counter_m = re.search(r'Counter="([^"]*)"', attrs)
            if counter_m:
                counter_val = counter_m.group(1)
                ref_m = re.match(r'^%(\w+)%$', counter_val)
                if ref_m:
                    ref_xname = ref_m.group(1)
                    source_type = xname_type.get(ref_xname, "UNKNOWN")
                    counter_sources[source_type] += 1
                elif counter_val.isdigit():
                    counter_sources["HARDCODED_INTEGER"] += 1
                else:
                    counter_sources[f"OTHER:{counter_val[:30]}"] += 1

        # GetCellValue RowNumber source
        for m in re.finditer(r'<GetCellValue\s([^>]+)>', xoml):
            attrs = m.group(1)
            row_m = re.search(r'RowNumber="([^"]*)"', attrs)
            if row_m:
                row_val = row_m.group(1)
                ref_m = re.match(r'^%(\w+)%$', row_val)
                if ref_m:
                    ref_xname = ref_m.group(1)
                    source_type = xname_type.get(ref_xname, "UNKNOWN")
                    row_number_sources[source_type] += 1
                else:
                    row_number_sources[f"OTHER:{row_val[:30]}"] += 1

    return {
        "exitwhile_counter_sources": [
            {"sourceActivityType": display(t), "count": c}
            for t, c in counter_sources.most_common()
        ],
        "getcellvalue_rownumber_sources": [
            {"sourceActivityType": display(t), "count": c}
            for t, c in row_number_sources.most_common()
        ],
    }


# ---------------------------------------------------------------------------
# LOADER — handles both TotalExport XML and raw JSON
# ---------------------------------------------------------------------------

def load_workflows_from_xml_dir(xml_dir: str) -> list[dict]:
    workflows = []
    path = Path(xml_dir)

    xml_files = list(path.glob("**/*.xml"))
    print(f"Found {len(xml_files)} XML files in {xml_dir}")

    for fpath in xml_files:
        if "export_summary" in fpath.name.lower():
            continue
        try:
            xoml = load_xoml(str(fpath))
            activities = extract_activity_sequence(xoml)
            if not activities:
                continue
            workflows.append({
                "file": fpath.name,
                "xoml": xoml,
                "activities": activities,
            })
        except Exception as e:
            print(f"  SKIP {fpath.name}: {e}")

    print(f"Loaded {len(workflows)} valid workflows")
    return workflows


# ---------------------------------------------------------------------------
# SUMMARY WRITER
# ---------------------------------------------------------------------------

def write_summary(
    sequences, clusters, columns, memoryset, conditions, exitwhile, output_dir
):
    lines = []
    lines.append("WORKFLOW MINING SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Total workflows analysed: {len(sequences)}")
    lines.append("")

    lines.append("TOP 20 ACTIVITY CLUSTERS (PrefixSpan)")
    lines.append("-" * 40)
    for i, c in enumerate(clusters[:20], 1):
        pattern_str = " → ".join(c["pattern_display"])
        lines.append(f"  {i:2}. [{c['support_count']} wf, {c['support_pct']}%] {pattern_str}")
    lines.append("")

    lines.append("COLUMN NAME CONVENTIONS PER DATATABLE PRODUCER")
    lines.append("-" * 40)
    for producer, data in columns.items():
        top = ", ".join(f'"{x["column"]}" ({x["frequency"]}x)' for x in data["top_columns"][:8])
        lines.append(f"  {producer}:")
        lines.append(f"    Top columns: {top}")
        if data.get("note"):
            lines.append(f"    Note: {data['note']}")
    lines.append("")

    lines.append("MEMORYSET PATTERNS")
    lines.append("-" * 40)
    lines.append(f"  Total MemorySet instances: {memoryset['total_memoryset_instances']}")
    lines.append(f"  Scope: {memoryset['scope_distribution']}")
    lines.append(f"  Value types: {memoryset['value_type_distribution']}")
    lines.append("  Top variable names:")
    for v in memoryset["top_variable_names"][:15]:
        lines.append(f"    {v['name']} ({v['count']}x)")
    lines.append("  Top header patterns (first 5 activities):")
    for h in memoryset["top_header_patterns"][:8]:
        lines.append(f"    [{h['count']}x] {' → '.join(h['pattern'])}")
    lines.append("")

    lines.append("IFELSE CONDITION VALUES (ground truth)")
    lines.append("-" * 40)
    for c in conditions[:20]:
        lines.append(
            f"  ConditionType={c['conditionType']!r:20} Value={c['value']!r:20} "
            f"Timeout={c['useBranchWhenTimeout']} ({c['count']}x)"
        )
    lines.append("")

    lines.append("EXITWHILE / GETCELLVALUE SOURCES")
    lines.append("-" * 40)
    lines.append("  ExitWhile Counter sources:")
    for s in exitwhile["exitwhile_counter_sources"]:
        lines.append(f"    {s['sourceActivityType']}: {s['count']}x")
    lines.append("  GetCellValue RowNumber sources:")
    for s in exitwhile["getcellvalue_rownumber_sources"]:
        lines.append(f"    {s['sourceActivityType']}: {s['count']}x")

    summary_path = os.path.join(output_dir, "mined_summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Wrote {summary_path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Mine Resolve Actions XML workflows")
    parser.add_argument(
        "--xml_dir",
        default="workflows_raw/xml",
        help="Path to directory containing workflow XML files (default: workflows_raw/xml)",
    )
    parser.add_argument(
        "--output_dir",
        default="data",
        help="Directory to write output JSON files (default: data)",
    )
    parser.add_argument(
        "--min_support",
        type=float,
        default=0.05,
        help="Minimum support fraction for PrefixSpan (default: 0.05 = 5%% of workflows)",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # --- Load ---
    print("\n[1/6] Loading workflows...")
    workflows = load_workflows_from_xml_dir(args.xml_dir)
    if not workflows:
        print("No workflows loaded. Check --xml_dir path.")
        return

    # --- Sequences ---
    print("[2/6] Extracting activity sequences...")
    sequences = mine_sequences(workflows)
    seq_path = os.path.join(args.output_dir, "mined_activity_sequences.json")
    with open(seq_path, "w") as f:
        json.dump(
            [{"file": wf["file"], "sequence": seq} for wf, seq in zip(workflows, sequences)],
            f, indent=2
        )
    print(f"  Wrote {seq_path}")

    # --- PrefixSpan ---
    print("[3/6] Running PrefixSpan cluster mining...")
    clusters = run_prefixspan(sequences, min_support=args.min_support)
    clusters_path = os.path.join(args.output_dir, "mined_prefixspan_clusters.json")
    with open(clusters_path, "w") as f:
        json.dump(clusters, f, indent=2)
    print(f"  Found {len(clusters)} patterns. Wrote {clusters_path}")

    # --- Column conventions ---
    print("[4/6] Mining DataTable column name conventions...")
    columns = mine_column_conventions(workflows)
    col_path = os.path.join(args.output_dir, "mined_column_conventions.json")
    with open(col_path, "w") as f:
        json.dump(columns, f, indent=2)
    print(f"  Wrote {col_path}")

    # --- MemorySet patterns ---
    print("[5/6] Mining MemorySet/MultiMemorySet patterns...")
    memoryset = mine_memoryset_patterns(workflows)
    mem_path = os.path.join(args.output_dir, "mined_memoryset_patterns.json")
    with open(mem_path, "w") as f:
        json.dump(memoryset, f, indent=2)
    print(f"  Wrote {mem_path}")

    # --- IfElse conditions ---
    print("[5b/6] Mining IfElse condition ground truth...")
    conditions = mine_ifelse_conditions(workflows)
    cond_path = os.path.join(args.output_dir, "mined_ifelse_conditions.json")
    with open(cond_path, "w") as f:
        json.dump(conditions, f, indent=2)
    print(f"  Wrote {cond_path}")

    # --- ExitWhile sources ---
    print("[5c/6] Mining ExitWhile/GetCellValue variable sources...")
    exitwhile = mine_exitwhile_patterns(workflows)
    ew_path = os.path.join(args.output_dir, "mined_exitwhile_patterns.json")
    with open(ew_path, "w") as f:
        json.dump(exitwhile, f, indent=2)
    print(f"  Wrote {ew_path}")

    # --- Summary ---
    print("[6/6] Writing human-readable summary...")
    write_summary(sequences, clusters, columns, memoryset, conditions, exitwhile, args.output_dir)

    print("\nDone. Output files:")
    for fname in [
        "mined_activity_sequences.json",
        "mined_prefixspan_clusters.json",
        "mined_column_conventions.json",
        "mined_memoryset_patterns.json",
        "mined_ifelse_conditions.json",
        "mined_exitwhile_patterns.json",
        "mined_summary.txt",
    ]:
        fpath = os.path.join(args.output_dir, fname)
        if os.path.exists(fpath):
            size = os.path.getsize(fpath)
            print(f"  {fname:45} {size:>8} bytes")


if __name__ == "__main__":
    main()