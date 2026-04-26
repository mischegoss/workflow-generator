"""
tools/pipeline_stages.py

Deterministic Python replacements for five LLM agents:
  PatternMatcherAgent    → run_pattern_match()
  ActivityRetrieverAgent → run_retrieval()
  PlacerAgent            → run_skeleton_builder()   ← Phase 3: replaces LLM
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
# Stage 4a — Skeleton builder (Phase 3: deterministic replacement for PlacerAgent)
# ---------------------------------------------------------------------------

def run_skeleton_builder(decomposition: dict, activity_manifest: list) -> dict:
    """
    Phase 3: deterministic replacement for PlacerAgent.

    Builds placed_skeleton from decomposition zone tags and activity_manifest.
    No LLM involved — pure Python structure assembly.

    xName generation: lowercase(first char) + rest + counter per type
    e.g. getCellValue1, getCellValue2, ping1, whileActivity1

    Zone assignment from decomposition.steps[].zone:
      linear         → top-level sequence (no container)
      pre_container  → top-level, before the container
      container      → IS the WhileActivity or IfElseActivity (skip — injected
                        from template; these steps are not placed individually)
      container_body → inside SequenceActivity (while) or IfElseBranchActivity (ifelse)
      post_container → top-level, after the container

    Control flow detection:
      loop_type in variable_contract  → While
      branch steps present            → IfElse
      both                            → while_ifelse
      usergroup steps present         → UserGroup
      else                            → linear
    """
    decomposition = _ensure_dict(decomposition)
    steps = decomposition.get("steps", [])
    variable_contract = decomposition.get("variable_contract",
                        decomposition.get("variable_contracts", {}))

    # ── Detect control flow type ──────────────────────────────────────────────
    loop_type = variable_contract.get("loop_type", "none")
    has_loop = loop_type.lower() in ("while", "foreach")
    has_branch = any(
        s.get("control_flow") in ("ifelse",) or s.get("intent") == "branch"
        for s in steps
    )
    has_usergroup = any(s.get("control_flow") == "usergroup" for s in steps)

    if has_loop and has_branch:
        cf_type = "while_ifelse"
    elif has_loop:
        cf_type = "While"
    elif has_usergroup:
        cf_type = "UserGroup"
    elif has_branch:
        cf_type = "IfElse"
    else:
        cf_type = "linear"

    # ── Build manifest lookup: step_id → selected_activity ───────────────────
    manifest_by_step: dict = {
        entry["step_id"]: entry.get("selected_activity", "")
        for entry in activity_manifest
        if entry.get("step_id")
    }

    # ── xName counter ─────────────────────────────────────────────────────────
    xname_counters: dict = {}

    def make_xname(ct: str) -> str:
        base = ct[0].lower() + ct[1:]
        xname_counters[base] = xname_counters.get(base, 0) + 1
        return f"{base}{xname_counters[base]}"

    # ── Zone ordering ─────────────────────────────────────────────────────────
    ZONE_ORDER = {
        "linear":         0,
        "pre_container":  0,
        "container":      1,
        "container_body": 2,
        "post_container": 3,
    }
    steps_sorted = sorted(
        steps,
        key=lambda s: ZONE_ORDER.get(s.get("zone", "linear"), 0)
    )

    # ── Separate steps by zone ────────────────────────────────────────────────
    # "container" steps are skipped — the container itself is injected from the
    # template (WhileActivity, IfElseActivity etc.) not placed per-step.
    if cf_type == "linear":
        pre_steps   = []
        body_steps  = [s for s in steps_sorted
                       if s.get("zone", "linear") != "container"]
        post_steps  = []
    else:
        pre_steps   = [s for s in steps_sorted
                       if s.get("zone", "linear") == "pre_container"]
        body_steps  = [s for s in steps_sorted
                       if s.get("zone", "linear") == "container_body"]
        post_steps  = [s for s in steps_sorted
                       if s.get("zone", "linear") == "post_container"]
        # Fall back: if Decomposer didn't emit zone tags, treat everything as body
        if not body_steps and not pre_steps and not post_steps:
            body_steps = [s for s in steps_sorted
                          if s.get("zone", "linear") != "container"]

    # Activity types that are structural containers — they are always injected
    # by the skeleton template, never placed by make_node from manifest steps.
    # If retrieval returns one of these for a content step (e.g. IfElseActivity
    # for a display step when Decomposer emits control_flow=ifelse context),
    # skip it rather than creating a ghost container activity in the wrong place.
    _CONTAINER_TYPES_SKIP = frozenset({
        "WhileActivity", "SequenceActivity", "IfElseActivity",
        "IfElseBranchActivity", "ExitWhile", "ReturnValue",
        "ForEachActivity", "ParallelActivity", "UserGroup",
    })

    # ── make_node: convert one manifest step to (xname, node) ────────────────
    def make_node(step: dict) -> tuple[str, dict] | None:
        step_id = step.get("step_id", "")
        ct = manifest_by_step.get(step_id, "")
        if not ct or ct.upper() == "UNAVAILABLE":
            return None
        ct = _normalise_ct(ct)
        if not ct:
            return None
        # Never place structural container types as content activities.
        # These are always injected by the skeleton template.
        if ct in _CONTAINER_TYPES_SKIP:
            print(f"[skeleton_builder] skipping structural type '{ct}' "
                  f"for step '{step_id}' — template-injected only")
            return None
        xname = make_xname(ct)
        node: dict = {
            "xName":          xname,
            "CustomTypeName": ct,
        }
        return xname, node

    # ── Assemble skeleton ─────────────────────────────────────────────────────
    raw: dict = {}

    # Place pre-container activities
    for step in pre_steps:
        result = make_node(step)
        if result:
            xname, node = result
            raw[xname] = node

    if cf_type == "linear":
        for step in body_steps:
            result = make_node(step)
            if result:
                xname, node = result
                raw[xname] = node

    elif cf_type == "While":
        while_xname = make_xname("WhileActivity")
        seq_xname   = make_xname("SequenceActivity")
        exit_xname  = make_xname("ExitWhile")

        # ExitWhile is always the first child of SequenceActivity.
        # Exclude structural intents — exit_loop is template-injected as ExitWhile
        # above; placing it again from the manifest creates a duplicate.
        _STRUCTURAL_INTENTS = frozenset({"exit_loop", "loop", "branch"})
        seq_body: dict = {
            exit_xname: {
                "xName":          exit_xname,
                "CustomTypeName": "ExitWhile",
            },
        }
        for step in body_steps:
            if step.get("intent") in _STRUCTURAL_INTENTS:
                continue
            result = make_node(step)
            if result:
                xname, node = result
                seq_body[xname] = node

        seq_node: dict = {
            "xName":          seq_xname,
            "CustomTypeName": "SequenceActivity",
            **seq_body,
        }
        while_node: dict = {
            "xName":          while_xname,
            "CustomTypeName": "WhileActivity",
            "Condition":      "{x:Null}",
            seq_xname:        seq_node,
        }
        raw[while_xname] = while_node

    elif cf_type == "while_ifelse":
        while_xname  = make_xname("WhileActivity")
        seq_xname    = make_xname("SequenceActivity")
        exit_xname   = make_xname("ExitWhile")
        ifelse_xname = make_xname("IfElseActivity")
        br1_xname    = make_xname("IfElseBranchActivity")
        br2_xname    = make_xname("IfElseBranchActivity")

        m1 = _re.search(r'(\d+)$', br1_xname)
        m2 = _re.search(r'(\d+)$', br2_xname)
        rv1_xname = f"returnValue{m1.group(1) if m1 else '1'}"
        rv2_xname = f"returnValue{m2.group(1) if m2 else '2'}"

        # Split body_steps into while-sequence steps and ifelse-branch steps.
        #
        # Primary split: use the intent='branch' step as a structural divider.
        # The Decomposer emits one step with intent='branch' to mark the IfElse
        # activity itself. Everything before it in body_steps belongs in the
        # SequenceActivity (before the IfElse); everything after it belongs in
        # the IfElse branches. This is more reliable than control_flow tags
        # because the Decomposer frequently emits 'while' or 'linear' for display
        # steps that logically belong inside the conditional branches.
        #
        # Fallback: if no branch step exists, use control_flow tag.
        _STRUCTURAL_INTENTS = frozenset({"exit_loop", "loop", "branch"})

        branch_step_idx = next(
            (i for i, s in enumerate(body_steps)
             if s.get("intent") == "branch"),
            None
        )

        if branch_step_idx is not None:
            # Steps before the branch marker → while sequence body
            while_seq_steps = [
                s for s in body_steps[:branch_step_idx]
                if s.get("intent") not in _STRUCTURAL_INTENTS
            ]
            # Steps after the branch marker → IfElse branch activities.
            # Use step position to assign branches: first half → branch1,
            # second half → branch2. Works for 2-branch if/else patterns.
            after_branch = [
                s for s in body_steps[branch_step_idx + 1:]
                if s.get("intent") not in _STRUCTURAL_INTENTS
            ]
            mid = max(1, len(after_branch) // 2)
            ifelse_branch_steps = []
            for i, s in enumerate(after_branch):
                s = dict(s)
                # Assign to branch 2 if in the second half and no explicit hint
                if "branch" not in s:
                    s["branch"] = 2 if i >= mid else 1
                ifelse_branch_steps.append(s)
        else:
            # Fallback: use intent to assign steps rather than control_flow alone.
            # control_flow tags from the Decomposer are unreliable — action steps
            # like Ping often get tagged control_flow='ifelse' because they're in
            # a while_ifelse workflow, even though they execute unconditionally
            # before the branch. Intent is a better signal:
            # - display/send_email/set_variable → branch content (shown conditionally)
            # - all other action intents → while-sequence (unconditional)
            _BRANCH_CONTENT_INTENTS = frozenset({
                "display", "send_email", "set_variable", "write", "update",
                "log", "notify", "store",
            })
            # Intent is the authoritative signal. control_flow tags from the
            # Decomposer are unreliable — action steps like Ping often get
            # tagged control_flow='ifelse' when inside a while_ifelse workflow.
            # Rule: a step belongs in the IfElse branches ONLY if its intent
            # signals conditional output (display, send_email, etc.).
            # All other action steps (intent='other', 'get_cell', etc.) belong
            # in the while-sequence and execute unconditionally.
            while_seq_steps = [
                s for s in body_steps
                if s.get("intent") not in _STRUCTURAL_INTENTS
                and s.get("intent") not in _BRANCH_CONTENT_INTENTS
            ]
            ifelse_branch_steps = [
                s for s in body_steps
                if s.get("intent") not in _STRUCTURAL_INTENTS
                and s.get("intent") in _BRANCH_CONTENT_INTENTS
            ]

        branch1: dict = {
            "xName":          br1_xname,
            "CustomTypeName": "IfElseBranchActivity",
            rv1_xname: {"xName": rv1_xname, "CustomTypeName": "ReturnValue"},
        }
        branch2: dict = {
            "xName":          br2_xname,
            "CustomTypeName": "IfElseBranchActivity",
            rv2_xname: {"xName": rv2_xname, "CustomTypeName": "ReturnValue"},
        }
        for step in ifelse_branch_steps:
            result = make_node(step)
            if result:
                xname, node = result
                branch_hint = step.get("branch", 0)
                if branch_hint == 2:
                    branch2[xname] = node
                else:
                    branch1[xname] = node

        ifelse_node: dict = {
            "xName":          ifelse_xname,
            "CustomTypeName": "IfElseActivity",
            br1_xname:        branch1,
            br2_xname:        branch2,
        }

        # ExitWhile first, then while-body activities, then IfElse last
        seq_body_wi: dict = {
            exit_xname: {
                "xName":          exit_xname,
                "CustomTypeName": "ExitWhile",
            },
        }
        for step in while_seq_steps:
            result = make_node(step)
            if result:
                xname, node = result
                seq_body_wi[xname] = node
        seq_body_wi[ifelse_xname] = ifelse_node
        seq_node_wi: dict = {
            "xName":          seq_xname,
            "CustomTypeName": "SequenceActivity",
            **seq_body_wi,
        }
        while_node_wi: dict = {
            "xName":          while_xname,
            "CustomTypeName": "WhileActivity",
            "Condition":      "{x:Null}",
            seq_xname:        seq_node_wi,
        }
        raw[while_xname] = while_node_wi

    elif cf_type == "IfElse":
        ifelse_xname = make_xname("IfElseActivity")
        br1_xname    = make_xname("IfElseBranchActivity")
        br2_xname    = make_xname("IfElseBranchActivity")

        m1 = _re.search(r'(\d+)$', br1_xname)
        m2 = _re.search(r'(\d+)$', br2_xname)
        rv1_xname = f"returnValue{m1.group(1) if m1 else '1'}"
        rv2_xname = f"returnValue{m2.group(1) if m2 else '2'}"

        branch1: dict = {
            "xName":          br1_xname,
            "CustomTypeName": "IfElseBranchActivity",
            rv1_xname: {"xName": rv1_xname, "CustomTypeName": "ReturnValue"},
        }
        branch2: dict = {
            "xName":          br2_xname,
            "CustomTypeName": "IfElseBranchActivity",
            rv2_xname: {"xName": rv2_xname, "CustomTypeName": "ReturnValue"},
        }
        _STRUCTURAL_INTENTS = frozenset({"exit_loop", "loop", "branch"})
        for step in body_steps:
            if step.get("intent") in _STRUCTURAL_INTENTS:
                continue
            result = make_node(step)
            if result:
                xname, node = result
                branch_hint = step.get("branch", 0)
                if branch_hint == 2:
                    branch2[xname] = node
                else:
                    branch1[xname] = node

        ifelse_node: dict = {
            "xName":          ifelse_xname,
            "CustomTypeName": "IfElseActivity",
            br1_xname:        branch1,
            br2_xname:        branch2,
        }
        raw[ifelse_xname] = ifelse_node

    elif cf_type == "UserGroup":
        ug_xname = make_xname("UserGroup")
        ug_body: dict = {}
        for step in body_steps:
            result = make_node(step)
            if result:
                xname, node = result
                ug_body[xname] = node
        ug_node: dict = {
            "xName":          ug_xname,
            "CustomTypeName": "UserGroup",
            **ug_body,
        }
        raw[ug_xname] = ug_node

    # Place post-container activities
    for step in post_steps:
        result = make_node(step)
        if result:
            xname, node = result
            raw[xname] = node

    print(f"[skeleton_builder] Built {len(raw)} top-level activities "
          f"(cf_type={cf_type})")
    return {
        "workflow_raw_data":  raw,
        "variable_contracts": variable_contract,
    }


# ---------------------------------------------------------------------------
# Wirer output normalizer — Children-list → xName-keyed dict
# ---------------------------------------------------------------------------

def _convert_children_format(node: dict) -> dict:
    """
    Recursively convert Wirer's Children-list format to the pipeline's
    xName-keyed dict format.

    Wirer sometimes returns:
        {"xName": "whileActivity1", "CustomTypeName": "WhileActivity",
         "Children": [{"xName": "getCellValue1", ...}, ...]}

    Pipeline expects:
        {"xName": "whileActivity1", "CustomTypeName": "WhileActivity",
         "sequenceActivity1": {"xName": "sequenceActivity1",
                               "CustomTypeName": "SequenceActivity",
                               "exitWhile1": {...}, "getCellValue1": {...}}}

    Key conversions:
      WhileActivity.Children  → inject SequenceActivity wrapper, ExitWhile first
      IfElseActivity.Children → branches keyed by xName, CT normalised
      IfElseBranch.Children   → activities keyed by xName + ReturnValue hoisted
      Other.Children          → children keyed by xName (flat)
    """
    ct = node.get("CustomTypeName", "")
    # Normalise IfElseBranch → IfElseBranchActivity
    if ct == "IfElseBranch":
        ct = "IfElseBranchActivity"

    # Build result without Children/ReturnValue — those are restructured below
    result = {k: v for k, v in node.items()
              if k not in ("Children", "ReturnValue")}
    result["CustomTypeName"] = ct

    children = node.get("Children") or []
    if not isinstance(children, list):
        children = []

    if ct == "WhileActivity":
        # Wrap children in a SequenceActivity, ExitWhile first
        xname = node.get("xName", "whileActivity1")
        m = _re.search(r'(\d+)$', xname)
        n = m.group(1) if m else "1"
        seq_xname = f"sequenceActivity{n}"

        exit_nodes  = [c for c in children if c.get("CustomTypeName") == "ExitWhile"]
        other_nodes = [c for c in children if c.get("CustomTypeName") != "ExitWhile"]
        ordered = exit_nodes + other_nodes

        seq_dict: dict = {"xName": seq_xname, "CustomTypeName": "SequenceActivity"}
        for child in ordered:
            child_xname = child.get("xName", "")
            if child_xname:
                seq_dict[child_xname] = _convert_children_format(child)
        result[seq_xname] = seq_dict

    elif ct == "IfElseActivity":
        for child in children:
            child_ct = child.get("CustomTypeName", "")
            if child_ct in ("IfElseBranch", "IfElseBranchActivity"):
                child_xname = child.get("xName", "")
                if child_xname:
                    result[child_xname] = _convert_children_format(child)

    elif ct == "IfElseBranchActivity":
        # Hoist ReturnValue dict → keyed child node
        rv = node.get("ReturnValue")
        if rv and isinstance(rv, dict):
            branch_xname = node.get("xName", "branch1")
            m = _re.search(r'(\d+)$', branch_xname)
            n = m.group(1) if m else "1"
            rv_xname = f"returnValue{n}"
            rv_node = dict(rv)
            rv_node["xName"] = rv_xname
            rv_node["CustomTypeName"] = "ReturnValue"
            result[rv_xname] = rv_node
        # Add child activities
        for child in children:
            child_xname = child.get("xName", "")
            if child_xname:
                result[child_xname] = _convert_children_format(child)

    else:
        # Generic container — just key children by xName
        for child in children:
            child_xname = child.get("xName", "")
            if child_xname:
                result[child_xname] = _convert_children_format(child)

    return result


def normalize_wirer_output(workflow_json: dict) -> dict:
    """
    Normalize WirerAgent output from Children-list format to xName-keyed dict
    format expected by enrichment, fragments, scaffold, and xml_composer.

    Detects the Children format by checking whether workflow_raw_data has a
    'Children' key with a list value. If so, converts it. If workflow_raw_data
    is already in the correct format (dict-of-dicts keyed by xName), returns
    unchanged.

    Also strips any workflow-level metadata fields (xName, Description,
    CustomTypeName, Version, etc.) that Wirer placed as siblings of activity
    dicts inside workflow_raw_data.
    """
    import copy
    workflow_json = _ensure_dict(workflow_json)
    raw = workflow_json.get("workflow_raw_data", {})
    if not isinstance(raw, dict):
        return workflow_json

    result = dict(workflow_json)

    # ── Case 1: Children list at top level of workflow_raw_data ──────────────
    top_children = raw.get("Children")
    if isinstance(top_children, list) and top_children:
        print(f"[normalize] Converting Children-list format — "
              f"{len(top_children)} top-level activities")
        new_raw: dict = {}
        for child in top_children:
            xname = child.get("xName", "")
            if xname:
                new_raw[xname] = _convert_children_format(child)
        result["workflow_raw_data"] = new_raw
        # Fall through to backfill+strip — do NOT return early here.

    # ── Case 2: metadata fields mixed in with activity dicts ─────────────────
    # Also runs after Case 1 to strip any leftover wrapper fields.
    _METADATA_KEYS = frozenset({
        "xName", "Description", "description", "CustomTypeName",
        "Version", "Category", "CreatedBy", "Name", "Pnumber",
        "ActivityID", "ActivityName", "DisplayName", "DateLic",
    })
    current_raw = result.get("workflow_raw_data", {})
    non_dict = [k for k, v in current_raw.items()
                if not isinstance(v, dict) and k in _METADATA_KEYS]
    if non_dict:
        print(f"[normalize] Stripping {len(non_dict)} metadata keys "
              f"from workflow_raw_data: {non_dict[:8]}")
        result["workflow_raw_data"] = {
            k: v for k, v in current_raw.items() if isinstance(v, dict)
        }

    # ── Backfill TableName on CreateMemoryTable from variable_contracts ───────
    # Always runs regardless of which format was detected above.
    # Also replaces template placeholder values ending in "_value".
    _backfill_table_vars(result)

    # ── Strip template metadata sub-dicts ────────────────────────────────────
    # Always runs — catches ActivityInfo/Output sub-dicts from enrichment
    # templates regardless of Wirer output format.
    _strip_metadata_dicts(result.get("workflow_raw_data", {}))

    return result


def _strip_metadata_dicts(nodes: dict) -> None:
    """
    Recursively strip ActivityInfo and Output metadata sub-dicts from all
    activity nodes. These are enrichment template artifacts — the platform
    does not use them and they cause validator false positives.
    Mutates in place.
    """
    _META_KEYS = frozenset({"ActivityInfo", "Output"})
    for xname, node in list(nodes.items()):
        if not isinstance(node, dict):
            continue
        for key in _META_KEYS:
            node.pop(key, None)
        # Recurse into child activity dicts
        for k, v in node.items():
            if isinstance(v, dict) and v.get("CustomTypeName"):
                _strip_metadata_dicts({k: v})


def _backfill_table_vars(workflow_json: dict) -> None:
    """
    Backfill TableName on CreateMemoryTable nodes that are missing it,
    using variable_contracts.variables[type=table] as the source.
    Mutates workflow_json in place.
    """
    vc = workflow_json.get("variable_contracts", {})
    if isinstance(vc, str):
        try:
            import json as _j; vc = _j.loads(vc)
        except Exception:
            vc = {}
    table_vars = [
        v.get("name", "").strip()
        for v in vc.get("variables", [])
        if v.get("type") in ("table", "MemoryTable") and v.get("name", "").strip()
    ]
    if not table_vars:
        return

    raw = workflow_json.get("workflow_raw_data", {})
    if not isinstance(raw, dict):
        return

    def _walk_backfill(nodes: dict) -> None:
        for xname, node in nodes.items():
            if not isinstance(node, dict):
                continue
            ct = node.get("CustomTypeName", "")
            tname_cur = node.get("TableName", "").strip()
            if ct == "CreateMemoryTable" and (not tname_cur or tname_cur.endswith("_value")):
                node["TableName"] = table_vars[0]
                print(f"[normalize] Backfilled TableName='{table_vars[0]}' "
                      f"on '{xname}' from variable_contracts")
            # Recurse into nested containers
            for k, v in node.items():
                if isinstance(v, dict) and v.get("CustomTypeName"):
                    _walk_backfill({k: v})

    _walk_backfill(raw)


def run_strip_metadata_dicts(workflow_json: dict) -> dict:
    """
    Public wrapper for _strip_metadata_dicts.
    Strips ActivityInfo and Output sub-dicts from all activity nodes.
    Called after _apply_wirer_patches in _run_post_wirer_stages to ensure
    template artifacts are removed regardless of Wirer output format.
    Returns a modified copy.
    """
    import copy
    result = copy.deepcopy(_ensure_dict(workflow_json))
    raw = result.get("workflow_raw_data", {})
    if isinstance(raw, dict):
        _strip_metadata_dicts(raw)
    return result


# ---------------------------------------------------------------------------
# Wirer patch applier
# ---------------------------------------------------------------------------

def _apply_wirer_patches(wirer_output: dict, enriched_workflow: dict) -> dict:
    """
    Apply WirerAgent patch output onto enriched_workflow.

    WirerAgent returns {"wirer_patches": {xName: {field: value, ...}, ...}}.
    This function deep-merges those patches onto the enriched_workflow skeleton,
    which already has the correct structure and all template defaults.

    Falls back to normalize_wirer_output() if Wirer returned a legacy
    full-workflow response (has "workflow_raw_data" key instead of
    "wirer_patches").

    Patch application rules:
    - Patches are applied recursively: if a patch key matches a nested
      activity xName, it patches that node regardless of depth.
    - Empty string values in patches overwrite existing values (Wirer
      intentionally clearing a field).
    - None values in patches are skipped (Wirer leaving field unchanged).
    - Structural fields (xName, CustomTypeName) in patches are ignored.
    """
    import copy

    wirer_output = _ensure_dict(wirer_output)

    # ── Legacy fallback: full workflow_raw_data response ─────────────────────
    if "workflow_raw_data" in wirer_output and "wirer_patches" not in wirer_output:
        print("[wirer_patches] Legacy full-workflow response detected — "
              "falling back to normalize_wirer_output")
        return normalize_wirer_output(wirer_output)

    patches = wirer_output.get("wirer_patches", {})
    if not isinstance(patches, dict) or not patches:
        print("[wirer_patches] No patches found in wirer_output — "
              "returning enriched_workflow unchanged")
        return copy.deepcopy(enriched_workflow) if enriched_workflow else wirer_output

    # Start from a deep copy of the enriched_workflow skeleton
    result = copy.deepcopy(enriched_workflow)

    # ── Flatten nested activity patches to top level ─────────────────────────
    # Wirer sometimes nests activity patches inside parent patches, e.g.:
    #   "ifElseBranchActivity1": {"displayValue1": {CustomTypeName: ...}, ...}
    # Extract those nested activity dicts to the top level so _find_and_patch
    # can locate the real skeleton nodes. Also removes hallucinated activities
    # (IfElseActivity2 etc.) that don't match any skeleton node — they are
    # simply ignored by _find_and_patch since no xName matches them.
    def _flatten_patches(raw_patches: dict) -> dict:
        flat: dict = {}
        for key, patch in raw_patches.items():
            if not isinstance(patch, dict):
                flat[key] = patch
                continue
            flat_patch: dict = {}
            for field, value in patch.items():
                if isinstance(value, dict) and value.get("CustomTypeName"):
                    # Nested activity — hoist to top level keyed by its xName
                    nested_xname = value.get("xName", field)
                    if nested_xname and nested_xname not in flat:
                        flat[nested_xname] = value
                else:
                    flat_patch[field] = value
            if flat_patch:
                flat[key] = flat_patch
        return flat

    patches = _flatten_patches(patches)

    _SKIP_PATCH_FIELDS = frozenset({"xName", "CustomTypeName"})

    def _apply_to_node(node: dict, patch: dict) -> None:
        """
        Merge patch fields onto node.
        Skips structural fields (xName, CustomTypeName), None values, and any
        value that is a dict — dict values are activity nodes, not field values,
        and should have been flattened to the top level by _flatten_patches.
        """
        for field, value in patch.items():
            if field in _SKIP_PATCH_FIELDS:
                continue
            if value is None:
                continue
            if isinstance(value, dict):
                # Dict values are either nested activities (already flattened)
                # or hallucinations — never set them as raw field values.
                continue
            node[field] = value

    def _find_and_patch(nodes: dict, patches_remaining: dict) -> None:
        """Recursively walk nodes, applying any matching patches by xName."""
        if not patches_remaining:
            return
        for key, node in nodes.items():
            if not isinstance(node, dict):
                continue
            xname = node.get("xName", "")
            if xname and xname in patches_remaining:
                _apply_to_node(node, patches_remaining[xname])
            # Recurse into nested activity nodes
            _find_and_patch(node, patches_remaining)

    raw = result.get("workflow_raw_data", {})
    if isinstance(raw, dict):
        _find_and_patch(raw, patches)

    n_patched = sum(1 for v in patches.values()
                    if isinstance(v, dict) and not v.get("CustomTypeName"))
    print(f"[wirer_patches] Applied patches for {n_patched} activit(ies)")
    return result


# ---------------------------------------------------------------------------
# Stage 4b — Enrich
# ---------------------------------------------------------------------------

def run_enrichment(placed_skeleton: dict, activity_manifest: list) -> dict:
    """
    Stage 4b: loads activity templates and applies manifest wire hints.

    D5: manifest lookup is now keyed by xName → pre_filled_fields rather than
    activity type → pre_filled_fields. Previously, two steps with the same
    activity type (e.g. two GetCellValue steps for different columns) caused
    the second step's wire hints to silently overwrite the first's.

    Strategy:
      1. Build xname_to_hints: map each xName in the placed skeleton to the
         pre_filled_fields of the manifest entry whose selected_activity matches
         that node's CustomTypeName, consuming each entry at most once in order.
      2. Fall back to type-level lookup for xNames not matched (structural
         containers, injected activities, ExitWhile, ReturnValue etc.).
    """
    from tools.build_tools import load_activity_template
    from tools.annotation_tools import _ensure_dict

    placed_skeleton = _ensure_dict(placed_skeleton)
    raw = placed_skeleton.get("workflow_raw_data", placed_skeleton)

    # ── D5: build xName-keyed lookup ─────────────────────────────────────────
    # Walk the placed skeleton to get all xNames in order, then pair them with
    # manifest entries by matching CustomTypeName to selected_activity in order.
    # Each manifest entry is consumed once — prevents collision when two steps
    # use the same activity type.
    def _collect_xnames_in_order(nodes: dict) -> list[tuple[str, str]]:
        """Returns [(xname, CustomTypeName), ...] in document order, all levels."""
        result = []
        for xname, node in nodes.items():
            if not isinstance(node, dict):
                continue
            ct = _normalise_ct(node.get("CustomTypeName", ""))
            if ct:
                result.append((xname, ct))
            # Recurse into nested activities
            for k, v in node.items():
                if isinstance(v, dict) and v.get("CustomTypeName"):
                    result.extend(_collect_xnames_in_order({k: v}))
        return result

    xname_order = _collect_xnames_in_order(raw)

    # Pair each (xname, ct) with a matching unconsumed manifest entry
    manifest_entries = list(activity_manifest) if isinstance(activity_manifest, list) else []
    consumed = [False] * len(manifest_entries)
    xname_to_hints: dict[str, dict] = {}
    # Also keep type-level fallback for unmatched nodes
    type_to_hints: dict[str, dict] = {}
    for entry in manifest_entries:
        act = entry.get("selected_activity", "")
        if act and act not in type_to_hints:
            type_to_hints[act] = entry.get("pre_filled_fields", {})

    for xname, ct in xname_order:
        # Find first unconsumed manifest entry with matching activity type
        for i, entry in enumerate(manifest_entries):
            if consumed[i]:
                continue
            if entry.get("selected_activity") == ct:
                xname_to_hints[xname] = entry.get("pre_filled_fields", {})
                consumed[i] = True
                break

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
                # Merge template fields onto node, but exclude template-level
                # nested activity dicts (keys whose value has a CustomTypeName)
                # that are NOT present in the actual node. These are template
                # scaffold children that bleed through and create ghost activities
                # (e.g. IfElseActivity2/3 in IfElseBranchActivity templates).
                # Only scalar/string template fields should survive; nested
                # activity placement belongs to the skeleton builder.
                template_scalar = {
                    k: v for k, v in template.items()
                    if not (isinstance(v, dict) and v.get("CustomTypeName")
                            and k not in node)
                }
                merged = {**template_scalar, **{
                    k: v for k, v in node.items()
                    if v is not None and v != ""
                }}
                merged["xName"] = xname or merged.get("xName", "")
                # D5: use xName-keyed hints first, fall back to type-level
                hints = xname_to_hints.get(xname) or type_to_hints.get(ct, {})
                for field, value in hints.items():
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
# Nested sub-object normalization (Advanced, Result)
# ---------------------------------------------------------------------------
#
# Activity templates load nested Advanced and Result sub-objects with
# placeholder xNames (typically xName=CustomTypeName, e.g. xName="Advanced")
# and unset Description/description fields. Validation rejects workflows
# where any of these are duplicated or missing description text.
#
# This module turns that mechanical work into a deterministic stage that
# replaces the previous Wirer-prompt band-aid. Runs as part of
# apply_fragments() so it fires both before WirerAgent (Stage 4c) and after
# (Stage 4f) — the post-Wirer pass repairs anything Wirer regressed.
#
# Idempotent: a second invocation leaves correctly-formatted sub-objects
# unchanged. Counters increment across the WHOLE workflow, not per parent.
#
# ReturnValue sub-objects are deliberately NOT included here. They are
# already handled by F6-F9 in _walk_top_level / _walk_branch_body / etc.,
# which set defaults including empty Description strings via
# _apply_returnvalue_fragment.
# ---------------------------------------------------------------------------

# CustomTypeNames whose nested instances need xName + Description fix.
_NESTED_NORMALIZED_TYPES: frozenset = frozenset({"Advanced", "Result"})

# Description templates per nested type, keyed off parent activity type.
# Walked at runtime — see _description_template_for().
_NESTED_DESCRIPTION_TEMPLATES: dict[str, str] = {
    "Advanced": "Advanced settings for {parent}.",
    "Result":   "Result of {parent}.",
}


def _description_template_for(nested_ct: str, parent_ct: str) -> str:
    """Return the Description text for a nested sub-object given its
    CustomTypeName and the parent activity's CustomTypeName."""
    template = _NESTED_DESCRIPTION_TEMPLATES.get(
        nested_ct, "{nested} for {parent}."
    )
    return template.format(nested=nested_ct, parent=parent_ct or "Activity")


