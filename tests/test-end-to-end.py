import os
os.environ["DATA_DIR"] = "./data"
os.environ["OUTPUT_DIR"] = "./output"

print("=" * 60)
print("FULL PIPELINE TOOLS TEST — Certificate Expiry Workflow")
print("=" * 60)

# ── STAGE 1: DECOMPOSE ────────────────────────────────────────
print("\n[1] DECOMPOSE TOOLS")
from tools.decompose_tools import assess_complexity, estimate_activity_count, decompose_workflow

prompt = (
    "Create a workflow that stores expiration dates for security certificates "
    "and sends an email 5 days before a security certificate will expire. "
    "The workflow should loop through each certificate, calculate the days "
    "remaining until expiration, and if 5 or fewer days remain send a reminder email."
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
    ("create memory table store data", "CreateMemoryTable"),
    ("count rows in table", "GetRowsCount"),
    ("get current date", "GetDate"),
    ("get cell value from table", "GetCellValue"),
    ("calculate date difference days remaining", "DateDifference"),
    ("send email notification", "SendEmail"),
    ("display value in logs", "DisplayValue"),
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
        {"description": "create memory table with certificate names and expiration dates"},
        {"description": "count rows in certificate table"},
        {"description": "get current date"},
        {"description": "loop through each certificate row"},
        {"description": "get expiration date from current row"},
        {"description": "get server name from current row"},
        {"description": "calculate days difference between today and expiration date"},
        {"description": "if days remaining 5 or fewer send reminder email"},
    ],
    "variable_contract": {"loop_type": "While"},
}
candidates = match_pattern(decomposition)
result = score_pattern_match(candidates)
print(f"  Match status: {result['match_status']} (score={result['score']})")
print(f"  Fallback CF: {result['fallback_examples']}")

examples = get_examples_for_control_flow("While+IfElse", max_examples=2)
print(f"  While+IfElse examples found: {len(examples)}")
if examples:
    print(f"  Example source: {examples[0]['source_file']}")

warnings = check_cooccurrence(["CreateMemoryTable", "GetRowsCount", "WhileActivity", "GetCellValue", "DateDifference"])
print(f"  Co-occurrence warnings: {len(warnings)}")
print("  PASS")

# ── STAGE 4: BUILD TOOLS ──────────────────────────────────────
print("\n[4] BUILD TOOLS")
from tools.build_tools import (
    load_activity_template, resolve_control_flow,
    fill_scaffold_params, generate_pnumber, generate_workflow_name
)

for activity in ["CreateMemoryTable", "GetRowsCount", "GetDate", "GetCellValue", "DateDifference", "SendEmail"]:
    tmpl = load_activity_template(activity)
    assert tmpl.get("CustomTypeName") == activity, f"{activity} template should load"
    print(f"  {activity} template loaded ✓")

steps = [
    {"step_id": "s1", "description": "create memory table", "intent": "other", "control_flow": "linear"},
    {"step_id": "s2", "description": "count rows", "intent": "count_rows", "control_flow": "linear"},
    {"step_id": "s3", "description": "get date", "intent": "get_date", "control_flow": "linear"},
    {"step_id": "s4", "description": "loop through certs", "intent": "loop", "control_flow": "while"},
]
cf_result = resolve_control_flow(steps)
assert cf_result["control_flow_applied"] is True
print(f"  Control flow resolved ✓")

pnum = generate_pnumber()
assert pnum.isdigit(), "Pnumber should be numeric"
name = generate_workflow_name("Certificate Expiry Email Reminder")
assert " " not in name
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
        "getCertDate1": {
            "xName": "getCertDate1", "CustomTypeName": "GetDate",
            "Description": "Get current date", "description": "Get current date", "notes": "",
        },
        "sendEmail1": {
            "xName": "sendEmail1", "CustomTypeName": "SendEmail",
            "Description": "Send expiry reminder", "description": "Send expiry reminder", "notes": "",
            "SmtpServer": "mail.company.com",
            "Password": "secret123",
            "To": "admin@company.com",
            "Subject": "Certificate expiring: %serverName%",
        },
        "dateDiff1": {
            "xName": "dateDiff1", "CustomTypeName": "DateDifference",
            "Description": "Calculate days to expiry", "description": "Calculate days to expiry", "notes": "",
            "FirstDate": "%currentDate%",
            "SecondDate": "%expiryDate%",
        },
    }
}

