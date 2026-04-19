"""
apply_patches.py
================
Applies targeted patches to pipeline_stages.py and xml_composer.py.
Run from the project root: python apply_patches.py

Safe to run multiple times — idempotent.
"""

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent


# REMOVED_SECRET
# Patch 1: pipeline_stages.py — _INVALID_FIELDS_BY_TYPE
# REMOVED_SECRET

OLD_INVALID = '''\
_INVALID_FIELDS_BY_TYPE: dict[str, frozenset] = {
    # ── Original 6 (RitaLab-confirmed) ───────────────────────────────────────
    "GetRowsCount":  frozenset({"TableName", "ResultSetName", "ColumnType",
                                "ColumnNumber", "ColumnName", "RowNumber"}),
    "GetCellValue":  frozenset({"TableName"}),
    "ExitWhile":     frozenset({"Condition", "IsExpression", "Expression"}),
    "DisplayValue":  frozenset({"ResultSet", "ResultSetName"}),
    "Ping":          frozenset({"ResultSet", "ResultSetName", "TableName"}),
    "MemorySet":     frozenset({"ResultSet", "ResultSetName", "TableName"}),

    # ── Extended (template-confirmed, no these fields in their schemas) ───────
    "GetDate":            frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "DateDifference":     frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "SendEmail":          frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    # Note: PowerShellScript.ResultSet intentionally NOT denied —
    # PowerShell can legitimately receive a ResultSet as input.
    "PowerShellScript":   frozenset({"TableName", "ColumnType", "ColumnNumber",
                                     "RowNumber"}),
    "ServiceStatus":      frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "ServiceStart":       frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "ServiceStop":        frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "FileExist":          frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "ADUserExists":       frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "FunctionCalculator": frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "IsEmpty":            frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "Contains":           frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
}'''

NEW_INVALID = '''\
_INVALID_FIELDS_BY_TYPE: dict[str, frozenset] = {
    # ── Original 6 (RitaLab-confirmed) ───────────────────────────────────────
    "GetRowsCount":  frozenset({"TableName", "ResultSetName", "ColumnType",
                                "ColumnNumber", "ColumnName", "RowNumber"}),
    "GetCellValue":  frozenset({"TableName"}),
    "ExitWhile":     frozenset({"Condition", "IsExpression", "Expression"}),
    "DisplayValue":  frozenset({"ResultSet", "ResultSetName"}),
    "Ping":          frozenset({"ResultSet", "ResultSetName", "TableName"}),
    "MemorySet":     frozenset({"ResultSet", "ResultSetName", "TableName"}),

    # ── Extended (template-confirmed) ────────────────────────────────────────
    "GetDate": frozenset({
        "ResultSet", "ResultSetName", "TableName",
        "ColumnType", "ColumnNumber", "RowNumber",
        # AddDate fields that bleed onto GetDate via documentation template:
        "TimeToAdd", "TimeZoneName", "TimeInterval", "FuturePast",
    }),
    "DateDifference":     frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "SendEmail":          frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    # Note: PowerShellScript.ResultSet intentionally NOT denied.
    "PowerShellScript":   frozenset({"TableName", "ColumnType", "ColumnNumber",
                                     "RowNumber"}),
    "ServiceStatus":      frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "ServiceStart":       frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "ServiceStop":        frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "FileExist":          frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "ADUserExists":       frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "FunctionCalculator": frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "IsEmpty":            frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),
    "Contains":           frozenset({"ResultSet", "ResultSetName", "TableName",
                                     "ColumnType", "ColumnNumber", "RowNumber"}),

    # ── New: confirmed from XML analysis (t02–t09) ────────────────────────────
    # ResultSetFilter: Wirer invents Filter/FilterExpression/InputResultSet/OutputResultSet.
    # Platform fields are: VariableName (input), FilterStatement, SortStatement, ResultSetName.
    "ResultSetFilter": frozenset({
        "Filter", "FilterExpression", "InputResultSet", "OutputResultSet",
    }),
    # FTPListFiles has no ResultSet input field — its output is referenced as %xName%.
    "FTPListFiles": frozenset({"ResultSet", "ResultSetName"}),
    # TSQLQuery: Wirer writes Database instead of DatabaseName.
    "TSQLQuery":    frozenset({"Database"}),
    # MatchRegularExpression: Wirer uses wrong field names from documentation template.
    # Platform fields: MatchFormula (the regex), TheValue (the input string).
    "MatchRegularExpression": frozenset({"RegularExpression", "InputValue", "Regex"}),
    # RunPowerShellOnRemoteHost: Wirer uses Script/Domain/Impersonation.
    # Platform field: ScriptBlock.
    "RunPowerShellOnRemoteHost": frozenset({"Script", "Domain", "Impersonation"}),
}'''

# REMOVED_SECRET
# Patch 2: pipeline_stages.py — _scaffold_node
# REMOVED_SECRET

