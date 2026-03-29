#!/usr/bin/env python3
"""
Apply Fix 2 to tools/validation_tools.py:
Add variable_contracts.variables to valid_vars in validate_variable_references.
Run from repo root: python3 patch_validation_tools.py
"""
import os

path = os.path.join("tools", "validation_tools.py")
with open(path, "r") as f:
    content = f.read()

old = """    for activity in raw.values():
        if isinstance(activity, dict):
            collect_vars(activity)

    # ── Pass 2: check all %ref% values against valid_vars ────────────────────"""

new = """    for activity in raw.values():
        if isinstance(activity, dict):
            collect_vars(activity)

    # Also collect from variable_contracts.variables — Wirer's Children format
    # records all variable names here even when individual activities are missing
    # their field values (e.g. CreateMemoryTable.TableName not set by Wirer).
    # This prevents r5 false positives on legitimate table variable references.
    _vc = workflow_json.get("variable_contracts", {})
    if isinstance(_vc, str):
        try:
            import json as _j; _vc = _j.loads(_vc)
        except Exception:
            _vc = {}
    for _var in _vc.get("variables", []):
        _vname = _var.get("name", "").strip()
        if _vname:
            valid_vars.add(_vname)

    # ── Pass 2: check all %ref% values against valid_vars ────────────────────"""

assert old in content, f"Target block not found in {path}"
assert content.count(old) == 1, "Multiple matches found"
content = content.replace(old, new, 1)

with open(path, "w") as f:
    f.write(content)
print(f"Patched {path} — added variable_contracts collection to validate_variable_references")