annotated = add_verify_notes(annotate_placeholders(wf))
raw = annotated["workflow_raw_data"]

assert raw["sendEmail1"]["SmtpServer"] == "PLACEHOLDER_SMTP_SERVER"
assert raw["sendEmail1"]["Password"] == "PLACEHOLDER_SMTP_PASS"
print(f"  SMTP replaced: SmtpServer={raw['sendEmail1']['SmtpServer']} ✓")
assert "VERIFY" in raw["getCertDate1"]["notes"]
print(f"  GetDate namespace VERIFY note: present ✓")

summary = collect_placeholder_summary(annotated)
placeholder_count = sum(1 for i in summary if i["kind"] == "placeholder")
verify_count = sum(1 for i in summary if i["kind"] == "verify")
print(f"  Summary: {placeholder_count} placeholders, {verify_count} VERIFY notes")
print("  PASS")

# ── STAGE 6: VALIDATION TOOLS ────────────────────────────────
print("\n[6] VALIDATION TOOLS")
from tools.validation_tools import run_all_validators

# Valid cert workflow structure
valid_wf = {
    "workflow_raw_data": {
        "createCertTable1": {
            "xName": "createCertTable1", "CustomTypeName": "CreateMemoryTable",
            "Description": "Create cert table", "description": "Create cert table",
            "TableName": "certTable", "ColumnNumber": "3", "RowNumber": "3",
        },
        "getRowCount1": {
            "xName": "getRowCount1", "CustomTypeName": "GetRowsCount",
            "Description": "Count certs", "description": "Count certs",
            "ResultSet": "%createCertTable1%",
        },
        "getDate1": {
            "xName": "getDate1", "CustomTypeName": "GetDate",
            "Description": "Get today", "description": "Get today",
             "FuturePast": "Current", "TimeInterval": "Days",
            "TimeToAdd": "0", "DateFormat": "MM/dd/yyyy",
            "TimeZoneName": "UTC",
},
        "whileActivity1": {
            "xName": "whileActivity1", "CustomTypeName": "WhileActivity",
            "whileSeq1": {"xName": "whileSeq1", "CustomTypeName": "SequenceActivity"},
            "exitWhile1": {
                "xName": "exitWhile1", "CustomTypeName": "ExitWhile",
                "Counter": "%getRowCount1%",
            },
        },
    }
}
r = run_all_validators(valid_wf)
assert r["status"] == "valid", f"Should be valid, got errors: {r['errors']}"
print(f"  Valid cert workflow: {r['status']} ✓")

# Invalid — SequenceActivity inside While with extra fields
bad_wf = {
    "workflow_raw_data": {
        "while1": {
            "xName": "while1", "CustomTypeName": "WhileActivity",
            "seq1": {
                "xName": "seq1", "CustomTypeName": "SequenceActivity",
                "ExtraField": "not allowed",
                "Description": "not allowed either",
            },
            "exitWhile1": {"xName": "exitWhile1", "CustomTypeName": "ExitWhile"},
        },
    }
}
r2 = run_all_validators(bad_wf)
assert r2["status"] == "invalid"
assert any("SequenceActivity" in e for e in r2["errors"])
assert any("ExitWhile" in e for e in r2["errors"])
print(f"  Invalid workflow caught {len(r2['errors'])} errors ✓")
print("  PASS")

# ── STAGE 7: COMPOSE TOOLS ────────────────────────────────────
print("\n[7] COMPOSE TOOLS")
from tools.compose_tools import serialize_to_xml, write_output_file, format_chat_response

