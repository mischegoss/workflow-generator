#!/usr/bin/env python3
"""
build_activity_categories.py

Merges three sources to produce data/activity_categories.json:

  1. docs — data/docs_categories_source.json (from scrape_docs_categories.py).
     Vendor-authoritative. ~309 activities, uses docs-style category names
     and includes subcategory where applicable.

  2. rules — name-prefix and exact-match rules in CATEGORY_RULES below.
     Produces the SAME category vocabulary as docs so the two sources are
     consistent. Covers activities the scraped docs didn't reach
     (BMC Helix, Cherwell, SAP, ChatGPT, Math, Flow Control, etc.).

  3. uncategorized — activities that match neither. Written to the output
     JSON with category="Uncategorized" for hand-editing.

Priority: docs > rules > uncategorized. When docs and rules both classify
an activity, docs wins.

Output schema per activity:
    "ADCreateAccount": {
      "category":    "Active Directory",
      "subcategory": "Accounts",
      "display":     "AD Create Account",
      "source":      "docs"
    }

Usage:
    python3 build_activity_categories.py
    python3 build_activity_categories.py --samples-per-category 3

Stdlib only.
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════
# DISPLAY LABEL GENERATION (for activities not in docs)
# ═══════════════════════════════════════════════════════════════════════

# Compound words that must NOT be camelCase-split
COMPOUNDS = [
    ("PowerShell",  "PowerShell"),
    ("ServiceNow",  "ServiceNow"),
    ("ServiceDesk", "ServiceDesk"),
    ("SharePoint",  "SharePoint"),
    ("DropBox",     "Dropbox"),
    ("OneDrive",    "OneDrive"),
    ("NetBackup",   "NetBackup"),
    ("ChatGPT",     "ChatGPT"),
    ("MySQL",       "MySQL"),
    ("PostgreSQL",  "PostgreSQL"),
    ("VMware",      "VMware"),
    ("vCenter",     "vCenter"),
]

# Multi-letter tokens kept uppercase in display output
ACRONYMS = {
    "AD", "API", "AWS", "CPU", "CSV", "DB", "DB2", "DLL", "DNS",
    "FTP", "HTML", "HTTP", "HTTPS", "IIS", "IP", "JSON", "LDAP",
    "MS", "NG", "OS", "PDF", "RN", "SCCM", "SFTP", "SMB", "SMTP",
    "SN", "SNMP", "SNOW", "SQL", "SSH", "SSL", "TLS", "UDP", "UI",
    "URL", "VM", "WMI", "XLS", "XLSX", "XML", "YAML", "VPC",
}


def _protect_compounds(name: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}
    working = name
    for i, (compound, display) in enumerate(COMPOUNDS):
        if compound in working:
            marker = f"Compound{i:02d}Placeholder"
            working = working.replace(compound, marker)
            placeholders[marker.lower()] = display
    return working, placeholders


def split_camel(name: str) -> list[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"[_\-]", " ", s)
    return [w for w in s.split() if w]


def to_display(name: str) -> str:
    working, placeholders = _protect_compounds(name)
    words = split_camel(working)
    out = []
    for w in words:
        wl = w.lower()
        if wl in placeholders:
            out.append(placeholders[wl])
        elif w.upper() in ACRONYMS:
            out.append(w.upper())
        elif len(w) <= 2 and w.isupper():
            out.append(w.upper())
        elif not w:
            continue
        else:
            out.append(w[0].upper() + w[1:].lower())
    return " ".join(out)


# ═══════════════════════════════════════════════════════════════════════
# NORMALIZATION (for docs matching)
# ═══════════════════════════════════════════════════════════════════════

def normalize(name: str) -> str:
    """
    Collapse to match key: lowercase, alphanumeric only.
    'ADCreateAccount' and 'AD Create Account' both normalize to
    'adcreateaccount', enabling direct lookup against docs.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ═══════════════════════════════════════════════════════════════════════
# CATEGORY RULES (all categories use docs vocabulary)
# ═══════════════════════════════════════════════════════════════════════
# ORDER MATTERS — first match wins. Rules for activities whose prefix or
# name would ALSO match a broader rule below must appear first.
#
# Two specific ordering hazards worth calling out:
#   - SNMP* must match before SN* (else SNMP-prefixed goes to ServiceNow).
#   - Specific Convert* exacts (Math, Time Controls, Tables, Development)
#     must appear before the generic `prefix Convert → Text`.

