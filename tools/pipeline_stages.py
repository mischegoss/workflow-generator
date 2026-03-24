"""
tools/pipeline_stages.py

Deterministic Python replacements for five LLM agents:
  PatternMatcherAgent  → run_pattern_match()
  ActivityRetrieverAgent → run_retrieval()
  AnnotationAgent      → run_annotation()
  ValidationAgent      → run_validation()

run_output() lives in output_tools.py.

All functions accept plain Python dicts/lists.
_ensure_dict guards are kept for safety at the boundaries but are
not needed for intra-pipeline calls.
"""

import json
import os
from typing import Any

from tools.pattern_tools import (
    load_pattern_library,
    match_pattern,
    score_pattern_match,
    _detect_fallback_cf,
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
# Helpers
# ---------------------------------------------------------------------------

def _load_wiring_map() -> dict:
    """Load wiring_map.json once and cache. Returns empty dict on missing file."""
    if not hasattr(_load_wiring_map, "_cache"):
        data_dir = os.getenv("DATA_DIR", "data")
        path = os.path.join(data_dir, "wiring_map.json")
        try:
            with open(path) as f:
                _load_wiring_map._cache = json.load(f)
        except FileNotFoundError:
            print(f"[pipeline_stages] Warning: wiring_map.json not found at {path}")
            _load_wiring_map._cache = {}
    return _load_wiring_map._cache


def _build_wiring_lookup(wiring_map: dict) -> dict:
    """
    Flatten wiring_map into a dict keyed by activity TypeName for O(1) lookup.
    Returns: { "SendEmail": { "pre_filled_fields": {...} }, ... }
    """
    lookup = {}
    for entry in wiring_map.get("wiring", []):
        type_name = entry.get("activity_type")
        if type_name:
            lookup[type_name] = entry
    return lookup


# ---------------------------------------------------------------------------
# Stage 2 — Pattern match  (replaces PatternMatcherAgent)
# ---------------------------------------------------------------------------

def run_pattern_match(decomposition: dict) -> dict:
    """
    Replaces PatternMatcherAgent.
    Loads pattern library, matches against decomposition, scores result.
    Falls back to _detect_fallback_cf on NO_MATCH.

    Input:  decomposition dict (from DecomposerAgent session state)
    Output: pattern_match dict with match_status, matched_pattern (if any),
            fallback_examples (if NO_MATCH)
    """
    decomposition = _ensure_dict(decomposition)
    library = load_pattern_library()
    candidates = match_pattern(decomposition, library)
    result = score_pattern_match(candidates)

    if result.get("match_status") == "NO_MATCH":
        fallback = _detect_fallback_cf(decomposition)
        result["fallback_examples"] = [fallback] if fallback else []

    return result


# ---------------------------------------------------------------------------
# Stage 3 — Retrieve activities  (replaces ActivityRetrieverAgent)
# ---------------------------------------------------------------------------

def run_retrieval(decomposition: dict) -> list:
    """
    Replaces ActivityRetrieverAgent.
    Calls retrieve_all_steps() directly — no LLM dispatcher.
    Attaches pre_filled_fields from wiring_map to each manifest entry.

    Input:  decomposition dict
    Output: list of manifest entries trimmed to 4 StructureBuilder fields
            + pre_filled_fields where available
    """
    decomposition = _ensure_dict(decomposition)
    load_activity_list()

    steps = decomposition.get("steps", [])
    manifest = retrieve_all_steps(steps)

    # Load wiring map and attach pre_filled_fields
    wiring_map = _load_wiring_map()
    wiring_lookup = _build_wiring_lookup(wiring_map)

    trimmed = []
    for s in manifest:
        activity_type = s.get("selected_activity") or ""
        entry = {
            "step_id": s["step_id"],
            "selected_activity": activity_type,
            "status": s["status"],
            "frequency_tier": s.get("frequency_tier", "medium"),
        }
        # Attach pre_filled_fields if wiring data exists for this activity type
        if activity_type in wiring_lookup:
            wiring_entry = wiring_lookup[activity_type]
            pre_filled = wiring_entry.get("pre_filled_fields")
            if pre_filled:
                entry["pre_filled_fields"] = pre_filled

        trimmed.append(entry)

    return trimmed


# ---------------------------------------------------------------------------
# Stage 5 — Annotate  (replaces AnnotationAgent)
# ---------------------------------------------------------------------------

def run_annotation(workflow_json: dict, activity_manifest: Any) -> dict:
    """
    Replaces AnnotationAgent — direct 4-function chain.
    No LLM; each function is pure Python.

    Input:  workflow_json  — validated JSON from StructureBuilderAgent
            activity_manifest — list from run_retrieval (or dict with 'steps' key)
    Output: { 'annotated_workflow_json': dict, 'placeholder_summary': list }
    """
    workflow_json = _ensure_dict(workflow_json)

    # Normalise manifest — annotation tools expect a dict with a 'steps' key
    if isinstance(activity_manifest, list):
        manifest_dict = {"steps": activity_manifest}
    else:
        manifest_dict = _ensure_dict(activity_manifest)

    result = inject_unavailable_stubs(workflow_json, manifest_dict)
    result = annotate_placeholders(result)
    result = add_verify_notes(result)
    summary = collect_placeholder_summary(result)

    return {
        "annotated_workflow_json": result,
        "placeholder_summary": summary,
    }


# ---------------------------------------------------------------------------
# Stage 6 — Validate  (replaces ValidationAgent)
# ---------------------------------------------------------------------------

def run_validation(annotation_result: dict) -> dict:
    """
    Replaces ValidationAgent — single run_all_validators() call.
    On failure, errors are returned in the result dict for StructureBuilder
    correction retry (orchestrated in pipeline.py).

    Input:  annotation_result dict from run_annotation()
    Output: {
        'status': 'valid' | 'invalid',
        'workflow_json': dict | None,
        'placeholder_summary': list,
        'errors': list,
        'verify_notes': list,
    }
    """
    annotation_result = _ensure_dict(annotation_result)
    workflow_json = annotation_result.get("annotated_workflow_json", {})
    placeholder_summary = annotation_result.get("placeholder_summary", [])

    val_result = run_all_validators(workflow_json)

    return {
        "status": val_result["status"],
        "workflow_json": workflow_json if val_result["status"] == "valid" else None,
        "placeholder_summary": placeholder_summary,
        "errors": val_result.get("errors", []),
        "verify_notes": val_result.get("verify_notes", []),
    }