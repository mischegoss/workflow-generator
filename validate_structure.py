"""
validate_structurebuilder.py
============================
Validates every testable claim in the StructureBuilder instruction's
RETURNVALUE TYPE RULES section against the actual workflow corpus.

Runs four test families:

  1. TYPE ASSIGNMENT   — For each activity in TIER_1 / TIER_2, does the
                         corpus agree at or above the confidence threshold?
                         Flags any activity where corpus Type distribution
                         contradicts the assigned tier.

  2. CONDITIONTYPE     — For each activity, are the listed ConditionType
                         values the ones the corpus actually uses?
                         Reports any high-frequency ConditionType not listed,
                         and any listed ConditionType not seen in the corpus.

  3. VALUE COVERAGE    — For Tier 1 fixed-value activities (Ping → "Success",
                         ServiceStatus → "Running", etc.), confirms those
                         exact strings appear in the corpus and reports the
                         full top-value distribution.

  4. GAPS              — Activity types that appear as IfElse predecessors
                         in the corpus with >= min_gap_count condition branches
                         but are in NEITHER tier. These are holes in the
                         instruction that need to be filled.

Also checks DEFAULT branch structure assumptions:
  - Default branches should always use Type="StoredValue"
  - Default branches should always have UseBranchWhenTimeout="True"
  - Default branches should always have ConditionType=""

Usage:
    python validate_structurebuilder.py
    python validate_structurebuilder.py --xml-dir /path/to/workflows_raw
    python validate_structurebuilder.py --confidence 0.70 --min-gap-count 3
    python validate_structurebuilder.py --output-json results.json
"""

import argparse
import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# StructureBuilder instruction claims — edit to match current instruction
# ---------------------------------------------------------------------------

# Tier 1: instruction claims Type="StoredValue" for all condition branches
TIER_1 = {
    "IsEmpty", "IsNumeric", "ADUserExists", "FileExist", "FolderExist",
    "FolderExistRemote", "XMLEditNode", "Contains", "Ping", "ServiceStatus",
    "ServiceStart", "ServiceStop", "ADRemoveFromGroup", "ADAddtoGroup",
    "FileCopy", "VMPowerOn", "VMMarkTemplate", "SLNPMManageNode",
    "SLNPMUnManageNode", "PowerShellScript", "URLCheck",
}

# Tier 2: instruction claims Type="UserDefinedValue" for all condition branches
TIER_2 = {
    "GetRowsCount", "Length", "WorkflowCounter", "DateDifference", "Counter",
    "FunctionCalculator", "GetCellValue", "GetCellValueAdvanced", "MemorySet",
    "MultiMemorySet", "RunWorkflow", "DisplayValue", "TSQLStatement", "TSQLQuery",
    "ResultSetFilter", "HTTPRequest", "SingleSSHCommand", "PowerShell", "SendEmail",
    "MatchRegularExpression", "ReplaceString", "RegistryQuery", "LowerCase",
    "ConvertPasswordToPlaintext", "ReadFile", "XMLEvaluateXpathExpression",
    "MsTeamsSendMessage", "ChatGPTQuery", "ADPassExpDaysLeft", "FileSize",
    "DiskSpace", "SystemUptime", "TerminateWorkflow", "VMPowerState", "SNUpdateRecord",
    "SetCellValue", "NestedJsonToTable",
}

# Tier 1 fixed values: instruction claims these specific Value strings
# Map: activity → set of claimed valid values (condition branches only)
TIER_1_VALUES = {
    "IsEmpty":            {"True", "False"},
    "IsNumeric":          {"True", "False"},
    "ADUserExists":       {"True", "False"},
    "FileExist":          {"True", "False"},
    "FolderExist":        {"True", "False"},
    "FolderExistRemote":  {"True", "False"},
    "XMLEditNode":        {"True", "False"},
    "Contains":           {"True", "False"},
    "Ping":               {"Success"},
    "ServiceStatus":      {"Running", "Stopped"},
    "ServiceStart":       {"Success"},
    "ServiceStop":        {"Success"},
    "ADRemoveFromGroup":  {"Success"},
    "ADAddtoGroup":       {"Success"},
    "FileCopy":           {"Success"},
    "VMPowerOn":          {"Success"},
    "VMMarkTemplate":     {"Success"},
    "SLNPMManageNode":    {"Success"},
    "SLNPMUnManageNode":  {"Success"},
    "PowerShellScript":   {"Success"},
    "URLCheck":           {"Success"},
}

