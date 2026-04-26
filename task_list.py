#!/usr/bin/env python3
"""
mine_task_taxonomy.py
─────────────────────
First-pass task taxonomy miner. Reads the corpus and produces a reviewable
draft of the task taxonomy that you can then hand-edit into the final form.

Pipeline:
  1. Extract leaf-activity sequences from ./workflows_raw/xml
  2. Group activities into functional families (Read, Iterate, Notify, etc.)
     using a seed classifier + corpus co-occurrence
  3. Identify task candidates — recurring, cohesive activity clusters
  4. Pair each task with the existing scaffold from pattern_library.json
     when structure maps cleanly (gives us Variant C patterns for free)
  5. Emit draft files:
       data/task_taxonomy_draft.json       — the A+B+C taxonomy, editable
       data/task_match_phrases_draft.json  — prompt phrases per task
       task_mining_report.md               — human-readable summary for review

Usage:
    python mine_task_taxonomy.py
    python mine_task_taxonomy.py --xml-dir ./workflows_raw/xml
    python mine_task_taxonomy.py --min-support 5 --dry-run

After running, review the report and edit task_taxonomy_draft.json by hand.
Then rename the draft file to remove _draft and wire it into the pipeline.

Requires: Python stdlib only.
"""

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET


# ─────────────────────────────────────────────────────────────────────────────
# Repo-relative defaults — match the conventions in mine_corpus.py
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT          = Path(__file__).resolve().parent
_DEFAULT_XML_DIR    = _REPO_ROOT / "workflows_raw" / "xml"
_DEFAULT_DATA_DIR   = _REPO_ROOT / "data"
_DEFAULT_PATTERN_LIB = _DEFAULT_DATA_DIR / "patterns" / "pattern_library.json"
_DEFAULT_CATEGORIES = _DEFAULT_DATA_DIR / "activity_categories.json"


# ─────────────────────────────────────────────────────────────────────────────
# SEED CLASSIFICATION — functional families of activities
# ─────────────────────────────────────────────────────────────────────────────
#
# This is the "manual curation" starting point. Each entry maps a family
# label to a list of activity names. These are first-pass groupings based
# on what each activity *accomplishes*, not what platform category it's in.
#
# Activities NOT in this map are classified as "uncategorized" and surface
# in the mining report for manual review.
#
# Edit this dict to improve the taxonomy. It's the single highest-leverage
# knob in the whole script.

