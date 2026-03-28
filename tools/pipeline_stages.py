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
# Wiring map helpers
# ---------------------------------------------------------------------------

def _load_wiring_map() -> list:
    """
    Load wiring_map.json once and cache.
    Handles both a bare list and a {"wiring": [...]} dict wrapper.
    """
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
    """
    Build a nested lookup from a flat list of wiring entries.

    Structure:
      { source_activity: { target_activity: { field: {"pct": N, "authoritative": bool} } } }

    First-write-wins: authoritative entries are prepended in wiring_map.json.
    """
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
    """
    Load dependency_graph.json once and cache.
    Returns dict: { consumer_type: { required_inputs: [...] } }
    """
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
    """
    Walks the manifest in order and checks whether each activity's required
    table and session inputs are satisfied by a preceding activity.

    Returns list of warning dicts:
      { step_id, activity, field, dependency_type, expected_providers, note }

    Only flags:
    - dep_type "table" or "session" (string deps are too noisy to flag)
    - Where none of the expected providers appear earlier in the manifest
    - Where the activity appears in the dependency graph
    """
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
# Stage 2 — Pattern match  (replaces PatternMatcherAgent)
# ---------------------------------------------------------------------------

def run_pattern_match(decomposition: dict) -> dict:
    """
    Replaces PatternMatcherAgent.
    match_pattern takes only decomposition (loads library internally).
    decomposition passed to score_pattern_match so fallback_examples is set correctly.
    """
    decomposition = _ensure_dict(decomposition)
    candidates    = match_pattern(decomposition)
    result        = score_pattern_match(candidates, decomposition)
    return result


# ---------------------------------------------------------------------------
# Stage 3 — Retrieve activities  (replaces ActivityRetrieverAgent)
# ---------------------------------------------------------------------------

def run_retrieval(decomposition: dict) -> list:
    """
    Replaces ActivityRetrieverAgent.

    Three enrichments applied after manifest assembly:

    1. Confidence + candidates passthrough (retrieval_tools): entries with
       status="UNCERTAIN" include confidence score and top-3 candidates so
       StructureBuilder can exercise judgment on low-confidence selections.

    2. Wiring hints (wiring_map.json): corpus-derived hints attached as
       _wire_hint_<field> keys in pre_filled_fields.

    3. Dependency warnings (dependency_graph.json): checks manifest order
       for missing table/session prerequisites.
    """
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

    # Enrichment 1: wiring hints from wiring_map
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

    # Enrichment 2: dependency warnings
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
# Stage 4b — Enrich  (Python, deterministic — between Placer and Wirer)
# ---------------------------------------------------------------------------

def run_enrichment(placed_skeleton: dict, activity_manifest: list) -> dict:
    """
    For every activity in the placed skeleton:
      1. Load its full template from activity_json_syntax.json
      2. Merge template fields as base (non-destructive — xName/CustomTypeName win)
      3. Apply any pre_filled_fields from the manifest entry (authoritative)
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

        ct = node.get("CustomTypeName", "")
        xname = node.get("xName", "")

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
# Stage 4c — Apply structural fragments  (between run_enrichment and WirerAgent)
# ---------------------------------------------------------------------------

# Status producers: activities whose output is a boolean/status value.
# For IfElse checking a status producer, ALL branches get IsValid=True,
# UseStoredValue=True. Both branches are explicit predefined-value branches
# (Success/Failure). WirerAgent fills Value. No catch-all else.
_STATUS_PRODUCERS: frozenset = frozenset({
    "Ping", "ServiceStatus", "FileExist", "FTPFileExists",
    "ADUserExists", "PowerShellScript", "PowerShell",
    "ADIsAccountDisabled", "ADIsAccountLocked",
})

# Scalar producers: activities whose output is a string or number.
# Condition branches get UserDefinedValue. Catch-all else gets IsValid=False.
_SCALAR_PRODUCERS: frozenset = frozenset({
    "GetRowsCount", "DateDifference", "FunctionCalculator",
    "GetCellValue", "GetCellValueAdvanced", "GetDate", "Contains",
    "IsEmpty", "InStr", "InStrRev", "Len", "ConvertPasswordToPlaintext",
    "GetLength", "SubStringByText", "Trim", "Replace",
})

# Fields confirmed invalid on ReturnValue — cause malformed/uneditable activity
# in the designer. Confirmed by RitaLab testing March 2026.
_RETURNVALUE_FORBIDDEN_FIELDS: frozenset = frozenset({
    "visible", "disabled", "isFavorite", "isJsonValid",
    "readPermission", "writePermission", "modulePermissions",
    "activityLicenseType", "Timeout", "TimeInSeconds",
    "TargetModuleID", "TargetModuleName", "Path", "label", "notes",
})


def _clean_returnvalue(node: dict) -> None:
    """Remove fields the platform does not accept on ReturnValue. Mutates in place."""
    for field in _RETURNVALUE_FORBIDDEN_FIELDS:
        node.pop(field, None)


def _apply_returnvalue_fragment(node: dict, preceding_ct: str | None) -> None:
    """
    Apply F6 (defaults) then F7 or F8 based on preceding activity type.
    Also strips forbidden fields confirmed from RitaLab testing.
    Mutates node in place.

    Status producers (F7): ALL branches IsValid=True, UseStoredValue=True.
      Both branches are explicit predefined-value branches. WirerAgent fills Value.

    Scalar producers (F8): condition branches UserDefinedValue/IsValid=True,
      catch-all else IsValid=False/Formula={x:Null}.

    No producer context (F6): condition branches StoredValue/IsValid=True,
      catch-all else IsValid=False/Formula={x:Null}.
    """
    _clean_returnvalue(node)

    # F6 — baseline defaults
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
        # F7: all branches explicit, IsValid=True, UseStoredValue=True
        node["Type"] = "StoredValue"
        node["UseStoredValue"] = "True"
        node["IsValid"] = "True"
        node.setdefault("Formula", "")

    elif preceding_ct in _SCALAR_PRODUCERS:
        # F8: condition branches UserDefinedValue; else catch-all
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
        # F6 only
        if has_condition:
            node.setdefault("IsValid", "True")
            node.setdefault("Formula", "")
        else:
            node["IsValid"] = "False"
            node["Formula"] = "{x:Null}"
            node.pop("UseStoredValue", None)


def _walk_branch_body(activities: dict, preceding_ct: str | None,
                      parent_while_xname: str | None,
                      nearest_getrowscount_xname: str | None) -> None:
    """Walk children of an IfElseBranchActivity. Applies F4, F5, F6/F7/F8, recurses."""
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
    """Walk an IfElseActivity's branches. preceding_ct determines ReturnValue tier."""
    for k, branch in ifelse_node.items():
        if not isinstance(branch, dict):
            continue
        if branch.get("CustomTypeName") != "IfElseBranchActivity":
            continue
        _walk_branch_body(branch, preceding_ct, parent_while_xname, nearest_getrowscount_xname)


