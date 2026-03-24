import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Repo-relative defaults
# ─────────────────────────────────────────────────────────────────────────────
 
_REPO_ROOT   = Path(__file__).parent
_DEFAULT_XML = _REPO_ROOT / "xml_raw"
_DEFAULT_LIB = _REPO_ROOT / "data" / "patterns" / "pattern_library.json"
_DEFAULT_OUT = _REPO_ROOT / "data"
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Field classification
# ─────────────────────────────────────────────────────────────────────────────
 
STRUCTURAL_FIELDS = {
    "activityLicenseType", "visible", "disabled", "isFavorite", "isJsonValid",
    "readPermission", "writePermission", "IsValid",
    "Timeout", "TimeInSeconds", "RecoveryMethodSelection",
    "TypeName", "DisplayName", "label", "name", "description", "Description",
    "exitWhileInsideWhile", "isValid",
    "ConditionNumber", "UseCustomeCondition", "Disabled", "ClusterID", "ClusterName",
    "UseStoredValue", "isDefault", "useAlternateSetting",
}
 
ALWAYS_PARAM_FIELDS = {
    "TableName", "ResultSet", "ResultSetName",
    "ForEachTableVariable", "ForEachOutputVariableName",
    "RowNumber", "ColumnNumber", "ColumnName",
    "VariableName", "VariableValue",
    "Counter", "whileSequenceActivity",
    "To", "Cc", "Subject", "Body", "Attachments", "DestinationNumber",
    "MessageType", "TemplateNumber",
    "HostName", "HostId", "ServiceName",
    "Query", "ConnectionString", "ConnectionStringTextBox",
    "Code", "Script", "Command", "TheValue", "TheValue2",
    "ValueToDisplay",
    "SourcePath", "TargetPath", "SrcPath", "DstPath", "FilePath",
    "URL", "Url",
    "WorkflowID", "WorkflowName", "variables",
    "TableAsString",
    "Value", "Formula", "ConditionType", "ConditionName", "Type", "UseBranchWhenTimeout",
}
 
CREDENTIAL_FIELDS = {
    "Password", "SrcPassword", "DstPassword", "ArchivePassword",
    "LoginPassword", "ACPassword", "AdminPassword", "CertificatePassword",
    "api_key", "token", "openai_api_key", "UserName", "Username",
    "SmtpServer", "SmtpPort", "SmtpUser", "SmtpPass",
}
 
STRIP_FIELDS = {
    "notes", "modulePermissions", "isFavorite",
    "DateLic", "DateCreated", "DateCreatedUser", "DateModified", "DateModifiedUser",
}
 
CONFIG_FIELDS = {
    "FuturePast", "TimeInterval", "DateFormat", "TimeZoneName", "TimeToAdd", "IsNowSelected",
    "ReturnFormat", "FirstDateFormat", "SecondDateFormat",
    "ColumnType",
    "VariableScope", "IsSaved", "IsAppend",
    "UseStoredValue",
    "Channel", "SendRN", "DestinationType", "DestinationTypeCc",
    "Method", "ContentType", "AuthType",
    "Encoding", "FileType",
    "StartupType",
    "SelectionType", "SortDirection", "FilterType",
    "isEmptyGrid",
}
 
CONTAINER_TYPES = {
    "IfElseActivity", "IfElseBranchActivity", "SequenceActivity",
    "WhileActivity", "ForEachActivity", "ParallelActivity", "UserGroup",
    "ExitWhile", "ReturnValue", "Continue",
}
 
_SKIP_TAGS = {
    "schema", "element", "complexType", "sequence", "choice",
    "resultSet", "NewDataSet", "SerializedData", "ParamName", "ParamValue",
    "annotation", "documentation", "attribute", "restriction", "enumeration",
}
 
_XAML_NS   = "http://schemas.microsoft.com/winfx/2006/xaml"
_XNAME_KEY = f"{{{_XAML_NS}}}Name"
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  XML parsing
# ─────────────────────────────────────────────────────────────────────────────
 
def _local_name(tag):
    if not tag.startswith("{"):
        return tag
    closing = tag.find("}")
    return tag[closing + 1:] if closing != -1 else tag
 
 
def _extract_clr_ns(tag):
    if not tag.startswith("{clr-namespace:"):
        return None
    closing = tag.index("}")
    return (tag[closing + 1:], tag[1:closing])
 
 
def _elem_to_dict(elem):
    custom_type = _local_name(elem.tag)
    if custom_type in _SKIP_TAGS or custom_type.startswith("xs:"):
        return None
 
    xname    = elem.attrib.get(_XNAME_KEY, "")
    activity = {"xName": xname, "CustomTypeName": custom_type}
 
    for raw_key, val in elem.attrib.items():
        local_key = _local_name(raw_key)
        if local_key in ("Name", "Class") and raw_key.startswith("{"):
            continue
        if local_key == "xName":
            continue
        activity[local_key] = val
 
    for child in elem:
        child_dict = _elem_to_dict(child)
        if child_dict:
            key = child_dict.get("xName") or child_dict.get("CustomTypeName", "unknown")
            activity[key] = child_dict
 
    return activity
 
 
