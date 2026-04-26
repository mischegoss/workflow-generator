"""
tools/system_extraction.py

Extracts the target external system mentioned in a workflow step.

Used by the retrieval scorer to apply a system-bias term: when a step
mentions ServiceNow, candidate activities whose module_type is ServiceNow
score higher than INTERNAL alternatives. The catalog's module_type field
(set by the merge script) is the authoritative source — this helper just
identifies what the user asked for in the step text.

Returns None when no known system is mentioned. None is the correct
default — most workflow steps don't target a specific external system,
and the scorer should fall through to keyword matching as it does today.

Adding a new system: include both the formal product name and common
user shorthand. Avoid 1-2 letter aliases (too many false positives).
The system name returned must match the module_type values used in the
merged activity catalog exactly.
"""

import re
from typing import Annotated

# Canonical system name -> phrases that indicate it in step text.
# Phrases are matched case-insensitively as whole words/phrases.
SYSTEM_ALIASES: dict[str, list[str]] = {
    "ServiceNow":     ["servicenow", "service now", "snow",
                       "sn ticket", "sn incident", "sn record"],
    "Jira":           ["jira", "atlassian"],
    "AWS":            ["aws", "amazon web services", "ec2", "s3", "cloudwatch"],
    "MsTeams":        ["msteams", "ms teams", "microsoft teams",
                       "teams chat", "teams channel"],
    "Slack":          ["slack"],
    "Cherwell":       ["cherwell"],
    "Splunk":         ["splunk"],
    "Salesforce":     ["salesforce", "sfdc"],
    "SolarWinds NPM": ["solarwinds npm", "solarwinds", "npm node"],
    "CA Spectrum":    ["ca spectrum", "spectrum alarm"],
    "McAfee ESM":     ["mcafee esm", "mcafee"],
    "BMC Remedyforce": ["bmc remedyforce", "remedyforce"],
    "BMC TrueSight":  ["bmc truesight", "truesight"],
    "Microsoft System Center - Operations Manager":
                      ["scom", "system center operations manager"],
    "HP - Operations Manager": ["hp operations manager", "hp om", "hpom"],
    "IBM - Tivoli/Omnibus":    ["tivoli", "ibm netcool", "omnibus"],
    "IBM QRadar":     ["qradar", "ibm qradar"],
}


def extract_system_from_step(
    step: Annotated[dict, "A single step dict from decomposition"],
) -> str | None:
    """
    Returns the canonical system name mentioned in the step, or None.

    Searches the step's description and intent. Returns the first system
    whose alias matches; SYSTEM_ALIASES iteration order is the implicit
    tie-breaker (Python 3.7+ preserves insertion order).
    """
    if not step:
        return None

    haystack = " ".join([
        str(step.get("description", "")),
        str(step.get("intent", "")).replace("_", " "),
    ]).lower()

    if not haystack.strip():
        return None

    for system, aliases in SYSTEM_ALIASES.items():
        for alias in aliases:
            # Whole-phrase boundary match — avoids "snippet" matching "sn"
            if re.search(r"(?:^|\W)" + re.escape(alias) + r"(?:\W|$)", haystack):
                return system

    return None