CATEGORY_RULES: list[tuple[str, str, str]] = [
    # ───── Active Directory ─────
    ("prefix", "AzureAD",  "Active Directory"),  # before Azure* and AD*
    ("prefix", "AD",       "Active Directory"),

    # ───── Amazon EC2 ─────
    ("prefix", "AWS",      "Amazon EC2"),

    # ───── Azure ─────
    ("prefix", "Azure",    "Azure"),

    # ───── BMC Helix Remedyforce ─────
    ("prefix", "BMCHelix", "BMC Helix Remedyforce"),

    # ───── BMC Remedyforce ─────
    ("prefix", "BMCRemedy",   "BMC Remedyforce"),
    ("prefix", "RemedyForce", "BMC Remedyforce"),

    # ───── BMC TrueSight Operations Management ─────
    ("prefix", "TSOM",     "BMC TrueSight Operations Management"),
    ("prefix", "BMCITSM",  "BMC TrueSight Operations Management"),

    # ───── CA Spectrum ─────
    ("prefix", "CAS",      "CA Spectrum"),

    # ───── Cherwell ─────
    ("prefix", "Cherwell", "Cherwell"),

    # ───── Cisco ─────
    ("prefix", "Cisco",          "Cisco"),
    ("prefix", "StartCisco",     "Cisco"),
    ("prefix", "TerminateCisco", "Cisco"),
    ("exact",  "SendCiscoCommand","Cisco"),

    # ───── Communication ─────
    ("prefix", "SMTP",       "Communication"),
    ("prefix", "SendEmail",  "Communication"),
    ("exact",  "SendRN",     "Communication"),
    ("exact",  "SendText",   "Communication"),
    ("exact",  "SendSMS",    "Communication"),
    ("prefix", "PlivoSend",  "Communication"),
    ("exact",  "AttachmentList", "Communication"),
    ("exact",  "SendSyslog", "Communication"),
    ("exact",  "WaitForEmail","Communication"),
    ("contains", "Email",    "Communication"),
    ("prefix", "MsTeams",    "Communication"),

    # ───── Compression ─────
    ("prefix", "Zip",        "Compression"),

    # ───── CyberArk ─────
    ("prefix", "CyberArk",   "CyberArk"),

    # ───── Database ─────
    ("prefix", "TSQL",       "Database"),
    ("prefix", "MySQL",      "Database"),
    ("prefix", "Oracle",     "Database"),
    ("prefix", "DB2",        "Database"),
    ("prefix", "PostgreSQL", "Database"),
    ("exact",  "ADLDAPQuery","Database"),

    # ───── Development ─────
    # Includes ConvertPasswordToPlaintext before the generic Convert rule
    ("prefix", "PowerShell",  "Development"),
    ("prefix", "Powershell",  "Development"),
    ("exact",  "Executor",       "Development"),
    ("exact",  "UnlockExecutor", "Development"),
    ("exact",  "LockExecutor",   "Development"),
    ("exact",  "RunWorkflow",    "Development"),
    ("exact",  "StopWorkflow",   "Development"),
    ("exact",  "PythonCode",     "Development"),
    ("exact",  "RunRemoteExecutable",     "Development"),
    ("exact",  "ConvertPasswordToPlaintext","Development"),
    ("prefix", "ChatGPT",  "Development"),
    ("prefix", "Ayehu",    "Development"),
    ("prefix", "AY",       "Development"),
    ("exact",  "UserAuthentication",       "Development"),
    ("exact",  "MicrosoftGetanaccesstoken","Development"),
    ("exact",  "BuildNetsuiteAuth",        "Development"),
    ("prefix", "Duo",      "Development"),

    # ───── FTP ─────  (must match before generic File/Folder rules)
    ("prefix", "FTP",  "FTP"),
    ("prefix", "SFTP", "FTP"),
    ("exact",  "StartFTPSession",     "FTP"),
    ("exact",  "TerminateFTPSession", "FTP"),

    # ───── Files and Folders ─────
    ("prefix", "File",   "Files and Folders"),
    ("prefix", "Folder", "Files and Folders"),
    ("prefix", "ReadCSV",  "Files and Folders"),
    ("prefix", "ReadXLS",  "Files and Folders"),
    ("prefix", "ReadPDF",  "Files and Folders"),
    ("prefix", "ReadExcel","Files and Folders"),
    ("prefix", "ReadFile", "Files and Folders"),
    ("prefix", "ReadContinuousFile", "Files and Folders"),
    ("prefix", "ReadWord", "Files and Folders"),
    ("prefix", "WriteCSV", "Files and Folders"),
    ("prefix", "WriteXLS", "Files and Folders"),
    ("prefix", "WriteFile","Files and Folders"),
    ("prefix", "ExcelWrite","Files and Folders"),
    ("exact",  "DeleteFile",  "Files and Folders"),
    ("exact",  "DeleteFolder","Files and Folders"),
    ("exact",  "LoadFile",    "Files and Folders"),
    ("exact",  "LoadRemoteFile","Files and Folders"),
    ("exact",  "SetFilePermissions", "Files and Folders"),
    ("exact",  "SetFolderPermissions","Files and Folders"),
    ("exact",  "ShareFolder",         "Files and Folders"),
    ("exact",  "UnshareFolder",       "Files and Folders"),
    ("exact",  "VolumeList",  "Files and Folders"),
    ("exact",  "WriteBaseSixtyFourStringToFile", "Files and Folders"),
    ("exact",  "UploadGoogleDriveFile", "Files and Folders"),
    ("exact",  "ListFolderBox","Files and Folders"),
    ("prefix", "GDrive",       "Files and Folders"),

    # ───── HP ArcSight ─────
    ("prefix", "HPArcsight", "HP ArcSight"),

    # ───── HP Operations Manager ─────
    ("prefix", "HPOM",   "HP Operations Manager"),

    # ───── HP Service Manager ─────
    ("prefix", "HPSM",   "HP Service Manager"),
    ("prefix", "MFSMAX", "HP Service Manager"),  # MicroFocus SMAX / successor lineage

    # ───── HyperV ─────
    ("prefix", "Hyper",  "HyperV"),

    # ───── IBM QRadar ─────
    ("prefix", "QRadar", "IBM QRadar"),

    # ───── IBM Tivoli Omnibus ─────
    ("prefix", "IBMTO",  "IBM Tivoli Omnibus"),

    # ───── Incidents ─────
    ("prefix", "Incident", "Incidents"),
    ("exact",  "NewIncident",       "Incidents"),
    ("exact",  "ResetIncident",     "Incidents"),
    ("exact",  "CloseIncident",     "Incidents"),
    ("exact",  "DeleteIncident",    "Incidents"),
    ("exact",  "ClearDisplayedIncident", "Incidents"),
    ("exact",  "GetIncidentOpenDuration","Incidents"),
    ("exact",  "GetIncidentSeverity",    "Incidents"),
    ("exact",  "GetIncidentTicketID",    "Incidents"),
    ("exact",  "SetIncidentTicketID",    "Incidents"),
    ("exact",  "GetOpenIncidents",  "Incidents"),
    ("exact",  "UpdateIncidentEvent","Incidents"),
    ("exact",  "ChangeSeverity",    "Incidents"),
    ("exact",  "AdvancedCommunicate","Incidents"),
    ("exact",  "Communicate",       "Incidents"),
    ("exact",  "DisplayIncident",   "Incidents"),
    ("exact",  "DisplayIDoc",       "Incidents"),
    ("exact",  "UserAssignment",    "Incidents"),
    ("exact",  "RemoveUserAssignment","Incidents"),
    ("exact",  "LastResponse",      "Incidents"),

    # ───── Self Service ─────
    ("exact",  "SelfServiceResponse", "Self Service"),

    # ───── JSON ─────
    ("contains", "JSONtoTable","JSON"),
    ("contains", "JsonToTable","JSON"),
    ("contains", "NestedJson", "JSON"),
    ("prefix",   "JSON",       "JSON"),
    ("prefix",   "Json",       "JSON"),
    ("contains", "Json",       "JSON"),

    # ───── Jira ─────
    ("prefix", "Jira", "Jira"),

    # ───── Math ─────
    # ConvertHexStringToDecimal must be here (before generic Convert-prefix in Text)
    ("exact", "Abs",      "Math"),
    ("exact", "Ceiling",  "Math"),
    ("exact", "Floor",    "Math"),
    ("exact", "Max",      "Math"),
    ("exact", "Min",      "Math"),
    ("exact", "Pow",      "Math"),
    ("exact", "Round",    "Math"),
    ("exact", "Sgn",      "Math"),
    ("exact", "Sqr",      "Math"),
    ("exact", "Truncate", "Math"),
    ("exact", "IsNumeric","Math"),
    ("exact", "FunctionCalculator",     "Math"),
    ("exact", "EpochConverter",         "Math"),
    ("exact", "BuildRandomString",      "Math"),
    ("exact", "PasswordGenerator",      "Math"),
    ("exact", "RandomNumberGenerator",  "Math"),
    ("exact", "ConvertHexStringToDecimal","Math"),

    # ───── McAfee ESM ─────
    ("prefix", "ESM", "McAfee ESM"),

    # ───── Message Queue ─────
    ("prefix", "MQ",  "Message Queue"),

    # ───── Microsoft Exchange ─────
    ("prefix", "XCH", "Microsoft Exchange"),

    # ───── Network ─────
    ("exact", "Ping",        "Network"),
    ("exact", "PingLatency", "Network"),
    ("exact", "Tracert",     "Network"),
    ("exact", "Telnet",      "Network"),
    ("exact", "ResolveDNS",  "Network"),
    ("exact", "WakeonLan",   "Network"),
    ("exact", "GetExternalInternetIPAddress","Network"),
    ("exact", "GetInterfacesStatus",         "Network"),
    ("exact", "DownloadWebsiteCertificate",  "Network"),
    ("exact", "GetWebsiteCertificateDetails","Network"),
    ("exact", "URLCheck",    "Network"),
    ("exact", "EncodeURL",   "Network"),

    # ───── SNMP (MUST come BEFORE SN* ServiceNow rules) ─────
    ("prefix", "SNMP", "SNMP"),

    # ───── SSH Sessions ─────
    ("prefix", "StartSSH",     "SSH Sessions"),
    ("prefix", "TerminateSSH", "SSH Sessions"),
    ("prefix", "SendSSH",      "SSH Sessions"),
    ("exact",  "SingleSSHCommand", "SSH Sessions"),
    ("exact",  "MultiSSHCommands", "SSH Sessions"),

    # ───── Salesforce ─────
    ("prefix", "Salesforce", "Salesforce"),

    # ───── ServiceNow (after SNMP, before Services) ─────
    ("prefix", "SNOW", "ServiceNow"),
    ("prefix", "SN",   "ServiceNow"),

    # ───── Services (Windows services — must not catch ServiceNow/ServiceDesk) ─────
    ("prefix", "ServiceStatus",       "Services"),
    ("prefix", "ServiceStart",        "Services"),
    ("prefix", "ServiceStop",         "Services"),
    ("prefix", "ServiceList",         "Services"),
    ("exact",  "ServicePause",              "Services"),
    ("exact",  "ServiceRestart",            "Services"),
    ("exact",  "ServiceResume",             "Services"),
    ("exact",  "SetServiceLogonCredentials","Services"),
    ("exact",  "SetServiceStartupType",     "Services"),

    # ───── Slack ─────
    ("prefix", "Slack", "Slack"),

    # ───── SolarWinds NPM ─────
    ("prefix", "SLNPM", "SolarWinds NPM"),

    # ───── Splunk ─────
    ("prefix", "Splunk", "Splunk"),

    # ───── Tables ─────
    # ConvertToHTMLTable / ConvertTextToTable / ConvertTableToJSON / TabletoXML
    # all before generic Convert-prefix below
    ("prefix", "CreateMemoryTable",  "Tables"),
    ("prefix", "AddMemoryTable",     "Tables"),
    ("prefix", "DeleteMemoryTable",  "Tables"),
    ("prefix", "RenameMemoryTable",  "Tables"),
    ("contains","MemoryTable",       "Tables"),
    ("prefix", "GetCellValue",       "Tables"),
    ("prefix", "SetCellValue",       "Tables"),
    ("prefix", "GetColumn",          "Tables"),
    ("prefix", "AddColumn",          "Tables"),
    ("prefix", "DeleteColumn",       "Tables"),
    ("prefix", "GetRows",            "Tables"),
    ("prefix", "ResultSet",          "Tables"),
    ("prefix", "SortTable",          "Tables"),
    ("prefix", "SortResultSet",      "Tables"),
    ("exact",  "FindMissingTableEntries", "Tables"),
    ("exact",  "ReplaceValuesInTable",    "Tables"),
    ("exact",  "CleanTable",          "Tables"),
    ("exact",  "RotateTable",         "Tables"),
    ("exact",  "SumCells",            "Tables"),
    ("exact",  "TableSearch",         "Tables"),
    ("exact",  "TableDateFormatter",  "Tables"),
    ("exact",  "TableReplaceCellValues","Tables"),
    ("exact",  "TableToEventBody",    "Tables"),
    ("exact",  "GetTableRowNumberByString","Tables"),
    ("exact",  "GetTableRowsByRange", "Tables"),
    ("exact",  "REMOVED_SECRET","Tables"),
    ("exact",  "SearchRowByInterval", "Tables"),
    ("exact",  "ConvertTextToTable",  "Tables"),
    ("exact",  "ConvertToHTMLTable",  "Tables"),
    ("exact",  "ConvertTableToJSON",  "Tables"),
    ("exact",  "TabletoXML",          "Tables"),
    ("exact",  "MemoryClean",         "Tables"),

    # ───── Telnet Sessions ─────
    ("prefix", "StartTelnet",     "Telnet Sessions"),
    ("prefix", "TerminateTelnet", "Telnet Sessions"),
    ("prefix", "SendTelnet",      "Telnet Sessions"),

    # ───── Time Controls ─────
    # ConvertUNIXTimeToHumanReadable / ConvertDateToFileTime before Text's prefix Convert
    ("prefix", "GetDate",        "Time Controls"),
    ("prefix", "FormatDate",     "Time Controls"),
    ("prefix", "AddDate",        "Time Controls"),
    ("prefix", "DateDifference", "Time Controls"),
    ("exact",  "Wait",           "Time Controls"),
    ("exact",  "WaitforCMD",     "Time Controls"),
    ("exact",  "ConditionalWait","Time Controls"),
    ("exact",  "GetUNIXTimestamp","Time Controls"),
    ("exact",  "ConvertUNIXTimeToHumanReadable", "Time Controls"),
    ("exact",  "ConvertDateToFileTime",          "Time Controls"),
    ("exact",  "SystemUptime",   "Time Controls"),

    # ───── Text ─────
    # `prefix Convert` lives here — all the specific Convert* exacts above
    # must have already been checked before we fall through to this.
    ("exact",  "Split",          "Text"),
    ("prefix", "Replace",        "Text"),
    ("exact",  "Trim",           "Text"),
    ("exact",  "LeftTrim",       "Text"),
    ("exact",  "RightTrim",      "Text"),
    ("exact",  "TrimbyString",   "Text"),
    ("exact",  "Length",         "Text"),
    ("exact",  "IndexOf",        "Text"),
    ("exact",  "InStr",          "Text"),
    ("exact",  "InStrRev",       "Text"),
    ("prefix", "SubString",      "Text"),
    ("exact",  "SubStringByText","Text"),
    ("exact",  "Contains",       "Text"),
    ("exact",  "MatchRegularExpression", "Text"),
    ("exact",  "UpperCase",      "Text"),
    ("exact",  "LowerCase",      "Text"),
    ("prefix", "Convert",        "Text"),  # generic fallback after specific exacts
    ("prefix", "UcFirst",        "Text"),
    ("prefix", "Ucfirst",        "Text"),
    ("exact",  "Left",           "Text"),
    ("exact",  "Right",          "Text"),
    ("exact",  "DecodeHTML",     "Text"),
    ("exact",  "EncodeHTML",     "Text"),
    ("exact",  "ExtractLineFromText", "Text"),
    ("exact",  "IsEmpty",        "Text"),
    ("exact",  "StrComp",        "Text"),
    ("exact",  "StrReverse",     "Text"),
    ("exact",  "ExtractByPattern","Text"),
    ("exact",  "GetHeaderValue", "Text"),
    ("prefix", "FilePathParser", "Text"),

    # ───── VMware ─────
    ("prefix", "VM",       "VMware"),
    ("prefix", "VCenter",  "VMware"),
    ("prefix", "vCenter",  "VMware"),
    ("prefix", "VPC",      "VMware"),

    # ───── Virus Total ─────
    ("prefix", "VT",         "Virus Total"),
    ("prefix", "VirusTotal", "Virus Total"),

    # ───── Web ─────
    ("prefix", "HTTP",  "Web"),
    ("prefix", "Http",  "Web"),
    ("contains","WebService", "Web"),
    ("exact",  "WebFetchCookie",      "Web"),
    ("prefix", "ApplicationPool",     "Web"),
    ("prefix", "IIS",                 "Web"),
    ("exact",  "CreateVirtualDirectory","Web"),
    ("exact",  "EditVirtualDirectory",  "Web"),

    # ───── Windows ─────
    ("prefix", "SCCM",            "Windows"),
    ("prefix", "Registry",        "Windows"),
    ("prefix", "Process",         "Windows"),
    ("exact",  "CreateWindowsEventLog", "Windows"),
    ("exact",  "GetWindowEventLogs",    "Windows"),
    ("exact",  "WMIQuery",        "Windows"),
    ("exact",  "WMI",             "Windows"),
    ("exact",  "GetInstalledSoftware","Windows"),
    ("exact",  "ServerOSDetails", "Windows"),
    ("exact",  "DiskSpace",       "Windows"),
    ("exact",  "CPU",             "Windows"),
    ("exact",  "Memory",          "Windows"),
    ("prefix", "Server",          "Windows"),
    ("exact",  "EnablePrivilegedCommands","Windows"),
    ("exact",  "PerformanceMonitor","Windows"),

    # ───── XML ─────
    ("exact",  "DisplayXMLSession","XML"),  # catch before `prefix Display`
    ("contains","XMLSession",      "XML"),
    ("contains","Xpath",           "XML"),
    ("contains","XMLtoTable",      "XML"),
    ("prefix",  "XML",             "XML"),
    ("prefix",  "Xml",             "XML"),

    # ══════ Non-docs categories (practical groupings) ══════

    # ───── Flow Control ─────
    ("exact", "WhileActivity",           "Flow Control"),
    ("exact", "IfElseActivity",          "Flow Control"),
    ("exact", "ParallelActivity",        "Flow Control"),
    ("exact", "ForEachActivity",         "Flow Control"),
    ("exact", "ExitWhile",               "Flow Control"),
    ("exact", "SequenceActivity",        "Flow Control"),
    ("exact", "SequentialWorkflowActivity","Flow Control"),
    ("exact", "Continue",                "Flow Control"),
    ("exact", "Terminate",               "Flow Control"),
    ("exact", "TerminateWorkflow",       "Flow Control"),
    ("exact", "GoTo",                    "Flow Control"),
    ("exact", "ReturnValue",             "Flow Control"),
    ("exact", "IfElseCondition",         "Flow Control"),
    ("exact", "IfElseBranchActivity",    "Flow Control"),
    ("exact", "UserGroup",               "Flow Control"),
    ("exact", "NewEvent",                "Flow Control"),

    # ───── Variables ─────
    ("exact", "MemorySet",      "Variables"),
    ("exact", "MultiMemorySet", "Variables"),
    ("exact", "SetMemory",      "Variables"),
    ("exact", "Counter",        "Variables"),
    ("exact", "WorkflowCounter","Variables"),
    ("exact", "ProcessCounter", "Variables"),

    # ───── Logging & Display ─────
    ("prefix", "Display", "Logging & Display"),
]