OLD_SCAFFOLD = '''\
    elif ct == "TSQLQuery":
        _sif(node, "SiteId",             "-1")
        _sif(node, "SiteName",           "")
        _sif(node, "isUserAuthenticate", "False")
        _sif(node, "UserName",           "")
        _sif(node, "Password",           "")'''

NEW_SCAFFOLD = '''\
    elif ct == "TSQLQuery":
        _sif(node, "SiteId",               "-1")
        _sif(node, "SiteName",             "")
        _sif(node, "isUserAuthenticate",   "False")
        _sif(node, "UserName",             "")
        _sif(node, "Password",             "")
        _sif(node, "ConnectionStringTextBox", "")

    elif ct == "TSQLStatement":
        _sif(node, "SiteId",               "-1")
        _sif(node, "SiteName",             "")
        _sif(node, "isUserAuthenticate",   "False")
        _sif(node, "UserName",             "")
        _sif(node, "Password",             "")
        _sif(node, "ConnectionStringTextBox", "")

    elif ct in {"FTPListFiles", "FTPDownloadFile", "FTPDeleteFile",
                "FTPUploadFile", "FTPFileExists"}:
        _sif(node, "Port",    "21")
        _sif(node, "UseSSL",  "False")
        _sif(node, "SSLMode", "None")
        if ct in {"FTPDeleteFile", "FTPDownloadFile"}:
            _sif(node, "Path", "")

    elif ct == "HTTPRequest":
        _sif(node, "Url",            "")
        _sif(node, "RequestType",    "GET")
        _sif(node, "Codepage",       "65001")
        _sif(node, "IgnoreCodePage", "False")
        _sif(node, "Sectype",        "None")

    elif ct == "GoTo":
        _sif(node, "ActivityName", "")'''

# Also patch ResultSetFilter scaffold to add FilterStatement/SortStatement/ResultSetName
OLD_RESULTSETFILTER = '''\
    elif ct == "ResultSetFilter":
        if nearest_table_var:
            _sif(node, "VariableName", f"%{nearest_table_var}%")'''

NEW_RESULTSETFILTER = '''\
    elif ct == "ResultSetFilter":
        if nearest_table_var:
            _sif(node, "VariableName",   f"%{nearest_table_var}%")
            _sif(node, "ResultSetName",  nearest_table_var)
        # FilterStatement and SortStatement are required per controls.
        # Empty string is valid — Wirer fills FilterStatement with logic.
        _sif(node, "FilterStatement", "")
        _sif(node, "SortStatement",   "")'''


# REMOVED_SECRET
# Patch 3: xml_composer.py — STRIP_FIELDS + Timeout format
# REMOVED_SECRET

OLD_SKIP_FIELDS = '''\
    SKIP_FIELDS = {
        "notes",
        "workflow_name",
        "variable_contracts",
        "modulePermissions",
        "isFavorite",
    }'''

NEW_SKIP_FIELDS = '''\
    SKIP_FIELDS = {
        "notes",
        "workflow_name",
        "variable_contracts",
        "modulePermissions",
        "isFavorite",
    }

    # Fields that appear in activity JSON (from documentation templates, Wirer
    # hallucinations, or new-platform features) but are NOT valid XOML attributes
    # in the platform version we target. Confirmed by comparing generated XML
    # against corpus examples from 609 real workflows.
    #
    # These are stripped during serialization — they cause import failures or
    # are silently ignored depending on platform version, so stripping is safer.
    STRIP_FIELDS = {
        # Documentation/template metadata — not XML attributes
        "AutomationAsCode", "AutomationLicenceType", "Category", "HelpText",
        "RunbookParameters", "Integration", "Version",
        # New-platform execution control fields — not in our corpus target version.
        # Strip until confirmed importable via RitaLab.
        "IsEnabled", "IsResultBase64", "ContinueOnError", "WaitingForApproval",
        "RunAs", "NotificationTemplate", "SuccessWhen",
        "ResultEvaluationType", "ResultValue",
        "ResultIsExpression", "ResultExpression",
        "ResultCondition", "ResultConditionValue",
        "ResultConditionIsExpression", "ResultConditionExpression",
        "ResultConditionAndOr", "ResultConditionAndOrValue",
        "REMOVED_SECRET", "ResultConditionAndOrExpression",
        "ResultConditionAndOrCondition", "REMOVED_SECRET",
        "REMOVED_SECRET",
        "REMOVED_SECRET",
        "RetryCount", "RetryInterval",
        # Canvas coordinates (UI only)
        "x", "y",
        # Wrong-case duplicate of platform's lowercase "name" field
        "Name",
        # Wirer sometimes adds these non-platform fields
        "StoredValue", "ExecutionTimeout", "ExecutionRetries",
    }'''

OLD_SERIALIZE_LOOP = '''\
        for key, value in activity.items():
            if key in self.SKIP_FIELDS or key in ("xName", "CustomTypeName"):
                continue
            if isinstance(value, dict):
                child_elem = self._serialize_activity(value)
                if child_elem is not None:
                    child_elements.append(child_elem)
            elif isinstance(value, bool):
                attribs[key] = str(value)
            elif value is None:
                attribs[key] = "{x:Null}"
            elif isinstance(value, (int, float)):
                attribs[key] = str(value)
            else:
                attribs[key] = str(value)'''