# Cert workflow matching the sample structure
cert_wf = {
    "workflow_raw_data": {
        "certTable1": {
            "xName": "certTable1", "CustomTypeName": "CreateMemoryTable",
            "Description": "Create certificate expiration table",
            "description": "Create certificate expiration table",
            "TableName": "certificates_expiration_dates",
            "ColumnNumber": "3", "RowNumber": "3",
        },
        "getCertRowCount1": {
            "xName": "getCertRowCount1", "CustomTypeName": "GetRowsCount",
            "Description": "Count certificates", "description": "Count certificates",
            "ResultSet": "%certTable1%",
        },
        "getCurrentDate1": {
            "xName": "getCurrentDate1", "CustomTypeName": "GetDate",
            "Description": "Get current date", "description": "Get current date",
            "FuturePast": "Current", "TimeInterval": "Days",
            "TimeToAdd": "0", "DateFormat": "MM/dd/yyyy",
            "notes": "",
        },
        "loopCerts1": {
            "xName": "loopCerts1", "CustomTypeName": "WhileActivity",
            "whileSeq1": {"xName": "whileSeq1", "CustomTypeName": "SequenceActivity"},
            "exitWhile1": {
                "xName": "exitWhile1", "CustomTypeName": "ExitWhile",
                "Counter": "%getCertRowCount1%",
            },
            "getExpDate1": {
                "xName": "getExpDate1", "CustomTypeName": "GetCellValue",
                "Description": "Get expiration date", "description": "Get expiration date",
                "ResultSet": "%certTable1%", "RowNumber": "%loopCerts1%",
                "ColumnNumber": "Cert Dates",
            },
            "getServerName1": {
                "xName": "getServerName1", "CustomTypeName": "GetCellValue",
                "Description": "Get server name", "description": "Get server name",
                "ResultSet": "%certTable1%", "RowNumber": "%loopCerts1%",
                "ColumnNumber": "Server",
            },
            "dateDiff1": {
                "xName": "dateDiff1", "CustomTypeName": "DateDifference",
                "Description": "Calculate days to expiry", "description": "Calculate days to expiry",
                "FirstDate": "%getCurrentDate1%", "SecondDate": "%getExpDate1%",
                "ReturnFormat": "Days",
            },
        },
    }
}

xml = serialize_to_xml(cert_wf, "CertificateExpiryReminder", "WF-CERT001")
assert xml.startswith('<?xml version="1.0"')
assert "TotalExport" in xml
assert "CertificateExpiryReminder" in xml
assert "&lt;" in xml
print(f"  XML length: {len(xml)} chars ✓")

file_result = write_output_file(xml, "CertificateExpiryReminder")
assert os.path.exists(file_result["output_file"])
print(f"  Written to: {file_result['output_file']} ✓")

# Write to project root for inspection
root_xml_path = "./test_certificate_expiry_workflow.xml"
with open(root_xml_path, "w", encoding="utf-8") as f:
    f.write(xml)
print(f"  XML written to project root: {root_xml_path} ✓")

summary_items = [
    {"kind": "placeholder", "activity": "sendEmail1", "type": "SendEmail",
     "field": "TargetModuleName", "placeholder": "PLACEHOLDER_EMAIL_MODULE"},
    {"kind": "verify", "activity": "getCurrentDate1", "type": "GetDate",
     "field": "notes", "message": "VERIFY: CLR namespace for GetDate not confirmed"},
    {"kind": "verify", "activity": "certTable1", "type": "CreateMemoryTable",
     "field": "notes", "message": "VERIFY: CLR namespace for CreateMemoryTable not confirmed"},
]
chat = format_chat_response(
    {"status": "valid", "errors": [], "verify_notes": []},
    file_result,
    summary_items
)
assert "STATUS: incomplete" in chat
assert "PLACEHOLDER_EMAIL_MODULE" in chat
assert "VERIFY" in chat
print(f"  Chat response status: incomplete ✓")
print(f"  PLACEHOLDER present: ✓")
print(f"  VERIFY note present: ✓")
print("  PASS")

# ── FINAL SUMMARY ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("ALL 7 TOOL MODULES PASSED")
print("=" * 60)