"""
tools/visualize.py

Stage 8 — Mermaid diagram generation. Runs after Stage 7 (output) in the
pipeline. Produces a sibling .mmd file alongside the workflow JSON.

INPUT:  workflow_json (the wired+validated workflow dict)
        decomposition  (Decomposer step list — used as task-bucket fallback)
OUTPUT: Mermaid flowchart string; written to <json_path>.mmd

DESIGN
  - Top-level activities → flowchart nodes connected in workflow_raw_data
    insertion order
  - WhileActivity → Mermaid subgraph wrapping its loop body, with a
    back-edge from the last body activity to the While node
  - IfElseActivity → splits flow into branch edges labeled with a short
    condition derived from each branch's ReturnValue.Value (lowercase),
    falling back to "if" / "else"
  - SequenceActivity, ReturnValue, Advanced, Result, ErrorHandling are
    structural metadata — skipped as nodes
  - Task subgraphs group activities by task_id resolved via
    data/task_taxonomy.json's activities[].activity → task_id reverse
    index. Containers (WhileActivity, IfElseActivity) are bucketed
    structurally to iterate_rows / branch_decision so the visual matches
    the user's mental model.
  - Activities with no taxonomy entry get bucketed by structural role
    (loop scaffolding → iterate_rows; branch scaffolding → branch_decision)
    or fall through to "other".

WHY ACTIVITY-NAME LOOKUP, NOT PHRASE MATCHING
  task_taxonomy.json already declares which activities belong to which
  task. The Decomposer's step descriptions and the matcher's
  prompt-level phrase matching are both noisier signals than this
  explicit mapping. Phrase matching is reserved as a fallback for
  activities the taxonomy doesn't list.

NEVER FATAL
  Any failure during render returns an empty string and logs to stderr.
  pipeline.py catches and continues — viz is supplementary, never blocks
  the workflow JSON output.

ENV
  DATA_DIR — root for data files. Defaults to "/app/data".

USAGE
  from tools.visualize import write_mermaid
  mmd_path = write_mermaid(workflow_json, decomposition, json_path)
  # mmd_path == "json_files/MyWorkflow_123.mmd" (or "" on failure)

SMOKE TEST
  python -m tools.visualize
"""

import json
import os
import re
import sys
from pathlib import Path


DATA_DIR      = Path(os.getenv("DATA_DIR", "/app/data"))
TAXONOMY_PATH = DATA_DIR / "task_taxonomy.json"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CustomTypeNames that are structural / metadata and should not appear as
# nodes in the diagram. Their contents are still walked.
_STRUCTURAL_TYPES = {
    "Advanced",
    "Result",
    "ErrorHandling",
    "ReturnValue",
    "SequenceActivity",
}

# Container activities — their bodies are walked and rendered as subgraphs
# or branches, but the containers themselves DO appear as nodes (the loop
# anchor for While, the decision diamond for IfElse).
_CONTAINER_TYPES = {
    "WhileActivity",
    "IfElseActivity",
    "IfElseBranchActivity",
}

# Structural fallback bucketing for containers and loop scaffolding when
# the taxonomy doesn't list them explicitly.
_STRUCTURAL_TASK_BUCKET = {
    "WhileActivity":  "iterate_rows",
    "GetRowsCount":   "iterate_rows",
    "ExitWhile":      "iterate_rows",
    "IfElseActivity": "branch_decision",
}

_NODE_LABEL_MAX = 50          # truncate longer activity descriptions
_BRANCH_LABEL_MAX = 20        # truncate longer branch condition labels
_UNASSIGNED_TASK = "other"    # bucket label for activities with no task

# Mermaid node shapes per role. Mermaid syntax:
#   id["label"]   rectangle (default)
#   id(("label")) circle
#   id{"label"}   diamond
#   id>"label"]   asymmetric (used for I/O)
_SHAPE_BY_ROLE = {
    "while":   ('[["',  '"]]'),    # subroutine shape — loop anchor
    "ifelse":  ('{"',   '"}'),     # diamond — decision
    "default": ('["',   '"]'),     # rectangle — normal step
}


# ---------------------------------------------------------------------------
# Module-level state — taxonomy loaded once
# ---------------------------------------------------------------------------

_taxonomy_loaded:        bool = False
_activity_to_task:       dict[str, str] = {}     # CustomTypeName → task_id
_task_label:             dict[str, str] = {}     # task_id → human label
_task_order:             list[str] = []          # canonical task ordering