# ConditionType claims per activity (listed in instruction as valid options)
# Map: activity → frozenset of acceptable ConditionType strings
CONDITIONTYPE_CLAIMS = {
    # Tier 1
    "IsEmpty":            frozenset({"Equals"}),
    "IsNumeric":          frozenset({"Equals"}),
    "ADUserExists":       frozenset({"Equals"}),
    "FileExist":          frozenset({"Equals"}),
    "FolderExist":        frozenset({"Equals"}),
    "FolderExistRemote":  frozenset({"Equals"}),
    "XMLEditNode":        frozenset({"Equals"}),
    "Contains":           frozenset({"Equals"}),
    "Ping":               frozenset({"Equals"}),
    "ServiceStatus":      frozenset({"Equals"}),
    "ServiceStart":       frozenset({"Equals"}),
    "ServiceStop":        frozenset({"Equals"}),
    "ADRemoveFromGroup":  frozenset({"Equals"}),
    "ADAddtoGroup":       frozenset({"Equals"}),
    "FileCopy":           frozenset({"Equals"}),
    "VMPowerOn":          frozenset({"Equals"}),
    "VMMarkTemplate":     frozenset({"Equals"}),
    "SLNPMManageNode":    frozenset({"Equals"}),
    "SLNPMUnManageNode":  frozenset({"Equals"}),
    "PowerShellScript":   frozenset({"Equals"}),
    "URLCheck":           frozenset({"Equals"}),
    # Tier 2
    "GetRowsCount":       frozenset({">", "Equals"}),
    "Length":             frozenset({">", "Equals"}),
    "WorkflowCounter":    frozenset({">", "=>"}),
    "DateDifference":     frozenset({">", "<", "=>"}),
    "Counter":            frozenset({">", "Equals"}),
    "FunctionCalculator": frozenset({"Equals", ">"}),
    "GetCellValue":       frozenset({"Equals", "Contains", "Formula"}),
    "GetCellValueAdvanced": frozenset({"Equals", "Formula"}),
    "MemorySet":          frozenset({"Formula", "Contains"}),
    "MultiMemorySet":     frozenset({"Formula"}),
    "RunWorkflow":        frozenset({"Formula", ">"}),
    "DisplayValue":       frozenset({"Formula", "Contains"}),
    "TSQLStatement":      frozenset({"Equals", "Formula"}),
    "TSQLQuery":          frozenset({"Formula", "Equals"}),
    "ResultSetFilter":    frozenset({"Formula", "Equals"}),
    "HTTPRequest":        frozenset({"Formula", "Equals"}),
    "SingleSSHCommand":   frozenset({"Formula", "Contains"}),
    "PowerShell":         frozenset({"Contains", "Equals"}),
    "SendEmail":          frozenset({"Contains"}),
    "MatchRegularExpression": frozenset({"Formula", "Contains"}),
    "ReplaceString":      frozenset({"Equals", "Contains"}),
    "RegistryQuery":      frozenset({"Equals", "Formula"}),
    "LowerCase":          frozenset({"Equals", "Contains"}),
    "ConvertPasswordToPlaintext": frozenset({"Equals"}),
    "ReadFile":           frozenset({"Contains", "Formula"}),
    "XMLEvaluateXpathExpression": frozenset({"Equals", "Formula"}),
    "MsTeamsSendMessage": frozenset({"Equals", "Formula"}),
    "ChatGPTQuery":       frozenset({"Contains", "Formula"}),
    "ADPassExpDaysLeft":  frozenset({">", "<"}),
    "FileSize":           frozenset({">", "Equals"}),
    "DiskSpace":          frozenset({">", "<"}),
    "SystemUptime":       frozenset({">", "<"}),
    "TerminateWorkflow":  frozenset({"Formula"}),
    "VMPowerState":       frozenset({"Equals", "Formula"}),
    "SNUpdateRecord":     frozenset({"Equals"}),
    "SetCellValue":       frozenset({"Equals", "Formula"}),
    "NestedJsonToTable":  frozenset({"Formula", "Equals"}),
}

