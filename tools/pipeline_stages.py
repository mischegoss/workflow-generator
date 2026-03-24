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
    Returns the flat list of wiring entries, or [] on missing file.
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

    First-write-wins: authoritative entries are prepended in wiring_map.json
    so they are never overwritten by corpus entries.
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

    Attaches pre_filled_fields from wiring_map to consecutive manifest pairs
    where pct_of_target >= 80 or authoritative=true.

    Issue 1 fix: Authoritative wiring entries (ExitWhile→GetCellValue.RowNumber,
    ExitWhile→SetCellValue.RowNumber) are SKIPPED when generating pre_filled_fields
    hints. These entries have authoritative=true because they are correct by
    platform rule — but the hint value would be "%exitWhile%" (TypeName-derived),
    which does NOT match the actual xName StructureBuilder assigns (e.g. "%exitWhile1%").
    Sending a wrong authoritative hint contradicts StructureBuilder's checklist
    item 7, which already enforces the correct rule.

    Corpus-derived wirings with pct >= 80 do generate hints because they reference
    the source TypeName and the LLM will use them as guidance (not authoritative),
    filling in the actual xName itself.
    """
    decomposition  = _ensure_dict(decomposition)
    load_activity_list()

    steps    = decomposition.get("steps", [])
    manifest = retrieve_all_steps(steps)

    wiring_entries = _load_wiring_map()
    wiring_lookup  = _build_wiring_lookup(wiring_entries)

    # Trim manifest to StructureBuilder fields
    trimmed = []
    for s in manifest:
        activity_type = s.get("selected_activity") or ""
        trimmed.append({
            "step_id":           s["step_id"],
            "selected_activity": activity_type,
            "status":            s["status"],
            "frequency_tier":    s.get("frequency_tier", "medium"),
        })

    # Walk consecutive pairs and attach pre_filled_fields
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

        pre_filled = {}
        for field, info in tgt_wirings.items():
            pct  = info.get("pct", 0)
            auth = info.get("authoritative", False)

            # Issue 1 fix: skip authoritative entries — the hint TypeName
            # won't match the actual xName. StructureBuilder's checklist
            # item 7 handles these rules correctly without a hint.
            if auth:
                continue

            if pct >= 80:
                # Corpus-derived hint: camelCase source TypeName
                hint = src_type[0].lower() + src_type[1:]
                pre_filled[field] = f"%{hint}%"

        if pre_filled:
            curr["pre_filled_fields"] = pre_filled

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