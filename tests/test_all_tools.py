import os
os.environ["DATA_DIR"] = "./data"
os.environ["OUTPUT_DIR"] = "./output"

print("=" * 60)
print("FULL PIPELINE TOOLS TEST — Birthday Workflow")
print("=" * 60)

# ── STAGE 1: DECOMPOSE ────────────────────────────────────────
print("\n[1] DECOMPOSE TOOLS")
from tools.decompose_tools import assess_complexity, estimate_activity_count, decompose_workflow

prompt = (
    "Get the current date, match against user birthdays in ServiceNow, "
    "create a list of first names and emails for people with a birthday today. "
    "Display the list. If there are no birthdays display no birthdays."
)
complexity = assess_complexity(prompt)
estimate = estimate_activity_count(prompt, complexity)
decomp_stub = decompose_workflow(prompt, complexity["complexity"])

print(f"  Complexity: {complexity['complexity']}")
print(f"  Loop signals: {complexity['loop_signals']}, Branch signals: {complexity['branch_signals']}")
print(f"  Estimated activities: {estimate['estimated_total']}")
print(f"  Routing: {estimate['routing']}")
assert estimate["routing"] == "single", "Should route to single pipeline"
assert estimate["estimated_total"] <= 25, "Should be under ceiling"
print("  PASS")

# ── STAGE 2: RETRIEVAL ────────────────────────────────────────
print("\n[2] RETRIEVAL TOOLS")
from tools.retrieval_tools import load_activity_list, retrieve_activities, validate_activity

load_activity_list()
queries = [
    ("get current date", "GetDate"),
    ("query servicenow records", "SNGetRecord"),
    ("count rows in table", "GetRowsCount"),
    ("get cell value from table", "GetCellValue"),
    ("store value in variable", "MemorySet"),
    ("display value in logs", "DisplayValue"),
    ("format date convert", "FormatDate"),
]
for query, expected in queries:
    results = retrieve_activities(query)
    top = results[0]["activity_name"] if results else None
    valid = validate_activity(expected)
    assert valid["valid"], f"{expected} not in activity list"
    print(f"  '{query}' → {top} (expected {expected}) {'✓' if top == expected else '~'}")

invalid = validate_activity("FakeInventedActivity")
assert not invalid["valid"], "Should reject invented activity"
print("  FakeInventedActivity correctly rejected ✓")
print("  PASS")

# ── STAGE 3: PATTERN MATCHING ─────────────────────────────────
print("\n[3] PATTERN TOOLS")
from tools.pattern_tools import (
    load_pattern_library, load_activity_ranks,
    match_pattern, score_pattern_match, get_examples_for_control_flow, check_cooccurrence
)

patterns = load_pattern_library()
ranks = load_activity_ranks()
assert len(patterns) > 0, "Pattern library should not be empty"
assert len(ranks) > 0, "Activity ranks should not be empty"
print(f"  Patterns loaded: {len(patterns)}")
print(f"  Rank pairs loaded: {len(ranks)}")

decomposition = {
    "steps": [
        {"description": "get current date"},
        {"description": "query servicenow birthday matches"},
        {"description": "count rows check birthdays"},
        {"description": "if no birthdays display message"},
        {"description": "loop over rows get name email display"},
    ],
    "variable_contract": {"loop_type": "While"},
}
candidates = match_pattern(decomposition)
result = score_pattern_match(candidates)
print(f"  Match status: {result['match_status']} (score={result['score']})")
print(f"  Fallback CF: {result['fallback_examples']}")

examples = get_examples_for_control_flow("While", max_examples=2)
assert len(examples) > 0, "Should find While examples"
print(f"  While examples found: {len(examples)}")
print(f"  Example source: {examples[0]['source_file']}")

warnings = check_cooccurrence(["WhileActivity", "GetCellValue", "MemorySet"])
assert any("GetRowsCount" in w["message"] for w in warnings), "Should warn about missing GetRowsCount"
print(f"  Co-occurrence warning: {warnings[0]['message'][:60]}...")
print("  PASS")

# ── STAGE 4: BUILD TOOLS ──────────────────────────────────────
print("\n[4] BUILD TOOLS")
from tools.build_tools import (
    load_activity_template, resolve_control_flow,
    fill_scaffold_params, generate_pnumber, generate_workflow_name
)

tmpl = load_activity_template("GetDate")
assert tmpl.get("CustomTypeName") == "GetDate", "GetDate template should load"
print(f"  GetDate template loaded: {list(tmpl.keys())[:4]}")

