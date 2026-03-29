"""
patch_output_manual.py

Second-pass patch for the 87 unresolved entries that patch_output.py
couldn't resolve automatically.

The auto-patch resolved 248 entries correctly. These 87 remain because their
display names don't appear as DisplayName values in activity_json_syntax.json
(the syntax file uses the same string for CustomTypeName and DisplayName for
most activities, so the display_to_custom mapping was empty).

Run AFTER patch_output.py from repo root:
  python3 patch_output_manual.py

Safe to re-run — skips entries that are already correct.
"""

import json
from pathlib import Path

REGISTRY_FILE = Path("data/activity_output_registry.json")

# Display name → CustomTypeName for the 87 unresolved entries.
# Verified against activity_list.txt where possible.
# Third-party integrations marked with ??? where CustomTypeName is uncertain
# — those are skipped by this script and remain flagged.
MANUAL_MAPPINGS = {
    # Core platform activities — high confidence
    "File Exists":              "FileExist",
    "Folder Exists":            "FolderExist",
    "Get Windows Event Logs":   "GetWindowEventLogs",
    "Hash Check":               "FileChecksumComparison",
    "List Folder":              "ListFolderBox",
    "Read Excel":               "ReadXLS",
    "Write Excel":              "ExcelWrite",
    "Get CPU":                  "CPU",
    "Get Disk Space":           "DiskSpace",
    "Get Memory":               "Memory",
    "Get Operating System":     "OperatingSystem",
    "Get Length":               "GetLength",
    "Get Name Without Extension": "FileGetNameWithoutExt",
    "Get File Extension":       "FileGetExt",
    "Get File Path":            "FileGetPath",
    "Get File Root":            "FileGetRoot",
    "Get File Size":            "FileSize",
    "Get File Version":         "FileVersion",
    "Get Folder Size":          "FolderSize",
    "Get Volume List":          "VolumeList",
    "Get Installed Software":   "GetInstalledSoftware",
    "Get Process CPU Usage":    "ProcessCPUUsage",
    "Get Process Memory Usage": "ProcessMemoryUsage",
    "Get Process Owner":        "ProcessOwner",
    "Get Service Startup Type": "ServiceStartupType",
    "Process List":             "ProcessList",
    "InStr Reverse":            "InStrRev",
    "Start Process":            "ProcessStart",
    "Kill Process":             "ProcessKill",
    "Resume Service":           "ServiceResume",
    "Sign":                     "Sgn",
    "Square":                   "Sqr",
    "Power":                    "Pow",
    "Trace Route":              "TraceRoute",
    "Copy File":                "FileCopy",
    "Move File":                "FileMove",
    "Copy Folder":              "FolderCopy",
    "Delete Registry":          "DeleteRegistryKey",
    "Query Registry Value":     "QueryRegistryValue",
    "IIS Reset":                "IISReset",
    "IIS Start":                "IISStart",
    "IIS Stop":                 "IISStop",
    "Submit File":              "SubmitFile",
    "Zip Compression":          "ZipCompression",
    "Zip Decompression":        "ZipDecompression",
    "File Download":            "FileDownload",
    "Download Email Attachment": "DownloadEmailAttachment",
    "Generate Password":        "GeneratePassword",
    "WMI Query":                "WMIQuery",
    "SNMP Get Request":         "SNMPGetRequest",
    "SNMP Get Next Request":    "SNMPGetNextRequest",
    "Add XML Attribute":        "AddXMLAttribute",
    "Add XML Node":             "AddXMLNode",
    "Edit XML Attribute":       "EditXMLAttribute",
    "Edit XML Node":            "EditXMLNode",
    "Delete XML Attribute":     "DeleteXMLAttribute",
    "Jira General Command":     "JiraGenericCommand",
    "AD Computer Last Logged In Date": "ADComputerLoggedInDate",
    "AD User Last Logged In Date":     "ADUserLoggedInDate",

    # AWS activities
    "AWS Copy Image":           "AWSCopyImage",
    "AWS Execute CLI":          "AWSSSMCommandExecute",
    "AWS Reboot Instance":      "AWSRebootInstance",
    "AWS Start Instance":       "AWSStartInstance",
    "AWS Stop Instance":        "AWSStopInstance",
    "AWS Terminate Instance":   "AWSTerminateInstance",

    # VM / HyperV
    "VM List Templates":        "HyperVListTemplates",
    "VM IP Address":            "HyperVIPAddress",
    "VM Host List":             "HyperVList",
    "HyperV Refresh":           "HyperVRefreshVM",
    "Server Hibernate":         "Hibernate",
    "Server Standby":           "Standby",

    # SolarWinds
    "SolarWinds NPM Get Alert":     "SLNPMGetAlert",
    "SolarWinds NPM Get Node":      "SLNPMGetNode",
    "SolarWinds NPM Add Note":      "SLNPMAddNote",
    "SolarWinds NPM Manage Node":   "SLNPMManageNode",
    "SolarWinds NPM Unmanage Node": "SLNPMUnManageNode",
    "SolarWinds Acknowledge Alert": "SLNPMAcknowledgeAlert",
    "SolarWinds Unacknowledge Alert": "SLNPMUnacknowledgeAlert",

    # Third-party integrations — CustomTypeNames uncertain, leaving as display name
    # for now. Category 6 will remain disabled for these until confirmed.
    # "BMC Remedyforce Get Record":           leave unresolved
    # "BMC TrueSight Operations Management":  leave unresolved
    # "Cherwell Get Attachments List":        leave unresolved
    # "Is Alert in HTML Format":              leave unresolved
}


def main():
    with open(REGISTRY_FILE) as f:
        registry = json.load(f)

    updated = 0
    skipped = 0
    not_found = 0

    for entry in registry:
        name = entry["activityName"]
        if name in MANUAL_MAPPINGS:
            new_name = MANUAL_MAPPINGS[name]
            if new_name != name:
                entry["activityName"] = new_name
                updated += 1
            else:
                skipped += 1  # already correct

    with open(REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)

    remaining_unresolved = [
        e["activityName"] for e in registry
        if " " in e["activityName"]  # display names have spaces; CustomTypeNames don't
    ]

    print(f"=== Manual patch results ===")
    print(f"  Updated:   {updated}")
    print(f"  Skipped (already correct): {skipped}")
    print(f"  Total entries: {len(registry)}")
    print()
    if remaining_unresolved:
        print(f"=== {len(remaining_unresolved)} still unresolved (Category 6 disabled for these) ===")
        for name in remaining_unresolved:
            print(f"  {name}")
    else:
        print("All entries resolved.")
    print()
    print(f"Done. Written to {REGISTRY_FILE}")


if __name__ == "__main__":
    main()