def _walk_while(while_node: dict, while_xname: str,
                nearest_getrowscount_xname: str | None) -> None:
    """Walk a WhileActivity's SequenceActivity body. Applies F2, F3, F4, F5, recurses."""
    seq_xname: str | None = None
    seq_node: dict | None = None
    for k, v in while_node.items():
        if isinstance(v, dict) and v.get("CustomTypeName") == "SequenceActivity":
            seq_xname = k
            seq_node = v
            break

    if seq_node is None:
        return

    last_ct: str | None = None
    local_getrowscount = nearest_getrowscount_xname

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
    """Walk a top-level SequenceActivity (e.g. inside ParallelActivity)."""
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
    """
    Walk top-level workflow_raw_data entries.

    ReturnValue at the top level is invalid — only valid inside
    IfElseBranchActivity. Top-level ReturnValue nodes are removed here.
    The validator also flags them as errors.
    """
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
    Apply all structural invariants (F1-F8) to the workflow.

    Runs AFTER run_enrichment(), BEFORE WirerAgent.
    Deep-copies input and returns the modified copy.

    Fragment summary:
      F1  WhileActivity.Condition = "{x:Null}"
      F2  ExitWhile: exitWhileInsideWhile, isValid, TypeName, whileSequenceActivity
      F3  ExitWhile.Counter = %{nearest GetRowsCount xName}%
      F4  GetCellValue.RowNumber = %{parent WhileActivity xName}%, ColumnType="Name"
      F5  MemorySet: VariableScope="Workflow", IsSaved="False", IsAppend="False"
      F6  ReturnValue defaults + forbidden field removal
      F7  ReturnValue status tier — all branches IsValid=True, UseStoredValue=True
      F8  ReturnValue scalar tier — condition branches UserDefinedValue,
          catch-all else IsValid=False/Formula={x:Null}

    Platform rules enforced (confirmed RitaLab March 2026):
      - ReturnValue only valid inside IfElseBranchActivity — top-level removed
      - ReturnValue forbidden fields stripped (visible, disabled, isFavorite, etc.)
      - Else branch: IsValid=False, Formula={x:Null}, no UseStoredValue
      - Status producer branches: all IsValid=True, UseStoredValue=True
    """
    import copy
    result = copy.deepcopy(workflow_json)
    raw = result.get("workflow_raw_data", result)

    if not isinstance(raw, dict):
        print("[fragments] Warning: workflow_raw_data is not a dict — skipping")
        return result

    _walk_top_level(raw)
    print(f"[fragments] Applied F1-F8 to {len(raw)} top-level activities")
    return result


def run_fragments(enriched_workflow: dict) -> dict:
    """
    Stage 4c (deterministic): applies structural fragment rules F1-F8.
    Slots between run_enrichment() and WirerAgent.
    Overwrites enriched_workflow in session state so WirerAgent reads the
    fragment-enforced version without any instruction changes required.
    """
    enriched_workflow = _ensure_dict(enriched_workflow)
    return apply_fragments(enriched_workflow)


# ---------------------------------------------------------------------------
# Stage 5 — Annotate  (replaces AnnotationAgent)
# ---------------------------------------------------------------------------

def run_annotation(workflow_json: dict, activity_manifest: Any) -> dict:
    """Replaces AnnotationAgent — direct 4-function chain."""
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
# Stage 6 — Validate  (replaces ValidationAgent)
# ---------------------------------------------------------------------------

def run_validation(annotation_result: dict) -> dict:
    """
    Replaces ValidationAgent — single run_all_validators() call.

    verify_notes derived from placeholder_summary (kind == "verify"),
    not from val_result which does not carry this key.
    """
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