def apply_rules(name: str) -> str | None:
    """Return the first matching category, or None."""
    for kind, pattern, category in CATEGORY_RULES:
        if kind == "prefix" and name.startswith(pattern):
            return category
        if kind == "exact" and name == pattern:
            return category
        if kind == "contains" and pattern in name:
            return category
    return None


# ═══════════════════════════════════════════════════════════════════════
# CATEGORIZATION (docs > rules > uncategorized)
# ═══════════════════════════════════════════════════════════════════════

def categorize(name: str, docs_lookup: dict[str, dict]) -> dict:
    match_key = normalize(name)

    # Priority 1: docs (vendor-authoritative)
    if match_key in docs_lookup:
        entry = docs_lookup[match_key]
        return {
            "category":    entry["category"],
            "subcategory": entry.get("subcategory"),
            "display":     entry["display"],
            "source":      "docs",
        }

    # Priority 2: rules (docs-vocabulary categories via name patterns)
    cat = apply_rules(name)
    if cat is not None:
        return {
            "category":    cat,
            "subcategory": None,
            "display":     to_display(name),
            "source":      "rules",
        }

    # Priority 3: uncategorized
    return {
        "category":    "Uncategorized",
        "subcategory": None,
        "display":     to_display(name),
        "source":      "uncategorized",
    }