ALL_CLAIMED = TIER_1 | TIER_2

# ---------------------------------------------------------------------------
# XML parsing (same approach as mine_returnvalue_context.py)
# ---------------------------------------------------------------------------

_XAML_NS   = "http://schemas.microsoft.com/winfx/2006/xaml"
_XNAME_KEY = f"{{{_XAML_NS}}}Name"

CONTAINER_TYPES = {
    "SequentialWorkflowActivity", "SequenceActivity",
    "IfElseActivity", "IfElseBranchActivity",
    "WhileActivity", "ForEachActivity",
    "ParallelActivity", "UserGroup", "ExitWhile",
}
SKIP_AS_PRECEDING = CONTAINER_TYPES | {"ReturnValue", "Continue"}


def _local_name(tag: str) -> str:
    return tag[tag.index("}") + 1:] if tag.startswith("{") else tag


def _sanitise_xoml(xoml: str) -> str:
    return xoml.replace("&&&", "___TRIPLE_AMP___")


def parse_workflow_file(path: Path) -> ET.Element | None:
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError as e:
        print(f"  [SKIP] {path.name}: outer parse error — {e}")
        return None

    tag = _local_name(root.tag)
    if tag == "SequentialWorkflowActivity":
        return root
    if tag == "TotalExport":
        wf_info = root.find(".//WorkflowInfo")
        if wf_info is None:
            return None
        xoml_raw = wf_info.get("Xoml", "")
        if not xoml_raw:
            return None
        try:
            return ET.fromstring(_sanitise_xoml(xoml_raw))
        except ET.ParseError as e:
            print(f"  [SKIP] {path.name}: inner XOML parse — {e}")
            return None
    return None


def _find_preceding(parent: ET.Element, ifelse_elem: ET.Element) -> str:
    preceding = "_no_preceding"
    for child in parent:
        if child is ifelse_elem:
            break
        atype = _local_name(child.tag)
        if atype not in SKIP_AS_PRECEDING:
            preceding = atype
    return preceding


def _extract_rv(branch: ET.Element) -> dict | None:
    for child in branch:
        if _local_name(child.tag) == "ReturnValue":
            return {
                "Type":                 child.get("Type", ""),
                "ConditionType":        child.get("ConditionType", ""),
                "Value":                child.get("Value", ""),
                "UseStoredValue":       child.get("UseStoredValue", ""),
                "UseBranchWhenTimeout": child.get("UseBranchWhenTimeout", ""),
            }
    return None


def _is_default(rv: dict) -> bool:
    return rv["UseBranchWhenTimeout"] == "True" and rv["ConditionType"] == ""


def walk(elem: ET.Element, parent: ET.Element | None,
         condition_records: list, default_records: list) -> None:
    atype = _local_name(elem.tag)
    if atype == "IfElseActivity":
        preceding = _find_preceding(parent, elem) if parent is not None else "_no_preceding"
        for branch in elem:
            if _local_name(branch.tag) != "IfElseBranchActivity":
                continue
            rv = _extract_rv(branch)
            if rv is None:
                continue
            record = {"preceding": preceding, **rv}
            if _is_default(rv):
                default_records.append(record)
            else:
                condition_records.append(record)
        for branch in elem:
            walk(branch, elem, condition_records, default_records)
    else:
        for child in elem:
            walk(child, elem, condition_records, default_records)


# ---------------------------------------------------------------------------
# Test runner helpers
# ---------------------------------------------------------------------------

PASS  = "PASS"
FAIL  = "FAIL"
WARN  = "WARN"
GAP   = "GAP"
INFO  = "INFO"


def _pct(n, total):
    return round(100 * n / total) if total else 0


def _top(counter: Counter, n=5):
    return [(v, c) for v, c in counter.most_common(n)]


# ---------------------------------------------------------------------------
# Test 1 — Type assignment
# ---------------------------------------------------------------------------