def _load_taxonomy() -> None:
    """Loads task_taxonomy.json once. Builds activity→task reverse index
    plus a task_id→label map for subgraph headers. Graceful on failure:
    visualize still works without taxonomy, just with no task subgraphs."""
    global _taxonomy_loaded, _activity_to_task, _task_label, _task_order
    if _taxonomy_loaded:
        return
    _taxonomy_loaded = True
    try:
        with open(TAXONOMY_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"[visualize] WARNING: taxonomy not loadable at {TAXONOMY_PATH}: "
              f"{e}. Subgraphs will be disabled.", file=sys.stderr)
        return

    for entry in doc.get("atomic_tasks", []):
        task_id = entry.get("task_id")
        if not task_id:
            continue
        _task_label[task_id] = entry.get("label", task_id)
        _task_order.append(task_id)
        for act in entry.get("activities", []):
            name = act.get("activity")
            if name and name not in _activity_to_task:
                _activity_to_task[name] = task_id

    # Add the "other" bucket for activities not in the taxonomy
    _task_label[_UNASSIGNED_TASK] = "Other / scaffolding"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_id(xname: str) -> str:
    """Mermaid node IDs accept letters, digits, underscores, and a few
    others, but it's safest to strip to alphanum + underscore."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", str(xname or ""))
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "n_" + cleaned
    return cleaned


def _escape_label(text: str) -> str:
    """Mermaid labels in double quotes — escape inner double quotes and
    strip newlines. Also truncate runaway descriptions."""
    if text is None:
        return ""
    s = str(text).replace('"', "'").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _node_label(activity: dict) -> str:
    """Prefer human Description, fall back to xName."""
    desc = activity.get("Description") or activity.get("description") or ""
    desc = _escape_label(desc)
    if desc:
        return _truncate(desc, _NODE_LABEL_MAX)
    return _escape_label(activity.get("xName", "?"))


def _node_shape(custom_type: str) -> tuple[str, str]:
    if custom_type == "WhileActivity":
        return _SHAPE_BY_ROLE["while"]
    if custom_type == "IfElseActivity":
        return _SHAPE_BY_ROLE["ifelse"]
    return _SHAPE_BY_ROLE["default"]


def _resolve_task(custom_type: str) -> str:
    """Returns the task_id this CustomTypeName belongs to. Falls through:
    taxonomy lookup → structural bucket → "other"."""
    if custom_type in _activity_to_task:
        return _activity_to_task[custom_type]
    if custom_type in _STRUCTURAL_TASK_BUCKET:
        return _STRUCTURAL_TASK_BUCKET[custom_type]
    return _UNASSIGNED_TASK


def _is_activity_dict(value) -> bool:
    """An activity dict has a CustomTypeName. Sub-objects like Advanced
    do too, but those are filtered separately by _STRUCTURAL_TYPES."""
    return isinstance(value, dict) and "CustomTypeName" in value


def _branch_label(branch_dict: dict, fallback: str) -> str:
    """Extracts a short condition label from a branch's ReturnValue.Value.
    Walks the branch's children to find the first ReturnValue sub-object."""
    for value in branch_dict.values():
        if (isinstance(value, dict)
                and value.get("CustomTypeName") == "ReturnValue"):
            v = value.get("Value")
            if isinstance(v, str) and v.strip():
                return _truncate(_escape_label(v.lower()), _BRANCH_LABEL_MAX)
    return fallback


# ---------------------------------------------------------------------------
# Walker — extracts the diagram structure from workflow_raw_data
# ---------------------------------------------------------------------------

class _Node:
    """Lightweight render-time representation of a flowchart node."""
    __slots__ = ("xname", "node_id", "custom_type", "label", "task_id")

    def __init__(self, xname: str, custom_type: str, label: str, task_id: str):
        self.xname       = xname
        self.node_id     = _sanitize_id(xname)
        self.custom_type = custom_type
        self.label       = label
        self.task_id     = task_id


class _Edge:
    """Directed edge between two node IDs, optionally labeled."""
    __slots__ = ("src", "dst", "label")

    def __init__(self, src: str, dst: str, label: str | None = None):
        self.src   = src
        self.dst   = dst
        self.label = label


def _ordered_activities(parent: dict) -> list[tuple[str, dict]]:
    """Returns (xname, activity_dict) pairs in dict-insertion order,
    skipping structural metadata types."""
    out = []
    for key, value in parent.items():
        if not _is_activity_dict(value):
            continue
        ct = value.get("CustomTypeName")
        if ct in _STRUCTURAL_TYPES:
            continue
        out.append((key, value))
    return out


