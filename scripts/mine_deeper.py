"""
mine_workflows.py — nesting-aware XML mining pass.

Run from the project root:
    python mine_workflows.py

Reads:  workflows_raw/xml/*.xml
Writes: data/activity_transitions_stratified.json
        data/field_value_corpus.json
        data/field_defaults.json  (updates existing file, fills in gaps)

File format: raw XOML files. Root element is SequentialWorkflowActivity.
Each activity uses a CLR namespace prefix: <ns0:GetWindowEventLogs .../>
ET resolves these to Clark notation: {clr-namespace:GetWindowEventLogs;...}GetWindowEventLogs
The CustomTypeName is the local part after the closing }.
No TotalExport wrapper. No Xoml attribute to unescape.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# REMOVED_SECRET
# Config
# REMOVED_SECRET

XML_DIR         = Path("workflows_raw/xml")
DATA_DIR        = Path("data")
OUT_TRANSITIONS = DATA_DIR / "activity_transitions_stratified.json"
OUT_CORPUS      = DATA_DIR / "field_value_corpus.json"
OUT_DEFAULTS    = DATA_DIR / "field_defaults.json"

# Structural containers — traversed but not added to activity sequence
_CONTAINERS = frozenset({
    "SequentialWorkflowActivity",
    "WhileActivity",
    "SequenceActivity",
    "IfElseActivity",
    "IfElseBranchActivity",
    "ParallelActivity",
    "UserGroup",
    "ForEachActivity",
})

# Attribute names to skip when building field corpus
_SKIP_ATTRS = frozenset({
    "xName", "CustomTypeName", "TypeName", "name", "DisplayName",
    "activityLicenseType", "id", "visible", "disabled", "isFavorite",
    "isJsonValid", "IsValid", "Timeout", "TimeInSeconds",
    "RecoveryMethodSelection", "TargetModuleID", "TargetModuleName",
    "Path", "label", "ClusterID", "ClusterName", "Pnumber",
    "readPermission", "writePermission", "modulePermissions",
    # x:Name, x:Class come through as local names after Clark stripping
    "Name", "Class",
})

_MIN_TRANSITION_COUNT       = 2
_MIN_OBSERVATIONS_DEFAULT   = 5
_DEFAULT_DOMINANCE          = 0.60

# REMOVED_SECRET
# Tag helpers
# REMOVED_SECRET

def _local(tag: str) -> str:
    """
    Get the CustomTypeName from an ET tag.

    ET represents namespaced tags in Clark notation:
      {clr-namespace:GetWindowEventLogs;Assembly=...}GetWindowEventLogs
    The local name is everything after the last '}'.

    For the workflow namespace tags (WhileActivity, IfElseActivity, etc.)
    the tag is already plain: WhileActivity
    """
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


# REMOVED_SECRET
# Context label
# REMOVED_SECRET

def _context_label(ancestors: list) -> str:
    """
    Assign one of 7 context labels based on the ancestor stack.
    Each item in ancestors: (local_tag, branch_index)
    """
    types = [a[0] for a in ancestors]
    has_while  = "WhileActivity" in types
    has_branch = "IfElseBranchActivity" in types

    if not has_while and not has_branch:
        return "linear"

    branch_idx = None
    for ct, bidx in reversed(ancestors):
        if ct == "IfElseBranchActivity":
            branch_idx = bidx
            break

    while_pos  = next((i for i, (ct, _) in enumerate(ancestors)
                       if ct == "WhileActivity"), None)
    branch_pos = next((i for i, (ct, _) in enumerate(ancestors)
                       if ct == "IfElseBranchActivity"), None)

    if has_while and has_branch:
        if while_pos is not None and branch_pos is not None and while_pos < branch_pos:
            return "while_ifelse_branch_1" if branch_idx == 0 else "while_ifelse_branch_2"

    if has_branch and not has_while:
        return "ifelse_branch_1" if branch_idx == 0 else "ifelse_branch_2"

    if has_while and not has_branch:
        if sum(1 for ct, _ in ancestors if ct == "WhileActivity") >= 2:
            return "nested_while"
        return "while_body"

    return "while_body"


# REMOVED_SECRET
# Variable reference helpers
# REMOVED_SECRET

_VAR_RE = re.compile(r'^%[^%]+%$')

def _is_var_ref(value: str) -> bool:
    return bool(_VAR_RE.match(value.strip()))

def _var_source(value: str) -> str | None:
    m = re.match(r'^%([^%]+)%$', value.strip())
    return m.group(1) if m else None


# REMOVED_SECRET
# Recursive XOML walker
# REMOVED_SECRET

def _walk(node: ET.Element, ancestors: list, seq: list) -> None:
    """
    Recursively walk a XOML element tree.
    Appends (CustomTypeName, context_label, field_dict) to seq for leaf activities.
    """
    ct = _local(node.tag)

    # ── Container: recurse without adding to sequence ─────────────────────────
    if ct == "SequentialWorkflowActivity":
        for child in node:
            _walk(child, ancestors, seq)
        return

    if ct == "SequenceActivity":
        for child in node:
            _walk(child, ancestors + [("SequenceActivity", 0)], seq)
        return

    if ct == "WhileActivity":
        for child in node:
            _walk(child, ancestors + [("WhileActivity", 0)], seq)
        return

    if ct == "IfElseActivity":
        for branch_idx, child in enumerate(node):
            child_ct = _local(child.tag)
            if child_ct == "IfElseBranchActivity":
                for grandchild in child:
                    _walk(grandchild,
                          ancestors + [("IfElseActivity", 0),
                                       ("IfElseBranchActivity", branch_idx)],
                          seq)
            else:
                # Shouldn't happen, but handle defensively
                _walk(child, ancestors + [("IfElseActivity", 0)], seq)
        return

    if ct in ("ParallelActivity", "UserGroup", "ForEachActivity"):
        for child in node:
            _walk(child, ancestors + [(ct, 0)], seq)
        return

    if ct == "IfElseBranchActivity":
        # Defensive: should be handled by IfElseActivity above
        for i, child in enumerate(node):
            _walk(child, ancestors + [(ct, i)], seq)
        return

    # ── Leaf activity ─────────────────────────────────────────────────────────
    context = _context_label(ancestors)
    fields: dict = {}

    for attr_name, attr_val in node.attrib.items():
        # Strip Clark namespace from attribute name
        attr_local = attr_name.rsplit("}", 1)[1] if "}" in attr_name else attr_name
        # Skip x:Name style (attr_local would be "Name" or "Class" after stripping)
        if attr_local in _SKIP_ATTRS:
            continue
        if not attr_val:
            continue
        fields[attr_local] = attr_val

    seq.append((ct, context, fields))

    # Recurse into any child elements (unusual for leaf activities but be thorough)
    for child in node:
        child_ct = _local(child.tag)
        if child_ct not in _CONTAINERS:
            _walk(child, ancestors + [(ct, 0)], seq)


# REMOVED_SECRET
# Per-workflow parse
# REMOVED_SECRET

_VALID_ROOTS = frozenset({
    "SequentialWorkflowActivity", "SequenceActivity",
    "Workflow", "WorkflowActivity",
})

def _parse_workflow(xml_path: Path) -> list | None:
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"  [skip] Parse error in {xml_path.name}: {e}")
        return None

    root_ct = _local(root.tag)
    if root_ct not in _VALID_ROOTS:
        print(f"  [skip] Unrecognised root '{root_ct}' in {xml_path.name}")
        return None

    activities: list = []
    _walk(root, [], activities)
    return activities


# REMOVED_SECRET
# Accumulate statistics
# REMOVED_SECRET

def _accumulate(activities: list, transitions: dict, field_corpus: dict) -> None:
    for i, (ct, context, fields) in enumerate(activities):
        # Transition: ct → next_ct in this context
        if i < len(activities) - 1:
            next_ct = activities[i + 1][0]
            ctx_map = transitions.setdefault(ct, {}).setdefault(context, {})
            ctx_map[next_ct] = ctx_map.get(next_ct, 0) + 1

        # Field values
        for field, value in fields.items():
            if not value or value.endswith("_value"):
                continue
            entry = (
                field_corpus
                .setdefault(ct, {})
                .setdefault(field, {})
                .setdefault(context, {
                    "total": 0,
                    "is_var_count": 0,
                    "literal_values": {},
                    "variable_ref_sources": {},
                })
            )
            entry["total"] += 1
            if _is_var_ref(value):
                entry["is_var_count"] += 1
                src = _var_source(value)
                if src:
                    entry["variable_ref_sources"][src] = \
                        entry["variable_ref_sources"].get(src, 0) + 1
            else:
                entry["literal_values"][value] = \
                    entry["literal_values"].get(value, 0) + 1


# REMOVED_SECRET
# Build output dicts
# REMOVED_SECRET

def _build_transitions(transitions: dict) -> dict:
    out: dict = {}
    for ct, contexts in transitions.items():
        out[ct] = {}
        for context, nexts in contexts.items():
            total = sum(nexts.values())
            if not total:
                continue
            entries = [
                {"next": nct, "count": cnt, "pct": round(100 * cnt / total)}
                for nct, cnt in sorted(nexts.items(), key=lambda x: -x[1])
                if cnt >= _MIN_TRANSITION_COUNT
            ]
            if entries:
                out[ct][context] = entries
        if not out[ct]:
            del out[ct]
    return out


def _build_corpus(field_corpus: dict) -> dict:
    out: dict = {}
    for ct, fields in field_corpus.items():
        out[ct] = {}
        for field, contexts in fields.items():
            out[ct][field] = {"by_context": {}}
            for context, data in contexts.items():
                total = data["total"]
                if not total:
                    continue
                out[ct][field]["by_context"][context] = {
                    "total_observations": total,
                    "is_variable_ref_pct": round(100 * data["is_var_count"] / total, 1),
                    "literal_values": sorted(
                        [{"value": v, "count": c, "pct": round(100 * c / total, 1)}
                         for v, c in data["literal_values"].items()],
                        key=lambda x: -x["count"]
                    ),
                    "variable_ref_patterns": sorted(
                        [{"source_activity": s, "count": c}
                         for s, c in data["variable_ref_sources"].items()],
                        key=lambda x: -x["count"]
                    ),
                }
        if not out[ct]:
            del out[ct]
    return out


def _build_defaults(field_corpus: dict, existing: dict) -> dict:
    result = {ct: dict(fields) for ct, fields in existing.items()}
    for ct, fields in field_corpus.items():
        for field, contexts in fields.items():
            if ct in result and field in result[ct]:
                continue
            total = sum(d["total"] for d in contexts.values())
            if total < _MIN_OBSERVATIONS_DEFAULT:
                continue
            merged: dict = {}
            for d in contexts.values():
                for val, cnt in d["literal_values"].items():
                    merged[val] = merged.get(val, 0) + cnt
            if not merged:
                continue
            best = max(merged, key=merged.get)
            if merged[best] / total >= _DEFAULT_DOMINANCE:
                result.setdefault(ct, {})[field] = best
    return result


# REMOVED_SECRET
# Review gate
# REMOVED_SECRET

def _review_gate(corpus_out: dict, existing_defaults: dict) -> None:
    print("\n" + "=" * 60)
    print("REVIEW GATE — answer these before writing Tier 1-3 code")
    print("=" * 60)

    # Q1
    print("\n1. Fields with stable literal distributions (>80% one value, 10+ obs):")
    stable = []
    for ct, fields in corpus_out.items():
        for field, fdata in fields.items():
            for context, cdata in fdata["by_context"].items():
                if cdata["total_observations"] >= 10 and cdata["literal_values"]:
                    top = cdata["literal_values"][0]
                    if top["pct"] >= 80:
                        stable.append(
                            f"  {ct}.{field} [{context}]: "
                            f"'{top['value']}' {top['pct']}%"
                            f" ({cdata['total_observations']} obs)"
                        )
    for line in sorted(stable)[:40]:
        print(line)
    if len(stable) > 40:
        print(f"  ... and {len(stable)-40} more (see field_value_corpus.json)")

    # Q2
    print("\n2. Fields almost always variable references (>80%, 10+ obs):")
    var_heavy = []
    for ct, fields in corpus_out.items():
        for field, fdata in fields.items():
            for context, cdata in fdata["by_context"].items():
                if (cdata["total_observations"] >= 10 and
                        cdata["is_variable_ref_pct"] >= 80):
                    var_heavy.append(
                        f"  {ct}.{field} [{context}]: "
                        f"{cdata['is_variable_ref_pct']}% var refs"
                        f" ({cdata['total_observations']} obs)"
                    )
    for line in sorted(var_heavy)[:25]:
        print(line)
    if len(var_heavy) > 25:
        print(f"  ... and {len(var_heavy)-25} more")

    # Q3
    print("\n3. Fields where while_body vs linear top value differs:")
    ctx_sensitive = []
    for ct, fields in corpus_out.items():
        for field, fdata in fields.items():
            ctxs = fdata["by_context"]
            if "linear" in ctxs and "while_body" in ctxs:
                lin = ctxs["linear"]
                whl = ctxs["while_body"]
                if (lin["literal_values"] and whl["literal_values"] and
                        lin["total_observations"] >= 5 and
                        whl["total_observations"] >= 5):
                    lv = lin["literal_values"][0]["value"]
                    wv = whl["literal_values"][0]["value"]
                    if lv != wv:
                        ctx_sensitive.append(
                            f"  {ct}.{field}: linear='{lv}', while_body='{wv}'"
                        )
    for line in ctx_sensitive[:15]:
        print(line)
    if not ctx_sensitive:
        print("  (none — context does not shift literal distributions)")

    # Q4
    print("\n4. Conflicts with existing field_defaults.json:")
    conflicts = []
    for ct, fields in existing_defaults.items():
        if ct not in corpus_out:
            continue
        for field, ex_val in fields.items():
            if field not in corpus_out[ct]:
                continue
            merged: dict = {}
            total = 0
            for cdata in corpus_out[ct][field]["by_context"].values():
                total += cdata["total_observations"]
                for lv in cdata["literal_values"]:
                    merged[lv["value"]] = merged.get(lv["value"], 0) + lv["count"]
            if not merged or total < _MIN_OBSERVATIONS_DEFAULT:
                continue
            best = max(merged, key=merged.get)
            if best != ex_val:
                conflicts.append(
                    f"  {ct}.{field}: defaults='{ex_val}', "
                    f"corpus='{best}' ({merged[best]}/{total} obs)"
                )
    for line in conflicts:
        print(line)
    if not conflicts:
        print("  (no conflicts)")

    # Q5
    print("\n5. GetCellValue.ColumnNumber distribution (ColumnNumber bug impact):")
    gcv = corpus_out.get("GetCellValue", {}).get("ColumnNumber")
    if gcv:
        for context, cdata in gcv["by_context"].items():
            total = cdata["total_observations"]
            numeric = sum(lv["count"] for lv in cdata["literal_values"]
                          if lv["value"].isdigit())
            print(f"  [{context}] {total} obs, "
                  f"{cdata['is_variable_ref_pct']}% var refs, "
                  f"numeric: {numeric}/{total} "
                  f"({round(100*numeric/total) if total else 0}%)")
            for lv in cdata["literal_values"][:8]:
                print(f"    '{lv['value']}': {lv['count']} ({lv['pct']}%)")
    else:
        print("  GetCellValue.ColumnNumber not found in corpus")


# REMOVED_SECRET
# Main
# REMOVED_SECRET

def main() -> None:
    xml_files = sorted(XML_DIR.glob("*.xml"))
    if not xml_files:
        print(f"[mine] No XML files found in {XML_DIR}")
        return
    print(f"[mine] Found {len(xml_files)} XML files in {XML_DIR}")

    transitions:  dict = {}
    field_corpus: dict = {}
    parsed = skipped = 0

    for i, xml_path in enumerate(xml_files, 1):
        if i % 50 == 0 or i == len(xml_files):
            print(f"  [{i}/{len(xml_files)}] {xml_path.name}")
        activities = _parse_workflow(xml_path)
        if activities is None:
            skipped += 1
            continue
        _accumulate(activities, transitions, field_corpus)
        parsed += 1

    print(f"\n[mine] Parsed {parsed} workflows, skipped {skipped}")

    transitions_out = _build_transitions(transitions)
    with open(OUT_TRANSITIONS, "w", encoding="utf-8") as f:
        json.dump(transitions_out, f, indent=2)
    print(f"[mine] Wrote {OUT_TRANSITIONS}  ({len(transitions_out)} activity types)")

    corpus_out = _build_corpus(field_corpus)
    with open(OUT_CORPUS, "w", encoding="utf-8") as f:
        json.dump(corpus_out, f, indent=2)
    print(f"[mine] Wrote {OUT_CORPUS}  ({len(corpus_out)} activity types)")

    existing_defaults: dict = {}
    if OUT_DEFAULTS.exists():
        with open(OUT_DEFAULTS, encoding="utf-8") as f:
            existing_defaults = json.load(f)
        print(f"[mine] Loaded field_defaults.json  "
              f"({sum(len(v) for v in existing_defaults.values())} existing entries)")

    updated = _build_defaults(field_corpus, existing_defaults)
    with open(OUT_DEFAULTS, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2)
    new_n = (sum(len(v) for v in updated.values()) -
             sum(len(v) for v in existing_defaults.values()))
    print(f"[mine] Wrote {OUT_DEFAULTS}  ({new_n} new entries added)")

    _review_gate(corpus_out, existing_defaults)
    print("\n[mine] Done.")


if __name__ == "__main__":
    main()