def test_type_assignment(cond_by_activity: dict, confidence: float) -> list:
    results = []
    threshold = int(confidence * 100)

    for activity in sorted(ALL_CLAIMED):
        data = cond_by_activity.get(activity)
        if not data:
            results.append({
                "test":     "TYPE_ASSIGNMENT",
                "activity": activity,
                "status":   WARN,
                "message":  "No condition branches found in corpus — cannot confirm.",
                "detail":   {},
            })
            continue

        total      = data["total"]
        type_dist  = data["type_counter"]
        sv_pct     = _pct(type_dist.get("StoredValue", 0), total)
        udv_pct    = _pct(type_dist.get("UserDefinedValue", 0), total)

        if activity in TIER_1:
            expected_type = "StoredValue"
            actual_pct    = sv_pct
        else:
            expected_type = "UserDefinedValue"
            actual_pct    = udv_pct

        if actual_pct >= threshold:
            status  = PASS
            message = (f"Corpus agrees: {actual_pct}% use {expected_type} "
                       f"({data['total']} branches).")
        elif actual_pct >= 50:
            status  = WARN
            message = (f"Weak signal: only {actual_pct}% use {expected_type} "
                       f"({data['total']} branches). Consider narrowing the rule.")
        else:
            status  = FAIL
            message = (f"Corpus DISAGREES: only {actual_pct}% use {expected_type} "
                       f"— majority ({100 - actual_pct}%) use the other type. "
                       f"Tier assignment is likely wrong.")

        results.append({
            "test":     "TYPE_ASSIGNMENT",
            "activity": activity,
            "status":   status,
            "message":  message,
            "detail": {
                "total_branches":  total,
                "StoredValue_pct": sv_pct,
                "UserDefined_pct": udv_pct,
                "claimed_type":    expected_type,
            },
        })

    return results


# ---------------------------------------------------------------------------
# Test 2 — ConditionType coverage
# ---------------------------------------------------------------------------

def test_conditiontype_coverage(cond_by_activity: dict, min_pct: int = 10) -> list:
    """
    For each claimed activity, check whether:
    a) all listed ConditionTypes actually appear in the corpus
    b) any high-frequency ConditionType (>= min_pct) is NOT in the claim
    """
    results = []

    for activity, claimed_ctypes in sorted(CONDITIONTYPE_CLAIMS.items()):
        data = cond_by_activity.get(activity)
        if not data:
            continue

        total    = data["total"]
        ct_dist  = data["conditiontype_counter"]

        # ConditionTypes in corpus above noise floor
        corpus_ctypes = {
            ct for ct, count in ct_dist.items()
            if _pct(count, total) >= min_pct
        }

        # Claimed but not seen at threshold
        not_seen = claimed_ctypes - corpus_ctypes - {""}
        # Seen at threshold but not claimed
        not_claimed = corpus_ctypes - claimed_ctypes - {""}

        if not not_seen and not not_claimed:
            status  = PASS
            message = f"All claimed ConditionTypes confirmed in corpus."
        else:
            issues = []
            if not_claimed:
                issues.append(
                    f"HIGH-FREQ NOT LISTED: {sorted(not_claimed)} "
                    f"(each >= {min_pct}% in corpus)"
                )
            if not_seen:
                issues.append(
                    f"LISTED BUT RARE/ABSENT: {sorted(not_seen)} "
                    f"(each < {min_pct}% in corpus)"
                )
            status  = WARN
            message = " | ".join(issues)

        results.append({
            "test":     "CONDITIONTYPE",
            "activity": activity,
            "status":   status,
            "message":  message,
            "detail": {
                "total_branches":     total,
                "corpus_distribution": dict(ct_dist.most_common()),
                "claimed":            sorted(claimed_ctypes),
                "not_claimed_high_freq": sorted(not_claimed),
                "listed_but_rare":    sorted(not_seen),
            },
        })

    return results


# ---------------------------------------------------------------------------
# Test 3 — Value coverage (Tier 1 fixed-value activities)
# ---------------------------------------------------------------------------

