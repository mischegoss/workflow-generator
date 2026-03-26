import json
import os
import re
import xml.etree.ElementTree as ET


class WorkflowXmlComposer:
    """
    Corpus finding (609 real workflows): needs_prefix = 0.
    No activity type in the corpus uses an xmlns namespace prefix in XOML.
    The platform imports correctly without any prefix declarations.

    namespace_registry.json is loaded at startup for reference (CLR strings
    are valid data for future use), but ALL activities resolve to plain tags.

    Fix applied: removed html.escape() from compose(). ET handles attribute
    escaping automatically when serializing — calling html.escape() first
    caused double-escaping (&lt; → &amp;lt;), which made the stored Xoml
    unparseable by the platform and by convert_to_xml.py validation.
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

    _namespace_registry_data: dict | None = None
    _id_lookup: dict | None = None

    # ------------------------------------------------------------------ #
    #  Namespace registry — reference data, not used for prefixing        #
    # ------------------------------------------------------------------ #

    @classmethod
    def _load_namespace_registry_data(cls) -> dict:
        """
        Loads namespace_registry.json for reference only.
        NOT used for tag prefixing — all tags are plain (no prefix).
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
        """
        Returns the CLR namespace string for an activity if known.
        For reference/tooling use only — not used in XOML serialization.
        """
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

        Note: Xoml is passed directly as an ET attribute — no html.escape().
        ET.tostring() handles all required XML attribute escaping automatically.
        Calling html.escape() before setting the attribute causes double-escaping
        (&lt; → &amp;lt;) which makes the Xoml unparseable by the platform.
        """
        self._load_namespace_registry_data()

        raw_data = workflow_json.get("workflow_raw_data", workflow_json)
        xoml_string = self._build_xoml(raw_data, workflow_name)

        total_export = ET.Element("TotalExport", attrib={"sourceSystem": "NG"})
        workflows_elem = ET.SubElement(total_export, "Workflows")

        # Pass xoml_string directly — ET escapes it correctly on serialization
        ET.SubElement(
            workflows_elem,
            "WorkflowInfo",
            attrib={
                "Pnumber":     str(pnumber),
                "Name":        workflow_name,
                "Description": "",
                "DateLic":     "",
                "Xoml":        xoml_string,
            },
        )

        objects_elem = ET.SubElement(total_export, "Objects")
        for tag in [
            "Categories", "AlertCategories", "Modules", "Schedules2",
            "LogCategory", "LogTriggerCategory",
            "Schedules", "CustomActivities", "ActivitiesSource",
            "ScheduleCategoriesRelations",
        ]:
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
        Builds the inner XOML string.
        No xmlns: prefix declarations added — corpus confirms none are needed.
        """
        attribs = {
            "xmlns":   "http://schemas.microsoft.com/winfx/2006/xaml/workflow",
            "xmlns:x": "http://schemas.microsoft.com/winfx/2006/xaml",
            "x:Name":  "CustomWorkflow",
            "x:Class": "WorkflowDesignerControl.CustomWorkflow",
        }

        root = ET.Element("SequentialWorkflowActivity", attrib=attribs)

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

        # Corpus-confirmed: plain tag, no namespace prefix
        tag = custom_type

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
    