def _walk_sequence(
    activities: list[tuple[str, dict]],
    nodes:      list[_Node],
    edges:      list[_Edge],
    subgraphs:  dict[str, list[str]],
    prev_id:    str | None = None,
    inside_loop: bool = False,
) -> tuple[str | None, list[str]]:
    """
    Walks a linear sequence of activities, registering nodes / edges /
    subgraph membership.

    Returns a tuple (last_id, dangling_branch_tails):
      - last_id: node_id of the last activity in the sequence, or None
      - dangling_branch_tails: branch tails from a terminal IfElse that
        the caller must converge to wherever this sequence connects next
        (e.g. the loop anchor when this is a loop body)

    Containers (While, IfElse) recurse into their children. Branch tails
    inside the sequence converge at the next sequence anchor; tails at
    the very end of the sequence are returned for the caller to handle.
    """
    last_id = prev_id

    # Tails per IfElse node — populated when an IfElse is processed,
    # consumed when we discover the next sequence node.
    pending_branch_tails: list[str] = []

    def _connect_pending_to(target_id: str) -> None:
        nonlocal pending_branch_tails
        for tail in pending_branch_tails:
            if tail != target_id:
                edges.append(_Edge(tail, target_id))
        pending_branch_tails = []

    for xname, act in activities:
        ct = act.get("CustomTypeName", "")
        node = _Node(
            xname=xname,
            custom_type=ct,
            label=_node_label(act),
            task_id=_resolve_task(ct),
        )
        nodes.append(node)
        subgraphs.setdefault(node.task_id, []).append(node.node_id)

        # Pending branch tails from a previous IfElse converge HERE.
        if pending_branch_tails:
            _connect_pending_to(node.node_id)

        if last_id is not None:
            edges.append(_Edge(last_id, node.node_id))

        if ct == "WhileActivity":
            body_parent = None
            for child in act.values():
                if (isinstance(child, dict)
                        and child.get("CustomTypeName") == "SequenceActivity"):
                    body_parent = child
                    break
            if body_parent:
                body_acts = _ordered_activities(body_parent)
                body_last, body_tails = _walk_sequence(
                    body_acts, nodes, edges, subgraphs,
                    prev_id=node.node_id,
                    inside_loop=True,
                )
                # Loop back-edges: branch tails (if any) loop directly,
                # otherwise the body's last sequential activity loops.
                # Avoid double-edging by skipping the body_last edge when
                # body_last is the IfElse anchor whose branches are
                # already looping back.
                if body_tails:
                    for tail in body_tails:
                        if tail != node.node_id:
                            edges.append(_Edge(tail, node.node_id, label="loop"))
                elif body_last and body_last != node.node_id:
                    edges.append(_Edge(body_last, node.node_id, label="loop"))
            last_id = node.node_id

        elif ct == "IfElseActivity":
            branch_idx = 0
            branch_tails: list[str] = []
            for child_xname, child in act.items():
                if not isinstance(child, dict):
                    continue
                if child.get("CustomTypeName") != "IfElseBranchActivity":
                    continue
                branch_idx += 1
                fallback = "if" if branch_idx == 1 else "else"
                label = _branch_label(child, fallback)
                branch_acts = _ordered_activities(child)
                if not branch_acts:
                    # Empty branch — only emit a stub when we're inside
                    # a loop (the stub anchors the labeled edge that
                    # converges back to the loop). Outside a loop, an
                    # empty branch is a dead end and we just emit a
                    # labeled edge to a passthrough sink.
                    stub_id = f"{node.node_id}_branch{branch_idx}_empty"
                    nodes.append(_Node(
                        xname=stub_id, custom_type="_stub",
                        label="(no action)",
                        task_id=_UNASSIGNED_TASK,
                    ))
                    subgraphs.setdefault(_UNASSIGNED_TASK, []).append(stub_id)
                    edges.append(_Edge(node.node_id, stub_id, label=label))
                    branch_tails.append(stub_id)
                    continue
                first_xname, _first_act = branch_acts[0]
                first_node_id = _sanitize_id(first_xname)
                edges.append(_Edge(node.node_id, first_node_id, label=label))
                branch_last, inner_tails = _walk_sequence(
                    branch_acts, nodes, edges, subgraphs,
                    prev_id=None,
                    inside_loop=inside_loop,
                )
                if branch_last:
                    branch_tails.append(branch_last)
                # Nested IfElse tails inside this branch propagate upward
                branch_tails.extend(inner_tails)
            pending_branch_tails = branch_tails
            last_id = node.node_id

        else:
            last_id = node.node_id

    # End of sequence — return any unresolved branch tails so the caller
    # can converge them appropriately (loop back-edge, or dead-end if
    # this sequence was top-level).
    return last_id, pending_branch_tails