def test_value_coverage(cond_by_activity: dict) -> list:
    results = []

    for activity, claimed_values in sorted(TIER_1_VALUES.items()):
        data = cond_by_activity.get(activity)
        if not data:
            continue

        total       = data["total"]
        value_dist  = data["value_counter"]

        # What fraction of branches use the claimed values?
        claimed_count = sum(
            count for val, count in value_dist.items()
            if val in claimed_values
        )
        claimed_pct = _pct(claimed_count, total)

        top_values = _top(value_dist, 5)

        # Values in corpus not claimed (that appear at >= 5%)
        unclaimed_significant = [
            (v, c) for v, c in top_values
            if v not in claimed_values
            and v not in ("", "{x:Null}")
            and not v.startswith("%")
            and _pct(c, total) >= 5
        ]

        if claimed_pct >= 80 and not unclaimed_significant:
            status  = PASS
            message = (f"Claimed values cover {claimed_pct}% of corpus branches. "
                       f"No significant unclaimed values.")
        elif claimed_pct >= 60:
            status  = WARN
            detail_parts = []
            if claimed_pct < 80:
                detail_parts.append(f"Claimed values only cover {claimed_pct}% of branches.")
            if unclaimed_significant:
                detail_parts.append(
                    f"Significant unclaimed values: "
                    f"{[(v, _pct(c, total)) for v, c in unclaimed_significant]}"
                )
            message = " ".join(detail_parts)
        else:
            status  = FAIL
            message = (
                f"Claimed values cover only {claimed_pct}% of corpus branches. "
                f"Top actual values: {[(v, _pct(c, total)) for v, c in top_values[:3]]}"
            )

        results.append({
            "test":     "VALUE_COVERAGE",
            "activity": activity,
            "status":   status,
            "message":  message,
            "detail": {
                "total_branches":      total,
                "claimed_values":      sorted(claimed_values),
                "claimed_value_pct":   claimed_pct,
                "top_corpus_values":   [(v, c, _pct(c, total)) for v, c in top_values],
                "unclaimed_significant": [(v, _pct(c, total)) for v, c in unclaimed_significant],
            },
        })

    return results


# ---------------------------------------------------------------------------
# Test 4 — Gaps (activities not in either tier)
# ---------------------------------------------------------------------------

def test_gaps(cond_by_activity: dict, min_gap_count: int) -> list:
    results = []

    for activity, data in sorted(cond_by_activity.items(),
                                 key=lambda x: -x[1]["total"]):
        if activity.startswith("_"):
            continue
        if activity in ALL_CLAIMED:
            continue
        if data["total"] < min_gap_count:
            continue

        total     = data["total"]
        type_dist = data["type_counter"]
        sv_pct    = _pct(type_dist.get("StoredValue", 0), total)
        udv_pct   = _pct(type_dist.get("UserDefinedValue", 0), total)

        # Suggest a tier based on corpus signal
        if sv_pct >= 75:
            suggestion = f"TIER_1 candidate ({sv_pct}% StoredValue)"
        elif udv_pct >= 75:
            suggestion = f"TIER_2 candidate ({udv_pct}% UserDefinedValue)"
        else:
            suggestion = f"MIXED — needs disambiguation rule (SV={sv_pct}% UDV={udv_pct}%)"

        top_ct = data["conditiontype_counter"].most_common(3)
        top_v  = [(v, c) for v, c in data["value_counter"].most_common(5)
                  if v and not v.startswith("%") and v not in ("{x:Null}",)][:3]

        results.append({
            "test":     "GAP",
            "activity": activity,
            "status":   GAP,
            "message":  f"{total} condition branches in corpus — not in either tier. {suggestion}",
            "detail": {
                "total_branches":     total,
                "StoredValue_pct":    sv_pct,
                "UserDefined_pct":    udv_pct,
                "top_conditiontypes": dict(top_ct),
                "top_values":         top_v,
                "suggestion":         suggestion,
            },
        })

    return results


# ---------------------------------------------------------------------------
# Test 5 — Default branch structure
# ---------------------------------------------------------------------------