def _collect_ns_from_root(root):
    ns = {}
    for elem in root.iter():
        result = _extract_clr_ns(elem.tag)
        if result:
            type_name, clr_ns = result
            ns[type_name] = clr_ns
    return ns
 
 
def _parse_xoml_root(root):
    ns_map   = _collect_ns_from_root(root)
    raw_data = {}
    for child in root:
        activity = _elem_to_dict(child)
        if activity:
            xname = activity.get("xName", "")
            if xname:
                raw_data[xname] = activity
    return raw_data, ns_map
 
 
def parse_xml_file(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(text)
    except ET.ParseError as e:
        print(f"  [SKIP] {path.name}: XML parse error — {e}")
        return None
 
    tag = _local_name(root.tag)
 
    if tag == "SequentialWorkflowActivity":
        return _parse_xoml_root(root)
 
    if tag == "TotalExport":
        wf_info = root.find(".//WorkflowInfo")
        if wf_info is None:
            return None
        xoml_raw = wf_info.get("Xoml", "")
        if not xoml_raw:
            return None
        try:
            xoml_root = ET.fromstring(html.unescape(xoml_raw))
        except ET.ParseError as e:
            print(f"  [SKIP] {path.name}: inner Xoml parse error — {e}")
            return None
        return _parse_xoml_root(xoml_root)
 
    print(f"  [SKIP] {path.name}: unrecognised root tag '{tag}'")
    return None
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Sequence extraction
# ─────────────────────────────────────────────────────────────────────────────
 
def extract_sequence(raw_data):
    seq = []
 
    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        if ct:
            seq.append(ct)
        for key, val in node.items():
            if isinstance(val, dict) and key not in ("xName", "CustomTypeName"):
                walk(val)
 
    for activity in raw_data.values():
        walk(activity)
    return seq
 
 
def extract_leaf_sequence(raw_data):
    return [ct for ct in extract_sequence(raw_data) if ct not in CONTAINER_TYPES]
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Miner 1 — CLR namespace registry
# ─────────────────────────────────────────────────────────────────────────────
 
def mine_namespaces(all_ns):
    counters = defaultdict(Counter)
    for ns_map in all_ns:
        for type_name, clr_ns in ns_map.items():
            counters[type_name][clr_ns] += 1
    return {
        t: c.most_common(1)[0][0]
        for t, c in sorted(counters.items())
    }
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Miner 2 — Field defaults
# ─────────────────────────────────────────────────────────────────────────────
 
def mine_field_defaults(all_raw_data):
    counters = defaultdict(lambda: defaultdict(Counter))
 
    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        if ct and ct not in CONTAINER_TYPES:
            for field in CONFIG_FIELDS:
                val = node.get(field)
                if val is not None:
                    s = str(val).strip()
                    if (s and s not in ("{x:Null}", "", "None", "null")
                            and not s.startswith("%")
                            and not s.startswith("PARAM_")
                            and not s.startswith("PLACEHOLDER_")):
                        counters[ct][field][s] += 1
        for v in node.values():
            if isinstance(v, dict):
                walk(v)
 
    for raw_data in all_raw_data:
        for activity in raw_data.values():
            walk(activity)
 
    defaults = {}
    for ct, field_counters in sorted(counters.items()):
        ct_defaults = {}
        for field, counter in sorted(field_counters.items()):
            total = sum(counter.values())
            if total < 3:
                continue
            top_val, top_count = counter.most_common(1)[0]
            if top_count / total >= 0.60:
                ct_defaults[field] = top_val
        if ct_defaults:
            defaults[ct] = ct_defaults
 
    return defaults
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Miner 3 — Co-occurrence pairs
# ─────────────────────────────────────────────────────────────────────────────
 
def mine_cooccurrence(all_raw_data, window=3):
    pair_counts = Counter()
    for raw_data in all_raw_data:
        seq = extract_leaf_sequence(raw_data)
        for i, act in enumerate(seq):
            for j in range(i + 1, min(i + 1 + window, len(seq))):
                pair_counts[(act, seq[j])] += 1
    return [
        {"activity": a, "next": b, "rank": count}
        for (a, b), count in pair_counts.most_common()
    ]
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Miner 4 — Scaffolds
# ─────────────────────────────────────────────────────────────────────────────
 
def _sequence_contains(sequence, fragment):
    if not fragment or len(fragment) > len(sequence):
        return False
    flen = len(fragment)
    return any(sequence[i:i + flen] == fragment for i in range(len(sequence) - flen + 1))
 
 
def _is_variable_ref(value):
    return bool(re.match(r"^%[^%]+%$", str(value).strip()))
 
 
def _build_variance_map(matched):
    all_vals = defaultdict(set)
 
    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        for key, val in node.items():
            if key in ("xName", "CustomTypeName") or isinstance(val, dict) or key in STRIP_FIELDS:
                continue
            if ct:
                all_vals[(ct, key)].add(str(val) if val is not None else "")
        for v in node.values():
            if isinstance(v, dict):
                walk(v)
 
    for _, raw_data in matched:
        for activity in raw_data.values():
            walk(activity)
 
    return {pair: ("constant" if len(vals) == 1 else "varies") for pair, vals in all_vals.items()}
 
 
def _generalise_node(node, variance):
    result = {}
    ct = node.get("CustomTypeName", "")
 
    for key, value in node.items():
        if key in STRIP_FIELDS:
            continue
        if key == "xName":
            result["xName"] = f"PARAM_xname_{ct.lower()}"
            continue
        if key == "CustomTypeName":
            result[key] = value
            continue
        if isinstance(value, dict) and value.get("CustomTypeName"):
            result[key] = _generalise_node(value, variance)
            continue
        if isinstance(value, dict):
            result[key] = value
            continue
 
        s = str(value) if value is not None else ""
 
        if key in CREDENTIAL_FIELDS:
            result[key] = ""
        elif key in ALWAYS_PARAM_FIELDS:
            if s:
                result[key] = f"PARAM_{key}"
        elif key in STRUCTURAL_FIELDS:
            result[key] = value
        elif _is_variable_ref(s):
            result[key] = f"PARAM_{key}"
        elif variance.get((ct, key), "varies") == "constant":
            result[key] = value
        else:
            result[key] = f"PARAM_{key}"
 
    return result
 
 
def mine_scaffolds(workflows, patterns, target_ids, min_matches):
    matches = defaultdict(list)
    for filename, raw_data in workflows:
        seq = extract_sequence(raw_data)
        for pattern in patterns:
            pid = pattern["pattern_id"]
            if target_ids and pid not in target_ids:
                continue
            if _sequence_contains(seq, pattern.get("sequence_fragment", [])):
                matches[pid].append((filename, raw_data))
 
    results = {}
    for pattern in patterns:
        pid = pattern["pattern_id"]
        if target_ids and pid not in target_ids:
            continue
 
        matched = matches.get(pid, [])
 
        if len(matched) < min_matches:
            results[pid] = {
                "pattern_id":        pid,
                "control_flow":      pattern.get("control_flow"),
                "sequence_fragment": pattern.get("sequence_fragment", []),
                "match_count":       len(matched),
                "scaffold":          None,
                "status": f"insufficient_matches (need {min_matches}, got {len(matched)})",
            }
            continue
 
        best     = sorted(matched, key=lambda x: len(extract_sequence(x[1])))[0][1]
        variance = _build_variance_map(matched)
 
        scaffold = {}
        for xname, activity in best.items():
            if not isinstance(activity, dict):
                continue
            generalised = _generalise_node(activity, variance)
            key = generalised.get("xName", xname)
            scaffold[key] = generalised
 
        results[pid] = {
            "pattern_id":        pid,
            "control_flow":      pattern.get("control_flow"),
            "sequence_fragment": pattern.get("sequence_fragment", []),
            "match_count":       len(matched),
            "scaffold":          scaffold,
            "status":            "ok" if scaffold else "generation_failed",
        }
 
    return results
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────
 
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Mine workflow XML corpus → namespace_registry.json, field_defaults.json,\n"
            "activity_ranks.json, patterns/scaffolds.json.\n\n"
            "Run from repo root: python3 mine_corpus.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--xml-dir",     default=str(_DEFAULT_XML))
    parser.add_argument("--pattern-lib", default=str(_DEFAULT_LIB))
    parser.add_argument("--output-dir",  default=str(_DEFAULT_OUT))
    parser.add_argument("--patterns",    nargs="*", default=None,
                        help="Limit scaffold mining, e.g. --patterns p019 p020")
    parser.add_argument("--min-matches", type=int, default=3)
    parser.add_argument("--window",      type=int, default=3)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()
 
    xml_dir      = Path(args.xml_dir)
    pattern_path = Path(args.pattern_lib)
    output_dir   = Path(args.output_dir)
    target_ids   = set(args.patterns) if args.patterns else None
 
    # Preflight
    if not xml_dir.exists():
        print(f"ERROR: {xml_dir} not found.")
        print( "       mkdir xml_raw  and drop your XML files in there, then re-run.")
        sys.exit(1)
    if not pattern_path.exists():
        print(f"ERROR: pattern library not found at {pattern_path}")
        sys.exit(1)
 
    patterns = json.loads(pattern_path.read_text(encoding="utf-8"))
    active   = [p for p in patterns if not target_ids or p["pattern_id"] in target_ids]
    print(f"Pattern library: {len(patterns)} patterns"
          + (f"  (targeting {len(active)}: {sorted(target_ids)})" if target_ids else ""))
 
    # Parse
    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files found in {xml_dir}")
        sys.exit(1)
 
    print(f"Parsing {len(xml_files)} files", end="", flush=True)
 
    all_raw_data, all_ns, workflows, n_failed = [], [], [], 0
    for i, path in enumerate(xml_files):
        result = parse_xml_file(path)
        if result:
            raw_data, ns_map = result
            all_raw_data.append(raw_data)
            all_ns.append(ns_map)
            workflows.append((path.name, raw_data))
        else:
            n_failed += 1
        if (i + 1) % 50 == 0:
            print(f" {i + 1}", end="", flush=True)
 
    print(f"\n  Parsed: {len(workflows)}  |  Failed/skipped: {n_failed}")
 
    # Mine
    print("\n[1/4] Namespaces...", end=" ", flush=True)
    ns_registry = mine_namespaces(all_ns)
    print(f"{len(ns_registry)} activity types")
 
    print("[2/4] Field defaults...", end=" ", flush=True)
    field_defaults = mine_field_defaults(all_raw_data)
    total_fields   = sum(len(v) for v in field_defaults.values())
    print(f"{len(field_defaults)} activity types  |  {total_fields} fields")
    for ct, fields in sorted(field_defaults.items()):
        print(f"      {ct}: {fields}")
 
    print(f"[3/4] Co-occurrence pairs (window={args.window})...", end=" ", flush=True)
    cooccurrence = mine_cooccurrence(all_raw_data, window=args.window)
    filtered     = [p for p in cooccurrence if p["rank"] >= 2]
    print(f"{len(cooccurrence)} total  |  {len(filtered)} with rank ≥ 2")
    print("      Top 10:")
    for pair in cooccurrence[:10]:
        print(f"        {pair['activity']:35s} → {pair['next']:35s}  ({pair['rank']})")
 
    print(f"[4/4] Scaffolds (min_matches={args.min_matches})...", end=" ", flush=True)
    scaffold_results = mine_scaffolds(workflows, patterns, target_ids, args.min_matches)
    ok       = sum(1 for r in scaffold_results.values() if r["status"] == "ok")
    skipped  = sum(1 for r in scaffold_results.values() if "insufficient" in r.get("status", ""))
    failed_s = sum(1 for r in scaffold_results.values() if r.get("status") == "generation_failed")
    print(f"{ok} generated  |  {skipped} skipped  |  {failed_s} failed")
    for pid, r in sorted(scaffold_results.items()):
        cf    = r.get("control_flow", "")
        count = r["match_count"]
        if r["status"] == "ok":
            types = [v.get("CustomTypeName","?") for v in (r["scaffold"] or {}).values() if isinstance(v, dict)]
            print(f"      ✓ {pid} [{cf}]  {count} matches  →  {types}")
        else:
            print(f"      – {pid} [{cf}]  {r['status']}")
 
    if args.dry_run:
        print("\nDry run — nothing written.")
        return
 
    # Write
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "patterns").mkdir(parents=True, exist_ok=True)
 
    (output_dir / "namespace_registry.json").write_text(
        json.dumps(ns_registry, indent=2), encoding="utf-8")
    (output_dir / "field_defaults.json").write_text(
        json.dumps(field_defaults, indent=2), encoding="utf-8")
    (output_dir / "activity_ranks.json").write_text(
        json.dumps(filtered, indent=2), encoding="utf-8")
    (output_dir / "patterns" / "scaffolds.json").write_text(
        json.dumps({
            "generated_from":         str(xml_dir),
            "total_workflows_parsed": len(workflows),
            "scaffolds":              scaffold_results,
        }, indent=2), encoding="utf-8")
 
    print(f"""
Written to {output_dir}/
 
  namespace_registry.json   ({len(ns_registry)} entries)
    → Add to NAMESPACE_REGISTRY in serializer/xml_composer.py
    → Remove confirmed types from UNCONFIRMED_NAMESPACE_ACTIVITIES in tools/annotation_tools.py
 
  field_defaults.json   ({total_fields} fields across {len(field_defaults)} types)
    → Reference for seeding activity templates in tools/build_tools.py
 
  activity_ranks.json   ({len(filtered)} pairs, was 271)
    → Replaces data/activity_ranks.json directly
 
  patterns/scaffolds.json   ({ok} scaffolds)
    → Paste each scaffold into pattern_library.json:
        pattern["scaffold"] = scaffolds["scaffolds"]["p019"]["scaffold"]
""")
 
 
if __name__ == "__main__":
    main()
    