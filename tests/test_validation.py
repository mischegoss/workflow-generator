import os
os.environ["DATA_DIR"] = "./data"

from tools.validation_tools import run_all_validators

valid_wf = {
    "workflow_raw_data": {
        "getDate1": {
            "xName": "getDate1", "CustomTypeName": "GetDate",
            "Description": "Get date", "description": "Get date",
        },
        "whileActivity1": {
            "xName": "whileActivity1", "CustomTypeName": "WhileActivity",
            "whileSequenceActivity1": {
                "xName": "whileSequenceActivity1", "CustomTypeName": "SequenceActivity",
            },
            "exitWhile1": {
                "xName": "exitWhile1", "CustomTypeName": "ExitWhile",
                "Counter": "%getRowsCount1%",
            },
        },
    }
}
r = run_all_validators(valid_wf)
print("Valid workflow:", r["status"])
print("Errors:", r["errors"])
print("Verify notes:", len(r["verify_notes"]))

bad_wf = {
    "workflow_raw_data": {
        "act1": {
            "xName": "act1", "CustomTypeName": "GetDate",
            "Description": "Get date", "description": "Get date",
        },
        "act1_dup": {
            "xName": "act1",
            "CustomTypeName": "MemorySet",
            "Description": "Set var", "description": "Set var",
        },
        "while1": {
            "xName": "while1", "CustomTypeName": "WhileActivity",
            "Counter": "BAD",
            "seq1": {
                "xName": "seq1", "CustomTypeName": "SequenceActivity",
                "ExtraField": "should not be here",
            },
            "exitWhile1": {
                "xName": "exitWhile1", "CustomTypeName": "ExitWhile",
            },
        },
    }
}
r2 = run_all_validators(bad_wf)
print("\nInvalid workflow:", r2["status"])
for e in r2["errors"]:
    print(" ", e)