tmpl2 = load_activity_template("SNGetRecord")
assert tmpl2.get("CustomTypeName") == "SNGetRecord", "SNGetRecord template should load"
print(f"  SNGetRecord template loaded: {list(tmpl2.keys())[:4]}")

steps = [
    {"step_id": "s1", "description": "get date", "intent": "get_date", "control_flow": "linear"},
    {"step_id": "s2", "description": "loop over rows", "intent": "loop", "control_flow": "while"},
]
cf_result = resolve_control_flow(steps)
assert cf_result["control_flow_applied"] is True
assert len(cf_result["warnings"]) > 0, "Should warn about missing GetRowsCount"
print(f"  Control flow warning: {cf_result['warnings'][0][:60]}...")

scaffold = {
    "workflow_raw_data": {
        "getDate1": {"xName": "getDate1", "CustomTypeName": "GetDate", "VariableName": "PARAM_todayDate"},
        "snGet1": {"xName": "snGet1", "CustomTypeName": "SNGetRecord", "ResultSet": "PARAM_birthdayTable"},
    }
}
contract = {"variables": [
    {"name": "todayDate", "type": "string", "source": "GetDate output"},
    {"name": "birthdayTable", "type": "table", "source": "SNGetRecord output"},
]}
filled = fill_scaffold_params(scaffold, contract)
assert filled["workflow_raw_data"]["getDate1"]["VariableName"] == "%todayDate%"
assert filled["workflow_raw_data"]["snGet1"]["ResultSet"] == "%birthdayTable%"
print(f"  Scaffold fill: todayDate → {filled['workflow_raw_data']['getDate1']['VariableName']} ✓")
print(f"  Scaffold fill: birthdayTable → {filled['workflow_raw_data']['snGet1']['ResultSet']} ✓")

pnum = generate_pnumber()
assert pnum.startswith("WF-"), "Pnumber should start with WF-"
name = generate_workflow_name("Birthday Notification Workflow!")
assert " " not in name and "!" not in name, "Name should be safe"
print(f"  Pnumber: {pnum}")
print(f"  Safe name: {name}")
print("  PASS")

# ── STAGE 5: ANNOTATION TOOLS ────────────────────────────────
print("\n[5] ANNOTATION TOOLS")
from tools.annotation_tools import (
    annotate_placeholders, add_verify_notes,
    inject_unavailable_stubs, collect_placeholder_summary
)

wf = {
    "workflow_raw_data": {
        "getDate1": {
            "xName": "getDate1", "CustomTypeName": "GetDate",
            "Description": "Get date", "description": "Get date", "notes": "",
        },
        "snGet1": {
            "xName": "snGet1", "CustomTypeName": "SNGetRecord",
            "Description": "Query SN", "description": "Query SN", "notes": "",
            "SelectedModuleName": "myModule",
        },
        "sendEmail1": {
            "xName": "sendEmail1", "CustomTypeName": "SendEmail",
            "Description": "Send email", "description": "Send email", "notes": "",
            "SmtpServer": "mail.company.com",
            "Password": "secret123",
        },
    }
}

annotated = add_verify_notes(annotate_placeholders(wf))
raw = annotated["workflow_raw_data"]

assert raw["sendEmail1"]["SmtpServer"] == "PLACEHOLDER_SMTP_SERVER"
assert raw["sendEmail1"]["Password"] == "PLACEHOLDER_SMTP_PASS"
print(f"  SMTP replaced: SmtpServer={raw['sendEmail1']['SmtpServer']} ✓")

assert "VERIFY" in raw["snGet1"]["notes"]
assert "VERIFY" in raw["getDate1"]["notes"]
print(f"  SNGetRecord VERIFY note: present ✓")
print(f"  GetDate namespace VERIFY note: present ✓")

manifest = {"steps": [{"step_id": "s99", "status": "UNAVAILABLE", "query": "unknown action"}]}
stubbed = inject_unavailable_stubs(annotated, manifest)
assert "placeholder_s99" in stubbed["workflow_raw_data"]
print(f"  UNAVAILABLE stub injected: placeholder_s99 ✓")

summary = collect_placeholder_summary(annotated)
placeholder_count = sum(1 for i in summary if i["kind"] == "placeholder")
verify_count = sum(1 for i in summary if i["kind"] == "verify")
print(f"  Summary: {placeholder_count} placeholders, {verify_count} VERIFY notes")
print("  PASS")