def _xname_already_correct(xname: str, custom_type: str) -> bool:
    """
    Returns True if xname matches the deterministic pattern derived from
    custom_type — lowercased first character + rest + digits.
      ('advanced1', 'Advanced') → True
      ('advanced42', 'Advanced') → True
      ('Advanced', 'Advanced')  → False  (capital A)
      ('advanced', 'Advanced')  → False  (no counter)
      ('advancedX', 'Advanced') → False  (non-digit suffix)
    """
    if not xname or not custom_type:
        return False
    expected_prefix = custom_type[0].lower() + custom_type[1:]
    if not xname.startswith(expected_prefix):
        return False
    suffix = xname[len(expected_prefix):]
    return suffix.isdigit() and len(suffix) > 0


def _normalize_nested_subobjects(raw: dict) -> int:
    """
    Walk the workflow tree in document order. For every nested Advanced or
    Result sub-object encountered:

      1. Assign a numbered xName (advanced1, advanced2, ..., result1, ...)
         if the current xName is missing, equal to the CustomTypeName, or
         otherwise non-conformant. Counters increment per CustomTypeName
         across the WHOLE workflow.
      2. Set Description and description (both spellings) using a generic
         template keyed off the parent activity's CustomTypeName, e.g.
         "Advanced settings for GetRowsCount." Existing non-empty values
         that aren't placeholders are preserved.

    Mutates raw in place. Returns the count of sub-objects normalized.

    Idempotent — a second call leaves correctly-formatted sub-objects
    unchanged. Counters max-track existing correct numbers so subsequent
    assignments don't collide.

    ReturnValue sub-objects are NOT touched — F6-F9 handle them.
    """
    counters: dict[str, int] = {}     # CustomTypeName -> next counter value
    normalized_count = 0

    def _walk(nodes: dict, parent_ct: str | None) -> None:
        nonlocal normalized_count
        for key, node in nodes.items():
            if not isinstance(node, dict):
                continue
            ct = node.get("CustomTypeName", "")
            if not ct:
                continue

            # Only normalize Advanced/Result — and only when we know the
            # parent activity's CT (template derivation needs it)
            if ct in _NESTED_NORMALIZED_TYPES and parent_ct:
                current_xname = node.get("xName", "")

                if _xname_already_correct(current_xname, ct):
                    # Preserve. Update counter so future assignments don't
                    # collide with this one.
                    suffix = current_xname[len(ct[0].lower() + ct[1:]):]
                    try:
                        n = int(suffix)
                        if n > counters.get(ct, 0):
                            counters[ct] = n
                    except ValueError:
                        pass
                else:
                    # Assign a fresh numbered xName.
                    counters[ct] = counters.get(ct, 0) + 1
                    new_xname = f"{ct[0].lower()}{ct[1:]}{counters[ct]}"
                    node["xName"] = new_xname
                    normalized_count += 1

                # Populate Description / description if missing or placeholder.
                template = _description_template_for(ct, parent_ct)

                d_upper = node.get("Description", "")
                if not d_upper or (isinstance(d_upper, str)
                                   and d_upper.endswith("_value")):
                    node["Description"] = template

                d_lower = node.get("description", "")
                if not d_lower or (isinstance(d_lower, str)
                                   and d_lower.endswith("_value")):
                    node["description"] = template

            # Recurse into all CT-bearing children regardless of whether
            # the current node was normalized. The recursion uses THIS
            # node's CT as the parent for its descendants.
            for child_key, child_val in node.items():
                if (isinstance(child_val, dict)
                        and child_val.get("CustomTypeName")):
                    _walk({child_key: child_val}, parent_ct=ct)

    _walk(raw, parent_ct=None)
    return normalized_count