ACTIVITY_FAMILIES = {
    # ── DATA INPUT ──────────────────────────────────────────────────────────
    "read_file": [
        "ReadXLS", "ReadExcel", "ReadCSV", "ReadFile", "ReadTextFile",
        "ReadXML", "ParseJson", "ParseJSON", "JsonToTable", "JSONtoTable",
        "JSONtoTableAdvanced", "XmlToTable", "XMLtoTable",
        "ConvertTextToTable",
    ],
    "query_database": [
        "TSQLQuery", "TSQLStatement", "MySQLQuery", "OracleQuery", "DB2Query",
        "SQLQuery", "ExecuteSQLStatement",
    ],
    "query_api": [
        "HTTPRequest", "HttpRequest", "WebServiceCall", "RESTRequest",
        "CrawlWebsiteExtractText",
    ],
    "query_itsm": [
        "SNGetRecord", "SNGetRecords", "SNUpdateRecord", "SNCreateRecord",
        "MFSMAXListTickets", "MFSMAXGetTicket", "MFSMAXAddComment",
        "JiraGetIssue", "JiraSearch", "ZendeskGetTicket",
    ],
    "query_directory": [
        "ADUserExists", "ADGetUser", "ADListGroup", "ADListUsers",
        "ADGetGroupMembers",
    ],
    "query_system_state": [
        "WMIQuery", "ServiceStatus", "ServiceList", "ProcessList",
        "GetInstalledSoftware", "FileExist", "FolderExist", "FolderExistRemote",
        "FileExistRemote", "Ping", "TraceRoute", "CPU", "Memory",
    ],
    "read_structured_input": [
        "FTPListFiles", "FolderList", "ListFolder", "StartJsonSession",
        "StartXMLSession",
    ],

    # ── TABLE/DATA MANIPULATION ────────────────────────────────────────────
    "create_table": [
        "CreateMemoryTable",
    ],
    "filter_table": [
        "ResultSetFilter", "RemoveEmptyRowsAndColumnsFromTable",
        "MemoryTableGetUniqueRows",
    ],
    "sort_table": [
        "SortTable",
    ],
    "count_table_rows": [
        "GetRowsCount", "GetColumnsCount",
    ],
    "read_table_cell": [
        "GetCellValue", "GetRows", "GetColumns", "GetColumnName",
    ],
    "modify_table": [
        "SetCellValue", "AddMemoryTableRow", "DeleteMemoryTableRows",
        "MemoryTableUnion", "RotateTable",
    ],
    "convert_table": [
        "ConvertToHTMLTable", "ConvertTableToJSON", "TabletoXML",
        "DatatableifyHTML",
    ],

    # ── VARIABLES ──────────────────────────────────────────────────────────
    "set_variable": [
        "MemorySet", "MultiMemorySet",
    ],
    "clean_memory": [
        "MemoryClean",
    ],

    # ── FLOW CONTROL ───────────────────────────────────────────────────────
    "iterate_rows": [
        "WhileActivity", "ExitWhile",
    ],
    "branch_decision": [
        "IfElseActivity", "IfElseBranchActivity", "ReturnValue",
    ],
    "parallel_execution": [
        "ParallelActivity", "UserGroup",
    ],
    "sequence_control": [
        "SequenceActivity", "SequentialWorkflowActivity",
    ],
    "terminate": [
        "TerminateWorkflow", "ExitWhile",
    ],
    "goto": [
        "GoTo", "Continue",
    ],
    "wait": [
        "Wait",
    ],
    "invoke_subworkflow": [
        "RunWorkflow", "WorkflowCounter",
    ],
    "lock": [
        "LockExecutor",
    ],

    # ── NOTIFICATION / OUTPUT ──────────────────────────────────────────────
    "send_email": [
        "SendEmail", "SMTPSendEmail", "SendSMTPEmail",
    ],
    "send_notification": [
        "NewEvent", "DisplayIncident", "SelfServiceResponse",
    ],
    "display_value": [
        "DisplayValue", "DisplayMultiValue",
    ],
    "write_file": [
        "WriteFile", "WriteXLS", "WriteCSV", "FTPDownloadFile", "FTPDeleteFile",
    ],

    # ── STRING / DATA OPS ──────────────────────────────────────────────────
    "date_operations": [
        "GetDate", "DateDifference", "AddDate", "GetUNIXTimestamp",
    ],
    "string_operations": [
        "Split", "SubString", "Length", "Contains", "IsEmpty", "IndexOf",
        "ReplaceString", "UcFirst", "Trim", "ConvertHtmlToPlainText",
    ],
    "math_operations": [
        "FunctionCalculator", "RandomNumberGenerator",
    ],
    "password_operations": [
        "PasswordGenerator", "ConvertPasswordToPlaintext",
    ],

    # ── SCRIPTING ──────────────────────────────────────────────────────────
    "run_script": [
        "PowerShell", "PowerShellScript", "Executor", "Command",
        "RunCommand", "CommandLine",
    ],

    # ── AD / USER MGMT ─────────────────────────────────────────────────────
    "manage_ad_account": [
        "ADCreateAccount", "ADDisableAccount", "ADEnableAccount",
        "ADDeleteAccount", "ADResetPassword", "ADAddtoGroup",
        "ADRemoveFromGroup",
    ],

    # ── SERVER OPS ─────────────────────────────────────────────────────────
    "server_action": [
        "ServerRestart", "ServerShutdown", "ServiceStart", "ServiceStop",
        "FolderCreate", "SetFolderPermissions",
    ],
}


