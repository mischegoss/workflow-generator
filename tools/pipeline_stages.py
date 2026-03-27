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
       Entries with status="INTENT_MATCH" or "MATCHED" carry confidence too
       for transparency, but StructureBuilder treats them as authoritative.

    2. Wiring hints (wiring_map.json): corpus-derived hints attached as
       _wire_hint_<field> keys in pre_filled_fields. Tell StructureBuilder
       which upstream activity TYPE typically feeds a field. Format:
       "_wire_hint_ResultSet": "TSQLQuery:91pct". Not authoritative — the
       LLM uses these as context to assign the actual xName.

    3. Dependency warnings (dependency_graph.json): checks manifest order
       for missing table/session prerequisites. If GetCellValue appears
       but no table producer precedes it, a prerequisite_note is attached
       to that manifest entry so StructureBuilder adds the missing activity.

    Authoritative wiring entries (ExitWhile→GetCellValue.RowNumber) are
    SKIPPED for hints — the TypeName-derived xName would be wrong.
    StructureBuilder's checklist item 7 handles these correctly.
    """
    decomposition  = _ensure_dict(decomposition)
    load_activity_list()

    steps    = decomposition.get("steps", [])
    manifest = retrieve_all_steps(steps)

    wiring_entries = _load_wiring_map()
    wiring_lookup  = _build_wiring_lookup(wiring_entries)
    dep_graph      = _load_dependency_graph()

    # Build trimmed manifest — pass confidence and candidates for UNCERTAIN entries
    # so StructureBuilder has enough context to exercise judgment.
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

        # Always pass confidence when present — StructureBuilder uses it
        # to distinguish INTENT_MATCH (1.0, deterministic) from UNCERTAIN (<0.35)
        if "confidence" in s:
            entry["confidence"] = s["confidence"]

        # For UNCERTAIN entries, include the top-3 candidates so StructureBuilder
        # can choose a better fit if the step description makes the correct
        # activity clear from context. Without this, UNCERTAIN is a no-op signal.
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
                continue  # handled by StructureBuilder checklist

            if pct >= 80:
                wiring_hints[f"_wire_hint_{field}"] = f"{src_type}:{pct}pct"

        if wiring_hints:
            curr["pre_filled_fields"] = wiring_hints

    # Enrichment 2: dependency warnings from dependency_graph
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
# Stage 5 — Annotate  (replaces AnnotationAgent)
# ---------------------------------------------------------------------------

def run_annotation(workflow_json: dict, activity_manifest: Any) -> dict:
    """
    Replaces AnnotationAgent — direct 4-function chain.
    """
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