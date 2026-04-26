"""
tools/post_wirer_repair.py

Post-Wirer deterministic repair pass.

The Wirer LLM occasionally overwrites enrichment-seeded values with
hallucinated alternatives, or drops required fields entirely. This module
runs after the Wirer and:

  CLAMP — for any field whose value violates an enum_values.json constraint,
  replace it with the first allowed value (the corpus-dominant choice that
  build_tools.load_activity_template would have seeded). Variable references
  (%foo%) are left alone — they're resolved at runtime, not enum-checked.

  RESTORE — for any required field that's missing, restore from
  field_defaults.json if a corpus-dominant default exists.

  ANNOTATE — for any required field that's missing AND has no deterministic
  source, attach an UPDATE BEFORE RUNNING note to the activity. The user
  sees this in the post-import summary alongside the existing categories.

The pass is idempotent — running it twice produces the same output as
running it once. It's safe to slot in as a standalone post-processing stage.
"""

import json
import os
import re
from typing import Annotated

# ---------------------------------------------------------------------------
# Data file caches — independent of build_tools so this module can be
# imported and used standalone (e.g. for diagnostics).
# ---------------------------------------------------------------------------

_enum_values_cache: dict | None = None
_field_defaults_cache: dict | None = None
_controls_cache: dict | None = None


def _load_enum_values() -> dict:
    """Returns dict: { activityName: { fieldKey: { values: [...] } } }"""
    global _enum_values_cache
    if _enum_values_cache is not None:
        return _enum_values_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "enum_values.json")
    try:
        with open(path, encoding="utf-8") as f:
            _enum_values_cache = json.load(f)
    except FileNotFoundError:
        _enum_values_cache = {}
    return _enum_values_cache


def _load_field_defaults() -> dict:
    """Returns dict: { activityName: { fieldKey: dominant_value } }"""
    global _field_defaults_cache
    if _field_defaults_cache is not None:
        return _field_defaults_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "field_defaults.json")
    try:
        with open(path, encoding="utf-8") as f:
            _field_defaults_cache = json.load(f)
    except FileNotFoundError:
        _field_defaults_cache = {}
    return _field_defaults_cache


def _load_controls_index() -> dict:
    """Returns dict: { activityName: [control_dict, ...] }"""
    global _controls_cache
    if _controls_cache is not None:
        return _controls_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activities_controls.json")
    try:
        with open(path, encoding="utf-8") as f:
            controls = json.load(f)
        _controls_cache = {
            entry["activityName"]: entry.get("controls", [])
            for entry in controls
        }
    except FileNotFoundError:
        _controls_cache = {}
    return _controls_cache


# ---------------------------------------------------------------------------
# Constants — kept consistent with validation_tools.py so the validator
# and the repair pass agree on what to skip.
# ---------------------------------------------------------------------------

_CONTAINER_TYPES = frozenset({
    "WhileActivity", "SequenceActivity", "IfElseActivity", "IfElseBranchActivity",
    "ParallelActivity", "UserGroup", "ForEachActivity", "ExitWhile", "ReturnValue",
    "IfElseCondition", "Workflow", "Advanced", "Result", "ErrorHandling",
})