# Prompt phrases that should trigger each task. This is a starting point —
# the script will augment these based on corpus-derived activity sequences
# and you should hand-edit them after review.

TASK_PROMPT_PHRASES = {
    "read_file": [
        "read excel", "read spreadsheet", "read csv", "read file",
        "load file", "import data from", "parse json", "parse xml",
        "extract data from", "read the excel", "read the csv",
    ],
    "query_database": [
        "query database", "run sql", "select from", "sql query", "query the db",
        "run a query", "database lookup",
    ],
    "query_api": [
        "call api", "http request", "rest call", "fetch from api",
        "call endpoint", "hit the api", "web request",
    ],
    "query_itsm": [
        "servicenow", "get ticket", "get incident", "jira", "get record",
        "update ticket", "create incident", "look up incident",
    ],
    "query_directory": [
        "active directory", "check user", "user exists", "list group",
        "group members", "ad lookup", "ad user",
    ],
    "query_system_state": [
        "ping", "check server", "service status", "check service",
        "is running", "health check", "is up", "wmi", "check cpu",
        "check memory", "file exists", "folder exists",
    ],
    "create_table": [
        "create table", "make a table", "new memory table",
    ],
    "filter_table": [
        "filter", "exclude", "only keep", "where", "remove rows where",
        "keep only",
    ],
    "sort_table": [
        "sort", "order by", "ascending", "descending",
    ],
    "count_table_rows": [
        "count rows", "number of rows", "how many rows", "row count",
    ],
    "read_table_cell": [
        "get cell", "read column", "get column value", "get value from",
    ],
    "modify_table": [
        "set cell", "update row", "add row", "delete row", "update the column",
    ],
    "convert_table": [
        "convert to html", "convert to json", "format as table", "html table",
    ],
    "set_variable": [
        "set variable", "store in variable", "assign", "save to", "put in",
    ],
    "iterate_rows": [
        "for each", "loop through", "iterate over", "for every",
        "go through each", "repeat for each",
    ],
    "branch_decision": [
        "if", "when", "depending on", "check if", "decide", "branch on",
        "based on",
    ],
    "send_email": [
        "send email", "email", "notify via email", "email admin",
        "send a mail", "send message",
    ],
    "send_notification": [
        "notify", "alert", "send notification", "raise event",
    ],
    "display_value": [
        "display", "show", "print", "output",
    ],
    "write_file": [
        "write to file", "save file", "export to", "download file",
    ],
    "date_operations": [
        "current date", "today", "days between", "add days", "subtract days",
        "date difference", "how old", "expiry", "expiration",
    ],
    "string_operations": [
        "split", "substring", "contains", "is empty", "replace", "trim",
    ],
    "math_operations": [
        "calculate", "sum", "add numbers", "percentage",
    ],
    "terminate": [
        "stop workflow", "terminate", "exit", "end workflow",
    ],
    "wait": [
        "wait", "pause", "delay", "sleep",
    ],
    "run_script": [
        "run script", "powershell", "execute command", "run command",
    ],
    "manage_ad_account": [
        "create account", "disable account", "reset password",
        "add to group", "create user",
    ],
    "server_action": [
        "restart server", "reboot", "start service", "stop service",
        "shutdown", "create folder",
    ],
}


# Activities that are structural — they shape workflows but aren't a
# task in themselves. Filtered out during task identification.

STRUCTURAL_ACTIVITIES = {
    "SequentialWorkflowActivity", "SequenceActivity", "WorkflowInfo",
    "WhileActivity", "IfElseActivity", "IfElseBranchActivity", "ExitWhile",
    "ReturnValue", "ParallelActivity", "UserGroup",
}


# ─────────────────────────────────────────────────────────────────────────────
# XML parsing — reused from mine_corpus.py conventions
# ─────────────────────────────────────────────────────────────────────────────