def test_default_branch_structure(default_records: list) -> list:
    results = []
    total = len(default_records)

    if total == 0:
        results.append({
            "test":     "DEFAULT_BRANCH",
            "activity": "_all_",
            "status":   WARN,
            "message":  "No default branches found in corpus.",
            "detail":   {},
        })
        return results

    def _check(field, expected_val, label):
        actual = Counter(r[field] for r in default_records)
        match_count = actual.get(expected_val, 0)
        match_pct   = _pct(match_count, total)
        status  = PASS if match_pct >= 90 else (WARN if match_pct >= 70 else FAIL)
        message = (
            f"Claim: {label}={expected_val!r}. "
            f"Corpus: {match_pct}% match ({match_count}/{total}). "
            + (f"Other values: {dict(actual.most_common(3))}" if match_pct < 100 else "")
        )
        return {"test": "DEFAULT_BRANCH", "activity": "_defaults_",
                "status": status, "message": message,
                "detail": {"field": field, "expected": expected_val,
                           "match_pct": match_pct, "distribution": dict(actual)}}

    results.append(_check("Type",                 "StoredValue", "Type"))
    results.append(_check("UseBranchWhenTimeout", "True",        "UseBranchWhenTimeout"))
    results.append(_check("ConditionType",        "",            "ConditionType"))
    results.append(_check("UseStoredValue",       "False",       "UseStoredValue"))

    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_condition_records(records: list) -> dict:
    """
    Returns dict: { activity: {
        total, type_counter, conditiontype_counter, value_counter
    }}
    """
    out = {}
    for r in records:
        act = r["preceding"]
        if act not in out:
            out[act] = {
                "total":                0,
                "type_counter":         Counter(),
                "conditiontype_counter": Counter(),
                "value_counter":        Counter(),
            }
        d = out[act]
        d["total"] += 1
        d["type_counter"][r["Type"]] += 1
        d["conditiontype_counter"][r["ConditionType"]] += 1
        v = r["Value"].strip()
        if v and not v.startswith("%") and v not in ("{x:Null}",):
            d["value_counter"][v] += 1
    return out


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

STATUS_ORDER = {FAIL: 0, GAP: 1, WARN: 2, PASS: 3, INFO: 4}
STATUS_ICON  = {FAIL: "✗", GAP: "○", WARN: "△", PASS: "✓", INFO: "·"}


