import os
os.environ["DATA_DIR"] = "./data"
os.environ["OUTPUT_DIR"] = "./output"

from tools.compose_tools import serialize_to_xml, write_output_file, format_chat_response

wf = {
    "workflow_raw_data": {
        "getDate1": {
            "xName": "getDate1",
            "CustomTypeName": "GetDate",
            "Description": "Get current date",
            "description": "Get current date",
            "FuturePast": "Current",
            "TimeInterval": "Day",
            "TimeToAdd": "0",
            "DateFormat": "MM/dd/yyyy",
            "VariableName": "todayDate",
            "VariableScope": "Workflow",
        },
        "displayValue1": {
            "xName": "displayValue1",
            "CustomTypeName": "DisplayValue",
            "Description": "Display result",
            "description": "Display result",
            "ValueToDisplay": "%todayDate%",
        },
    }
}

# Test 1: serialize to XML
xml = serialize_to_xml(wf, "TestWorkflow", "WF-TEST001")
print("XML length:", len(xml))
print("Starts correctly:", xml.startswith('<?xml version="1.0"'))
print("Contains TotalExport:", "TotalExport" in xml)
print("Contains WorkflowInfo:", "WorkflowInfo" in xml)
print()

# Test 2: write to file
result = write_output_file(xml, "TestWorkflow")
print("Output file:", result["output_file"])
print("File exists:", os.path.exists(result["output_file"]))
print()

# Test 3: format chat response — complete workflow
validation_ok = {"status": "valid", "errors": [], "verify_notes": []}
placeholder_summary = []
response = format_chat_response(validation_ok, result, placeholder_summary)
print("=== Complete workflow response ===")
print(response)
print()

# Test 4: format chat response — incomplete workflow with placeholders
placeholder_summary_with_items = [
    {"kind": "placeholder", "activity": "snGet1", "type": "SNGetRecord",
     "field": "SelectedModuleName", "placeholder": "PLACEHOLDER_SN_MODULE"},
    {"kind": "verify", "activity": "snGet1", "type": "SNGetRecord",
     "field": "notes", "message": "VERIFY: XMLTableResult requires manual UI config"},
]
validation_with_notes = {
    "status": "valid",
    "errors": [],
    "verify_notes": ["[.getDate1] 'GetDate' has no controls entry — check manually"],
}
response2 = format_chat_response(validation_with_notes, result, placeholder_summary_with_items)
print("=== Incomplete workflow response ===")
print(response2)