NEW_SERIALIZE_LOOP = '''\
        for key, value in activity.items():
            if key in self.SKIP_FIELDS or key in self.STRIP_FIELDS:
                continue
            if key in ("xName", "CustomTypeName"):
                continue
            if isinstance(value, dict):
                child_elem = self._serialize_activity(value)
                if child_elem is not None:
                    child_elements.append(child_elem)
            elif isinstance(value, bool):
                attribs[key] = str(value)
            elif value is None:
                attribs[key] = "{x:Null}"
            elif isinstance(value, (int, float)):
                # Timeout as integer — convert to HH:MM:SS
                if key == "Timeout":
                    attribs[key] = self._format_timeout(value)
                else:
                    attribs[key] = str(value)
            else:
                # String Timeout that isn't HH:MM:SS format (e.g. "100", "False")
                if key == "Timeout":
                    attribs[key] = self._format_timeout(value)
                else:
                    attribs[key] = str(value)'''

OLD_BUILD_FORMULA = '''\
    @staticmethod
    def _build_formula(condition_type: str, value: str) -> str | None:
        if not condition_type:
            return None
        return f"={condition_type}(&&&,{value})"'''

NEW_BUILD_FORMULA = '''\
    @staticmethod
    def _build_formula(condition_type: str, value: str) -> str | None:
        if not condition_type:
            return None
        return f"={condition_type}(&&&,{value})"

    @staticmethod
    def _format_timeout(value) -> str:
        """
        Convert a Timeout value to HH:MM:SS format.
        Handles: integer seconds (100 → 00:01:40), float, boolean leak ("False"),
        already-formatted strings ("00:01:00" → unchanged).
        """
        s = str(value).strip()
        if ":" in s:
            return s  # already HH:MM:SS
        try:
            seconds = int(float(s))
            if seconds <= 0:
                return "00:01:00"
            h = seconds // 3600
            m = (seconds % 3600) // 60
            sec = seconds % 60
            return f"{h:02d}:{m:02d}:{sec:02d}"
        except (ValueError, TypeError):
            return "00:01:00"  # safe default for boolean/garbage values'''


# REMOVED_SECRET
# Apply all patches
# REMOVED_SECRET

def apply_patch(path: pathlib.Path, old: str, new: str, label: str) -> bool:
    if not path.exists():
        print(f"  ERROR: {path} not found")
        return False
    src = path.read_text(encoding="utf-8")
    if old not in src:
        if new in src:
            print(f"  SKIP {label}: already applied")
            return True
        print(f"  ERROR {label}: old text not found in {path.name}")
        return False
    patched = src.replace(old, new, 1)
    # Validate it still parses
    try:
        ast.parse(patched)
    except SyntaxError as e:
        print(f"  ERROR {label}: patch introduces syntax error: {e}")
        return False
    path.write_text(patched, encoding="utf-8")
    print(f"  OK    {label}")
    return True


print("Applying patches...")
print()

ps_path  = ROOT / "tools" / "pipeline_stages.py"
xc_path  = ROOT / "serializer" / "xml_composer.py"

results = []

results.append(apply_patch(ps_path, OLD_INVALID, NEW_INVALID,
    "pipeline_stages: _INVALID_FIELDS_BY_TYPE — add ResultSetFilter/FTPListFiles/TSQLQuery/MatchRE/RunPS"))

results.append(apply_patch(ps_path, OLD_RESULTSETFILTER, NEW_RESULTSETFILTER,
    "pipeline_stages: _scaffold_node ResultSetFilter — add FilterStatement/SortStatement/ResultSetName"))

results.append(apply_patch(ps_path, OLD_SCAFFOLD, NEW_SCAFFOLD,
    "pipeline_stages: _scaffold_node — add TSQLStatement/FTP/HTTPRequest/GoTo scaffold rules"))

results.append(apply_patch(xc_path, OLD_SKIP_FIELDS, NEW_SKIP_FIELDS,
    "xml_composer: add STRIP_FIELDS"))

results.append(apply_patch(xc_path, OLD_BUILD_FORMULA, NEW_BUILD_FORMULA,
    "xml_composer: add _format_timeout static method"))

results.append(apply_patch(xc_path, OLD_SERIALIZE_LOOP, NEW_SERIALIZE_LOOP,
    "xml_composer: apply STRIP_FIELDS and Timeout format in _serialize_activity"))

print()
passed = sum(results)
print(f"Patches applied: {passed}/{len(results)}")
if passed < len(results):
    print("Some patches failed — check errors above.")
    sys.exit(1)
else:
    print("All patches applied successfully.")
    print()
    print("Files modified:")
    print(f"  {ps_path}")
    print(f"  {xc_path}")
    print()
    print("Copy validation_tools.py manually:")
    print("  cp apply_patches.py ../tools/  # or wherever it lives")