def print_report(all_results: list, show_passing: bool = False) -> None:
    by_test = defaultdict(list)
    for r in all_results:
        by_test[r["test"]].append(r)

    totals = Counter(r["status"] for r in all_results)

    print("\n" + "=" * 72)
    print("STRUCTUREBUILDER INSTRUCTION VALIDATION REPORT")
    print("=" * 72)
    print(f"  Total checks: {len(all_results)}")
    print(f"  {STATUS_ICON[PASS]} PASS: {totals[PASS]}  "
          f"{STATUS_ICON[WARN]} WARN: {totals[WARN]}  "
          f"{STATUS_ICON[FAIL]} FAIL: {totals[FAIL]}  "
          f"{STATUS_ICON[GAP]} GAP:  {totals[GAP]}")
    print()

    for test_name in ["TYPE_ASSIGNMENT", "CONDITIONTYPE", "VALUE_COVERAGE",
                      "DEFAULT_BRANCH", "GAP"]:
        results = by_test.get(test_name, [])
        if not results:
            continue

        section_titles = {
            "TYPE_ASSIGNMENT": "TEST 1 — Type Assignment",
            "CONDITIONTYPE":   "TEST 2 — ConditionType Coverage",
            "VALUE_COVERAGE":  "TEST 3 — Value Coverage (Tier 1)",
            "DEFAULT_BRANCH":  "TEST 4 — Default Branch Structure",
            "GAP":             "TEST 5 — Gaps (activities not in either tier)",
        }
        print("-" * 72)
        print(section_titles[test_name])
        print()

        sorted_results = sorted(results, key=lambda r: STATUS_ORDER[r["status"]])
        for r in sorted_results:
            if r["status"] == PASS and not show_passing:
                continue
            icon = STATUS_ICON[r["status"]]
            act  = r["activity"]
            msg  = r["message"]
            print(f"  {icon} {act}")
            print(f"    {msg}")

            # Extra detail for failures and gaps
            if r["status"] in (FAIL, GAP, WARN) and r.get("detail"):
                d = r["detail"]
                if test_name == "TYPE_ASSIGNMENT" and r["status"] != PASS:
                    print(f"    Corpus: SV={d.get('StoredValue_pct')}%  "
                          f"UDV={d.get('UserDefined_pct')}%  "
                          f"(n={d.get('total_branches')})")
                elif test_name == "CONDITIONTYPE":
                    if d.get("not_claimed_high_freq"):
                        print(f"    Add to claim:   {d['not_claimed_high_freq']}")
                    if d.get("listed_but_rare"):
                        print(f"    Remove/review:  {d['listed_but_rare']}")
                    top = list(d.get("corpus_distribution", {}).items())[:5]
                    print(f"    Corpus top:     {top}")
                elif test_name == "VALUE_COVERAGE":
                    print(f"    Top corpus:     {d.get('top_corpus_values', [])[:4]}")
                    if d.get("unclaimed_significant"):
                        print(f"    Missing values: {d['unclaimed_significant']}")
                elif test_name == "GAP":
                    print(f"    ConditionTypes: {d.get('top_conditiontypes')}")
                    print(f"    Top values:     {d.get('top_values')}")
            print()

        pass_count = sum(1 for r in results if r["status"] == PASS)
        non_pass   = len(results) - pass_count
        if show_passing or non_pass > 0:
            print(f"  ({pass_count}/{len(results)} passing"
                  + (f", {non_pass} need attention)" if non_pass else ")")
                  )
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Validate StructureBuilder instruction claims against corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--xml-dir", default=str(Path(__file__).parent / "workflows_raw"),
        help="Directory of workflow XML files"
    )
    parser.add_argument(
        "--confidence", type=float, default=0.75,
        help="Min fraction for Type assignment to PASS (default: 0.75)"
    )
    parser.add_argument(
        "--min-gap-count", type=int, default=3,
        help="Min condition branches for a gap activity to be reported (default: 3)"
    )
    parser.add_argument(
        "--conditiontype-min-pct", type=int, default=10,
        help="Min pct for a ConditionType to be considered 'high frequency' (default: 10)"
    )
    parser.add_argument(
        "--show-passing", action="store_true",
        help="Include passing checks in output (default: failures/gaps/warns only)"
    )
    parser.add_argument(
        "--output-json", default=None,
        help="Optional path to write full results as JSON"
    )
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        print(f"ERROR: {xml_dir} not found.")
        raise SystemExit(1)

    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files under {xml_dir}")
        raise SystemExit(1)

    print(f"Parsing {len(xml_files)} files...")
    condition_records = []
    default_records   = []
    n_ok = n_fail = 0

    for path in xml_files:
        root = parse_workflow_file(path)
        if root is None:
            n_fail += 1
            continue
        n_ok += 1
        walk(root, parent=None,
             condition_records=condition_records,
             default_records=default_records)

    print(f"  OK: {n_ok}  Failed: {n_fail}")
    print(f"  Condition branches: {len(condition_records)}")
    print(f"  Default branches:   {len(default_records)}")

    cond_by_activity = aggregate_condition_records(condition_records)

    # Run all tests
    all_results = []
    all_results += test_type_assignment(cond_by_activity, args.confidence)
    all_results += test_conditiontype_coverage(cond_by_activity, args.conditiontype_min_pct)
    all_results += test_value_coverage(cond_by_activity)
    all_results += test_default_branch_structure(default_records)
    all_results += test_gaps(cond_by_activity, args.min_gap_count)

    print_report(all_results, show_passing=args.show_passing)

    if args.output_json:
        out_path = Path(args.output_json)
        # Convert Counters to plain dicts for JSON serialisation
        serialisable = []
        for r in all_results:
            rc = dict(r)
            rc["detail"] = {
                k: dict(v) if isinstance(v, Counter) else v
                for k, v in rc.get("detail", {}).items()
            }
            serialisable.append(rc)
        out_path.write_text(json.dumps(serialisable, indent=2), encoding="utf-8")
        print(f"Full results written to {out_path}")


if __name__ == "__main__":
    main()