def normalize_nested_subobjects(workflow_json: dict) -> dict:
    """
    Public stage entry point. Deep-copies, normalizes nested sub-objects,
    returns the new workflow. Use this when calling as a standalone stage;
    use _normalize_nested_subobjects when mutating in place inside another
    stage (e.g. apply_fragments).
    """
    import copy
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)
    raw = result.get("workflow_raw_data", result)
    if isinstance(raw, dict):
        n = _normalize_nested_subobjects(raw)
        if n:
            print(f"[nested] Normalized {n} nested sub-object(s) "
                  f"(Advanced/Result xNames + Descriptions)")
    return result


# ---------------------------------------------------------------------------
# Stage 4c / 4f — Apply structural fragments (F1-F9 + nested normalization)
# ---------------------------------------------------------------------------
#
# Called twice in the pipeline:
#   Stage 4c — before WirerAgent: enforces invariants on skeleton
#   Stage 4f — after WirerAgent:  enforces invariants on activities Wirer
#               reconstructed that were absent from the skeleton
#
# apply_fragments() is idempotent — safe to call twice.
#
# F1   WhileActivity.Condition = "{x:Null}"
#      Strip spurious comparison fields Wirer adds (LeftOperand, Operator,
#      RightOperand, SuccessReason) — platform rejects these on WhileActivity
# F2   ExitWhile: exitWhileInsideWhile, isValid, TypeName, whileSequenceActivity
# F3   ExitWhile.Counter = %{nearest GetRowsCount xName}%
# F4   GetCellValue.RowNumber = %{parent WhileActivity xName}%, ColumnType="Name"
# F5   MemorySet defaults (VariableScope, IsSaved, IsAppend)
# F6   ReturnValue defaults + forbidden field removal
# F7   ReturnValue status tier (preceding activity is Status producer)
# F8   ReturnValue scalar tier (preceding activity is Scalar producer)
# F9   Inject missing ReturnValue into every IfElseBranchActivity
# F11  Nested Advanced/Result sub-objects: numbered xNames + Descriptions
#      (replaces former Wirer-prompt band-aid)
# ---------------------------------------------------------------------------