# ---------------------------------------------------------------------------
# Mermaid emission
# ---------------------------------------------------------------------------

def _emit_mermaid(
    nodes:     list[_Node],
    edges:     list[_Edge],
    subgraphs: dict[str, list[str]],
) -> str:
    """Assembles the final Mermaid flowchart text from collected pieces."""
    lines = ["flowchart TD"]

    # Define each node with its shape + label
    for n in nodes:
        open_, close_ = _node_shape(n.custom_type)
        lines.append(f"    {n.node_id}{open_}{n.label}{close_}")

    lines.append("")

    # Emit edges
    for e in edges:
        if e.label:
            lines.append(f'    {e.src} -->|"{e.label}"| {e.dst}')
        else:
            lines.append(f"    {e.src} --> {e.dst}")

    # Task subgraphs — emit only those with at least one node, in canonical
    # taxonomy order, with "other" last
    if subgraphs:
        lines.append("")
        ordered_task_ids = [t for t in _task_order if t in subgraphs]
        if _UNASSIGNED_TASK in subgraphs:
            ordered_task_ids.append(_UNASSIGNED_TASK)
        # Tasks present but not in _task_order (e.g. taxonomy not loaded)
        # get appended in dict order
        for t in subgraphs:
            if t not in ordered_task_ids:
                ordered_task_ids.append(t)

        for task_id in ordered_task_ids:
            members = subgraphs[task_id]
            label = _task_label.get(task_id, task_id)
            sg_id = f"task_{_sanitize_id(task_id)}"
            lines.append(f'    subgraph {sg_id}["{_escape_label(label)}"]')
            for member_id in members:
                lines.append(f"        {member_id}")
            lines.append("    end")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_mermaid(workflow_json: dict, decomposition: dict | None = None) -> str:
    """
    Renders a workflow_json to a Mermaid flowchart string.

    decomposition is currently unused (the activity→task resolution
    relies on the taxonomy reverse index, which is more reliable than
    Decomposer-step phrase overlap). Kept as a parameter so callers can
    pass it without breaking when a future fallback path needs it.

    Returns "" on any failure rather than raising — viz must never block
    the pipeline.
    """
    try:
        _load_taxonomy()
        if not isinstance(workflow_json, dict):
            print(f"[visualize] WARNING: workflow_json is not a dict "
                  f"(type={type(workflow_json).__name__}); skipping",
                  file=sys.stderr)
            return ""

        raw = workflow_json.get("workflow_raw_data", workflow_json)
        if not isinstance(raw, dict) or not raw:
            print("[visualize] WARNING: workflow_raw_data is empty; "
                  "skipping", file=sys.stderr)
            return ""

        nodes:     list[_Node] = []
        edges:     list[_Edge] = []
        subgraphs: dict[str, list[str]] = {}

        top_acts = _ordered_activities(raw)
        if not top_acts:
            print("[visualize] WARNING: no top-level activities found; "
                  "skipping", file=sys.stderr)
            return ""

        _walk_sequence(top_acts, nodes, edges, subgraphs,
                       prev_id=None, inside_loop=False)
        return _emit_mermaid(nodes, edges, subgraphs)

    except Exception as e:
        print(f"[visualize] ERROR during render: {e}", file=sys.stderr)
        return ""