_EXCLUDED_REQUIRED_FIELDS = frozenset({
    "XMLTableResult", "XMLTableSelectionResult", "DictionaryAsXml",
    "FieldsList", "TargetModuleName", "TemplateName", "ColumnType",
    "ColumnNumber", "RowNumber", "TimeZoneName",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_variable_ref(value: str) -> bool:
    """%xName% or %variableName% — runtime-resolved, never enum-checked."""
    return isinstance(value, str) and bool(re.match(r'^%[^%]+%$', value.strip()))


def _allowed_values_for(activity_name: str, field_key: str,
                        enum_values: dict) -> list[str]:
    """Returns the list of valid string values for this enum-constrained field,
    or [] if the field is not enum-constrained."""
    entry = enum_values.get(activity_name, {}).get(field_key, {})
    if not isinstance(entry, dict):
        return []
    values = entry.get("values", [])
    return [
        str(v["value"])
        for v in values
        if isinstance(v, dict) and "value" in v and "note" not in v
    ]


def _append_repair_note(node: dict, msg: str) -> None:
    """Same shape as annotation_tools._append_note — UPDATE/VERIFY notes are
    surfaced in the post-import summary by collect_placeholder_summary."""
    existing = node.get("notes", "")
    if msg not in existing:
        node["notes"] = (existing + "  " + msg).strip()


# ---------------------------------------------------------------------------
# Repair pass
# ---------------------------------------------------------------------------

def repair_workflow(
    workflow_json: Annotated[dict, "Workflow JSON dict (typically post-Wirer)"],
) -> tuple[dict, list[str]]:
    """
    Walks workflow_json and applies CLAMP / RESTORE / ANNOTATE per field.

    Returns:
      (repaired_workflow, change_log)

    where change_log is a human-readable list of what was changed, suitable
    for printing to the pipeline console. The workflow dict itself is the
    same object passed in (mutated in place), so callers can either
    rebind or rely on the mutation. Returning it explicitly keeps the
    function composable in stage chains.

    Idempotency: clamp only fires when a field has an out-of-enum value;
    after one pass, all enum fields are valid. Restore only fires on
    missing fields; after one pass, all restorable fields are present.
    Running this twice produces no additional changes.
    """
    if not isinstance(workflow_json, dict):
        return workflow_json, []

    enum_values     = _load_enum_values()
    field_defaults  = _load_field_defaults()
    controls_index  = _load_controls_index()
    change_log: list[str] = []

    def repair_node(node: dict, path: str = "") -> None:
        type_name = node.get("CustomTypeName", "") or node.get("TypeName", "")
        xname     = node.get("xName", "") or path or "?"

        # Skip container types — they don't have enum-constrained leaf fields
        # in the same way activities do. Recursion still descends into them.
        is_container = type_name in _CONTAINER_TYPES

        if not is_container and type_name:
            # ── CLAMP pass ──────────────────────────────────────────────────
            # Walk every field on the node. If it has an enum constraint AND
            # the current value isn't valid AND isn't a variable reference,
            # replace with first allowed value.
            for field_key, val in list(node.items()):
                if not isinstance(val, str):
                    continue
                if not val:
                    continue
                if _is_variable_ref(val):
                    continue
                if val == "{x:Null}":
                    continue
                if val.endswith("_value"):
                    # Unfilled placeholder — handle in restore pass below
                    continue

                allowed = _allowed_values_for(type_name, field_key, enum_values)
                if not allowed:
                    continue
                if val in allowed:
                    continue

                # Out-of-enum value — clamp to first allowed
                replacement = allowed[0]
                node[field_key] = replacement
                change_log.append(
                    f"  [repair clamp] {xname}.{field_key}: "
                    f"{val!r} → {replacement!r} (out of enum)"
                )

            # ── RESTORE / ANNOTATE pass ─────────────────────────────────────
            # For each required field per activities_controls.json:
            #   - if present, leave alone
            #   - if missing AND has corpus default, restore
            #   - if missing AND has enum, restore with first enum value
            #   - if missing AND no deterministic source, annotate
            controls = controls_index.get(type_name, [])
            for control in controls:
                field_key = control.get("fieldKey", "")
                if not field_key:
                    continue
                if not control.get("required"):
                    continue
                if field_key in _EXCLUDED_REQUIRED_FIELDS:
                    continue
                if field_key in node and node[field_key] not in ("",):
                    # Field present and non-empty — nothing to do
                    continue
                if field_key in node and isinstance(node[field_key], str) and \
                   node[field_key].endswith("_value"):
                    # Unfilled template placeholder — treat as missing
                    pass
                elif field_key in node:
                    # Field present but empty — try to restore
                    pass

                # Try corpus default first
                default_val = field_defaults.get(type_name, {}).get(field_key)
                if default_val is not None:
                    node[field_key] = default_val
                    change_log.append(
                        f"  [repair restore] {xname}.{field_key}: "
                        f"-> {default_val!r} (from field_defaults.json)"
                    )
                    continue

                # Try first enum value
                allowed = _allowed_values_for(type_name, field_key, enum_values)
                if allowed:
                    node[field_key] = allowed[0]
                    change_log.append(
                        f"  [repair restore] {xname}.{field_key}: "
                        f"-> {allowed[0]!r} (first enum value)"
                    )
                    continue

                # No deterministic source — annotate
                field_name = control.get("fieldName", field_key)
                msg = (
                    f"UPDATE BEFORE RUNNING: '{field_key}' ({field_name}) is a "
                    f"required field for {type_name} but no deterministic default "
                    f"is available. Set this value in the platform UI before running."
                )
                _append_repair_note(node, msg)
                change_log.append(
                    f"  [repair annotate] {xname}.{field_key}: "
                    f"missing, no default — added UPDATE note"
                )

        # Recurse into nested dict values
        for key, value in list(node.items()):
            if isinstance(value, dict):
                repair_node(value, path=key)

    raw = workflow_json.get("workflow_raw_data", {})
    if isinstance(raw, dict):
        for xname, activity in raw.items():
            if isinstance(activity, dict):
                repair_node(activity, path=xname)

    return workflow_json, change_log