_STATUS_PRODUCERS: frozenset = frozenset({
    # Confirmed status producers — return Success/Failure string
    # Used by F7 to select the correct ReturnValue tier
    "Ping", "ServiceStatus", "FileExist", "FTPFileExists",
    "ADUserExists", "PowerShellScript", "PowerShell",
    "ADIsAccountDisabled", "ADIsAccountLocked",
    # D1 additions — service management (all return Success/Failure)
    "ServiceStart", "ServiceStop", "ServiceRestart",
    "ServicePause", "ServiceResume", "FileExistRemote",
    "ADUnlockAccount", "ApplicationPoolStatus",
    "ApplicationPoolStart", "ApplicationPoolStop",
    "ApplicationPoolRecycle",
})

_SCALAR_PRODUCERS: frozenset = frozenset({
    # Confirmed scalar producers — return a single string or number
    # Used by F8 to select the correct ReturnValue tier
    "GetRowsCount", "DateDifference", "FunctionCalculator",
    "GetCellValue", "GetCellValueAdvanced", "GetDate", "Contains",
    "IsEmpty", "InStr", "InStrRev", "Len", "ConvertPasswordToPlaintext",
    "GetLength", "SubStringByText", "Trim", "Replace",
    # D1 additions — string functions
    "Left", "Right", "Mid", "UpperCase", "LowerCase",
    "LeftTrim", "RightTrim", "Length",
    # D1 additions — math functions
    "Max", "Min", "Abs", "Ceiling", "Floor", "Round", "Sgn",
    # D1 additions — date/format functions
    "FormatDate", "AddDate", "GetUNIXTimestamp", "EpochConverter",
    # D1 additions — string search/encode
    "IndexOf", "EncodeURL", "JSONEncodeString",
    "MatchRegularExpression", "ExtractLineFromText",
    # D1 additions — returns string (not DataTable despite the name)
    "ConvertToHTMLTable",
})

