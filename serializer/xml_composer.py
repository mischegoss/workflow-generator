import json
import os
import re
import xml.etree.ElementTree as ET

# Register namespaces once at module load.
# '' = default namespace → activity tags serialize as <WhileActivity> not <ns0:WhileActivity>
# 'x' = XAML namespace → attributes serialize as x:Name, x:Class
# Without this, ET auto-generates ns0:/ns1: prefixes which the platform cannot resolve.
_WORKFLOW_NS = "http://schemas.microsoft.com/winfx/2006/xaml/workflow"
_XAML_NS     = "http://schemas.microsoft.com/winfx/2006/xaml"
ET.register_namespace("",  _WORKFLOW_NS)
ET.register_namespace("x", _XAML_NS)


def _wf(tag: str) -> str:
    """Return Clark-notation tag for the workflow namespace: {ns}Tag"""
    return f"{{{_WORKFLOW_NS}}}{tag}"


def _x(attr: str) -> str:
    """Return Clark-notation attribute for the XAML namespace: {ns}attr"""
    return f"{{{_XAML_NS}}}{attr}"


class WorkflowXmlComposer:
    """
    Serialises workflow JSON to a TotalExport XOML XML string for import
    into Resolve Actions.

    Namespace handling (confirmed from 609 real workflow exports):
    - Activity tags use the DEFAULT workflow namespace with NO prefix.
      <WhileActivity> not <ns0:WhileActivity>
    - x:Name and x:Class use the 'x' XAML namespace prefix.
    - No other namespace prefixes appear on activity tags.
    - ET.register_namespace at module level enforces this. Child elements
      must be created with Clark notation {ns}Tag — plain string tags
      cause ET to emit ns0: prefixes which the platform cannot resolve,
      resulting in empty/broken loop bodies on import.

    Other fixes:
    - WorkflowInfo emits all 12 required attributes (XomlStatus, WorkflowType,
      WorkflowFolderId, etc.) — omitting these causes save failure on import.
    - Objects block uses the exact 28 child tags the platform expects.
    - html.escape() removed from compose() — ET handles attribute escaping.
      Double-escaping (&lt; → &amp;lt;) made Xoml unparseable.
    """

    SKIP_FIELDS = {
        "notes",
        "workflow_name",
        "variable_contracts",
        "modulePermissions",
        "isFavorite",
    }

    NO_DEFAULTS = {
        "IfElseActivity", "IfElseBranchActivity", "SequenceActivity",
        "WhileActivity", "ParallelActivity", "ForEachActivity", "UserGroup",
        "ExitWhile", "ReturnValue", "Continue",
    }

    SEQUENCE_CONTAINERS = {"WhileActivity", "ForEachActivity"}

    ACTIVITY_DEFAULTS = {
        "visible":                 "True",
        "disabled":                "False",
        "isFavorite":              "False",
        "isJsonValid":             "True",
        "readPermission":          "True",
        "writePermission":         "True",
        "notes":                   "",
        "Timeout":                 "00:01:00",
        "TimeInSeconds":           "60",
        "RecoveryMethodSelection": "{x:Null}",
        "TargetModuleID":          "",
        "TargetModuleName":        "",
        "Path":                    "{x:Null}",
        "activityLicenseType":     "1",
        "IsValid":                 "True",
    }

    # WorkflowInfo attributes required by the platform for a successful save.
    # Confirmed from exported workflow exemplars — omitting any causes save failure.
    WORKFLOW_INFO_DEFAULTS = {
        "XomlStatus":             "0",
        "Details":                "",
        "WorkflowType":           "0",
        "WorkflowFolderId":       "0",
        "Permissions":            "",
        "ErrorHandling":          "",
        "CurrentRevisionNumber":  "1",
        "WorkflowParentId":       "0",
        "DateCreated":            "",
        "DateCreatedUser":        "",
        "DateModified":           "",
        "DateModifiedUser":       "",
    }

    # Objects children required by the platform, in the order the platform expects.
    # Wrong or missing tags cause save failures on import.
    OBJECTS_CHILDREN = [
        "Hosts", "ErrorHandlers", "ErrorMessages", "MessageTemplates",
        "Sites", "Developments", "Users", "Groups", "UsersGroupsArray",
        "Domains", "Commands", "Classifications", "Incidents", "TimeFrames",
        "Variables", "Modules", "Conditions", "ConditionArrays",
        "ConditionObjects", "SoapWebServices", "Triggers",
        "TriggerConditionArrays", "LogCategory", "LogTriggerCategory",
        "Schedules", "CustomActivities", "ActivitiesSource",
        "ScheduleCategoriesRelations",
    ]

    _namespace_registry_data: dict | None = None
    _id_lookup: dict | None = None

    # ------------------------------------------------------------------ #
    #  Namespace registry — reference data, not used for prefixing        #
    # ------------------------------------------------------------------ #

    @classmethod
    def _load_namespace_registry_data(cls) -> dict:
        """
        Loads namespace_registry.json for reference only.
        NOT used for tag prefixing — all tags use the default namespace.
        """
        if cls._namespace_registry_data is not None:
            return cls._namespace_registry_data
        data_dir = os.getenv("DATA_DIR", "/app/data")
        path = os.path.join(data_dir, "namespace_registry.json")
        try:
            with open(path, encoding="utf-8") as f:
                cls._namespace_registry_data = json.load(f)
            print(f"[xml_composer] Loaded {len(cls._namespace_registry_data)} "
                  f"namespace entries (reference only — no prefixes applied)")
        except FileNotFoundError:
            print(f"[xml_composer] namespace_registry.json not found at {path} — ok")
            cls._namespace_registry_data = {}
        return cls._namespace_registry_data

    def get_clr_string(self, activity_name: str) -> str | None:
        """Returns the CLR namespace string for an activity if known."""
        data = self._load_namespace_registry_data()
        return data.get(activity_name)

    # ------------------------------------------------------------------ #
    #  Formula builder                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_formula(condition_type: str, value: str) -> str | None:
        if not condition_type:
            return None
        return f"={condition_type}(&&&,{value})"

    # ------------------------------------------------------------------ #
    #  Template id lookup                                                 #
    # ------------------------------------------------------------------ #

    def _load_id_lookup(self) -> dict:
        if self.__class__._id_lookup is not None:
            return self.__class__._id_lookup
        data_dir = os.getenv("DATA_DIR", "/app/data")
        path = os.path.join(data_dir, "activity_json_syntax.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            templates = data.get("settings", data) if isinstance(data, dict) else data
            self.__class__._id_lookup = {
                t.get("CustomTypeName") or t.get("TypeName"): str(t["id"])
                for t in templates
                if t.get("id") and (t.get("CustomTypeName") or t.get("TypeName"))
            }
        except Exception:
            self.__class__._id_lookup = {}
        return self.__class__._id_lookup

    # ------------------------------------------------------------------ #
    #  Public entry point                                                 #
    # ------------------------------------------------------------------ #

    def compose(self, workflow_json: dict, workflow_name: str, pnumber: str) -> str:
        """
        Serialises a workflow JSON dict to a TotalExport XOML XML string.

        Xoml is passed directly as an ET attribute — no html.escape().
        ET.tostring() handles all required XML attribute escaping automatically.
        """
        self._load_namespace_registry_data()

        raw_data = workflow_json.get("workflow_raw_data", workflow_json)
        xoml_string = self._build_xoml(raw_data, workflow_name)

        total_export = ET.Element("TotalExport", attrib={"sourceSystem": "NG"})
        workflows_elem = ET.SubElement(total_export, "Workflows")

        workflow_info_attrib = {
            "Pnumber":     str(pnumber),
            "Name":        workflow_name,
            "Description": "",
            "DateLic":     "",
            "Xoml":        xoml_string,
        }
        workflow_info_attrib.update(self.WORKFLOW_INFO_DEFAULTS)
        ET.SubElement(workflows_elem, "WorkflowInfo", attrib=workflow_info_attrib)

        objects_elem = ET.SubElement(total_export, "Objects")
        for tag in self.OBJECTS_CHILDREN:
            ET.SubElement(objects_elem, tag)

        ET.SubElement(total_export, "ObjectsRelations")
        ET.SubElement(total_export, "ExportKeys")

        ET.indent(total_export, space="  ")
        return ET.tostring(total_export, encoding="unicode", xml_declaration=False)

    # ------------------------------------------------------------------ #
    #  XOML builder                                                       #
    # ------------------------------------------------------------------ #

    def _build_xoml(self, raw_data: dict, workflow_name: str) -> str:
        """
        Builds the inner XOML string using Clark-notation tags so ET
        serializes plain <ActivityTag> elements under the default namespace,
        not <ns0:ActivityTag> which the platform cannot resolve.
        """
        # Root element uses Clark notation for namespace-qualified attributes
        root = ET.Element(
            _wf("SequentialWorkflowActivity"),
            attrib={
                _x("Name"):  "CustomWorkflow",
                _x("Class"): "WorkflowDesignerControl.CustomWorkflow",
            },
        )

        for xname, activity in raw_data.items():
            if isinstance(activity, dict):
                elem = self._serialize_activity(activity)
                if elem is not None:
                    root.append(elem)

        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode")

    # ------------------------------------------------------------------ #
    #  Activity serializer                                                #
    # ------------------------------------------------------------------ #

    def _serialize_activity(self, activity: dict) -> ET.Element | None:
        custom_type = activity.get("CustomTypeName", "")
        if not custom_type:
            return None

        id_lookup = self._load_id_lookup()

        # Use Clark notation so ET emits <ActivityName> (default namespace)
        # not <ns0:ActivityName> (auto-generated prefix).
        tag = _wf(custom_type)

        # x:Name uses Clark notation for the XAML namespace attribute
        attribs = {_x("Name"): activity.get("xName", "")}
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

        if custom_type not in self.NO_DEFAULTS:
            for k, v in self.ACTIVITY_DEFAULTS.items():
                if k not in attribs:
                    attribs[k] = v
            if "id" not in attribs and custom_type in id_lookup:
                attribs["id"] = id_lookup[custom_type]

            attribs["name"] = custom_type
            attribs["TypeName"] = custom_type
            attribs["DisplayName"] = custom_type
            attribs["label"] = custom_type

            if "Description" in attribs and "description" not in attribs:
                attribs["description"] = attribs["Description"]
            elif "description" not in attribs:
                attribs["description"] = ""

        if custom_type == "WhileActivity":
            # Condition="{x:Null}" is required for the platform to wire the ExitWhile
            # counter mechanism. Without it the loop body does not render on import.
            if "Condition" not in attribs:
                attribs["Condition"] = "{x:Null}"

        if custom_type == "GetCellValue":
            attribs["ColumnType"] = "Name"

        if custom_type == "ReturnValue":
            condition_type = attribs.get("ConditionType", "")
            value = attribs.get("Value", "")
            formula = self._build_formula(condition_type, value)
            attribs["Formula"] = "{x:Null}" if formula is None else formula

        elem = ET.Element(tag, attrib=attribs)

        if custom_type in self.SEQUENCE_CONTAINERS:
            seq_elem = None
            other_children = []
            for child in child_elements:
                # Clark notation: strip namespace to get local tag name
                child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_local == "SequenceActivity":
                    seq_elem = child
                else:
                    other_children.append(child)
            if seq_elem is not None:
                for child in other_children:
                    seq_elem.append(child)
                # Enforce ExitWhile-first ordering inside SequenceActivity.
                # Corpus confirms: ExitWhile must be the first child — the platform
                # uses it to wire the loop counter before executing body activities.
                exit_while = None
                rest = []
                for child in list(seq_elem):
                    child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if child_local == "ExitWhile":
                        exit_while = child
                    else:
                        rest.append(child)
                if exit_while is not None:
                    seq_elem[:] = [exit_while] + rest
                elem.append(seq_elem)
            else:
                for child in child_elements:
                    elem.append(child)

        elif custom_type == "IfElseBranchActivity":
            # Enforce ReturnValue-first ordering inside each branch.
            # Platform requires ReturnValue as the first child to evaluate
            # the branch condition before executing branch body activities.
            return_val = None
            rest = []
            for child in child_elements:
                child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_local == "ReturnValue" and return_val is None:
                    return_val = child
                else:
                    rest.append(child)
            ordered = ([return_val] if return_val is not None else []) + rest
            for child in ordered:
                elem.append(child)
        else:
            for child in child_elements:
                elem.append(child)

        return elem
    