def _local_name(tag: str) -> str:
    """Strip XML namespace prefix from a tag."""
    if not tag.startswith("{"):
        return tag
    closing = tag.find("}")
    return tag[closing + 1:] if closing != -1 else tag


def _extract_activity_types(elem: ET.Element, out: list) -> None:
    """Walk element tree, collect CustomTypeName for every activity node."""
    tag = _local_name(elem.tag)
    if tag and tag[0].isupper() and not tag.startswith("xs:"):
        # Only record leaf-ish activity tags, not XOML wrapper elements
        out.append(tag)
    for child in elem:
        _extract_activity_types(child, out)


def parse_workflow(path: Path) -> list[str] | None:
    """
    Extract the ordered list of activity type names from one workflow file.

    Handles both TotalExport (Xoml-wrapped) and raw XOML formats. Returns
    None on parse failure.
    """
    try:
        raw = path.read_bytes()
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    tag = _local_name(root.tag)
    if tag == "SequentialWorkflowActivity":
        xoml_root = root
    elif tag == "TotalExport":
        wf_info = root.find(".//WorkflowInfo")
        if wf_info is None:
            return None
        xoml_raw = wf_info.get("Xoml", "")
        if not xoml_raw:
            return None
        try:
            xoml_root = ET.fromstring(html.unescape(xoml_raw))
        except ET.ParseError:
            return None
    else:
        return None

    activities = []
    for child in xoml_root:
        _extract_activity_types(child, activities)
    return activities


# ─────────────────────────────────────────────────────────────────────────────
# Analysis — the core of the mining
# ─────────────────────────────────────────────────────────────────────────────

def build_activity_to_family() -> dict[str, str]:
    """Invert ACTIVITY_FAMILIES so we can look up family by activity name."""
    lookup = {}
    for family, activities in ACTIVITY_FAMILIES.items():
        for activity in activities:
            lookup[activity] = family
    return lookup


def classify_sequence(sequence: list[str],
                      activity_to_family: dict[str, str]) -> tuple[list[str], list[str]]:
    """
    Map a flat activity sequence to a family sequence.

    Returns (family_sequence, unmatched_activities).
    Consecutive same-family activities collapse to a single family token.
    """
    families = []
    unmatched = []
    prev_family = None
    for act in sequence:
        if act in STRUCTURAL_ACTIVITIES:
            continue
        fam = activity_to_family.get(act)
        if fam is None:
            unmatched.append(act)
            continue
        if fam != prev_family:
            families.append(fam)
            prev_family = fam
    return families, unmatched


def find_family_cooccurrences(family_sequences: list[list[str]],
                               window: int = 2) -> Counter:
    """
    Count how often families co-occur within a sliding window across
    all workflows. Used to identify task clusters — families that almost
    always appear together likely represent a single conceptual task.
    """
    cooc = Counter()
    for seq in family_sequences:
        seen = set()  # per-workflow dedup
        for i in range(len(seq)):
            for j in range(i + 1, min(i + window + 1, len(seq))):
                pair = tuple(sorted([seq[i], seq[j]]))
                if pair not in seen:
                    cooc[pair] += 1
                    seen.add(pair)
    return cooc


def find_family_prefixspan(family_sequences: list[list[str]],
                            min_support: int,
                            max_len: int = 4) -> list[dict]:
    """
    Minimal PrefixSpan over family sequences. Finds recurring ordered
    subsequences of families. Each result represents a candidate
    multi-family task (e.g., read → iterate → check → notify).
    """

    def project(db, item):
        projected = []
        for seq in db:
            for i, token in enumerate(seq):
                if token == item:
                    projected.append(seq[i + 1:])
                    break
        return projected

    def recurse(db, prefix, out):
        if len(prefix) >= max_len:
            return
        counts = Counter(token for seq in db for token in set(seq))
        for token, count in counts.items():
            if count < min_support:
                continue
            new_prefix = prefix + [token]
            out.append({"pattern": new_prefix, "support": count})
            recurse(project(db, token), new_prefix, out)

    results = []
    recurse(family_sequences, [], results)

    # Deduplicate, sort by support desc, filter to length >= 2
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: -x["support"]):
        key = tuple(r["pattern"])
        if key in seen or len(r["pattern"]) < 2:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def build_per_family_activities(sequences: list[list[str]],
                                 activity_to_family: dict[str, str]) -> dict[str, Counter]:
    """
    For each family, count which specific activities appear (and how often).
    Lets us pick canonical members of each task.
    """
    per_family = defaultdict(Counter)
    for seq in sequences:
        for act in seq:
            fam = activity_to_family.get(act)
            if fam:
                per_family[fam][act] += 1
    return per_family


