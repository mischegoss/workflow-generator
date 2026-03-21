import os
os.environ["DATA_DIR"] = "./data"

from serializer.xml_composer import WorkflowXmlComposer

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
        "memorySet1": {
            "xName": "memorySet1",
            "CustomTypeName": "MemorySet",
            "Description": "Store result",
            "description": "Store result",
            "VariableName": "myVar",
            "VariableScope": "Workflow",
            "IsSaved": "False",
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

composer = WorkflowXmlComposer()
xml = composer.compose(wf, "TestWorkflow", "WF-TEST001")
print(xml[:2000])