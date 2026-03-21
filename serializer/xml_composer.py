import xml.etree.ElementTree as ET
import uuid


class WorkflowXmlComposer:

    NAMESPACE_REGISTRY = {
        "IfElseActivity":       (None, None, None),
        "IfElseBranchActivity": (None, None, None),
        "SequenceActivity":     (None, None, None),
        "ParallelActivity":     (None, None, None),
        "ServiceStatus":  ("ns_svcstatus",  "ServiceStatus",  "ServiceStatus, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null"),
        "ServiceStart":   ("ns_svcstart",   "ServiceStart",   "ServiceStart, Version=1.4.0.0, Culture=neutral, PublicKeyToken=null"),
        "ReturnValue":    ("ns_retval",     "ReturnValue",    "ReturnValue, Version=1.2.0.0, Culture=neutral, PublicKeyToken=null"),
        "Continue":       ("ns_continue",   "Continue",       "Continue, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null"),
        "SendEmail":      ("ns_sendemail",  "SendEmail",      "SendEmail, Version=1.2.0.0, Culture=neutral, PublicKeyToken=null"),
        "WhileActivity":     ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "ExitWhile":         ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "ForEachActivity":   ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "UserGroup":         ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "MemorySet":         ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "DisplayValue":      ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "GetRowsCount":      ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "GetCellValue":      ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "RunWorkflow":       ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "Ping":              ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "MultiMemorySet":    ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "DisplayMultiValue": ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
        "GetRows":           ("ns0", "EyeShare.Workflow.Activities", "EyeShare.Workflow.Activities"),
    }

    SKIP_FIELDS = {
        "CustomTypeName", "isJsonValid", "isFavorite", "modulePermissions",
        "activityLicenseType", "readPermission", "writePermission",
    }

    def compose(self, workflow_json: dict, workflow_name: str, pnumber: str) -> str:
        raw_data = workflow_json.get("workflow_raw_data", {})
        xoml = self._build_xoml(raw_data, workflow_name)

        total_export = ET.Element("TotalExport", attrib={"sourceSystem": "NG"})
        ET.SubElement(total_export, "WorkflowInfo", attrib={
            "Pnumber": pnumber,
            "Name": workflow_name,
            "DateLic": "",
            "Xoml": xoml,
        })

        ET.indent(total_export, space="  ")
        return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
            total_export, encoding="unicode", xml_declaration=False
        )

    def _build_xoml(self, raw_data: dict, workflow_name: str) -> str:
        used_namespaces = self._collect_namespaces(raw_data)

        attribs = {
            "x:Name": workflow_name,
            "xmlns:x": "http://schemas.microsoft.com/winfx/2006/xaml",
            "xmlns":   "http://schemas.microsoft.com/winfx/2006/xaml/workflow",
        }
        for prefix, (clr_ns, assembly) in used_namespaces.items():
            attribs[f"xmlns:{prefix}"] = f"clr-namespace:{clr_ns};Assembly={assembly}"

        root = ET.Element("SequentialWorkflowActivity", attrib=attribs)

        for xname, activity in raw_data.items():
            if isinstance(activity, dict):
                elem = self._serialize_activity(activity)
                if elem is not None:
                    root.append(elem)

        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode")

    def _collect_namespaces(self, node, seen=None):
        if seen is None:
            seen = {}
        if isinstance(node, dict):
            ct = node.get("CustomTypeName", "")
            if ct in self.NAMESPACE_REGISTRY:
                prefix, clr_ns, assembly = self.NAMESPACE_REGISTRY[ct]
                if prefix and prefix not in seen:
                    seen[prefix] = (clr_ns, assembly)
            else:
                if ct and "ns0" not in seen:
                    seen["ns0"] = (
                        "EyeShare.Workflow.Activities",
                        "EyeShare.Workflow.Activities",
                    )
            for value in node.values():
                self._collect_namespaces(value, seen)
        return seen

    def _serialize_activity(self, activity: dict) -> ET.Element | None:
        custom_type = activity.get("CustomTypeName", "")
        if not custom_type:
            return None

        tag = self._resolve_tag(custom_type)
        attribs = {"x:Name": activity.get("xName", "")}
        child_elements = []

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
                attribs[key] = str(value)

        elem = ET.Element(tag, attrib=attribs)
        for child in child_elements:
            elem.append(child)
        return elem

    def _resolve_tag(self, custom_type: str) -> str:
        if custom_type not in self.NAMESPACE_REGISTRY:
            return f"ns0:{custom_type}"
        prefix, clr_ns, assembly = self.NAMESPACE_REGISTRY[custom_type]
        if prefix is None:
            return custom_type
        return f"{prefix}:{custom_type}"