def load_existing_scaffolds(pattern_lib_path: Path) -> dict[str, dict]:
    """
    Load scaffolds from pattern_library.json, keyed by pattern_id.
    Each scaffold is a pre-wired workflow structure with PARAM_ placeholders.
    These are our Variant C patterns — we'll pair tasks to them where possible.
    """
    if not pattern_lib_path.exists():
        return {}
    try:
        with open(pattern_lib_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    patterns = data if isinstance(data, list) else data.get("patterns", [])
    by_id = {}
    for p in patterns:
        pid = p.get("pattern_id")
        if pid and p.get("scaffold"):
            by_id[pid] = {
                "pattern_id": pid,
                "control_flow": p.get("control_flow", ""),
                "sequence_fragment": p.get("sequence_fragment", []),
                "trigger_keywords": p.get("trigger_keywords", []),
                "scaffold": p["scaffold"],
            }
    return by_id


def suggest_scaffold_for_task(task_families: list[str],
                               scaffolds_by_id: dict[str, dict],
                               activity_to_family: dict[str, str]) -> str | None:
    """
    Given a task's family sequence, find the best matching existing scaffold
    from pattern_library.json. Returns pattern_id or None.

    Matching: compare the task's family sequence to each scaffold's
    sequence_fragment (translated to families). Pick the scaffold with the
    highest Jaccard similarity.
    """
    best_pid = None
    best_score = 0.0
    task_set = set(task_families)

    for pid, scaffold in scaffolds_by_id.items():
        seq_frag = scaffold["sequence_fragment"]
        fam_frag = [activity_to_family.get(a) for a in seq_frag]
        fam_frag = [f for f in fam_frag if f]
        if not fam_frag:
            continue
        frag_set = set(fam_frag)
        intersection = len(task_set & frag_set)
        union = len(task_set | frag_set)
        score = intersection / union if union else 0
        if score > best_score:
            best_score = score
            best_pid = pid

    # Require reasonable overlap to claim a match
    return best_pid if best_score >= 0.5 else None


# ─────────────────────────────────────────────────────────────────────────────
# Taxonomy assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_task_taxonomy(family_sequences: list[list[str]],
                         activity_counter_per_family: dict[str, Counter],
                         cooccurrences: Counter,
                         frequent_subsequences: list[dict],
                         scaffolds_by_id: dict[str, dict],
                         activity_to_family: dict[str, str],
                         total_workflows: int) -> dict:
    """
    Assemble the draft taxonomy. Two sections:

    1. Atomic tasks (Variant A+B) — one per family, with canonical activities.
    2. Composite tasks (Variant C) — multi-family sequences frequent enough
       to warrant their own entry, paired with existing scaffolds when
       possible.
    """
    atomic_tasks = []
    for family, activities_counter in activity_counter_per_family.items():
        if not activities_counter:
            continue
        total = sum(activities_counter.values())
        top_activities = [
            {"activity": act, "count": count, "pct": round(count / total * 100, 1)}
            for act, count in activities_counter.most_common(8)
        ]
        atomic_tasks.append({
            "task_id": family,
            "kind": "atomic",
            "label": family.replace("_", " ").title(),
            "description": "",  # to be filled by human reviewer
            "activities": top_activities,
            "prompt_phrases": TASK_PROMPT_PHRASES.get(family, []),
            "total_workflow_occurrences": total,
            "needs_review": bool(not TASK_PROMPT_PHRASES.get(family)),
        })

    # Sort atomic tasks by frequency desc so reviewers see important ones first
    atomic_tasks.sort(key=lambda t: -t["total_workflow_occurrences"])

    composite_tasks = []
    for subseq in frequent_subsequences:
        pattern = subseq["pattern"]
        support = subseq["support"]
        if len(pattern) < 2:
            continue
        # Skip if it's just two tightly coupled families like
        # (iterate_rows, read_table_cell) that atomic tasks already cover
        # poorly. Require at least 3 families or high cross-family support.
        if len(pattern) == 2 and support < total_workflows * 0.15:
            continue

        task_id = "composite_" + "_then_".join(pattern)[:60]
        matched_pid = suggest_scaffold_for_task(pattern, scaffolds_by_id, activity_to_family)

        composite_tasks.append({
            "task_id": task_id,
            "kind": "composite",
            "label": " → ".join(f.replace("_", " ") for f in pattern).title(),
            "description": "",  # for human reviewer
            "family_sequence": pattern,
            "corpus_support": support,
            "corpus_support_pct": round(support / total_workflows * 100, 1),
            "suggested_scaffold_pattern_id": matched_pid,
            "prompt_phrases": [],  # human to fill
            "needs_review": True,
        })

    composite_tasks.sort(key=lambda t: -t["corpus_support"])

    # Trim composites to the top 25 — reviewers should focus on the important
    # ones first. Uncommon composites can be promoted later from telemetry.
    composite_tasks = composite_tasks[:25]

    return {
        "_meta": {
            "total_workflows_analyzed": total_workflows,
            "atomic_task_count": len(atomic_tasks),
            "composite_task_count": len(composite_tasks),
            "review_instructions": (
                "This is a DRAFT produced by mine_task_taxonomy.py. "
                "Review every task and: "
                "(1) fill the 'description' field, "
                "(2) prune or expand 'activities' to canonical members, "
                "(3) add/refine 'prompt_phrases' based on how your team describes these tasks, "
                "(4) for composite tasks, verify the suggested_scaffold_pattern_id matches intent, "
                "(5) delete tasks that don't make sense, add tasks the miner missed."
            ),
        },
        "atomic_tasks": atomic_tasks,
        "composite_tasks": composite_tasks,
    }


