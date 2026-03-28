"""
tools/pipeline_stages.py

Deterministic Python replacements for five LLM agents:
  PatternMatcherAgent    → run_pattern_match()
  ActivityRetrieverAgent → run_retrieval()
  AnnotationAgent        → run_annotation()
  ValidationAgent        → run_validation()

run_output() lives in output_tools.py.
"""

import json
import os
import re as _re
import csv
from typing import Any

from tools.pattern_tools import (
    match_pattern,
    score_pattern_match,
)
from tools.retrieval_tools import load_activity_list, retrieve_all_steps
from tools.annotation_tools import (
    inject_unavailable_stubs,
    annotate_placeholders,
    add_verify_notes,
    collect_placeholder_summary,
    _ensure_dict,
)
from tools.validation_tools import run_all_validators


# ---------------------------------------------------------------------------
# CustomTypeName normalisation
# ---------------------------------------------------------------------------

_CT_ALIASES: dict = {
    "SetVariableActivity":      "MemorySet",
    "SetVariable":              "MemorySet",
    "MemorySetActivity":        "MemorySet",
    "PowerShellScriptActivity": "PowerShellScript",
    "IfElseActivityActivity":   "IfElseActivity",
}

_valid_ct_names: set | None = None


def _load_valid_ct_names() -> set:
    global _valid_ct_names
    if _valid_ct_names is not None:
        return _valid_ct_names
    data_dir = os.getenv("DATA_DIR", "data")
    path = os.path.join(data_dir, "activity_list.txt")
    names = set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                name = row.get("name", "").strip()
                if name:
                    names.add(name)
    except FileNotFoundError:
        pass
    names.update({
        "WhileActivity", "SequenceActivity", "IfElseActivity",
        "IfElseBranchActivity", "ParallelActivity", "UserGroup",
        "ForEachActivity", "ExitWhile", "ReturnValue",
    })
    _valid_ct_names = names
    return _valid_ct_names


def _normalise_ct(ct: str) -> str:
    if not ct:
        return ct
    valid = _load_valid_ct_names()
    if ct in valid:
        return ct
    if ct in _CT_ALIASES:
        return _CT_ALIASES[ct]
    if ct.endswith("Activity"):
        stripped = ct[:-8]
        if stripped in valid:
            return stripped
    return ct


# ---------------------------------------------------------------------------
# Wiring map helpers
# ---------------------------------------------------------------------------

