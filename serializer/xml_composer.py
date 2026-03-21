import datetime
import xml.etree.ElementTree as ET


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
        "WhileActivity":     (None, None, None),
        "ExitWhile":         (None, None, None),
        "ForEachActivity":   (None, None, None),
        "UserGroup":         (None, None, None),
        "MemorySet":         (None, None, None),
        "DisplayValue":      (None, None, None),
        "GetRowsCount":      (None, None, None),
        "GetCellValue":      (None, None, None),
        "RunWorkflow":       (None, None, None),
        "Ping":              (None, None, None),
        "MultiMemorySet":    (None, None, None),
        "DisplayMultiValue": (None, None, None),
        "GetRows":           (None, None, None),
    }

    SKIP_FIELDS = {
        "CustomTypeName",
        "modulePermissions",
        "notes",
    }

    SEQUENCE_CONTAINERS = {"WhileActivity", "ForEachActivity"}

    def compose(self, workflow_json: dict, workflow_name: str, pnumber: str) -> str:
        raw_data = workflow_json.get("workflow_raw_data", {})
        xoml = self._build_xoml(raw_data, workflow_name)
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000")

        total_export = ET.Element("TotalExport", attrib={"sourceSystem": "NG"})

        workflows_elem = ET.SubElement(total_export, "Workflows")
        ET.SubElement(workflows_elem, "WorkflowInfo", attrib={
            "Pnumber":               pnumber,
            "Name":                  workflow_name,
            "Description":           workflow_json.get("description", ""),
            "Xoml":                  xoml,
            "XomlStatus":            "0",
            "Details":               "",
            "DateLic":               "",
            "WorkflowType":          "0",
            "WorkflowFolderId":      "0",
            "WorkflowParentId":      "0",
            "Permissions":           "",
            "ErrorHandling":         "",
            "CurrentRevisionNumber": "1",
            "DateCreated":           now,
            "DateCreatedUser":       "1",
            "DateModified":          now,
            "DateModifiedUser":      "1",
        })

        # Objects block — required by platform for valid import
        objects_elem = ET.SubElement(total_export, "Objects")
        for tag in [
            "Hosts", "ErrorHandlers", "ErrorMessages", "MessageTemplates",
            "Sites", "Developments", "Users", "Groups", "UsersGroupsArray",
            "Domains", "Commands", "Classifications", "Incidents", "TimeFrames",
            "Variables", "Modules", "Conditions", "ConditionArrays",
            "ConditionObjects", "SoapWebServices", "Triggers",
            "TriggerConditionArrays", "LogCategory", "LogTriggerCategory",
            "Schedules", "CustomActivities", "ActivitiesSource",
            "ScheduleCategoriesRelations",
        ]:
            ET.SubElement(objects_elem, tag)

        ET.SubElement(total_export, "ObjectsRelations")
        ET.SubElement(total_export, "ExportKeys")

        ET.indent(total_export, space="  ")
        return ET.tostring(total_export, encoding="unicode", xml_declaration=False)

    def _build_xoml(self, raw_data: dict, workflow_name: str) -> str:
        used_namespaces = self._collect_namespaces(raw_data)

        attribs = {
            "xmlns":   "http://schemas.microsoft.com/winfx/2006/xaml/workflow",
            "xmlns:x": "http://schemas.microsoft.com/winfx/2006/xaml",
            "x:Name":  "CustomWorkflow",
            "x:Class": "WorkflowDesignerControl.CustomWorkflow",
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

        if custom_type in self.SEQUENCE_CONTAINERS:
            seq_elem = None
            other_children = []
            for child in child_elements:
                child_tag = child.tag.split(":")[-1] if ":" in child.tag else child.tag
                if child_tag == "SequenceActivity":
                    seq_elem = child
                else:
                    other_children.append(child)
            if seq_elem is not None:
                for child in other_children:
                    seq_elem.append(child)
                elem.append(seq_elem)
            else:
                for child in child_elements:
                    elem.append(child)
        else:
            for child in child_elements:
                elem.append(child)

        return elem

    def _resolve_tag(self, custom_type: str) -> str:
        if custom_type not in self.NAMESPACE_REGISTRY:
            return custom_type
        prefix, clr_ns, assembly = self.NAMESPACE_REGISTRY[custom_type]
        if prefix is None:
            return custom_type
        return f"{prefix}:{custom_type}"