def build_match_phrases(taxonomy: dict) -> dict:
    """
    Extract just the prompt phrases into a separate file, keyed by task_id.
    The matcher will use this for deterministic prompt → task matching.
    """
    phrases = {}
    for task in taxonomy["atomic_tasks"] + taxonomy["composite_tasks"]:
        phrases[task["task_id"]] = task.get("prompt_phrases", [])
    return phrases


# ─────────────────────────────────────────────────────────────────────────────
# Report writer
# ─────────────────────────────────────────────────────────────────────────────

def write_report(taxonomy: dict,
                  unmatched_counter: Counter,
                  cooccurrences: Counter,
                  out_path: Path) -> None:
    lines = []
    lines.append("# Task Taxonomy Mining Report")
    lines.append("")
    lines.append(f"Workflows analyzed: {taxonomy['_meta']['total_workflows_analyzed']}")
    lines.append(f"Atomic tasks: {taxonomy['_meta']['atomic_task_count']}")
    lines.append(f"Composite tasks: {taxonomy['_meta']['composite_task_count']}")
    lines.append("")

    lines.append("## Atomic tasks (by corpus frequency)")
    lines.append("")
    lines.append("| Task | Occurrences | Canonical activities | Phrase count |")
    lines.append("|---|---|---|---|")
    for t in taxonomy["atomic_tasks"]:
        top_acts = ", ".join(a["activity"] for a in t["activities"][:5])
        flag = " ⚠" if t["needs_review"] else ""
        lines.append(
            f"| `{t['task_id']}`{flag} "
            f"| {t['total_workflow_occurrences']} "
            f"| {top_acts} "
            f"| {len(t['prompt_phrases'])} |"
        )
    lines.append("")
    lines.append("⚠ = no prompt phrases yet — needs human curation.")
    lines.append("")

    lines.append("## Composite tasks (top 25 by corpus support)")
    lines.append("")
    lines.append("| Task | Support | Support % | Suggested scaffold |")
    lines.append("|---|---|---|---|")
    for t in taxonomy["composite_tasks"]:
        scaffold = t.get("suggested_scaffold_pattern_id") or "—"
        lines.append(
            f"| {t['label']} "
            f"| {t['corpus_support']} "
            f"| {t['corpus_support_pct']}% "
            f"| {scaffold} |"
        )
    lines.append("")

    lines.append("## Unmatched activities (need family assignment)")
    lines.append("")
    if unmatched_counter:
        lines.append("These activities appeared in the corpus but aren't in any family.")
        lines.append("Add them to the appropriate family in ACTIVITY_FAMILIES and re-run.")
        lines.append("")
        lines.append("| Activity | Count |")
        lines.append("|---|---|")
        for act, count in unmatched_counter.most_common(30):
            lines.append(f"| `{act}` | {count} |")
        remaining = len(unmatched_counter) - 30
        if remaining > 0:
            lines.append(f"| ...{remaining} more... | |")
    else:
        lines.append("All corpus activities are classified. ✓")
    lines.append("")

    lines.append("## Top family co-occurrences (sliding window)")
    lines.append("")
    lines.append("Families that appear together in workflows. Useful for spotting ")
    lines.append("composite tasks the PrefixSpan miner may have missed.")
    lines.append("")
    lines.append("| Family A | Family B | Co-occurrences |")
    lines.append("|---|---|---|")
    for (fa, fb), count in cooccurrences.most_common(20):
        lines.append(f"| {fa} | {fb} | {count} |")
    lines.append("")

    lines.append("## Next steps for the reviewer")
    lines.append("")
    lines.append("1. Open `task_taxonomy_draft.json`.")
    lines.append("2. For every task with `needs_review: true`, decide if the task is valid.")
    lines.append("3. Write a clear one-sentence `description` for every task.")
    lines.append("4. Add or refine `prompt_phrases` — these are what users will actually type.")
    lines.append("5. For composite tasks, verify `suggested_scaffold_pattern_id` matches the intent.")
    lines.append("6. Delete `needs_review` field once each task is checked.")
    lines.append("7. Rename file from `task_taxonomy_draft.json` → `task_taxonomy.json`.")
    lines.append("8. Same for `task_match_phrases_draft.json` → `task_match_phrases.json`.")
    lines.append("")
    lines.append("Unmatched activities from the table above should be added to the ")
    lines.append("`ACTIVITY_FAMILIES` dict in `mine_task_taxonomy.py` and the script re-run.")

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mine a draft task taxonomy from workflow corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--xml-dir", default=str(_DEFAULT_XML_DIR),
                        help=f"Corpus directory (default: {_DEFAULT_XML_DIR})")
    parser.add_argument("--data-dir", default=str(_DEFAULT_DATA_DIR),
                        help=f"Output directory (default: {_DEFAULT_DATA_DIR})")
    parser.add_argument("--pattern-lib", default=str(_DEFAULT_PATTERN_LIB),
                        help=f"Existing pattern library (default: {_DEFAULT_PATTERN_LIB})")
    parser.add_argument("--min-support", type=int, default=10,
                        help="Minimum workflows for a composite task (default: 10)")
    parser.add_argument("--cooc-window", type=int, default=2,
                        help="Sliding window for co-occurrence analysis (default: 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stats, write nothing")
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    data_dir = Path(args.data_dir)
    pattern_lib = Path(args.pattern_lib)

    if not xml_dir.exists():
        print(f"ERROR: corpus directory not found: {xml_dir}")
        sys.exit(1)

    # ── Parse corpus ────────────────────────────────────────────────────────
    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files in {xml_dir}")
        sys.exit(1)

    print(f"Parsing {len(xml_files)} workflow files...")
    sequences = []
    n_failed = 0
    for path in xml_files:
        seq = parse_workflow(path)
        if seq is None:
            n_failed += 1
        elif seq:
            sequences.append(seq)
    print(f"  Parsed: {len(sequences)} | Failed: {n_failed}")

    # ── Classify into families ──────────────────────────────────────────────
    activity_to_family = build_activity_to_family()
    family_sequences = []
    all_unmatched = Counter()
    for seq in sequences:
        fam_seq, unmatched = classify_sequence(seq, activity_to_family)
        if fam_seq:
            family_sequences.append(fam_seq)
        for u in unmatched:
            all_unmatched[u] += 1

    print(f"  Family sequences: {len(family_sequences)}")
    print(f"  Unmatched activity types: {len(all_unmatched)} "
          f"({sum(all_unmatched.values())} total occurrences)")

    # ── Per-family activity counts ──────────────────────────────────────────
    per_family_acts = build_per_family_activities(sequences, activity_to_family)

    # ── Co-occurrence analysis ──────────────────────────────────────────────
    print(f"  Computing family co-occurrences (window={args.cooc_window})...")
    cooccurrences = find_family_cooccurrences(family_sequences, window=args.cooc_window)

    # ── PrefixSpan composite task mining ────────────────────────────────────
    print(f"  Mining frequent family subsequences (min_support={args.min_support})...")
    subsequences = find_family_prefixspan(
        family_sequences,
        min_support=args.min_support,
        max_len=4,
    )
    print(f"  Found {len(subsequences)} candidate composite patterns")

    # ── Load existing scaffolds for Variant C pairing ───────────────────────
    scaffolds_by_id = load_existing_scaffolds(pattern_lib)
    print(f"  Loaded {len(scaffolds_by_id)} existing scaffolds from pattern_library.json")

    # ── Build the taxonomy ──────────────────────────────────────────────────
    taxonomy = build_task_taxonomy(
        family_sequences,
        per_family_acts,
        cooccurrences,
        subsequences,
        scaffolds_by_id,
        activity_to_family,
        total_workflows=len(sequences),
    )

    # ── Build match phrases ─────────────────────────────────────────────────
    match_phrases = build_match_phrases(taxonomy)

    # ── Write outputs ───────────────────────────────────────────────────────
    if args.dry_run:
        print("\n[DRY RUN] Would write:")
        print(f"  {data_dir / 'task_taxonomy_draft.json'} "
              f"({taxonomy['_meta']['atomic_task_count']} atomic, "
              f"{taxonomy['_meta']['composite_task_count']} composite)")
        print(f"  {data_dir / 'task_match_phrases_draft.json'}")
        print(f"  {_REPO_ROOT / 'task_mining_report.md'}")
        return

    data_dir.mkdir(parents=True, exist_ok=True)

    taxonomy_path = data_dir / "task_taxonomy_draft.json"
    phrases_path = data_dir / "task_match_phrases_draft.json"
    report_path = _REPO_ROOT / "task_mining_report.md"

    taxonomy_path.write_text(
        json.dumps(taxonomy, indent=2), encoding="utf-8"
    )
    phrases_path.write_text(
        json.dumps(match_phrases, indent=2), encoding="utf-8"
    )
    write_report(taxonomy, all_unmatched, cooccurrences, report_path)

    print(f"\nWrote:")
    print(f"  {taxonomy_path}")
    print(f"  {phrases_path}")
    print(f"  {report_path}")
    print()
    print("Next steps:")
    print("  1. Review task_mining_report.md — it summarizes what was found")
    print("  2. Edit task_taxonomy_draft.json by hand (see _meta.review_instructions)")
    print("  3. Add any unmatched activities to ACTIVITY_FAMILIES and re-run")
    print("  4. Rename _draft files to final names when ready")


if __name__ == "__main__":
    main()