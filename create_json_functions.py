import json
from collections import defaultdict

# Define the base properties and their JSON Schema types
base_properties = {
    "xName": {"type": "string"},
    "activityLicenseType": {"type": "string"},
    "id": {"type": "string"},
    "name": {"type": "string"},
    "visible": {"type": "string"},
    "disabled": {"type": "string"},
    "isFavorite": {"type": "string"},
    "isJsonValid": {"type": "string"},
    "readPermission": {"type": "boolean"},
    "writePermission": {"type": "boolean"},
    "modulePermissions": {"type": ["object", "null"]},
    "IsValid": {"type": "string"},
    "Timeout": {"type": "string"},
    "TimeInSeconds": {"type": "string"},
    "RecoveryMethodSelection": {"type": ["string", "null"]},
    "Path": {"type": ["string", "null"]},
    "DisplayName": {"type": "string"},
    "Description": {"type": "string"},
    "TargetModuleID": {"type": "string"},
    "TargetModuleName": {"type": "string"},
    "TypeName": {"type": "string"},
    "label": {"type": "string"},
    "description": {"type": "string"},
    "notes": {"type": "string"},
    "CustomTypeName": {"type": "string"}
}


def get_type(value):
    if isinstance(value, bool):
        return "boolean"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "number"
    elif value is None:
        return ["null"]
    elif isinstance(value, dict):
        return "object"
    elif isinstance(value, list):
        return "array"
    else:
        return "string"


# Replace with the path to your file
with open("activity_json_syntax.json", "r") as f:
    data = json.load(f)

# Group activities by TypeName (activity type)
activities_by_type = defaultdict(list)
for activity in data["settings"]:
    activity_type = activity.get("TypeName", "UnknownActivityType")
    activities_by_type[activity_type].append(activity)

# Generate schemas for each activity type
for activity_type, acts in activities_by_type.items():
    # Start with the base properties
    schema_properties = base_properties.copy()
    required = set(base_properties.keys())

    # Add/extend with activity-specific properties
    for act in acts:
        for k, v in act.items():
            if k not in schema_properties:
                schema_properties[k] = {"type": get_type(v)}
                required.add(k)

    schema = {
        "type": "object",
        "properties": schema_properties,
        "required": sorted(list(required))
    }

    function_schema = {
        "type": "function",
        "function": {
            "name": f"create_{activity_type.lower()}_activity",
            "description": f"Creates a {activity_type} activity.",
            "parameters": schema
        },
        "strict": True
    }

    print(json.dumps(function_schema, indent=2))
    print("\n" + "=" * 60 + "\n")