# ═══════════════════════════════════════════════════════════════════════
# I/O
# ═══════════════════════════════════════════════════════════════════════

def load_activity_list(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if not row:
                continue
            name = row[0].strip()
            if not name or name.startswith("#") or name.lower() == "name":
                continue  # skip header row and empty/comment rows
            desc = row[1].strip() if len(row) > 1 else ""
            rows.append((name, desc))
    return rows


def load_docs_source(path: Path) -> dict[str, dict]:
    """Load the docs source and index by match_key."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    activities = data.get("activities", [])
    lookup: dict[str, dict] = {}
    for a in activities:
        mk = a.get("match_key")
        if mk:
            lookup[mk] = a
    return lookup


# ═══════════════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════════════

def print_report(catalog: dict[str, dict], samples_per_category: int) -> None:
    total = len(catalog)
    if total == 0:
        print("No activities to report.")
        return

    by_source: dict[str, int]        = defaultdict(int)
    by_category: dict[str, list[str]] = defaultdict(list)
    for name, info in catalog.items():
        by_source[info["source"]] += 1
        by_category[info["category"]].append(name)

    print("=" * 90)
    print("SUMMARY BY SOURCE")
    print("=" * 90)
    d = by_source["docs"]
    r = by_source["rules"]
    u = by_source["uncategorized"]
    print(f"  docs          {d:>5}  ({d/total*100:5.1f}%)  "
          "— vendor-authoritative")
    print(f"  rules         {r:>5}  ({r/total*100:5.1f}%)  "
          "— name-pattern fallback")
    print(f"  uncategorized {u:>5}  ({u/total*100:5.1f}%)  "
          "— hand-fix in JSON")
    print(f"  TOTAL         {total:>5}")
    print()

    print("=" * 90)
    print("BY CATEGORY")
    print("=" * 90)
    # Sort categories by count desc, uncategorized always last
    sortable = [(c, names) for c, names in by_category.items()
                if c != "Uncategorized"]
    sortable.sort(key=lambda x: -len(x[1]))
    if "Uncategorized" in by_category:
        sortable.append(("Uncategorized", by_category["Uncategorized"]))

    for cat, names in sortable:
        sample = sorted(names)[:samples_per_category]
        print(f"  {cat:<42} {len(names):>4}  e.g. {', '.join(sample[:3])}")
    print()

    if "Uncategorized" in by_category and by_category["Uncategorized"]:
        uncats = sorted(by_category["Uncategorized"])
        print("=" * 90)
        print(f"UNCATEGORIZED  ({len(uncats)} — edit the JSON to fix)")
        print("=" * 90)
        for i in range(0, len(uncats), 4):
            row = uncats[i:i + 4]
            print("  " + "  ".join(f"{n:<26}" for n in row))
        print()


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge docs + rules → activity_categories.json"
    )
    parser.add_argument("--activity-list",
                        default="./data/activity_list.txt")
    parser.add_argument("--docs-source",
                        default="./data/docs_categories_source.json")
    parser.add_argument("--output",
                        default="./data/activity_categories.json")
    parser.add_argument("--samples-per-category", type=int, default=5)
    args = parser.parse_args()

    activity_list_path = Path(args.activity_list)
    if not activity_list_path.exists():
        print(f"ERROR: {activity_list_path} not found.", file=sys.stderr)
        return 1

    activities = load_activity_list(activity_list_path)
    print(f"Loaded {len(activities)} activities from "
          f"{activity_list_path.name}")

    docs_path = Path(args.docs_source)
    docs_lookup = load_docs_source(docs_path)
    if docs_lookup:
        print(f"Loaded {len(docs_lookup)} docs entries from {docs_path.name}")
    else:
        print(f"No docs source at {docs_path} — using rules-only mode")
    print()

    # Build catalog
    catalog: dict[str, dict] = {}
    for name, _desc in activities:
        catalog[name] = categorize(name, docs_lookup)

    # Write JSON (sorted alphabetically by activity name)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_catalog = {k: catalog[k] for k in sorted(catalog)}
    out_path.write_text(
        json.dumps(sorted_catalog, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print_report(catalog, args.samples_per_category)
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())