# ── STAGE 6: VALIDATION TOOLS ────────────────────────────────
print("\n[6] VALIDATION TOOLS")
from tools.validation_tools import run_all_validators

valid_wf = {
    "workflow_raw_data": {
        "memorySet1": {
            "xName": "memorySet1", "CustomTypeName": "MemorySet",
            "Description": "Set var", "description": "Set var",
            "VariableName": "myVar", "VariableScope": "Workflow",
            "IsSaved": "False", "VariableValue": "%someValue%",
        },
        "displayValue1": {
            "xName": "displayValue1", "CustomTypeName": "DisplayValue",
            "Description": "Display", "description": "Display",
            "ValueToDisplay": "%myVar%",
        },
        "whileActivity1": {
            "xName": "whileActivity1", "CustomTypeName": "WhileActivity",
            "whileSeq1": {"xName": "whileSeq1", "CustomTypeName": "SequenceActivity"},
            "exitWhile1": {"xName": "exitWhile1", "CustomTypeName": "ExitWhile", "Counter": "%rowCount%"},
        },
    }
}
r = run_all_validators(valid_wf)
assert r["status"] == "valid", f"Should be valid, got errors: {r['errors']}"
print(f"  Valid workflow: {r['status']} ✓")

bad_wf = {
    "workflow_raw_data": {
        "dup1": {"xName": "dup1", "CustomTypeName": "DisplayValue",
                 "Description": "d", "description": "d", "ValueToDisplay": "x"},
        "dup2": {"xName": "dup1", "CustomTypeName": "MemorySet",
                 "Description": "d", "description": "d"},
        "while1": {
            "xName": "while1", "CustomTypeName": "WhileActivity",
            "Counter": "BAD",
            "exitWhile1": {"xName": "exitWhile1", "CustomTypeName": "ExitWhile"},
        },
    }
}
r2 = run_all_validators(bad_wf)
assert r2["status"] == "invalid"
assert any("Duplicate" in e for e in r2["errors"])
assert any("Counter" in e for e in r2["errors"])
print(f"  Invalid workflow caught {len(r2['errors'])} errors ✓")
print("  PASS")

# ── STAGE 7: COMPOSE TOOLS ────────────────────────────────────
print("\n[7] COMPOSE TOOLS")
from tools.compose_tools import serialize_to_xml, write_output_file, format_chat_response

final_wf = {
    "workflow_raw_data": {
        "getDate1": {
            "xName": "getDate1", "CustomTypeName": "GetDate",
            "Description": "Get date", "description": "Get date",
            "FuturePast": "Current", "TimeInterval": "Day",
            "TimeToAdd": "0", "DateFormat": "MM/dd/yyyy",
            "VariableName": "todayDate", "VariableScope": "Workflow",
        },
        "displayValue1": {
            "xName": "displayValue1", "CustomTypeName": "DisplayValue",
            "Description": "Display", "description": "Display",
            "ValueToDisplay": "%todayDate%",
        },
    }
}

xml = serialize_to_xml(final_wf, "BirthdayWorkflow", "WF-BDAY001")
assert xml.startswith('<?xml version="1.0"')
assert "TotalExport" in xml
assert "BirthdayWorkflow" in xml
assert "&lt;" in xml
print(f"  XML length: {len(xml)} chars ✓")

file_result = write_output_file(xml, "BirthdayWorkflow")
assert os.path.exists(file_result["output_file"])
print(f"  Written to: {file_result['output_file']} ✓")

summary_items = [
    {"kind": "placeholder", "activity": "snGet1", "type": "SNGetRecord",
     "field": "SelectedModuleName", "placeholder": "PLACEHOLDER_SN_MODULE"},
    {"kind": "verify", "activity": "snGet1", "type": "SNGetRecord",
     "field": "notes", "message": "VERIFY: XMLTableResult requires manual config"},
]
chat = format_chat_response(
    {"status": "valid", "errors": [], "verify_notes": []},
    file_result,
    summary_items
)
assert "STATUS: incomplete" in chat
assert "PLACEHOLDER_SN_MODULE" in chat
assert "VERIFY" in chat
print(f"  Chat response status: incomplete ✓")
print(f"  PLACEHOLDER present: ✓")
print(f"  VERIFY note present: ✓")
print("  PASS")

# ── FINAL SUMMARY ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("ALL 7 TOOL MODULES PASSED")
print("=" * 60)