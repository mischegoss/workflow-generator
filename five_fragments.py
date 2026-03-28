"""
test_fragment_workflows.py

Builds the 5 fragment test workflows using the actual pipeline code
(run_fragments, run_annotation, run_validation, WorkflowXmlComposer)
and verifies each one produces valid, importable XML.

Tests the full deterministic pipeline path without any LLM calls.

Usage:
    cd ~/Documents/workflow_generator
    DATA_DIR=./data python3 test_fragment_workflows.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

import json
import os
import sys
import xml.etree.ElementTree as ET

# ── Environment setup ────────────────────────────────────────────────────────
os.environ.setdefault("DATA_DIR", "./data")
sys.path.insert(0, ".")

from tools.pipeline_stages import run_fragments, run_annotation, run_validation
from serializer.xml_composer import WorkflowXmlComposer
from convert_to_xml import _validate_outer, _validate_xoml


# ── Helpers ──────────────────────────────────────────────────────────────────

def table_as_string(columns: list) -> str:
    """Build the TableAsString XML schema for CreateMemoryTable."""
    col_elements = "\n".join(
        f'                <xs:element name="{c}" type="xs:string" minOccurs="0" />'
        for c in columns
    )
    row_elements = "\n".join(f"    <{c} />" for c in columns)
    return (
        '<NewDataSet>\r\n'
        '  <xs:schema id="NewDataSet" xmlns="" xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:msdata="urn:schemas-microsoft-com:xml-msdata">\r\n'
        '    <xs:element name="NewDataSet" msdata:IsDataSet="true"'
        ' msdata:MainDataTable="resultSet" msdata:UseCurrentLocale="true">\r\n'
        '      <xs:complexType>\r\n'
        '        <xs:choice minOccurs="0" maxOccurs="unbounded">\r\n'
        '          <xs:element name="resultSet">\r\n'
        '            <xs:complexType>\r\n'
        '              <xs:sequence>\r\n'
        f'{col_elements}\r\n'
        '              </xs:sequence>\r\n'
        '            </xs:complexType>\r\n'
        '          </xs:element>\r\n'
        '        </xs:choice>\r\n'
        '      </xs:complexType>\r\n'
        '    </xs:element>\r\n'
        '  </xs:schema>\r\n'
        f'  <resultSet>\r\n{row_elements}\r\n  </resultSet>\r\n'
        '</NewDataSet>'
    )


def act(xname, ct, extra=None):
    """Standard activity node with all required base fields."""
    a = {
        "xName": xname,
        "CustomTypeName": ct,
        "name": ct,
        "visible": "True",
        "disabled": "False",
        "isFavorite": "False",
        "isJsonValid": "True",
        "readPermission": True,
        "writePermission": True,
        "modulePermissions": None,
        "IsValid": "True",
        "activityLicenseType": "1",
        "Timeout": "00:01:00",
        "TimeInSeconds": "60",
        "RecoveryMethodSelection": "{x:Null}",
        "TargetModuleID": "",
        "TargetModuleName": "",
        "Path": "{x:Null}",
        "Description": f"{ct} activity",
        "description": f"{ct} activity",
        "TypeName": ct,
        "label": ct,
        "notes": "",
    }
    if extra:
        a.update(extra)
    return a


def create_memory_table(xname, table_name, columns, desc=""):
    """Correct CreateMemoryTable node with required TableAsString schema."""
    return {
        "xName": xname,
        "CustomTypeName": "CreateMemoryTable",
        "name": "CreateMemoryTable",
        "visible": "True",
        "disabled": "False",
        "isFavorite": "False",
        "isJsonValid": "True",
        "readPermission": True,
        "writePermission": True,
        "modulePermissions": None,
        "IsValid": "True",
        "activityLicenseType": "1",
        "id": "416",
        "Timeout": "00:01:00",
        "TimeInSeconds": "60",
        "RecoveryMethodSelection": "{x:Null}",
        "TargetModuleID": "",
        "TargetModuleName": "",
        "Path": "{x:Null}",
        "ColumnNumber": str(len(columns)),
        "TableName": table_name,
        "RowNumber": "1",
        "isEmptyGrid": "0",
        "TableAsString": table_as_string(columns),
        "DisplayName": "Create Memory Table",
        "Description": desc or f"Create memory table {table_name}",
        "TypeName": "CreateMemoryTable",
        "label": "Create Memory Table",
        "description": desc or f"Create memory table {table_name}",
        "notes": "",
    }


def return_value(xname, extra=None):
    """
    Minimal valid ReturnValue node.
    Fragment rules will set IsValid, Formula, UseStoredValue correctly.
    Forbidden fields (visible, disabled, etc.) deliberately excluded.
    """
    rv = {
        "xName": xname,
        "CustomTypeName": "ReturnValue",
        "Formula": "",
        "ConditionNumber": "0",
        "Value": "",
        "ConditionType": "",
        "Description": "",
        "description": "",
        "Type": "StoredValue",
        "RecoveryMethodSelection": "{x:Null}",
        "ConditionName": "",
        "UseCustomeCondition": "False",
        "UseBranchWhenTimeout": "True",
        "DisplayName": "Return Value",
        "TypeName": "ReturnValue",
        "Disabled": "False",
        "ClusterID": "{x:Null}",
        "ClusterName": "{x:Null}",
    }
    if extra:
        rv.update(extra)
    return rv


def seq_container(xname):
    return {
        "xName": xname,
        "CustomTypeName": "SequenceActivity",
        "name": "SequenceActivity",
        "visible": "True",
        "disabled": "False",
        "isFavorite": "False",
        "isJsonValid": "True",
        "readPermission": True,
        "writePermission": True,
        "modulePermissions": None,
        "IsValid": "True",
        "Description": "SequenceActivity activity",
        "description": "SequenceActivity activity",
    }


def exit_while(xname):
    return {
        "xName": xname,
        "CustomTypeName": "ExitWhile",
        "name": "ExitWhile",
        "visible": "True",
        "disabled": "False",
        "isFavorite": "False",
        "isJsonValid": "True",
        "readPermission": True,
        "writePermission": True,
        "modulePermissions": None,
        "IsValid": "True",
        "Description": "ExitWhile activity",
        "description": "ExitWhile activity",
    }


def branch(xname, desc, children):
    b = {
        "xName": xname,
        "CustomTypeName": "IfElseBranchActivity",
        "name": "IfElseBranchActivity",
        "visible": "True",
        "disabled": "False",
        "isFavorite": "False",
        "isJsonValid": "True",
        "readPermission": True,
        "writePermission": True,
        "modulePermissions": None,
        "IsValid": "True",
        "Description": desc,
        "description": desc,
    }
    b.update(children)
    return b


def wrap(name, pnumber, raw):
    return {
        "name": name,
        "pnumber": pnumber,
        "workflow_type": "Regular",
        "created_by": "fragment-test",
        "placeholder_summary": [],
        "pipeline_notes": [],
        "errors": [],
        "workflow_raw_data": raw,
    }


# ── Workflow definitions ──────────────────────────────────────────────────────

def build_linear():
    """GetDate → MemorySet → DisplayValue. Tests F5 (MemorySet defaults)."""
    return wrap("TestLinear", "90001", {
        "getDate1": act("getDate1", "GetDate", {
            "id": "291",
            "DateFormat": "MM/dd/yyyy HH:mm",
            "FuturePast": "0",
            "TimeInterval": "Seconds",
            "TimeToAdd": "0",
            "TimeZoneName": "(UTC) Coordinated Universal Time",
            "Description": "Get current date and time",
            "description": "Get current date and time",
        }),
        "memorySet1": act("memorySet1", "MemorySet", {
            "id": "430",
            "VariableName": "currentDate",
            "VariableValue": "%getDate1%",
            "Description": "Store current date",
            "description": "Store current date",
            # VariableScope/IsSaved/IsAppend deliberately omitted — F5 must add them
        }),
        "displayValue1": act("displayValue1", "DisplayValue", {
            "id": "431",
            "ValueToDisplay": "Current date is: %currentDate%",
            "Description": "Display current date",
            "description": "Display current date",
        }),
    })


def build_while():
    """
    CreateMemoryTable → GetRowsCount → WhileActivity[ExitWhile + GetCellValue + DisplayValue]
    Tests: F1 (Condition), F2+F3 (ExitWhile fields), F4 (RowNumber), F5 (MemorySet)
    """
    seq = seq_container("whileSequenceActivity1")
    seq.update({
        "exitWhile1": exit_while("exitWhile1"),
        "getCellValue1": act("getCellValue1", "GetCellValue", {
            "id": "288",
            "ResultSet": "%serverTable%",
            "ColumnNumber": "server",
            "Description": "Get server name from current row",
            "description": "Get server name from current row",
            # RowNumber deliberately omitted — F4 must set it to %whileActivity1%
        }),
        "displayValue1": act("displayValue1", "DisplayValue", {
            "id": "431",
            "ValueToDisplay": "Processing server: %getCellValue1%",
            "Description": "Display current server",
            "description": "Display current server",
        }),
    })
    return wrap("TestWhile", "90002", {
        "createMemoryTable1": create_memory_table(
            "createMemoryTable1", "serverTable", ["server"],
            "Create table of servers to process"
        ),
        "getRowsCount1": act("getRowsCount1", "GetRowsCount", {
            "id": "287",
            "ResultSet": "%serverTable%",
            "Description": "Count rows in server table",
            "description": "Count rows in server table",
        }),
        "whileActivity1": {
            "xName": "whileActivity1",
            "CustomTypeName": "WhileActivity",
            "name": "WhileActivity",
            "visible": "True",
            "disabled": "False",
            "isFavorite": "False",
            "isJsonValid": "True",
            "readPermission": True,
            "writePermission": True,
            "modulePermissions": None,
            "IsValid": "True",
            "activityLicenseType": "1",
            "id": "432",
            "label": "WhileActivity",
            "Description": "Loop through each server",
            "description": "Loop through each server",
            # Condition deliberately omitted — F1 must add it
            "whileSequenceActivity1": seq,
        },
    })


def build_ifelse():
    """
    Ping → IfElseActivity[Branch1: ReturnValue+DisplayValue, Branch2: ReturnValue+DisplayValue]
    Tests: F6 defaults, F7 (Ping = StoredValue tier, all branches explicit)
    """
    return wrap("TestIfElse", "90003", {
        "ping1": act("ping1", "Ping", {
            "id": "104",
            "HostName": "PLACEHOLDER_hostname",
            "Description": "Ping the target server",
            "description": "Ping the target server",
        }),
        "ifElseActivity1": {
            "xName": "ifElseActivity1",
            "CustomTypeName": "IfElseActivity",
            "name": "IfElseActivity",
            "visible": "True",
            "disabled": "False",
            "isFavorite": "False",
            "isJsonValid": "True",
            "readPermission": True,
            "writePermission": True,
            "modulePermissions": None,
            "IsValid": "True",
            "id": "433",
            "label": "IfElseActivity",
            "Description": "Check ping result",
            "description": "Check ping result",
            "ifElseBranchActivity1": branch("ifElseBranchActivity1", "Ping succeeded", {
                "returnValue1": return_value("returnValue1", {
                    "ConditionType": "Equal",
                    "Value": "Success",
                }),
                "displayValue1": act("displayValue1", "DisplayValue", {
                    "id": "431",
                    "ValueToDisplay": "Ping succeeded: %ping1%",
                    "Description": "Log ping success",
                    "description": "Log ping success",
                }),
            }),
            "ifElseBranchActivity2": branch("ifElseBranchActivity2", "Ping failed", {
                "returnValue2": return_value("returnValue2", {
                    "Value": "Failure",
                    # No ConditionType — F7 will set IsValid=True, UseStoredValue=True
                }),
                "displayValue2": act("displayValue2", "DisplayValue", {
                    "id": "431",
                    "ValueToDisplay": "Ping failed: %ping1%",
                    "Description": "Log ping failure",
                    "description": "Log ping failure",
                }),
            }),
        },
    })


def build_while_ifelse():
    """
    CreateMemoryTable → GetRowsCount → WhileActivity[ExitWhile + GetCellValue + Ping + IfElse]
    Tests: F4 (RowNumber in loop), F7 (Ping tier inside loop)
    """
    seq = seq_container("whileSequenceActivity1")
    seq.update({
        "exitWhile1": exit_while("exitWhile1"),
        "getCellValue1": act("getCellValue1", "GetCellValue", {
            "id": "288",
            "ResultSet": "%serverTable%",
            "ColumnNumber": "server",
            "Description": "Get server name",
            "description": "Get server name",
        }),
        "ping1": act("ping1", "Ping", {
            "id": "104",
            "HostName": "%getCellValue1%",
            "Description": "Ping current server",
            "description": "Ping current server",
        }),
        "ifElseActivity1": {
            "xName": "ifElseActivity1",
            "CustomTypeName": "IfElseActivity",
            "name": "IfElseActivity",
            "visible": "True",
            "disabled": "False",
            "isFavorite": "False",
            "isJsonValid": "True",
            "readPermission": True,
            "writePermission": True,
            "modulePermissions": None,
            "IsValid": "True",
            "id": "433",
            "label": "IfElseActivity",
            "Description": "Check ping result",
            "description": "Check ping result",
            "ifElseBranchActivity1": branch("ifElseBranchActivity1", "Ping success", {
                "returnValue1": return_value("returnValue1", {
                    "ConditionType": "Equal",
                    "Value": "Success",
                }),
                "displayValue1": act("displayValue1", "DisplayValue", {
                    "id": "431",
                    "ValueToDisplay": "Server %getCellValue1% is UP",
                    "Description": "Log server up",
                    "description": "Log server up",
                }),
            }),
            "ifElseBranchActivity2": branch("ifElseBranchActivity2", "Ping failure", {
                "returnValue2": return_value("returnValue2", {
                    "Value": "Failure",
                }),
                "displayValue2": act("displayValue2", "DisplayValue", {
                    "id": "431",
                    "ValueToDisplay": "Server %getCellValue1% is DOWN",
                    "Description": "Log server down",
                    "description": "Log server down",
                }),
            }),
        },
    })
    return wrap("TestWhileIfElse", "90004", {
        "createMemoryTable1": create_memory_table(
            "createMemoryTable1", "serverTable", ["server"],
            "Create server list table"
        ),
        "getRowsCount1": act("getRowsCount1", "GetRowsCount", {
            "id": "287",
            "ResultSet": "%serverTable%",
            "Description": "Count servers",
            "description": "Count servers",
        }),
        "whileActivity1": {
            "xName": "whileActivity1",
            "CustomTypeName": "WhileActivity",
            "name": "WhileActivity",
            "visible": "True",
            "disabled": "False",
            "isFavorite": "False",
            "isJsonValid": "True",
            "readPermission": True,
            "writePermission": True,
            "modulePermissions": None,
            "IsValid": "True",
            "activityLicenseType": "1",
            "id": "432",
            "label": "WhileActivity",
            "Description": "Loop through servers and ping each",
            "description": "Loop through servers and ping each",
            "whileSequenceActivity1": seq,
        },
    })


def build_usergroup():
    """UserGroup[CreateMemoryTable + GetRowsCount + DisplayValue]. Tests UserGroup wrapping."""
    return wrap("TestUserGroup", "90005", {
        "userGroup1": {
            "xName": "userGroup1",
            "CustomTypeName": "UserGroup",
            "name": "UserGroup",
            "visible": "True",
            "disabled": "False",
            "isFavorite": "False",
            "isJsonValid": "True",
            "readPermission": True,
            "writePermission": True,
            "modulePermissions": None,
            "IsValid": "True",
            "id": "{x:Null}",
            "label": "UserGroup",
            "Description": "UserGroup activity",
            "description": "UserGroup activity",
            "createMemoryTable1": create_memory_table(
                "createMemoryTable1", "dataTable", ["name", "value"],
                "Create data table"
            ),
            "getRowsCount1": act("getRowsCount1", "GetRowsCount", {
                "id": "287",
                "ResultSet": "%dataTable%",
                "Description": "Count rows in data table",
                "description": "Count rows in data table",
            }),
            "displayValue1": act("displayValue1", "DisplayValue", {
                "id": "431",
                "ValueToDisplay": "Row count: %getRowsCount1%",
                "Description": "Display row count",
                "description": "Display row count",
            }),
        },
    })


# ── Fragment spot-checks ──────────────────────────────────────────────────────

def check_fragments(name, wf):
    """
    Verify key fragment fields are present and correct.
    Returns list of (label, pass/fail, got, expected) tuples.
    """
    checks = []
    raw = wf["workflow_raw_data"]

    def find_all(node, ct):
        """Find all nodes with given CustomTypeName anywhere in tree."""
        results = []
        if not isinstance(node, dict):
            return results
        if node.get("CustomTypeName") == ct:
            results.append(node)
        for v in node.values():
            if isinstance(v, dict):
                results.extend(find_all(v, ct))
        return results

    if name == "TestLinear":
        ms = raw.get("memorySet1", {})
        checks.append(("F5 MemorySet.VariableScope", ms.get("VariableScope"), "Workflow"))
        checks.append(("F5 MemorySet.IsSaved", ms.get("IsSaved"), "False"))
        checks.append(("F5 MemorySet.IsAppend", ms.get("IsAppend"), "False"))
        # No ReturnValue at top level
        checks.append(("No top-level ReturnValue", "returnValue1" not in raw, True))

    if name in ("TestWhile", "TestWhileIfElse"):
        was = find_all(raw.get("whileActivity1", {}), "WhileActivity")
        if was:
            wa = was[0]
            checks.append(("F1 WhileActivity.Condition", wa.get("Condition"), "{x:Null}"))
        ews = find_all(raw, "ExitWhile")
        if ews:
            ew = ews[0]
            checks.append(("F2 ExitWhile.exitWhileInsideWhile", ew.get("exitWhileInsideWhile"), "True"))
            checks.append(("F2 ExitWhile.whileSequenceActivity", bool(ew.get("whileSequenceActivity")), True))
            checks.append(("F3 ExitWhile.Counter", ew.get("Counter"), "%getRowsCount1%"))
        gcvs = find_all(raw, "GetCellValue")
        if gcvs:
            gcv = gcvs[0]
            checks.append(("F4 GetCellValue.RowNumber", gcv.get("RowNumber"), "%whileActivity1%"))
            checks.append(("F4 GetCellValue.ColumnType", gcv.get("ColumnType"), "Name"))

    if name in ("TestIfElse", "TestWhileIfElse"):
        rvs = find_all(raw, "ReturnValue")
        # Branch 1 (success): UseStoredValue=True, IsValid=True (F7)
        rv1 = next((r for r in rvs if r.get("ConditionType")), None)
        if rv1:
            checks.append(("F7 condition branch UseStoredValue", rv1.get("UseStoredValue"), "True"))
            checks.append(("F7 condition branch IsValid", rv1.get("IsValid"), "True"))
            checks.append(("F7 condition branch Type", rv1.get("Type"), "StoredValue"))
        # Branch 2 (else/failure): IsValid=True (status producer — no catch-all else)
        rv2 = next((r for r in rvs if not r.get("ConditionType")), None)
        if rv2:
            checks.append(("F7 else branch IsValid", rv2.get("IsValid"), "True"))
            checks.append(("F7 else branch UseStoredValue", rv2.get("UseStoredValue"), "True"))
        # No forbidden fields on any ReturnValue
        for rv in rvs:
            for bad in ("visible", "disabled", "isFavorite", "isJsonValid"):
                if bad in rv:
                    checks.append((f"ReturnValue no {bad}", False, True))

    if name == "TestUserGroup":
        checks.append(("No top-level ReturnValue", "returnValue1" not in raw, True))

    return checks


# ── XML validation ────────────────────────────────────────────────────────────

def validate_xml(wf):
    """Compose and validate XML. Returns (xml_string, errors)."""
    composer = WorkflowXmlComposer()
    xml_string = composer.compose(wf, wf["name"], wf["pnumber"])
    errors = []
    try:
        root = _validate_outer(xml_string)
    except ET.ParseError as e:
        errors.append(f"Outer XML invalid: {e}")
        return xml_string, errors
    try:
        _validate_xoml(root)
    except ET.ParseError as e:
        errors.append(f"Xoml invalid: {e}")
    return xml_string, errors


# ── Test runner ───────────────────────────────────────────────────────────────

TESTS = [
    ("TestLinear",       build_linear,       "GetDate → MemorySet → DisplayValue. Tests F5."),
    ("TestWhile",        build_while,        "While loop. Tests F1, F2, F3, F4, F5."),
    ("TestIfElse",       build_ifelse,       "Ping → IfElse. Tests F6, F7."),
    ("TestWhileIfElse",  build_while_ifelse, "While + IfElse inside loop. Tests F4, F7 in loop context."),
    ("TestUserGroup",    build_usergroup,    "UserGroup. Tests basic template correctness."),
]


def run_tests(output_dir="./test_xml_output"):
    import pathlib
    output_path = pathlib.Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("Fragment Pipeline Test Suite")
    print("=" * 65)
    print()

    total = 0
    passed = 0

    for test_name, builder_fn, description in TESTS:
        total += 1
        print(f"── {test_name} ──────────────────────────────────────")
        print(f"   {description}")

        test_errors = []

        # Step 1: Build raw workflow
        try:
            raw_wf = builder_fn()
        except Exception as e:
            print(f"   BUILD FAILED: {e}")
            test_errors.append(f"Build failed: {e}")
            continue

        # Step 2: Apply fragments
        try:
            wf = run_fragments(raw_wf)
        except Exception as e:
            print(f"   FRAGMENTS FAILED: {e}")
            test_errors.append(f"run_fragments failed: {e}")
            continue

        # Step 3: Fragment spot-checks
        checks = check_fragments(test_name, wf)
        check_failures = []
        for label, got, expected in checks:
            if got != expected:
                check_failures.append(f"{label}: got {repr(got)}, expected {repr(expected)}")

        if check_failures:
            for f in check_failures:
                print(f"   FRAGMENT CHECK FAIL: {f}")
                test_errors.append(f)
        else:
            print(f"   Fragment checks: {len(checks)}/{len(checks)} passed")

        # Step 4: Annotation
        try:
            annotation = run_annotation(wf, [])
        except Exception as e:
            print(f"   ANNOTATION FAILED: {e}")
            test_errors.append(f"run_annotation failed: {e}")
            annotation = None

        # Step 5: Validation
        if annotation:
            try:
                val = run_validation(annotation)
                if val["errors"]:
                    for err in val["errors"]:
                        print(f"   VALIDATION ERROR: {err}")
                        test_errors.append(err)
                else:
                    print(f"   Validation: PASSED")
                if val.get("verify_notes"):
                    for note in val["verify_notes"]:
                        print(f"   VERIFY: {note[:100]}")
            except Exception as e:
                print(f"   VALIDATION FAILED: {e}")
                test_errors.append(f"run_validation failed: {e}")

        # Step 6: XML composition and validation
        try:
            xml_string, xml_errors = validate_xml(wf)
            if xml_errors:
                for err in xml_errors:
                    print(f"   XML ERROR: {err}")
                    test_errors.append(err)
            else:
                print(f"   XML: VALID (outer + Xoml)")
                # Write file
                out_file = output_path / f"{test_name.lower()}.xml"
                out_file.write_text(xml_string, encoding="utf-8")
                print(f"   Written: {out_file}")
        except Exception as e:
            print(f"   XML COMPOSITION FAILED: {e}")
            test_errors.append(f"XML failed: {e}")

        # Result
        if not test_errors:
            passed += 1
            print(f"   RESULT: PASS ✓")
        else:
            print(f"   RESULT: FAIL ✗ ({len(test_errors)} error(s))")

        print()

    # Summary
    print("=" * 65)
    print(f"Results: {passed}/{total} passed")
    if passed == total:
        print("All fragment tests passed.")
    else:
        print(f"{total - passed} test(s) failed — see errors above.")
    print("=" * 65)

    return passed == total


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fragment pipeline test suite")
    parser.add_argument(
        "--output", default="./test_xml_output",
        help="Directory to write XML files (default: ./test_xml_output)"
    )
    args = parser.parse_args()

    success = run_tests(output_dir=args.output)
    sys.exit(0 if success else 1)