def _load_wiring_map() -> list:
    if not hasattr(_load_wiring_map, "_cache"):
        data_dir = os.getenv("DATA_DIR", "data")
        path = os.path.join(data_dir, "wiring_map.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                _load_wiring_map._cache = raw
            elif isinstance(raw, dict):
                _load_wiring_map._cache = raw.get("wiring", [])
            else:
                _load_wiring_map._cache = []
        except FileNotFoundError:
            print(f"[pipeline_stages] Warning: wiring_map.json not found at {path}")
            _load_wiring_map._cache = []
    return _load_wiring_map._cache


def _build_wiring_lookup(wiring_entries: list) -> dict:
    lookup: dict = {}
    for entry in wiring_entries:
        src   = entry.get("source_activity", "")
        tgt   = entry.get("target_activity", "")
        field = entry.get("target_field", "")
        pct   = entry.get("pct_of_target", 0)
        auth  = entry.get("authoritative", False)
        if not (src and tgt and field):
            continue
        tgt_map = lookup.setdefault(src, {}).setdefault(tgt, {})
        if field not in tgt_map:
            tgt_map[field] = {"pct": pct, "authoritative": auth}
    return lookup


# ---------------------------------------------------------------------------
# Dependency graph helpers
# ---------------------------------------------------------------------------

def _load_dependency_graph() -> dict:
    if not hasattr(_load_dependency_graph, "_cache"):
        data_dir = os.getenv("DATA_DIR", "data")
        path = os.path.join(data_dir, "dependency_graph.json")
        try:
            with open(path, encoding="utf-8") as f:
                _load_dependency_graph._cache = json.load(f)
            print(f"[pipeline_stages] Loaded dependency graph: "
                  f"{len(_load_dependency_graph._cache)} consumer types")
        except FileNotFoundError:
            print(f"[pipeline_stages] Warning: dependency_graph.json not found at {path}")
            _load_dependency_graph._cache = {}
    return _load_dependency_graph._cache


def _check_manifest_dependencies(manifest: list, dep_graph: dict) -> list:
    warnings = []
    seen_types: set = set()
    for entry in manifest:
        activity_type = entry.get("selected_activity", "")
        step_id       = entry.get("step_id", "")
        if not activity_type or entry.get("status") == "UNAVAILABLE":
            seen_types.add(activity_type)
            continue
        if activity_type in dep_graph:
            for dep in dep_graph[activity_type].get("required_inputs", []):
                dep_type  = dep.get("dependency_type", "")
                providers = dep.get("provided_by", [])
                field     = dep.get("field", "")
                note      = dep.get("notes", "")
                if dep_type not in ("table", "session"):
                    continue
                if not any(p in seen_types for p in providers):
                    warnings.append({
                        "step_id":            step_id,
                        "activity":           activity_type,
                        "field":              field,
                        "dependency_type":    dep_type,
                        "expected_providers": providers[:3],
                        "note":               note,
                    })
        seen_types.add(activity_type)
    return warnings


# ---------------------------------------------------------------------------
# Stage 2 — Pattern match
# ---------------------------------------------------------------------------

def run_pattern_match(decomposition: dict) -> dict:
    decomposition = _ensure_dict(decomposition)
    candidates    = match_pattern(decomposition)
    result        = score_pattern_match(candidates, decomposition)
    return result


# ---------------------------------------------------------------------------
# Stage 3 — Retrieve activities
# ---------------------------------------------------------------------------

def run_retrieval(decomposition: dict) -> list:
    decomposition  = _ensure_dict(decomposition)
    load_activity_list()

    steps    = decomposition.get("steps", [])
    manifest = retrieve_all_steps(steps)

    wiring_entries = _load_wiring_map()
    wiring_lookup  = _build_wiring_lookup(wiring_entries)
    dep_graph      = _load_dependency_graph()

    trimmed = []
    for s in manifest:
        activity_type = s.get("selected_activity") or ""
        status        = s["status"]
        entry: dict = {
            "step_id":           s["step_id"],
            "selected_activity": activity_type,
            "status":            status,
            "frequency_tier":    s.get("frequency_tier", "medium"),
        }
        if "confidence" in s:
            entry["confidence"] = s["confidence"]
        if status == "UNCERTAIN" and s.get("candidates"):
            entry["candidates"] = [
                {"activity_name": c["activity_name"],
                 "combined_score": c.get("combined_score", 0.0)}
                for c in s["candidates"][:3]
            ]
        trimmed.append(entry)

    for i in range(1, len(trimmed)):
        prev     = trimmed[i - 1]
        curr     = trimmed[i]
        src_type = prev["selected_activity"]
        tgt_type = curr["selected_activity"]
        if not src_type or not tgt_type:
            continue
        tgt_wirings = wiring_lookup.get(src_type, {}).get(tgt_type, {})
        if not tgt_wirings:
            continue
        wiring_hints = {}
        for field, info in tgt_wirings.items():
            pct  = info.get("pct", 0)
            auth = info.get("authoritative", False)
            if auth:
                continue
            if pct >= 80:
                wiring_hints[f"_wire_hint_{field}"] = f"{src_type}:{pct}pct"
        if wiring_hints:
            curr["pre_filled_fields"] = wiring_hints

    dep_warnings = _check_manifest_dependencies(trimmed, dep_graph)
    if dep_warnings:
        warn_by_step: dict = {}
        for w in dep_warnings:
            warn_by_step.setdefault(w["step_id"], []).append(w)
        for entry in trimmed:
            sid = entry["step_id"]
            if sid in warn_by_step:
                entry["missing_prerequisites"] = warn_by_step[sid]
                missing = [
                    f"{w['field']} ({w['dependency_type']}, "
                    f"needs one of: {w['expected_providers']})"
                    for w in warn_by_step[sid]
                ]
                entry["prerequisite_note"] = (
                    f"PREREQUISITE WARNING: {entry['selected_activity']} requires "
                    f"a preceding activity for: {'; '.join(missing)}. "
                    f"Add the appropriate source activity before this step."
                )
        print(f"[pipeline_stages] Dependency check: {len(dep_warnings)} missing "
              f"prerequisite(s) flagged in {len(warn_by_step)} step(s)")

    return trimmed


# ---------------------------------------------------------------------------
# Stage 4b — Enrich
# ---------------------------------------------------------------------------

def run_enrichment(placed_skeleton: dict, activity_manifest: list) -> dict:
    """
    For every activity in the placed skeleton:
      1. Normalise CustomTypeName (fixes PlacerAgent "Activity" suffix hallucination)
      2. Load its full template from activity_json_syntax.json
      3. Merge template fields as base (non-destructive — xName/CustomTypeName win)
      4. Apply any pre_filled_fields from the manifest entry (authoritative)
    """
    from tools.build_tools import load_activity_template
    from tools.annotation_tools import _ensure_dict

    placed_skeleton = _ensure_dict(placed_skeleton)
    raw = placed_skeleton.get("workflow_raw_data", placed_skeleton)

    manifest_lookup: dict = {}
    if isinstance(activity_manifest, list):
        for entry in activity_manifest:
            act = entry.get("selected_activity")
            if act:
                manifest_lookup[act] = entry.get("pre_filled_fields", {})

    def enrich_node(node: dict) -> dict:
        if not isinstance(node, dict):
            return node

        ct    = _normalise_ct(node.get("CustomTypeName", ""))
        xname = node.get("xName", "")

        if ct != node.get("CustomTypeName", ""):
            print(f"[enrichment] normalised CustomTypeName "
                  f"'{node['CustomTypeName']}' → '{ct}'")
            node = dict(node)
            node["CustomTypeName"] = ct

        if ct:
            template = load_activity_template(ct)
            if template:
                merged = {**template, **{
                    k: v for k, v in node.items()
                    if v is not None and v != ""
                }}
                merged["xName"] = xname or merged.get("xName", "")
                for field, value in manifest_lookup.get(ct, {}).items():
                    if not field.startswith("_wire_hint_"):
                        merged[field] = value
            else:
                merged = dict(node)
        else:
            merged = dict(node)

        for k, v in list(merged.items()):
            if isinstance(v, dict) and v.get("CustomTypeName"):
                merged[k] = enrich_node(v)

        return merged

    enriched = {}
    for xname, activity in raw.items():
        if isinstance(activity, dict):
            enriched[xname] = enrich_node(activity)
        else:
            enriched[xname] = activity

    return {
        "workflow_raw_data": enriched,
        "variable_contracts": placed_skeleton.get("variable_contracts", {}),
    }


# ---------------------------------------------------------------------------
# Stage 4c — Apply structural fragments  (F1-F9)
# ---------------------------------------------------------------------------

_STATUS_PRODUCERS: frozenset = frozenset({
    "Ping", "ServiceStatus", "FileExist", "FTPFileExists",
    "ADUserExists", "PowerShellScript", "PowerShell",
    "ADIsAccountDisabled", "ADIsAccountLocked",
})

_SCALAR_PRODUCERS: frozenset = frozenset({
    "GetRowsCount", "DateDifference", "FunctionCalculator",
    "GetCellValue", "GetCellValueAdvanced", "GetDate", "Contains",
    "IsEmpty", "InStr", "InStrRev", "Len", "ConvertPasswordToPlaintext",
    "GetLength", "SubStringByText", "Trim", "Replace",
})

_RETURNVALUE_FORBIDDEN_FIELDS: frozenset = frozenset({
    "visible", "disabled", "isFavorite", "isJsonValid",
    "readPermission", "writePermission", "modulePermissions",
    "activityLicenseType", "Timeout", "TimeInSeconds",
    "TargetModuleID", "TargetModuleName", "Path", "label", "notes",
})


def _clean_returnvalue(node: dict) -> None:
    for field in _RETURNVALUE_FORBIDDEN_FIELDS:
        node.pop(field, None)


def _apply_returnvalue_fragment(node: dict, preceding_ct: str | None) -> None:
    _clean_returnvalue(node)
    node.setdefault("Type", "StoredValue")
    node.setdefault("UseBranchWhenTimeout", "True")
    node.setdefault("ConditionType", "")
    node.setdefault("Value", "")
    node.setdefault("ConditionNumber", "0")
    node.setdefault("ConditionName", "")
    node.setdefault("UseCustomeCondition", "False")
    node.setdefault("Description", "")
    node.setdefault("description", "")
    node.setdefault("RecoveryMethodSelection", "{x:Null}")
    node.setdefault("Disabled", "False")
    node.setdefault("ClusterID", "{x:Null}")
    node.setdefault("ClusterName", "{x:Null}")
    node.setdefault("DisplayName", "Return Value")
    node.setdefault("TypeName", "ReturnValue")
    node.setdefault("name", "ReturnValue")

    has_condition = bool(node.get("ConditionType", ""))

    if preceding_ct in _STATUS_PRODUCERS:
        node["Type"] = "StoredValue"
        node["UseStoredValue"] = "True"
        node["IsValid"] = "True"
        node.setdefault("Formula", "")
    elif preceding_ct in _SCALAR_PRODUCERS:
        if has_condition:
            node["Type"] = "UserDefinedValue"
            node["UseStoredValue"] = "False"
            node["IsValid"] = "True"
            node.setdefault("Formula", "")
        else:
            node["IsValid"] = "False"
            node["Formula"] = "{x:Null}"
            node.pop("UseStoredValue", None)
    else:
        if has_condition:
            node.setdefault("IsValid", "True")
            node.setdefault("Formula", "")
        else:
            node["IsValid"] = "False"
            node["Formula"] = "{x:Null}"
            node.pop("UseStoredValue", None)


def _make_returnvalue(xname: str) -> dict:
    return {
        "xName": xname,
        "CustomTypeName": "ReturnValue",
        "Formula": "",
        "ConditionNumber": "0",
        "Value": "",
        "ConditionType": "",
        "Description": "",
        "description": "",
        "Type": "StoredValue",
        "RecoveryMethodSelection": "{x:Null}",
        "ConditionName": "",
        "UseCustomeCondition": "False",
        "UseBranchWhenTimeout": "True",
        "DisplayName": "Return Value",
        "TypeName": "ReturnValue",
        "Disabled": "False",
        "ClusterID": "{x:Null}",
        "ClusterName": "{x:Null}",
        "name": "ReturnValue",
    }


def _inject_returnvalue_if_missing(branch: dict, branch_key: str,
                                    branch_index: int) -> None:
    has_rv = any(
        isinstance(v, dict) and v.get("CustomTypeName") == "ReturnValue"
        for v in branch.values()
    )
    if has_rv:
        return

    branch_xname = branch.get("xName", f"ifElseBranchActivity{branch_index + 1}")
    m = _re.search(r'(\d+)$', branch_xname)
    rv_num   = m.group(1) if m else str(branch_index + 1)
    rv_xname = f"returnValue{rv_num}"

    existing_xnames = {v.get("xName", "") for v in branch.values() if isinstance(v, dict)}
    if rv_xname in existing_xnames:
        rv_xname = f"returnValue{rv_num}b"

    rv_node    = _make_returnvalue(rv_xname)
    new_branch = {rv_xname: rv_node}
    for k, v in branch.items():
        if k != rv_xname:
            new_branch[k] = v
    branch.clear()
    branch.update(new_branch)
    print(f"[fragments] F9 injected ReturnValue '{rv_xname}' into '{branch_xname}'")


def _walk_branch_body(activities: dict, preceding_ct: str | None,
                      parent_while_xname: str | None,
                      nearest_getrowscount_xname: str | None) -> None:
    last_ct = preceding_ct
    for xname, node in activities.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue
        if ct == "ReturnValue":
            _apply_returnvalue_fragment(node, preceding_ct)
        elif ct == "GetCellValue" and parent_while_xname:
            node["RowNumber"] = f"%{parent_while_xname}%"
            node["ColumnType"] = "Name"
        elif ct == "MemorySet":
            node.setdefault("VariableScope", "Workflow")
            node.setdefault("IsSaved", "False")
            node.setdefault("IsAppend", "False")
        elif ct == "WhileActivity":
            node["Condition"] = "{x:Null}"
            _walk_while(node, xname, nearest_getrowscount_xname)
        elif ct == "IfElseActivity":
            _walk_ifelse(node, last_ct, parent_while_xname, nearest_getrowscount_xname)
        if ct not in ("ReturnValue", "ExitWhile", "IfElseActivity",
                      "IfElseBranchActivity", "SequenceActivity"):
            last_ct = ct
        if ct == "GetRowsCount":
            nearest_getrowscount_xname = xname


def _walk_ifelse(ifelse_node: dict, preceding_ct: str | None,
                 parent_while_xname: str | None,
                 nearest_getrowscount_xname: str | None) -> None:
    branch_index = 0
    for k, branch in ifelse_node.items():
        if not isinstance(branch, dict):
            continue
        if branch.get("CustomTypeName") != "IfElseBranchActivity":
            continue
        _inject_returnvalue_if_missing(branch, k, branch_index)
        _walk_branch_body(branch, preceding_ct, parent_while_xname, nearest_getrowscount_xname)
        branch_index += 1


def _walk_while(while_node: dict, while_xname: str,
                nearest_getrowscount_xname: str | None) -> None:
    seq_xname: str | None = None
    seq_node: dict | None = None
    for k, v in while_node.items():
        if isinstance(v, dict) and v.get("CustomTypeName") == "SequenceActivity":
            seq_xname = k
            seq_node  = v
            break
    if seq_node is None:
        return

    last_ct: str | None = None
    local_getrowscount  = nearest_getrowscount_xname

    for xname, node in seq_node.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue
        if ct == "ExitWhile":
            node["exitWhileInsideWhile"] = "True"
            node["isValid"] = "True"
            node["TypeName"] = "ExitWhile"
            if seq_xname:
                node["whileSequenceActivity"] = seq_xname
            if local_getrowscount:
                node["Counter"] = f"%{local_getrowscount}%"
        elif ct == "GetCellValue":
            node["RowNumber"] = f"%{while_xname}%"
            node["ColumnType"] = "Name"
        elif ct == "MemorySet":
            node.setdefault("VariableScope", "Workflow")
            node.setdefault("IsSaved", "False")
            node.setdefault("IsAppend", "False")
        elif ct == "WhileActivity":
            node["Condition"] = "{x:Null}"
            _walk_while(node, xname, local_getrowscount)
        elif ct == "IfElseActivity":
            _walk_ifelse(node, last_ct, while_xname, local_getrowscount)
        elif ct == "ReturnValue":
            _apply_returnvalue_fragment(node, last_ct)
        if ct == "GetRowsCount":
            local_getrowscount = xname
        if ct not in ("ExitWhile", "ReturnValue", "SequenceActivity",
                      "IfElseActivity", "IfElseBranchActivity"):
            last_ct = ct


def _walk_sequence_as_scope(seq_node: dict,
                             nearest_getrowscount_xname: str | None) -> None:
    local_getrowscount = nearest_getrowscount_xname
    last_ct: str | None = None
    for xname, node in seq_node.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue
        if ct == "WhileActivity":
            node["Condition"] = "{x:Null}"
            _walk_while(node, xname, local_getrowscount)
        elif ct == "MemorySet":
            node.setdefault("VariableScope", "Workflow")
            node.setdefault("IsSaved", "False")
            node.setdefault("IsAppend", "False")
        elif ct == "IfElseActivity":
            _walk_ifelse(node, last_ct, None, local_getrowscount)
        elif ct == "ReturnValue":
            _apply_returnvalue_fragment(node, last_ct)
        if ct == "GetRowsCount":
            local_getrowscount = xname
        if ct not in ("ReturnValue", "IfElseActivity", "IfElseBranchActivity",
                      "SequenceActivity", "WhileActivity"):
            last_ct = ct


def _walk_top_level(raw: dict) -> None:
    last_getrowscount: str | None = None
    last_ct: str | None = None
    keys_to_remove = []

    for xname, node in raw.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue
        if ct == "ReturnValue":
            keys_to_remove.append(xname)
            continue
        if ct == "WhileActivity":
            node["Condition"] = "{x:Null}"
            _walk_while(node, xname, last_getrowscount)
        elif ct == "MemorySet":
            node.setdefault("VariableScope", "Workflow")
            node.setdefault("IsSaved", "False")
            node.setdefault("IsAppend", "False")
        elif ct == "IfElseActivity":
            _walk_ifelse(node, last_ct, None, last_getrowscount)
        elif ct == "SequenceActivity":
            _walk_sequence_as_scope(node, last_getrowscount)
        if ct == "GetRowsCount":
            last_getrowscount = xname
        if ct not in ("IfElseActivity", "IfElseBranchActivity",
                      "SequenceActivity", "WhileActivity"):
            last_ct = ct

    for key in keys_to_remove:
        print(f"[fragments] Removed invalid top-level ReturnValue: {key}")
        del raw[key]


def apply_fragments(workflow_json: dict) -> dict:
    """
    Apply structural invariants F1-F9. Idempotent.

    F1  WhileActivity.Condition = "{x:Null}"
    F2  ExitWhile: exitWhileInsideWhile, isValid, TypeName, whileSequenceActivity
    F3  ExitWhile.Counter = %{nearest GetRowsCount xName}%
    F4  GetCellValue.RowNumber = %{parent WhileActivity xName}%, ColumnType="Name"
    F5  MemorySet defaults (VariableScope, IsSaved, IsAppend)
    F6  ReturnValue defaults + forbidden field removal
    F7  ReturnValue status tier
    F8  ReturnValue scalar tier
    F9  Inject missing ReturnValue into every IfElseBranchActivity
    """
    import copy
    result = copy.deepcopy(workflow_json)
    raw    = result.get("workflow_raw_data", result)
    if not isinstance(raw, dict):
        print("[fragments] Warning: workflow_raw_data is not a dict — skipping")
        return result
    _walk_top_level(raw)
    print(f"[fragments] Applied F1-F9 to {len(raw)} top-level activities")
    return result


def run_fragments(enriched_workflow: dict) -> dict:
    """
    Stage 4c / 4f: applies F1-F9. Called twice in the pipeline:
      4c — before WirerAgent (structural context)
      4f — after merge of WirerAgent output (enforce invariants on final JSON)
    """
    enriched_workflow = _ensure_dict(enriched_workflow)
    return apply_fragments(enriched_workflow)


# ---------------------------------------------------------------------------
# Stage 4c.5 — Content scaffold  (deterministic, between fragments and Wirer)
# ---------------------------------------------------------------------------
#
# Applies confirmed wiring rules from RitaLab testing (March 2026, test XMLs
# 92001–92008). Sets fields that can be derived structurally from context —
# i.e. from the nearest preceding table-producing activity — so WirerAgent
# only needs to handle genuinely semantic fields (column names, values, etc.).
#
# CONTRACT: only sets fields that are empty/missing. Never overwrites a value
# already placed by enrichment, fragments, or a prior scaffold pass.
# This makes the stage safe to call multiple times (idempotent).
#
# Table producers and their output variable:
#   CreateMemoryTable  → TableName field value  (e.g. "serverList")
#   TSQLQuery          → xName                  (e.g. "tsqlQuery1")
#   JsonToTable        → xName                  (e.g. "jsonToTable1")
#   ResultSetFilter    → xName                  (e.g. "resultSetFilter1")
#   GetRows            → xName
#   NestedJsonToTable  → xName
#
# JSON session producers:
#   StartJsonSession   → xName  (referenced by JsonToTable.SessionName)
# ---------------------------------------------------------------------------

# Activities that produce a table variable reachable as %xName%
_TABLE_PRODUCERS_XNAME: frozenset = frozenset({
    "TSQLQuery",
    "JsonToTable",
    "ResultSetFilter",
    "GetRows",
    "NestedJsonToTable",
})

# Activities that produce a table variable named by a specific field
_TABLE_PRODUCERS_FIELD: dict = {
    "CreateMemoryTable": "TableName",
}

# Container types — scaffold skips their own node but recurses into children
_SCAFFOLD_CONTAINERS: frozenset = frozenset({
    "WhileActivity", "SequenceActivity", "IfElseActivity",
    "IfElseBranchActivity", "ParallelActivity", "UserGroup",
    "ForEachActivity",
})


def _get_table_var(node: dict) -> str | None:
    """
    Return the bare variable name (without %) for the table this node produces,
    or None if it is not a table producer.
    """
    ct = node.get("CustomTypeName", "")
    if ct in _TABLE_PRODUCERS_XNAME:
        return node.get("xName") or None
    field = _TABLE_PRODUCERS_FIELD.get(ct)
    if field:
        val = node.get(field, "").strip().strip("%")
        return val or None
    return None


def _sif(node: dict, field: str, value: str) -> None:
    """Set field on node only if currently absent or empty (set-if-empty)."""
    if not node.get(field):
        node[field] = value


def _scaffold_node(
    node: dict,
    nearest_table_var: str | None,
    nearest_session_xname: str | None,
    inside_while: bool,
    parent_while_xname: str | None,
) -> None:
    """
    Apply all confirmed scaffold rules to a single leaf activity node in-place.
    Rules are applied with _sif (set-if-empty) so Wirer values are never clobbered.
    """
    ct    = node.get("CustomTypeName", "")
    xname = node.get("xName", "")

    # ── GetRowsCount ─────────────────────────────────────────────────────────
    # ResultSet = %tableVar%  (confirmed: test_table_loop, test_json_session,
    #                          test_resultsetfilter, test_setcellvalue)
    if ct == "GetRowsCount":
        if nearest_table_var:
            _sif(node, "ResultSet", f"%{nearest_table_var}%")

    # ── GetCellValue ─────────────────────────────────────────────────────────
    # ResultSet    = %tableVar%       (confirmed: test_table_loop)
    # ResultSetName = tableVar (bare) (confirmed: test_table_loop — no % signs)
    # ColumnType   = "Name"           (confirmed: all table tests)
    # RowNumber    inside While already set by F4 — only set outside While
    elif ct == "GetCellValue":
        if nearest_table_var:
            _sif(node, "ResultSet",     f"%{nearest_table_var}%")
            _sif(node, "ResultSetName", nearest_table_var)  # bare — no %
        _sif(node, "ColumnType", "Name")
        # RowNumber outside While: do NOT scaffold — no safe default without
        # knowing which row the user intends. Leave for WirerAgent.

    # ── GetRowsCount already updates nearest_table_var — handled in walker ──

    # ── ResultSetFilter ───────────────────────────────────────────────────────
    # VariableName = %tableVar%  (confirmed: test_resultsetfilter)
    elif ct == "ResultSetFilter":
        if nearest_table_var:
            _sif(node, "VariableName", f"%{nearest_table_var}%")

    # ── DeleteMemoryTableRows ─────────────────────────────────────────────────
    # ResultSet    = %tableVar%   (confirmed: test_setcellvalue + corpus)
    # ResultSetName = %tableVar%  (confirmed: corpus — WITH % unlike GetCellValue)
    # RowNumber: must be integer(s) — never "All". No safe default; leave for Wirer.
    elif ct == "DeleteMemoryTableRows":
        if nearest_table_var:
            _sif(node, "ResultSet",     f"%{nearest_table_var}%")
            _sif(node, "ResultSetName", f"%{nearest_table_var}%")

    # ── AddMemoryTableRow ─────────────────────────────────────────────────────
    # ResultSet    = %tableVar%   (confirmed: corpus)
    # ResultSetName = %tableVar%  (confirmed: corpus — WITH %)
    # Selection    = "2"          (confirmed: corpus — end of table default)
    # RowNumber    = ""           (confirmed: corpus — always empty)
    elif ct == "AddMemoryTableRow":
        if nearest_table_var:
            _sif(node, "ResultSet",     f"%{nearest_table_var}%")
            _sif(node, "ResultSetName", f"%{nearest_table_var}%")
        _sif(node, "Selection",  "2")
        _sif(node, "RowNumber",  "")

    # ── SetCellValue ──────────────────────────────────────────────────────────
    # VariableName = %tableVar%  (confirmed: corpus)
    # ColumnType   = "Name"      (confirmed: corpus dominant pattern)
    # RowNumber: must be %var% — no safe default without knowing which var.
    elif ct == "SetCellValue":
        if nearest_table_var:
            _sif(node, "VariableName", f"%{nearest_table_var}%")
        _sif(node, "ColumnType", "Name")

    # ── JsonToTable ───────────────────────────────────────────────────────────
    # SessionName = literal xName of StartJsonSession (NO % signs — confirmed)
    # KeyPath: dot-notation — semantic, leave for Wirer
    elif ct == "JsonToTable":
        if nearest_session_xname:
            _sif(node, "SessionName", nearest_session_xname)  # no % signs

    # ── StartJsonSession ──────────────────────────────────────────────────────
    # StartSession = "True"  (confirmed: test_json_session)
    elif ct == "StartJsonSession":
        _sif(node, "StartSession", "True")

    # ── ReplaceString ─────────────────────────────────────────────────────────
    # ReplaceOriginalVariable = "False"  (confirmed: test_replacestring)
    # SourceString: semantic — leave for Wirer
    elif ct == "ReplaceString":
        _sif(node, "ReplaceOriginalVariable", "False")
        _sif(node, "IsMatchCase",   "False")
        _sif(node, "IsRegex",       "False")
        _sif(node, "RemoveSpaces",  "False")
        _sif(node, "RemoveNewLines","False")
        _sif(node, "RemoveTabs",    "False")

    # ── MultiMemorySet ────────────────────────────────────────────────────────
    # variableScope = "Workflow"  (confirmed: test_multimemoryset)
    # IsGlobal      = "False"     (confirmed: test_multimemoryset)
    elif ct == "MultiMemorySet":
        _sif(node, "variableScope", "Workflow")
        _sif(node, "IsGlobal",      "False")

    # ── TSQLQuery ─────────────────────────────────────────────────────────────
    # SiteId = "-1", SiteName = "", isUserAuthenticate = "False"
    # when using a variable-based connection string (corpus dominant pattern).
    # ConnectionStringTextBox / ConnectionString left to user — environment-specific.
    elif ct == "TSQLQuery":
        _sif(node, "SiteId",              "-1")
        _sif(node, "SiteName",            "")
        _sif(node, "isUserAuthenticate",  "False")
        _sif(node, "UserName",            "")
        _sif(node, "Password",            "")


def _scaffold_walk(
    nodes: dict,
    nearest_table_var: str | None,
    nearest_session_xname: str | None,
    inside_while: bool,
    parent_while_xname: str | None,
) -> tuple[str | None, str | None]:
    """
    Walk an ordered dict of activity nodes, applying scaffold rules and
    tracking nearest table producer / session producer as we go.

    Returns updated (nearest_table_var, nearest_session_xname) for the
    caller's scope — callers that need to propagate state upward use this.
    """
    for xname, node in nodes.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue

        if ct in _SCAFFOLD_CONTAINERS:
            # Recurse into container children, carrying current context
            if ct == "WhileActivity":
                # Find the SequenceActivity child and recurse
                for child_key, child_val in node.items():
                    if isinstance(child_val, dict) and \
                       child_val.get("CustomTypeName") == "SequenceActivity":
                        nearest_table_var, nearest_session_xname = _scaffold_walk(
                            child_val,
                            nearest_table_var,
                            nearest_session_xname,
                            inside_while=True,
                            parent_while_xname=xname,
                        )
                        break
            elif ct == "IfElseActivity":
                for child_key, child_val in node.items():
                    if isinstance(child_val, dict) and \
                       child_val.get("CustomTypeName") == "IfElseBranchActivity":
                        _scaffold_walk(
                            child_val,
                            nearest_table_var,
                            nearest_session_xname,
                            inside_while,
                            parent_while_xname,
                        )
            elif ct in ("SequenceActivity", "UserGroup"):
                nearest_table_var, nearest_session_xname = _scaffold_walk(
                    node,
                    nearest_table_var,
                    nearest_session_xname,
                    inside_while,
                    parent_while_xname,
                )
            else:
                # ParallelActivity, ForEachActivity, IfElseBranchActivity
                _scaffold_walk(
                    node,
                    nearest_table_var,
                    nearest_session_xname,
                    inside_while,
                    parent_while_xname,
                )
            # After recursing into container, update table tracker if container
            # is itself a table producer (e.g. ForEachActivity iterating a table)
            tv = _get_table_var(node)
            if tv:
                nearest_table_var = tv
        else:
            # Leaf activity — apply scaffold rules first, then update trackers
            _scaffold_node(
                node,
                nearest_table_var,
                nearest_session_xname,
                inside_while,
                parent_while_xname,
            )
            # Update session tracker before table tracker (JsonToTable needs session)
            if ct == "StartJsonSession":
                nearest_session_xname = xname
            # Update table tracker
            tv = _get_table_var(node)
            if tv:
                nearest_table_var = tv

    return nearest_table_var, nearest_session_xname


def apply_content_scaffold(workflow_json: dict) -> dict:
    """
    Apply deterministic semantic wiring rules (Stage 4c.5).

    Rules confirmed by RitaLab testing March 2026 (test XMLs 92001-92008):

    S1   GetRowsCount.ResultSet              = %nearestTableVar%
    S2   GetCellValue.ResultSet              = %nearestTableVar%
    S3   GetCellValue.ResultSetName          = nearestTableVar  (bare, no %)
    S4   GetCellValue.ColumnType             = "Name"
    S5   ResultSetFilter.VariableName        = %nearestTableVar%
    S6   DeleteMemoryTableRows.ResultSet     = %nearestTableVar%
    S7   DeleteMemoryTableRows.ResultSetName = %nearestTableVar%  (WITH %)
    S8   AddMemoryTableRow.ResultSet         = %nearestTableVar%
    S9   AddMemoryTableRow.ResultSetName     = %nearestTableVar%  (WITH %)
    S10  AddMemoryTableRow.Selection         = "2"  (end of table)
    S11  AddMemoryTableRow.RowNumber         = ""   (always empty)
    S12  SetCellValue.VariableName           = %nearestTableVar%
    S13  SetCellValue.ColumnType             = "Name"
    S14  JsonToTable.SessionName             = nearestSessionXName  (no %)
    S15  StartJsonSession.StartSession       = "True"
    S16  ReplaceString.ReplaceOriginalVariable = "False"
    S17  ReplaceString boolean flags         = "False" (IsMatchCase etc.)
    S18  MultiMemorySet.variableScope        = "Workflow"
    S19  MultiMemorySet.IsGlobal             = "False"
    S20  TSQLQuery.SiteId                    = "-1"
    S21  TSQLQuery.SiteName                  = ""
    S22  TSQLQuery.isUserAuthenticate        = "False"

    NOT scaffolded (left for WirerAgent — genuinely semantic):
      - GetCellValue.ColumnNumber      (which column to read)
      - GetCellValue.RowNumber outside While (which row — no safe default)
      - SetCellValue.ColNumber         (which column to write)
      - SetCellValue.NewValue          (value to write)
      - SetCellValue.RowNumber         (which row — must be %var% ref, but which var?)
      - DeleteMemoryTableRows.RowNumber (which rows — integer list, user-specified)
      - JsonToTable.KeyPath            (dot-notation path into JSON)
      - StartJsonSession.JsonString    (the JSON payload)
      - TSQLQuery.Query                (SQL query)
      - TSQLQuery.ConnectionStringTextBox / ConnectionString  (environment-specific)
      - MultiMemorySet.variableProperties  (the variable assignments)
      - DisplayValue.ValueToDisplay    (the message string)
      - MemorySet.VariableName/VariableValue  (semantic)
    """
    import copy
    result = copy.deepcopy(workflow_json)
    raw    = result.get("workflow_raw_data", result)

    if not isinstance(raw, dict):
        print("[scaffold] Warning: workflow_raw_data is not a dict — skipping")
        return result

    _scaffold_walk(
        raw,
        nearest_table_var=None,
        nearest_session_xname=None,
        inside_while=False,
        parent_while_xname=None,
    )

    print(f"[scaffold] Applied S1-S22 to workflow ({len(raw)} top-level activities)")
    return result


def run_content_scaffold(fragmented_workflow: dict) -> dict:
    """
    Stage 4c.5: deterministic semantic wiring scaffold.
    Runs after run_fragments() and before WirerAgent.

    Call site in agents/pipeline.py (insert after Stage 4c block):

        # ── Stage 4c.5: Python — Content scaffold ─────────────────────────
        try:
            scaffolded_workflow = run_content_scaffold(fragmented_workflow)
            ctx.session.state["enriched_workflow"] = scaffolded_workflow
            print(f"  [pipeline] scaffold ok")
        except Exception as e:
            print(f"  [pipeline] scaffold failed (non-fatal): {e}")
            scaffolded_workflow = fragmented_workflow  # fall through to Wirer

    Same block in CorrectionPipeline._run() after the fragments block.
    """
    fragmented_workflow = _ensure_dict(fragmented_workflow)
    return apply_content_scaffold(fragmented_workflow)


# ---------------------------------------------------------------------------
# Stage 5 — Annotate
# ---------------------------------------------------------------------------

def run_annotation(workflow_json: dict, activity_manifest: Any) -> dict:
    workflow_json = _ensure_dict(workflow_json)
    if isinstance(activity_manifest, list):
        manifest_dict = {"steps": activity_manifest}
    else:
        manifest_dict = _ensure_dict(activity_manifest)
    result  = inject_unavailable_stubs(workflow_json, manifest_dict)
    result  = annotate_placeholders(result)
    result  = add_verify_notes(result)
    summary = collect_placeholder_summary(result)
    return {
        "annotated_workflow_json": result,
        "placeholder_summary":     summary,
    }


# ---------------------------------------------------------------------------
# Stage 6 — Validate
# ---------------------------------------------------------------------------

def run_validation(annotation_result: dict) -> dict:
    annotation_result   = _ensure_dict(annotation_result)
    workflow_json       = annotation_result.get("annotated_workflow_json", {})
    placeholder_summary = annotation_result.get("placeholder_summary", [])
    val_result = run_all_validators(workflow_json)
    verify_notes = [
        item["message"]
        for item in placeholder_summary
        if item.get("kind") == "verify"
    ]
    return {
        "status":              val_result["status"],
        "workflow_json":       workflow_json if val_result["status"] == "valid" else None,
        "placeholder_summary": placeholder_summary,
        "errors":              val_result.get("errors", []),
        "verify_notes":        verify_notes,
    }