# Fields that must never appear on WhileActivity or SequenceActivity.
# WirerAgent sometimes adds these when reconstructing a skeleton from scratch.
_WHILE_SPURIOUS_FIELDS: frozenset = frozenset({
    "LeftOperand", "Operator", "RightOperand", "SuccessReason",
})
_SEQUENCE_SPURIOUS_FIELDS: frozenset = frozenset({
    "LeftOperand", "Operator", "RightOperand",
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

    # Enforce Type is always a valid value — never "Default", "Equals", or ""
    if node.get("Type") not in ("StoredValue", "UserDefinedValue"):
        node["Type"] = "StoredValue"

    # D3: Pre-compute Formula deterministically — pure function of ConditionType + Value.
    # Formula = =ConditionType(&&&,Value) when condition is active, {x:Null} otherwise.
    # WirerAgent instruction says "do not set Formula" but enforcing it here means
    # the correct value is always present regardless of instruction compliance.
    ct_val  = node.get("ConditionType", "")
    val_str = node.get("Value", "")
    is_valid = node.get("IsValid", "False")
    if ct_val and is_valid == "True":
        node["Formula"] = f"={ct_val}(&&&,{val_str})"
    else:
        node["Formula"] = "{x:Null}"


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
            for _f in _WHILE_SPURIOUS_FIELDS:
                node.pop(_f, None)
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

    _EXITWHILE_SPURIOUS = frozenset({
        "Condition", "IsExpression", "LeftOperand", "Operator", "RightOperand",
        "SuccessReason", "Expression",
    })

    for xname, node in seq_node.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue
        if ct == "ExitWhile":
            # Strip spurious fields Wirer may have added (e.g. Condition, IsExpression)
            for f in _EXITWHILE_SPURIOUS:
                node.pop(f, None)
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
            for _f in _WHILE_SPURIOUS_FIELDS:
                node.pop(_f, None)
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

    # Reorder seq_node so ExitWhile is always first (F2 platform rule).
    # Wirer sometimes places ExitWhile at the end of the sequence.
    exit_items = [(k, v) for k, v in seq_node.items()
                  if isinstance(v, dict) and v.get("CustomTypeName") == "ExitWhile"]
    other_items = [(k, v) for k, v in seq_node.items()
                   if not (isinstance(v, dict) and v.get("CustomTypeName") == "ExitWhile")]
    if exit_items and list(seq_node.items())[0][0] != exit_items[0][0]:
        seq_node.clear()
        for k, v in exit_items + other_items:
            seq_node[k] = v

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
            for _f in _WHILE_SPURIOUS_FIELDS:
                node.pop(_f, None)
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
            # F1: strip spurious Wirer-added comparison fields and enforce Condition.
            # Wirer sometimes adds LeftOperand/Operator/RightOperand when
            # reconstructing a collapsed skeleton — platform rejects these.
            for _f in _WHILE_SPURIOUS_FIELDS:
                node.pop(_f, None)
            node["Condition"] = "{x:Null}"
            _walk_while(node, xname, last_getrowscount)
        elif ct == "MemorySet":
            node.setdefault("VariableScope", "Workflow")
            node.setdefault("IsSaved", "False")
            node.setdefault("IsAppend", "False")
        elif ct == "IfElseActivity":
            _walk_ifelse(node, last_ct, None, last_getrowscount)
        elif ct == "SequenceActivity":
            # Strip spurious fields Wirer may have added to SequenceActivity
            for _f in _SEQUENCE_SPURIOUS_FIELDS:
                node.pop(_f, None)
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
    Apply structural invariants F1-F9 plus F11 (nested sub-object
    normalization). Idempotent — safe to call twice (Stage 4c before Wirer
    and Stage 4f after Wirer).
    """
    import copy
    result = copy.deepcopy(workflow_json)
    raw    = result.get("workflow_raw_data", result)
    if not isinstance(raw, dict):
        print("[fragments] Warning: workflow_raw_data is not a dict — skipping")
        return result
    _walk_top_level(raw)
    n_nested = _normalize_nested_subobjects(raw)
    n_top = len(raw)
    if n_nested:
        print(f"[fragments] Applied F1-F9 to {n_top} top-level activities; "
              f"F11 normalized {n_nested} nested sub-object(s)")
    else:
        print(f"[fragments] Applied F1-F9 to {n_top} top-level activities")
    return result


def run_fragments(enriched_workflow: dict) -> dict:
    """
    Stage 4c / 4f: applies F1-F9 + F11.
    Stage 4c: before WirerAgent (structural context on skeleton)
    Stage 4f: after WirerAgent  (enforce invariants on Wirer-reconstructed activities)
    """
    enriched_workflow = _ensure_dict(enriched_workflow)
    return apply_fragments(enriched_workflow)


# ---------------------------------------------------------------------------
# Stage 4c.5 — Content scaffold
# ---------------------------------------------------------------------------

_TABLE_PRODUCERS_XNAME: frozenset = frozenset({
    # Core query activities — confirmed DataTable output
    "TSQLQuery", "JsonToTable", "ResultSetFilter",
    "GetRows", "NestedJsonToTable",
    # Additional DataTable producers — confirmed via output registry
    "ReadExcelSpreadsheet",  # Excel spreadsheet → ResultSet (t09 fix)
    "ReadXLS",               # older Excel format → ResultSet
    "HTTPRequest",           # returns 3-col table: Status Code / Body / Request
    "Split",                 # splits string into single-column table of rows
    "MemoryTableUnion",      # merges two ResultSets → new ResultSet
    "AddMemoryTableRow",     # returns modified ResultSet
    "DeleteMemoryTableRows", # returns modified ResultSet
    # Probable DataTable producers — likely from corpus, flag for verification
    "FTPListFiles",          # FTP directory listing → ResultSet
    "ListFolderBox",         # folder contents → ResultSet
    "GetInstalledSoftware",  # software inventory → ResultSet
    "ProcessList",           # running process list → ResultSet
    "SNGetRecord",           # ServiceNow record query → ResultSet
})

_TABLE_PRODUCERS_FIELD: dict = {
    "CreateMemoryTable": "TableName",
}

_SCAFFOLD_CONTAINERS: frozenset = frozenset({
    "WhileActivity", "SequenceActivity", "IfElseActivity",
    "IfElseBranchActivity", "ParallelActivity", "UserGroup",
    "ForEachActivity",
})


def _get_table_var(node: dict) -> str | None:
    ct = node.get("CustomTypeName", "")
    if ct in _TABLE_PRODUCERS_XNAME:
        return node.get("xName") or None
    field = _TABLE_PRODUCERS_FIELD.get(ct)
    if field:
        val = node.get(field, "").strip().strip("%")
        # Skip template placeholder values — they are not real variable names
        if val and not val.endswith("_value"):
            return val
        return None
    return None


def _sif(node: dict, field: str, value: str) -> None:
    """
    Set field on node only if currently absent, empty, or a template placeholder
    (values ending with "_value" are enrichment placeholders, not real values).
    """
    current = node.get(field, "")
    if not current or (isinstance(current, str) and current.endswith("_value")):
        node[field] = value


def _apply_f10(node: dict, ct: str) -> None:
    """
    F10: sync Description <-> description on any activity node.
    Extracted from _scaffold_node so it can be called on container activities
    (WhileActivity, SequenceActivity, IfElseActivity, IfElseBranchActivity)
    which _scaffold_walk recurses into but does not pass to _scaffold_node.
    Idempotent — safe to call multiple times.
    """
    desc = node.get("Description", "")
    if not desc or desc.endswith("_value"):
        desc = node.get("DisplayName", ct)
    if desc:
        node["Description"] = desc
        existing_d = node.get("description", "")
        if not existing_d or existing_d.endswith("_value"):
            node["description"] = desc
    elif node.get("description", ""):
        existing_d = node.get("description", "")
        if not existing_d.endswith("_value"):
            node.setdefault("Description", existing_d)


def _scaffold_node(
    node: dict,
    nearest_table_var: str | None,
    nearest_session_xname: str | None,
    inside_while: bool,
    parent_while_xname: str | None,
) -> None:
    ct = node.get("CustomTypeName", "")

    # F10 is applied by _scaffold_walk before calling _scaffold_node,
    # so it fires on both container and leaf activities. No need to repeat here.

    # ── GetRowsCount ─────────────────────────────────────────────────────────
    if ct == "GetRowsCount":
        if nearest_table_var:
            # Authoritative rule: ResultSet is ALWAYS %tableName%, never an xName.
            # Use direct assignment (not _sif) so post-Wirer scaffold restores
            # any incorrect value Wirer may have written (e.g. %getRowsCount1%).
            node["ResultSet"] = f"%{nearest_table_var}%"

    # ── GetCellValue ─────────────────────────────────────────────────────────
    elif ct == "GetCellValue":
        if nearest_table_var:
            # Same authoritative rule — direct assignment, not _sif.
            node["ResultSet"]     = f"%{nearest_table_var}%"
            node["ResultSetName"] = nearest_table_var
        _sif(node, "ColumnType", "Name")

    # ── ResultSetFilter ───────────────────────────────────────────────────────
    elif ct == "ResultSetFilter":
        if nearest_table_var:
            _sif(node, "VariableName", f"%{nearest_table_var}%")

    # ── DeleteMemoryTableRows ─────────────────────────────────────────────────
    elif ct == "DeleteMemoryTableRows":
        if nearest_table_var:
            _sif(node, "ResultSet",     f"%{nearest_table_var}%")
            _sif(node, "ResultSetName", f"%{nearest_table_var}%")

    # ── AddMemoryTableRow ─────────────────────────────────────────────────────
    elif ct == "AddMemoryTableRow":
        if nearest_table_var:
            _sif(node, "ResultSet",     f"%{nearest_table_var}%")
            _sif(node, "ResultSetName", f"%{nearest_table_var}%")
        _sif(node, "Selection", "2")
        _sif(node, "RowNumber", "")

    # ── SetCellValue ──────────────────────────────────────────────────────────
    elif ct == "SetCellValue":
        if nearest_table_var:
            _sif(node, "VariableName", f"%{nearest_table_var}%")
        _sif(node, "ColumnType", "Name")

    # ── JsonToTable ───────────────────────────────────────────────────────────
    elif ct == "JsonToTable":
        if nearest_session_xname:
            _sif(node, "SessionName", nearest_session_xname)

    # ── StartJsonSession ──────────────────────────────────────────────────────
    elif ct == "StartJsonSession":
        _sif(node, "StartSession", "True")

    # ── ReplaceString ─────────────────────────────────────────────────────────
    elif ct == "ReplaceString":
        _sif(node, "ReplaceOriginalVariable", "False")
        _sif(node, "IsMatchCase",    "False")
        _sif(node, "IsRegex",        "False")
        _sif(node, "RemoveSpaces",   "False")
        _sif(node, "RemoveNewLines", "False")
        _sif(node, "RemoveTabs",     "False")

    # ── MultiMemorySet ────────────────────────────────────────────────────────
    elif ct == "MultiMemorySet":
        _sif(node, "variableScope", "Workflow")
        _sif(node, "IsGlobal",      "False")

    # ── CreateMemoryTable ─────────────────────────────────────────────────────
    # ColumnNumber, RowNumber, isEmptyGrid are required for the platform to
    # initialize a valid table object. Without them GetRowsCount throws
    # "key not present in dictionary" because there is no column schema.
    # Defaults signal an empty table — user populates via AddMemoryTableRow
    # or by setting TableAsString before running.
    elif ct == "CreateMemoryTable":
        _sif(node, "ColumnNumber", "1")   # minimum schema: 1 column
        _sif(node, "RowNumber",    "0")   # no pre-populated rows
        _sif(node, "isEmptyGrid",  "1")   # tells platform the table is empty

    # ── Ping / Service activities — HostId ───────────────────────────────────
    # HostId="-2" = any host — corpus dominant, RitaLab confirmed.
    # HostName is semantic (which server) — left for WirerAgent.
    elif ct in {"Ping", "ServiceStatus", "ServiceStart", "ServiceStop",
                "ServiceRestart", "ServicePause", "ServiceResume",
                "ApplicationPoolStatus", "ApplicationPoolStart",
                "ApplicationPoolStop", "ApplicationPoolRecycle"}:
        _sif(node, "HostId", "-2")

    # ── GetDate ───────────────────────────────────────────────────────────────
    # DateFormat: confirmed platform rule — always "MM/dd/yyyy HH:mm"
    # FuturePast: "0" = current date (corpus dominant)
    elif ct == "GetDate":
        _sif(node, "DateFormat", "MM/dd/yyyy HH:mm")
        _sif(node, "FuturePast", "0")

    # ── DateDifference ────────────────────────────────────────────────────────
    # FirstDateFormat/SecondDateFormat: confirmed platform rule
    # ReturnFormat is semantic (Days/Hours/etc.) — left for WirerAgent
    elif ct == "DateDifference":
        _sif(node, "FirstDateFormat",  "MM/dd/yyyy HH:mm")
        _sif(node, "SecondDateFormat", "MM/dd/yyyy HH:mm")

    # ── TSQLQuery ─────────────────────────────────────────────────────────────
    elif ct == "TSQLQuery":
        _sif(node, "SiteId",             "-1")
        _sif(node, "SiteName",           "")
        _sif(node, "isUserAuthenticate", "False")
        _sif(node, "UserName",           "")
        _sif(node, "Password",           "")


def _scaffold_walk(
    nodes: dict,
    nearest_table_var: str | None,
    nearest_session_xname: str | None,
    inside_while: bool,
    parent_while_xname: str | None,
) -> tuple[str | None, str | None]:
    for xname, node in nodes.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if not ct:
            continue

        # F10: apply to ALL activities including containers before any branching.
        # Containers (WhileActivity, SequenceActivity, etc.) are not passed to
        # _scaffold_node, so F10 must run here to cover them.
        _apply_f10(node, ct)

        if ct in _SCAFFOLD_CONTAINERS:
            if ct == "WhileActivity":
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
                _scaffold_walk(
                    node,
                    nearest_table_var,
                    nearest_session_xname,
                    inside_while,
                    parent_while_xname,
                )
            tv = _get_table_var(node)
            if tv:
                nearest_table_var = tv
        else:
            _scaffold_node(
                node,
                nearest_table_var,
                nearest_session_xname,
                inside_while,
                parent_while_xname,
            )
            if ct == "StartJsonSession":
                nearest_session_xname = xname
            tv = _get_table_var(node)
            if tv:
                nearest_table_var = tv

    return nearest_table_var, nearest_session_xname


def apply_content_scaffold(workflow_json: dict) -> dict:
    """
    Apply deterministic semantic wiring rules (Stage 4c.5).

    F10  Description/description sync — both fields present, no placeholders
    S1   GetRowsCount.ResultSet              = %nearestTableVar%
    S2   GetCellValue.ResultSet              = %nearestTableVar%
    S3   GetCellValue.ResultSetName          = nearestTableVar  (bare, no %)
    S4   GetCellValue.ColumnType             = "Name"
    S5   ResultSetFilter.VariableName        = %nearestTableVar%
    S6   DeleteMemoryTableRows.ResultSet     = %nearestTableVar%
    S7   DeleteMemoryTableRows.ResultSetName = %nearestTableVar%
    S8   AddMemoryTableRow.ResultSet         = %nearestTableVar%
    S9   AddMemoryTableRow.ResultSetName     = %nearestTableVar%
    S10  AddMemoryTableRow.Selection         = "2"
    S11  AddMemoryTableRow.RowNumber         = ""
    S12  SetCellValue.VariableName           = %nearestTableVar%
    S13  SetCellValue.ColumnType             = "Name"
    S14  JsonToTable.SessionName             = nearestSessionXName  (no %)
    S15  StartJsonSession.StartSession       = "True"
    S16  ReplaceString.ReplaceOriginalVariable = "False"
    S17  ReplaceString boolean flags         = "False"
    S18  MultiMemorySet.variableScope        = "Workflow"
    S19  MultiMemorySet.IsGlobal             = "False"
    S20  TSQLQuery.SiteId                    = "-1"
    S21  TSQLQuery.SiteName                  = ""
    S22  TSQLQuery.isUserAuthenticate        = "False"
    S23  Ping/Service.HostId                 = "-2"  (any host)
    S24  GetDate.DateFormat                  = "MM/dd/yyyy HH:mm"
    S25  GetDate.FuturePast                  = "0"
    S26  DateDifference.FirstDateFormat      = "MM/dd/yyyy HH:mm"
    S27  DateDifference.SecondDateFormat     = "MM/dd/yyyy HH:mm"
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

    print(f"[scaffold] Applied F10+S1-S22 to workflow ({len(raw)} top-level activities)")
    return result


def run_content_scaffold(fragmented_workflow: dict) -> dict:
    """Stage 4c.5: deterministic semantic wiring scaffold."""
    fragmented_workflow = _ensure_dict(fragmented_workflow)
    return apply_content_scaffold(fragmented_workflow)


# ---------------------------------------------------------------------------
# Stage 4b.6 — Deterministic wiring pass
# ---------------------------------------------------------------------------

def run_wiring_pass(workflow_json: dict) -> dict:
    """
    Apply authoritative and high-confidence wiring rules from wiring_map.json
    deterministically, without LLM involvement.

    Only applies rules where:
    - authoritative=True (platform-confirmed), OR
    - pct_of_target >= 85 AND workflow_count >= 8

    Walks the workflow in document order, tracks the xName of each activity,
    and applies (source_CT, target_CT) → target_field = %source_xName% rules
    when the pair is seen in sequence.

    Special cases handled separately from wiring_map:
    - CreateMemoryTable → GetRowsCount.ResultSet  (always = %TableName%)
    - CreateMemoryTable → GetCellValue.ResultSet  (always = %TableName%)
    These use TableName value, not xName.
    """
    import copy
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)
    raw = result.get("workflow_raw_data", {})
    if not isinstance(raw, dict):
        return result

    wiring = _load_wiring_map()

    # Build lookup: (source_CT, target_CT) → [(field, authoritative)]
    wire_lookup: dict = {}
    for entry in wiring:
        src  = entry.get("source_activity", "")
        tgt  = entry.get("target_activity", "")
        fld  = entry.get("target_field", "")
        auth = entry.get("authoritative", False)
        pct  = entry.get("pct_of_target", 0)
        cnt  = entry.get("workflow_count", 0)
        if not (src and tgt and fld):
            continue
        if not auth and (pct < 85 or cnt < 8):
            continue
        wire_lookup.setdefault((src, tgt), []).append((fld, auth))

    def _apply_wiring(nodes: dict,
                      prev_ct: str | None = None,
                      prev_xname: str | None = None,
                      table_name: str | None = None) -> tuple:
        """Walk nodes in order, applying wiring rules. Returns updated (prev_ct, prev_xname, table_name)."""
        for xname, node in nodes.items():
            if not isinstance(node, dict):
                continue
            ct = node.get("CustomTypeName", "")
            if not ct:
                continue

            # Track CreateMemoryTable TableName for downstream ResultSet wiring
            if ct == "CreateMemoryTable":
                tn = node.get("TableName", "").strip()
                if tn and not tn.endswith("_value"):
                    table_name = tn

            # Apply (prev_ct, ct) wiring rules
            if prev_ct and prev_xname:
                rules = wire_lookup.get((prev_ct, ct), [])
                for field, auth in rules:
                    if not node.get(field) or node.get(field, "").endswith("_value"):
                        node[field] = f"%{prev_xname}%"

            # Special: ResultSet on GetRowsCount/GetCellValue uses TableName var
            if ct in ("GetRowsCount", "GetCellValue", "ResultSetFilter") and table_name:
                if not node.get("ResultSet") or node.get("ResultSet","").endswith("_value"):
                    node["ResultSet"] = f"%{table_name}%"
                if ct == "GetCellValue":
                    if not node.get("ResultSetName") or node.get("ResultSetName","").endswith("_value"):
                        node["ResultSetName"] = table_name

            # Recurse into containers
            if ct in ("WhileActivity", "SequenceActivity", "IfElseActivity",
                      "IfElseBranchActivity", "UserGroup", "ForEachActivity"):
                prev_ct, prev_xname, table_name = _apply_wiring(
                    node, prev_ct, prev_xname, table_name
                )
                continue

            # Update prev for next sibling
            if ct not in ("ExitWhile", "ReturnValue", "IfElseActivity",
                          "IfElseBranchActivity", "SequenceActivity"):
                prev_ct = ct
                prev_xname = xname

        return prev_ct, prev_xname, table_name

    _apply_wiring(raw)
    n_raw = len(raw)
    print(f"[wiring_pass] Applied deterministic wiring to {n_raw} top-level activities")
    return result


def run_wiring(workflow_json: dict) -> dict:
    """Stage 4b.6: deterministic wiring from wiring_map.json."""
    return run_wiring_pass(_ensure_dict(workflow_json))


# ---------------------------------------------------------------------------
# Stage 4g — Post-Wirer cleanup
# ---------------------------------------------------------------------------

# Fields that are invalid on specific activity types — Wirer sometimes invents
# these based on misunderstanding which activity produces/consumes variables.
_INVALID_FIELDS_BY_TYPE: dict[str, frozenset] = {
    "GetRowsCount":  frozenset({"TableName", "ResultSetName", "ColumnType",
                                "ColumnNumber", "ColumnName", "RowNumber"}),
    "GetCellValue":  frozenset({"TableName"}),
    "ExitWhile":     frozenset({"Condition", "IsExpression", "Expression"}),
    "DisplayValue":  frozenset({"ResultSet", "ResultSetName"}),
    "Ping":          frozenset({"ResultSet", "ResultSetName", "TableName"}),
    "MemorySet":     frozenset({"ResultSet", "ResultSetName", "TableName"}),
}


def _strip_invalid_fields(raw: dict) -> int:
    """Strip activity-type-specific invalid fields added by Wirer."""
    stripped = 0
    for xname, node in raw.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        invalid = _INVALID_FIELDS_BY_TYPE.get(ct, frozenset())
        for field in invalid:
            if field in node:
                del node[field]
                stripped += 1
        # Recurse into containers
        for v in node.values():
            if isinstance(v, dict) and v.get("CustomTypeName"):
                stripped += _strip_invalid_fields({v.get("xName","_"): v})
    return stripped


def _remove_empty_nodes(raw: dict) -> int:
    """
    Recursively removes inert activity nodes — those that have no meaningful
    field values and would do nothing at runtime.

    Currently removes:
      MemorySet where VariableName='' AND VariableValue=''
        These store nothing. WirerAgent sometimes adds them when reconstructing
        a loop body from a collapsed PlacerAgent skeleton.

    Returns the count of nodes removed.
    """
    removed = 0
    keys_to_remove = []

    for xname, node in raw.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")

        if ct == "MemorySet":
            var_name  = node.get("VariableName", "").strip()
            var_value = node.get("VariableValue", "").strip()
            if not var_name and not var_value:
                keys_to_remove.append(xname)
                print(f"[cleanup] Stage 4g: removed empty MemorySet '{xname}'")
                removed += 1
                continue

        # Recurse into containers
        for k, v in node.items():
            if isinstance(v, dict) and v.get("CustomTypeName"):
                sub_raw = {k2: v2 for k2, v2 in v.items() if isinstance(v2, dict)}
                if sub_raw:
                    removed += _remove_empty_nodes(v)

    for key in keys_to_remove:
        del raw[key]

    return removed


def run_cleanup(workflow_json: dict) -> dict:
    """
    Stage 4g: post-Wirer cleanup pass.
    Removes inert nodes WirerAgent may have added when reconstructing
    a collapsed PlacerAgent skeleton.
    Runs after Stage 4f (fragments + scaffold) and before Stage 5 (annotation).
    """
    import copy
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)
    raw    = result.get("workflow_raw_data", result)
    if not isinstance(raw, dict):
        return result
    n = _remove_empty_nodes(raw)
    if n:
        print(f"[cleanup] Stage 4g: removed {n} empty node(s)")
    s = _strip_invalid_fields(raw)
    if s:
        print(f"[cleanup] Stage 4g: stripped {s} invalid field(s)")
    return result


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
        if item.get("kind") in ("verify", "update")
    ]
    return {
        "status":              val_result["status"],
        "workflow_json":       workflow_json if val_result["status"] == "valid" else None,
        "placeholder_summary": placeholder_summary,
        "errors":              val_result.get("errors", []),
        "verify_notes":        verify_notes,
    }