def write_mermaid(
    workflow_json: dict,
    decomposition: dict | None,
    json_path:     str,
) -> str:
    """
    Renders and writes the .mmd file alongside the JSON. Returns the
    path of the written .mmd file, or "" on failure.

    json_path is the absolute or relative path of the workflow JSON
    that was just written by Stage 7. The .mmd is written as a sibling
    with the same basename.
    """
    mmd = render_mermaid(workflow_json, decomposition)
    if not mmd:
        return ""
    try:
        json_p = Path(json_path)
        mmd_path = json_p.with_suffix(".mmd")
        mmd_path.write_text(mmd, encoding="utf-8")
        return str(mmd_path)
    except Exception as e:
        print(f"[visualize] ERROR writing .mmd file: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """Exercises render_mermaid against a synthetic workflow that mirrors
    the canonical 'ping servers' shape: GetRowsCount → While(Seq(ExitWhile,
    GetCellValue, Ping, IfElse(Branch(success), Branch(failure → SendEmail))))."""

    print("[smoke] DATA_DIR =", DATA_DIR.resolve())
    print("[smoke] taxonomy =", TAXONOMY_PATH.resolve())
    print()

    sample = {
        "workflow_raw_data": {
            "getRowsCount1": {
                "xName": "getRowsCount1",
                "CustomTypeName": "GetRowsCount",
                "Description": "Get the total number of rows in the serverTable.",
            },
            "whileActivity1": {
                "xName": "whileActivity1",
                "CustomTypeName": "WhileActivity",
                "Description": "Loop through each server in the serverTable.",
                "sequenceActivity1": {
                    "xName": "sequenceActivity1",
                    "CustomTypeName": "SequenceActivity",
                    "exitWhile1": {
                        "xName": "exitWhile1",
                        "CustomTypeName": "ExitWhile",
                        "Description": "Exit the loop after processing all servers.",
                    },
                    "getCellValue1": {
                        "xName": "getCellValue1",
                        "CustomTypeName": "GetCellValue",
                        "Description": "Get the server name from the current row.",
                    },
                    "ping1": {
                        "xName": "ping1",
                        "CustomTypeName": "Ping",
                        "Description": "Ping the current server.",
                    },
                    "ifElseActivity1": {
                        "xName": "ifElseActivity1",
                        "CustomTypeName": "IfElseActivity",
                        "Description": "Check if the ping was successful.",
                        "ifElseBranchActivity1": {
                            "xName": "ifElseBranchActivity1",
                            "CustomTypeName": "IfElseBranchActivity",
                            "returnValue1": {
                                "xName": "returnValue1",
                                "CustomTypeName": "ReturnValue",
                                "Value": "Success",
                            },
                        },
                        "ifElseBranchActivity2": {
                            "xName": "ifElseBranchActivity2",
                            "CustomTypeName": "IfElseBranchActivity",
                            "returnValue2": {
                                "xName": "returnValue2",
                                "CustomTypeName": "ReturnValue",
                                "Value": "Failure",
                            },
                            "sendEmail1": {
                                "xName": "sendEmail1",
                                "CustomTypeName": "SendEmail",
                                "Description": "Send an email to the administrator.",
                            },
                        },
                    },
                },
            },
        },
    }

    failures = 0

    mmd = render_mermaid(sample)
    if not mmd:
        print("  [FAIL] render returned empty string")
        return 1

    print("  [smoke] rendered output:")
    print()
    for line in mmd.splitlines():
        print(f"    {line}")
    print()

    # Structural assertions
    checks = [
        ("flowchart TD",            "header present"),
        ("getRowsCount1[",          "GetRowsCount node present"),
        ("whileActivity1[[",        "While renders as subroutine shape"),
        ("ifElseActivity1{",        "IfElse renders as diamond shape"),
        ("|\"success\"|",           "success branch label extracted"),
        ("|\"failure\"|",           "failure branch label extracted"),
        ("|\"loop\"|",              "loop back-edge present"),
        ("subgraph task_iterate_rows",   "iterate_rows subgraph present"),
        ("subgraph task_query_system_state", "query_system_state subgraph present"),
        ("subgraph task_send_email", "send_email subgraph present"),
        ("subgraph task_branch_decision", "branch_decision subgraph present"),
    ]
    for needle, label in checks:
        if needle in mmd:
            print(f"  [PASS] {label}")
        else:
            failures += 1
            print(f"  [FAIL] {label} (missing: {needle!r})")

    # Edge case: empty workflow
    empty_result = render_mermaid({"workflow_raw_data": {}})
    if empty_result == "":
        print("  [PASS] empty workflow returns empty string")
    else:
        failures += 1
        print(f"  [FAIL] empty workflow expected empty string, got {empty_result!r}")

    # Edge case: bad input
    bad_result = render_mermaid("not a dict")  # type: ignore[arg-type]
    if bad_result == "":
        print("  [PASS] non-dict input returns empty string")
    else:
        failures += 1
        print(f"  [FAIL] non-dict input expected empty string")

    print()
    total = len(checks) + 2
    print(f"[smoke] {total - failures}/{total} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(_smoke_test())