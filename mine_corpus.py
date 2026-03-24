"""
mine_corpus.py
──────────────
Single-pass corpus miner. Run from the repo root with workflow XML files in
./workflows_raw/

    cd ~/Documents/workflow_generator
    python3 mine_corpus.py

Produces six files in ./data/:

    data/namespace_registry.json        CLR namespace string per activity type
    data/field_defaults.json            Most-common config values per activity/field
    data/activity_ranks.json            Co-occurrence pairs (replaces existing)
    data/enum_values.json               Valid values for dropdown/radiobutton fields
    data/wiring_map.json                How activity outputs wire to next activity inputs
    data/patterns/scaffolds.json        PARAM_-generalised scaffolds per pattern

After running:
  • namespace_registry.json
      → Add entries to NAMESPACE_REGISTRY in serializer/xml_composer.py
      → Remove confirmed types from UNCONFIRMED_NAMESPACE_ACTIVITIES in
        tools/annotation_tools.py

  • activity_ranks.json
      → Replace data/activity_ranks.json directly
      → Counts are per-workflow binary — each workflow contributes at most 1
        to any given pair. Used by retrieval_tools._load_rank_data() for
        frequency_tier scoring.

  • enum_values.json
      → Load in tools/build_tools.py — replace "_value" placeholders in
        activity_json_syntax.json templates with real enum values at template
        load time. Also pass to StructureBuilder to enumerate valid choices
        for dropdown/radiobutton fields.
      → Values with fewer than --min-enum-obs observations are excluded (noise
        floor). SPOT-CHECK on first full run: verify no legitimate enum values
        ending in a digit were filtered by _is_xname_instance().

  • wiring_map.json
      → Load in tools/build_tools.py — when StructureBuilder places activity B
        after activity A, look up (A, B) and pre-fill connection fields
        deterministically.
      → workflow_count = workflows containing this wire (not loop iterations).
      → pct_of_target  = fraction of workflows where target field has any var ref.
      → Entries with authoritative=true are platform rules, not corpus-derived.

  • patterns/scaffolds.json
      → Paste scaffold values into pattern_library.json:
          pattern["scaffold"] = scaffolds["scaffolds"]["p019"]["scaffold"]
      → Representative is the workflow closest to median leaf count (not shortest).

  • field_defaults.json
      → Reference for config enum seeding in tools/build_tools.py

Options:
    --xml-dir            Directory of XML files   [default: ./workflows_raw]
    --pattern-lib        Pattern library path     [default: ./data/patterns/pattern_library.json]
    --controls           activities_controls.json [default: ./data/activities_controls.json]
    --output-dir         Where to write outputs   [default: ./data]
    --patterns           Limit scaffold mining to specific IDs, e.g. p019 p020
    --min-matches        Min workflows to generate a scaffold  [default: 3]
    --coverage-threshold Pattern match coverage threshold 0.0-1.0 [default: 0.45]
    --window             Co-occurrence sliding window size     [default: 3]
    --min-enum-obs       Min per-value observations for enum_values [default: 3]
    --dry-run            Print stats, write nothing

Dependencies: Python stdlib only.
"""

import argparse
import hashlib
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET


# -----------------------------------------------------------------------------
#  Repo-relative defaults
# -----------------------------------------------------------------------------

_REPO_ROOT    = Path(__file__).parent
_DEFAULT_XML  = _REPO_ROOT / "workflows_raw"
_DEFAULT_LIB  = _REPO_ROOT / "data" / "patterns" / "pattern_library.json"
_DEFAULT_CTRL = _REPO_ROOT / "data" / "activities_controls.json"
_DEFAULT_OUT  = _REPO_ROOT / "data"


# -----------------------------------------------------------------------------
#  Field classification  (drives scaffold PARAM_ decisions)
# -----------------------------------------------------------------------------

STRUCTURAL_FIELDS = {
    "activityLicenseType", "visible", "disabled", "isFavorite", "isJsonValid",
    "readPermission", "writePermission", "IsValid",
    "Timeout", "TimeInSeconds", "RecoveryMethodSection",
    "TypeName", "DisplayName", "label", "name", "description", "Description",
    "exitWhileInsideWhile", "isValid",
    "ConditionNumber", "UseCustomeCondition", "Disabled", "ClusterID", "ClusterName",
    "UseStoredValue", "isDefault", "useAlternateSetting",
}

ALWAYS_PARAM_FIELDS = {
    "TableName", "ResultSet", "ResultSetName",
    "ForEachTableVariable", "ForEachOutputVariableName",
    "RowNumber", "ColumnNumber", "ColumnName",
    "VariableName", "VariableValue",
    "Counter", "whileSequenceActivity",
    "To", "Cc", "Subject", "Body", "Attachments", "DestinationNumber",
    "MessageType", "TemplateNumber",
    "HostName", "HostId", "ServiceName",
    "Query", "ConnectionString", "ConnectionStringTextBox",
    "Code", "Script", "Command", "TheValue", "TheValue2",
    "ValueToDisplay",
    "SourcePath", "TargetPath", "SrcPath", "DstPath", "FilePath",
    "URL", "Url",
    "WorkflowID", "WorkflowName", "variables",
    "TableAsString",
    "Value", "Formula", "ConditionType", "ConditionName", "Type", "UseBranchWhenTimeout",
}

CREDENTIAL_FIELDS = {
    "Password", "SrcPassword", "DstPassword", "ArchivePassword",
    "LoginPassword", "ACPassword", "AdminPassword", "CertificatePassword",
    "api_key", "token", "openai_api_key", "UserName", "Username",
    "SmtpServer", "SmtpPort", "SmtpUser", "SmtpPass",
}

STRIP_FIELDS = {
    "notes", "modulePermissions", "isFavorite",
    "DateLic", "DateCreated", "DateCreatedUser", "DateModified", "DateModifiedUser",
}

CONFIG_FIELDS = {
    "FuturePast", "TimeInterval", "DateFormat", "TimeZoneName", "TimeToAdd", "IsNowSelected",
    "ReturnFormat", "FirstDateFormat", "SecondDateFormat",
    "ColumnType",
    "VariableScope", "IsSaved", "IsAppend",
    "UseStoredValue",
    "Channel", "SendRN", "DestinationType", "DestinationTypeCc",
    "Method", "ContentType", "AuthType",
    "Encoding", "FileType",
    "StartupType",
    "SelectionType", "SortDirection", "FilterType",
    "isEmptyGrid",
}

# Input types that represent a fixed set of valid values
ENUM_INPUT_TYPES = {"dropdown", "radiobutton", "radiobutton-extended"}

# Container/structural activity types — excluded from leaf sequences
CONTAINER_TYPES = {
    "IfElseActivity", "IfElseBranchActivity", "SequenceActivity",
    "WhileActivity", "ForEachActivity", "ParallelActivity", "UserGroup",
    "ExitWhile", "ReturnValue", "Continue",
}

# Top-level containers used for zone-based structural fingerprinting
MAJOR_CONTAINERS = {
    "WhileActivity", "ForEachActivity", "IfElseActivity", "ParallelActivity", "UserGroup",
}

_SKIP_TAGS = {
    "schema", "element", "complexType", "sequence", "choice",
    "resultSet", "NewDataSet", "SerializedData", "ParamName", "ParamValue",
    "annotation", "documentation", "attribute", "restriction", "enumeration",
}

_XAML_NS   = "http://schemas.microsoft.com/winfx/2006/xaml"
_XNAME_KEY = f"{{{_XAML_NS}}}Name"


# -----------------------------------------------------------------------------
#  Wiring map: platform-rule injection and suppression
# -----------------------------------------------------------------------------

# Corpus-observed wirings that are wrong on the platform.
# WhileActivity carries no Counter attribute — that role belongs to ExitWhile.
WIRING_SUPPRESSIONS = {
    # Confirmed wrong: WhileActivity produces no output variables.
    # ExitWhile is the correct source for all row-index wires.
    ("WhileActivity", "GetCellValue",  "RowNumber"),
    ("WhileActivity", "SetCellValue",  "RowNumber"),
    ("WhileActivity", "GetRows",       "RowNumber"),
    ("WhileActivity", "ADListGroup",   "GroupName"),
    ("WhileActivity", "GetColumnName", "ColumnNumber"),
}

# Authoritative platform rules injected at the top of wiring_map.json.
# These override any corpus signal for the same (source, target, field) triple.
WIRING_PLATFORM_RULES = [
    {
        "source_activity": "ExitWhile",
        "target_activity": "GetCellValue",
        "target_field":    "RowNumber",
        "workflow_count":  -1,
        "pct_of_target":   100,
        "authoritative":   True,
        "note":            "* Platform rule: ExitWhile.Counter drives loop row index",
    },
    {
        "source_activity": "ExitWhile",
        "target_activity": "SetCellValue",
        "target_field":    "RowNumber",
        "workflow_count":  -1,
        "pct_of_target":   100,
        "authoritative":   True,
        "note":            "* Platform rule: ExitWhile.Counter drives loop row index",
    },
]


# -----------------------------------------------------------------------------
#  XML parsing
# -----------------------------------------------------------------------------

def _local_name(tag):
    if not tag.startswith("{"):
        return tag
    closing = tag.find("}")
    return tag[closing + 1:] if closing != -1 else tag


def _extract_clr_ns(tag):
    if not tag.startswith("{clr-namespace:"):
        return None
    closing = tag.index("}")
    return (tag[closing + 1:], tag[1:closing])


def _elem_to_dict(elem):
    custom_type = _local_name(elem.tag)
    if custom_type in _SKIP_TAGS or custom_type.startswith("xs:"):
        return None

    xname    = elem.attrib.get(_XNAME_KEY, "")
    activity = {"xName": xname, "CustomTypeName": custom_type}

    for raw_key, val in elem.attrib.items():
        local_key = _local_name(raw_key)
        if local_key in ("Name", "Class") and raw_key.startswith("{"):
            continue
        if local_key == "xName":
            continue
        activity[local_key] = val

    for child in elem:
        child_dict = _elem_to_dict(child)
        if child_dict:
            key = child_dict.get("xName") or child_dict.get("CustomTypeName", "unknown")
            activity[key] = child_dict

    return activity


def _collect_ns_from_root(root):
    ns = {}
    for elem in root.iter():
        result = _extract_clr_ns(elem.tag)
        if result:
            type_name, clr_ns = result
            ns[type_name] = clr_ns
    return ns


def _parse_xoml_root(root):
    ns_map   = _collect_ns_from_root(root)
    raw_data = {}
    for child in root:
        activity = _elem_to_dict(child)
        if activity:
            xname = activity.get("xName", "")
            if xname:
                raw_data[xname] = activity
    return raw_data, ns_map


def parse_xml_file(path):
    """
    Parse a single XML file.
    Returns (raw_data, ns_map, file_hash) or None on failure.
    file_hash is SHA-256 of raw file bytes, used for corpus deduplication.
    """
    try:
        raw_bytes = path.read_bytes()
        text      = raw_bytes.decode("utf-8", errors="replace")
        file_hash = hashlib.sha256(raw_bytes).hexdigest()
        root      = ET.fromstring(text)
    except ET.ParseError as e:
        print(f"  [SKIP] {path.name}: XML parse error -- {e}")
        return None

    tag = _local_name(root.tag)

    if tag == "SequentialWorkflowActivity":
        raw_data, ns_map = _parse_xoml_root(root)
        return raw_data, ns_map, file_hash

    if tag == "TotalExport":
        wf_info = root.find(".//WorkflowInfo")
        if wf_info is None:
            return None
        xoml_raw = wf_info.get("Xoml", "")
        if not xoml_raw:
            return None
        try:
            xoml_root = ET.fromstring(html.unescape(xoml_raw))
        except ET.ParseError as e:
            print(f"  [SKIP] {path.name}: inner Xoml parse error -- {e}")
            return None
        raw_data, ns_map = _parse_xoml_root(xoml_root)
        return raw_data, ns_map, file_hash

    print(f"  [SKIP] {path.name}: unrecognised root tag '{tag}'")
    return None


# -----------------------------------------------------------------------------
#  Sequence extraction
# -----------------------------------------------------------------------------

def extract_sequence(raw_data):
    """Flat ordered list of ALL CustomTypeNames (includes containers)."""
    seq = []
    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        if ct:
            seq.append(ct)
        for key, val in node.items():
            if isinstance(val, dict) and key not in ("xName", "CustomTypeName"):
                walk(val)
    for activity in raw_data.values():
        walk(activity)
    return seq


def extract_leaf_sequence(raw_data):
    """Ordered list of leaf activity types only -- excludes container types."""
    return [ct for ct in extract_sequence(raw_data) if ct not in CONTAINER_TYPES]


def extract_structural_fingerprint(raw_data):
    """
    Structural fingerprint for zone-based pattern matching.

    Returns a dict with:
      containers      - set of MAJOR_CONTAINER types at the top level of the workflow
      body_containers - set of MAJOR_CONTAINER types found INSIDE a top-level loop.
                        Used by the While+IfElse rule: IfElseActivity must appear
                        here to qualify, not just anywhere in the workflow. This
                        fixes the Bug 2 issue where body (a leaf list) could never
                        contain IfElseActivity since collect_body skips CONTAINER_TYPES.
      pre_container   - leaf activities before the first major container
      body            - leaf activities inside any container (flattened)
      post_container  - leaf activities after the last major container
      all_leaves      - all leaf activities across the whole workflow (with repeats)
      distinct_leaves - set of unique leaf activity types. Used as the denominator
                        in the coverage gate (Bug 1 fix: instance count is wrong).
    """
    containers_seen = set()
    body_containers = set()  # Bug 2 fix: container types found inside a loop
    pre, body, post = [], [], []
    in_container    = False

    def collect_body(node):
        """Collect leaves AND nested container types from inside a container."""
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        if ct:
            if ct in MAJOR_CONTAINERS:
                body_containers.add(ct)  # Bug 2 fix: record inner containers
            elif ct not in CONTAINER_TYPES:
                body.append(ct)
        for key, val in node.items():
            if isinstance(val, dict) and key not in ("xName", "CustomTypeName"):
                collect_body(val)

    for xname in raw_data:
        node = raw_data[xname]
        if not isinstance(node, dict):
            continue
        ct = node.get("CustomTypeName", "")
        if ct in MAJOR_CONTAINERS:
            in_container = True
            containers_seen.add(ct)
            collect_body(node)
        elif not in_container:
            if ct and ct not in CONTAINER_TYPES:
                pre.append(ct)
        else:
            if ct and ct not in CONTAINER_TYPES:
                post.append(ct)

    all_leaves = extract_leaf_sequence(raw_data)
    return {
        "containers":      containers_seen,
        "body_containers": body_containers,
        "pre_container":   pre,
        "body":            body,
        "post_container":  post,
        "all_leaves":      all_leaves,
        "distinct_leaves": set(all_leaves),  # Bug 1 fix: precomputed distinct types
    }


# -----------------------------------------------------------------------------
#  Miner 1 -- CLR namespace registry
# -----------------------------------------------------------------------------

def mine_namespaces(all_ns):
    counters = defaultdict(Counter)
    for ns_map in all_ns:
        for type_name, clr_ns in ns_map.items():
            counters[type_name][clr_ns] += 1
    return {t: c.most_common(1)[0][0] for t, c in sorted(counters.items())}


# -----------------------------------------------------------------------------
#  Miner 2 -- Field config defaults
# -----------------------------------------------------------------------------

def mine_field_defaults(all_raw_data):
    counters = defaultdict(lambda: defaultdict(Counter))

    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        if ct and ct not in CONTAINER_TYPES:
            for field in CONFIG_FIELDS:
                val = node.get(field)
                if val is not None:
                    s = str(val).strip()
                    if (s and s not in ("{x:Null}", "", "None", "null")
                            and not s.startswith("%")
                            and not s.startswith("PARAM_")
                            and not s.startswith("PLACEHOLDER_")):
                        counters[ct][field][s] += 1
        for v in node.values():
            if isinstance(v, dict):
                walk(v)

    for raw_data in all_raw_data:
        for activity in raw_data.values():
            walk(activity)

    defaults = {}
    for ct, field_counters in sorted(counters.items()):
        ct_defaults = {}
        for field, counter in sorted(field_counters.items()):
            total = sum(counter.values())
            if total < 3:
                continue
            top_val, top_count = counter.most_common(1)[0]
            if top_count / total >= 0.60:
                ct_defaults[field] = top_val
        if ct_defaults:
            defaults[ct] = ct_defaults
    return defaults


# -----------------------------------------------------------------------------
#  Miner 3 -- Co-occurrence pairs  (per-workflow binary counts)
# -----------------------------------------------------------------------------

def mine_cooccurrence(all_raw_data, window=3):
    """
    Count co-occurrence pairs using per-workflow binary presence.

    Each (activity_A, activity_B) pair is counted at most once per workflow,
    regardless of how many times that pair appears within a single workflow.
    This prevents large looping workflows from dominating the frequency signal
    used by retrieval_tools._load_rank_data() for frequency_tier scoring.
    """
    pair_counts = Counter()
    for raw_data in all_raw_data:
        seq        = extract_leaf_sequence(raw_data)
        seen_in_wf = set()
        for i, act in enumerate(seq):
            for j in range(i + 1, min(i + 1 + window, len(seq))):
                pair = (act, seq[j])
                if pair not in seen_in_wf:
                    pair_counts[pair] += 1
                    seen_in_wf.add(pair)
    return [
        {"activity": a, "next": b, "rank": count}
        for (a, b), count in pair_counts.most_common()
    ]


# -----------------------------------------------------------------------------
#  Miner 4 -- Valid enum values for dropdown / radiobutton fields
# -----------------------------------------------------------------------------

def load_controls_index(controls_path):
    """Return {activity_name: {field_key: input_type}} for enum-type fields only."""
    if not controls_path.exists():
        return {}
    data = json.loads(controls_path.read_text(encoding="utf-8"))
    index = {}
    for entry in data:
        name     = entry.get("activityName", "")
        controls = entry.get("controls", [])
        enum_fields = {
            c["fieldKey"]: c["inputType"]
            for c in controls
            if c.get("inputType") in ENUM_INPUT_TYPES and c.get("fieldKey")
        }
        if enum_fields:
            index[name] = enum_fields
    return index


# Filter out xName instance values from enum fields.
# Activity instance xNames follow the pattern: camelCase prefix + digit suffix,
# e.g. startJsonSession1, getFTPFile3. These are not valid enum choices.
# SPOT-CHECK NOTE: legitimate enum values ending in a digit (e.g. "Level1",
# "Tier2") would also be filtered. Verify enum_values.json on first full run.
_XNAME_INSTANCE_RE = re.compile(r"^[a-z][a-zA-Z0-9]*[A-Za-z]\d+$")

def _is_xname_instance(value):
    """Return True if the value looks like an activity instance xName."""
    return bool(_XNAME_INSTANCE_RE.match(str(value).strip()))


def mine_enum_values(all_raw_data, controls_index, min_value_obs=3):
    """
    For every (activity_type, field_key) marked as dropdown or radiobutton in
    activities_controls.json, collect all distinct non-empty, non-variable
    values seen in the corpus and their frequencies.

    min_value_obs: minimum observations for a single value to be included.
    Values below this threshold are excluded as noise (typos, one-off configs).
    Excluded count is reported per-field for transparency.

    Output format:
    {
      "GetDate": {
        "FuturePast": {
          "input_type": "radiobutton",
          "total_observations": 380,
          "values_excluded_noise": 0,
          "values": [
            {"value": "0",       "count": 312, "pct": 82},
            {"value": "Current", "count": 68,  "pct": 18}
          ]
        }
      }
    }
    """
    counters = defaultdict(lambda: defaultdict(Counter))

    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        if ct and ct in controls_index:
            for field_key in controls_index[ct]:
                val = node.get(field_key)
                if val is not None:
                    s = str(val).strip()
                    if (s
                            and s not in ("{x:Null}", "", "None", "null",
                                          "{x:False}", "{x:True}")
                            and not s.startswith("%")
                            and not s.startswith("PARAM_")
                            and not s.startswith("PLACEHOLDER_")
                            and not _is_xname_instance(s)):
                        counters[ct][field_key][s] += 1
        for v in node.values():
            if isinstance(v, dict):
                walk(v)

    for raw_data in all_raw_data:
        for activity in raw_data.values():
            walk(activity)

    result = {}
    for ct, field_counters in sorted(counters.items()):
        ct_result = {}
        for field_key, counter in sorted(field_counters.items()):
            if not counter:
                continue
            total        = sum(counter.values())
            input_type   = controls_index.get(ct, {}).get(field_key, "unknown")
            clean_values = [(v, c) for v, c in counter.most_common() if c >= min_value_obs]
            noise_count  = sum(1 for _, c in counter.most_common() if c < min_value_obs)
            if not clean_values:
                continue  # entire field is noise -- skip
            ct_result[field_key] = {
                "input_type":            input_type,
                "total_observations":    total,
                "values_excluded_noise": noise_count,
                "values": [
                    {"value": v, "count": c, "pct": round(100 * c / total)}
                    for v, c in clean_values
                ],
            }
        if ct_result:
            result[ct] = ct_result
    return result


# -----------------------------------------------------------------------------
#  Miner 5 -- Activity wiring map  (per-workflow binary counts)
# -----------------------------------------------------------------------------

def mine_wiring_map(all_raw_data):
    """
    Extract how activity outputs wire to activity inputs across the corpus.

    For each workflow, builds an xName -> CustomTypeName index, then walks
    every non-credential string field looking for %xName% variable references.
    When a reference to a known xName is found in field F of activity B:
        source_type  = CustomTypeName of the referenced activity (A)
        target_type  = CustomTypeName of the activity holding the reference (B)
        target_field = field key F

    Counts are PER-WORKFLOW BINARY: each (source, target, field) triple
    contributes at most 1 per workflow. This makes pct_of_target meaningful
    as "fraction of workflows where this wire is used" rather than
    "fraction of loop iterations."

    Filters: workflow_count >= 3 AND pct_of_target >= 30.
    Suppresses WIRING_SUPPRESSIONS. Prepends WIRING_PLATFORM_RULES.
    """
    # wire_wf_sets[(src, tgt, field)] = set of workflow indices containing this wire
    # field_wf_sets[(tgt, field)]     = set of workflow indices where field has any var ref
    wire_wf_sets  = defaultdict(set)
    field_wf_sets = defaultdict(set)

    _var_re = re.compile(r"%([^%]+)%")

    for wf_idx, raw_data in enumerate(all_raw_data):
        xname_to_type = {}

        def index_xnames(node):
            if not isinstance(node, dict):
                return
            xname = node.get("xName", "")
            ct    = node.get("CustomTypeName", "")
            if xname and ct:
                xname_to_type[xname] = ct
            for v in node.values():
                if isinstance(v, dict):
                    index_xnames(v)

        for activity in raw_data.values():
            index_xnames(activity)

        def find_wirings(node):
            if not isinstance(node, dict):
                return
            target_type = node.get("CustomTypeName", "")
            if target_type and target_type not in CONTAINER_TYPES:
                for field_key, val in node.items():
                    if not isinstance(val, str):
                        continue
                    if field_key in CREDENTIAL_FIELDS or field_key in STRIP_FIELDS:
                        continue
                    for ref in _var_re.findall(val):
                        if ref in xname_to_type:
                            source_type = xname_to_type[ref]
                            if source_type != target_type:
                                wire_wf_sets[(source_type, target_type, field_key)].add(wf_idx)
                                field_wf_sets[(target_type, field_key)].add(wf_idx)
            for v in node.values():
                if isinstance(v, dict):
                    find_wirings(v)

        for activity in raw_data.values():
            find_wirings(activity)

    # Build output -- apply suppressions and threshold filter
    wiring = []
    for (src, tgt, field), wf_set in sorted(
        wire_wf_sets.items(), key=lambda x: -len(x[1])
    ):
        if (src, tgt, field) in WIRING_SUPPRESSIONS:
            continue
        wf_count    = len(wf_set)
        field_total = len(field_wf_sets.get((tgt, field), set()))
        pct         = round(100 * wf_count / field_total) if field_total else 0
        if wf_count >= 3 and pct >= 30:
            wiring.append({
                "source_activity": src,
                "target_activity": tgt,
                "target_field":    field,
                "workflow_count":  wf_count,
                "pct_of_target":   pct,
            })

    return WIRING_PLATFORM_RULES + wiring


# -----------------------------------------------------------------------------
#  Miner 6 -- Scaffolds with structural (zone-based) pattern matching
# -----------------------------------------------------------------------------

def _structural_match(fingerprint, pattern, coverage_threshold):
    """
    Zone-based structural matching with coverage gate.

    Bug fixes:
    1. Coverage denominator uses distinct_leaves (unique types) not all_leaves
       (instance list). Loop-heavy workflows have many repeated leaf entries
       which previously pushed coverage far below threshold incorrectly.
    2. While+IfElse checks body_containers for IfElseActivity. The old check
       used body_set (a leaf list) which can never contain IfElseActivity since
       collect_body explicitly skips CONTAINER_TYPES. body_containers is a
       parallel set populated specifically for this check.
    3. Empty frag_leaves returns False immediately -- prevents vacuous True
       match when a pattern's sequence_fragment contains only container types.
    """
    containers      = fingerprint["containers"]
    body_containers = fingerprint["body_containers"]  # Bug 2 fix
    distinct_leaves = fingerprint["distinct_leaves"]  # Bug 1 fix
    available       = (
        set(fingerprint["pre_container"])
        | set(fingerprint["body"])
        | distinct_leaves
    )
    cf          = pattern.get("control_flow", "Linear")
    fragment    = pattern.get("sequence_fragment", [])
    frag_leaves = [a for a in fragment if a not in CONTAINER_TYPES]

    # Bug 3 fix: refuse vacuous match on empty fragment
    if not frag_leaves:
        return False

    # Bug 1 fix: coverage gate on distinct type count, not instance count
    if distinct_leaves:
        matched_count = sum(1 for a in set(frag_leaves) if a in distinct_leaves)
        if (matched_count / len(distinct_leaves)) < coverage_threshold:
            return False

    if cf == "Linear":
        return all(a in distinct_leaves for a in frag_leaves)

    elif cf == "IfElse":
        has_ifelse = "IfElseActivity" in containers or "IfElseActivity" in distinct_leaves
        return has_ifelse and all(a in distinct_leaves for a in frag_leaves)

    elif cf == "While":
        has_while = bool(containers & {"WhileActivity", "ForEachActivity"})
        return has_while and all(a in available for a in frag_leaves)

    elif cf in ("While+IfElse", "while_ifelse"):
        has_while          = bool(containers & {"WhileActivity", "ForEachActivity"})
        has_ifelse_in_body = "IfElseActivity" in body_containers  # Bug 2 fix
        return has_while and has_ifelse_in_body and all(a in available for a in frag_leaves)

    elif cf == "UserGroup":
        has_ug = "UserGroup" in containers or "UserGroup" in distinct_leaves
        return has_ug and all(a in distinct_leaves for a in frag_leaves)

    else:
        return any(a in distinct_leaves for a in frag_leaves)


def _is_variable_ref(value):
    return bool(re.match(r"^%[^%]+%$", str(value).strip()))


def _build_variance_map(matched):
    all_vals = defaultdict(set)
    def walk(node):
        if not isinstance(node, dict):
            return
        ct = node.get("CustomTypeName", "")
        for key, val in node.items():
            if key in ("xName", "CustomTypeName") or isinstance(val, dict) or key in STRIP_FIELDS:
                continue
            if ct:
                all_vals[(ct, key)].add(str(val) if val is not None else "")
        for v in node.values():
            if isinstance(v, dict):
                walk(v)
    for _, raw_data in matched:
        for activity in raw_data.values():
            walk(activity)
    return {
        pair: ("constant" if len(vals) == 1 else "varies")
        for pair, vals in all_vals.items()
    }


def _generalise_node(node, variance):
    result = {}
    ct = node.get("CustomTypeName", "")
    for key, value in node.items():
        if key in STRIP_FIELDS:
            continue
        if key == "xName":
            result["xName"] = f"PARAM_xname_{ct.lower()}"
            continue
        if key == "CustomTypeName":
            result[key] = value
            continue
        if isinstance(value, dict) and value.get("CustomTypeName"):
            result[key] = _generalise_node(value, variance)
            continue
        if isinstance(value, dict):
            result[key] = value
            continue
        s = str(value) if value is not None else ""
        if key in CREDENTIAL_FIELDS:
            result[key] = ""
        elif key in ALWAYS_PARAM_FIELDS:
            if s:
                result[key] = f"PARAM_{key}"
        elif key in STRUCTURAL_FIELDS:
            result[key] = value
        elif _is_variable_ref(s):
            result[key] = f"PARAM_{key}"
        elif variance.get((ct, key), "varies") == "constant":
            result[key] = value
        else:
            result[key] = f"PARAM_{key}"
    return result


def _pick_representative(matched):
    """
    Choose the most representative matched workflow for scaffold generalisation.

    Bug 4 fix: select the workflow closest to the MEDIAN leaf count, not the
    shortest. Shortest risks picking a degenerate stub. The median member is
    the most central case and produces a scaffold covering the typical pattern,
    not an edge case.
    """
    if len(matched) == 1:
        return matched[0][1]
    ranked = sorted(matched, key=lambda x: len(extract_leaf_sequence(x[1])))
    return ranked[len(ranked) // 2][1]


def mine_scaffolds(workflows, patterns, target_ids, min_matches, coverage_threshold):
    """
    Match workflows to patterns using structural fingerprints, then generalise
    the median-representative into a PARAM_-substituted scaffold.
    """
    fingerprints = [
        (fname, rd, extract_structural_fingerprint(rd))
        for fname, rd in workflows
    ]

    matches = defaultdict(list)
    for fname, raw_data, fp in fingerprints:
        for pattern in patterns:
            pid = pattern["pattern_id"]
            if target_ids and pid not in target_ids:
                continue
            if _structural_match(fp, pattern, coverage_threshold):
                matches[pid].append((fname, raw_data))

    results = {}
    for pattern in patterns:
        pid = pattern["pattern_id"]
        if target_ids and pid not in target_ids:
            continue
        matched = matches.get(pid, [])

        if len(matched) < min_matches:
            results[pid] = {
                "pattern_id":        pid,
                "control_flow":      pattern.get("control_flow"),
                "sequence_fragment": pattern.get("sequence_fragment", []),
                "match_count":       len(matched),
                "scaffold":          None,
                "status":            f"insufficient_matches (need {min_matches}, got {len(matched)})",
            }
            continue

        best     = _pick_representative(matched)  # Bug 4 fix: median representative
        variance = _build_variance_map(matched)
        scaffold = {}
        for xname, activity in best.items():
            if not isinstance(activity, dict):
                continue
            generalised = _generalise_node(activity, variance)
            key = generalised.get("xName", xname)
            scaffold[key] = generalised

        results[pid] = {
            "pattern_id":        pid,
            "control_flow":      pattern.get("control_flow"),
            "sequence_fragment": pattern.get("sequence_fragment", []),
            "match_count":       len(matched),
            "scaffold":          scaffold,
            "status":            "ok" if scaffold else "generation_failed",
        }
    return results


# -----------------------------------------------------------------------------
#  Main
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Mine workflow XML corpus -> 6 data files.\n"
            "Run from repo root: python3 mine_corpus.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--xml-dir",            default=str(_DEFAULT_XML),
                        help=f"Raw XML directory (default: {_DEFAULT_XML})")
    parser.add_argument("--pattern-lib",        default=str(_DEFAULT_LIB))
    parser.add_argument("--controls",           default=str(_DEFAULT_CTRL),
                        help="activities_controls.json path for enum mining")
    parser.add_argument("--output-dir",         default=str(_DEFAULT_OUT))
    parser.add_argument("--patterns",           nargs="*", default=None,
                        help="Limit scaffold mining, e.g. --patterns p019 p020 p031")
    parser.add_argument("--min-matches",        type=int,   default=3,
                        help="Min workflows to generate a scaffold (default: 3)")
    parser.add_argument("--coverage-threshold", type=float, default=0.45,
                        help="Pattern match coverage threshold 0.0-1.0 (default: 0.45)")
    parser.add_argument("--window",             type=int,   default=3,
                        help="Co-occurrence sliding window size (default: 3)")
    parser.add_argument("--min-enum-obs",       type=int,   default=3,
                        help="Min per-value observations for enum_values (default: 3)")
    parser.add_argument("--dry-run",            action="store_true",
                        help="Print stats, write nothing")
    args = parser.parse_args()

    xml_dir            = Path(args.xml_dir)
    pattern_path       = Path(args.pattern_lib)
    controls_path      = Path(args.controls)
    output_dir         = Path(args.output_dir)
    target_ids         = set(args.patterns) if args.patterns else None
    coverage_threshold = args.coverage_threshold

    # -- Preflight -----------------------------------------------------------
    if not xml_dir.exists():
        print(f"ERROR: {xml_dir} not found.")
        print( "       mkdir workflows_raw  and put your XML files in there, then re-run.")
        sys.exit(1)
    if not pattern_path.exists():
        print(f"ERROR: pattern library not found at {pattern_path}")
        sys.exit(1)

    # -- Load supporting data ------------------------------------------------
    patterns       = json.loads(pattern_path.read_text(encoding="utf-8"))
    controls_index = load_controls_index(controls_path)

    print(f"Pattern library:      {len(patterns)} patterns"
          + (f"  (targeting {sorted(target_ids)})" if target_ids else ""))
    print(f"Controls index:       {len(controls_index)} activities with enum fields"
          + ("" if controls_path.exists() else "  [NOT FOUND -- enum mining skipped]"))
    print(f"Coverage threshold:   {coverage_threshold}")
    print(f"Min enum value obs:   {args.min_enum_obs}")

    # -- Parse XML files (with SHA-256 deduplication) ------------------------
    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files found in {xml_dir}")
        sys.exit(1)

    print(f"\nParsing {len(xml_files)} files (deduplicating by SHA-256)...",
          end="", flush=True)

    all_raw_data = []
    all_ns       = []
    workflows    = []
    seen_hashes  = set()
    n_failed     = 0
    n_dupes      = 0

    for i, path in enumerate(xml_files):
        result = parse_xml_file(path)
        if result:
            raw_data, ns_map, file_hash = result
            if file_hash in seen_hashes:
                n_dupes += 1
            else:
                seen_hashes.add(file_hash)
                all_raw_data.append(raw_data)
                all_ns.append(ns_map)
                workflows.append((path.name, raw_data))
        else:
            n_failed += 1
        if (i + 1) % 50 == 0:
            print(f" {i+1}", end="", flush=True)

    print(
        f"\n  Files total: {len(xml_files)}"
        f"  |  Unique: {len(workflows)}"
        f"  |  Duplicates skipped: {n_dupes}"
        f"  |  Failed/unparseable: {n_failed}"
    )

    # =========================================================================
    #  Run all six miners
    # =========================================================================

    print("\n[1/6] CLR namespaces...", end=" ", flush=True)
    ns_registry = mine_namespaces(all_ns)
    print(f"{len(ns_registry)} activity types")

    print("[2/6] Field defaults...", end=" ", flush=True)
    field_defaults = mine_field_defaults(all_raw_data)
    total_df = sum(len(v) for v in field_defaults.values())
    print(f"{len(field_defaults)} activity types  |  {total_df} fields")
    for ct, fields in sorted(field_defaults.items()):
        print(f"      {ct}: {fields}")

    print(f"[3/6] Co-occurrence pairs (window={args.window}, per-workflow binary)...",
          end=" ", flush=True)
    cooccurrence = mine_cooccurrence(all_raw_data, window=args.window)
    filtered_co  = [p for p in cooccurrence if p["rank"] >= 2]
    print(f"{len(cooccurrence)} total  |  {len(filtered_co)} with rank >= 2")
    print("      Top 10:")
    for pair in cooccurrence[:10]:
        print(f"        {pair['activity']:35s} -> {pair['next']:35s}  ({pair['rank']} wf)")

    print("[4/6] Enum values (dropdowns/radiobuttons)...", end=" ", flush=True)
    enum_values    = mine_enum_values(
        all_raw_data, controls_index, min_value_obs=args.min_enum_obs
    )
    total_ef       = sum(len(v) for v in enum_values.values())
    total_ev       = sum(len(f["values"]) for v in enum_values.values() for f in v.values())
    total_excluded = sum(
        f.get("values_excluded_noise", 0)
        for v in enum_values.values() for f in v.values()
    )
    print(
        f"{len(enum_values)} activity types  |  {total_ef} fields  "
        f"|  {total_ev} values kept  |  {total_excluded} noise values excluded"
    )
    all_ef = [
        (ct, fk, fd)
        for ct, fields in enum_values.items()
        for fk, fd in fields.items()
    ]
    all_ef.sort(key=lambda x: -x[2]["total_observations"])
    print("      Top 10 most-observed enum fields:")
    for ct, fk, fd in all_ef[:10]:
        vals_str = ", ".join(
            f'"{v["value"]}" ({v["pct"]}%)'
            for v in fd["values"][:4]
        )
        print(f"        {ct}.{fk} [{fd['input_type']}]: {vals_str}")

    print("[5/6] Wiring map (per-workflow binary counts)...", end=" ", flush=True)
    wiring_map     = mine_wiring_map(all_raw_data)
    corpus_wiring  = [w for w in wiring_map if not w.get("authoritative")]
    platform_rules = [w for w in wiring_map if w.get("authoritative")]
    print(
        f"{len(corpus_wiring)} corpus wirings  |  "
        f"{len(platform_rules)} platform rules injected"
    )
    print("      Top 15 (* = authoritative platform rule):")
    for w in wiring_map[:15]:
        star  = "* " if w.get("authoritative") else "  "
        count = w.get("workflow_count", -1)
        print(
            f"      {star}{w['source_activity']:28s} -> {w['target_activity']:28s}"
            f".{w['target_field']:22s}  ({count:4d} wf, {w['pct_of_target']}%)"
        )

    print(
        f"[6/6] Scaffolds "
        f"(coverage>={coverage_threshold}, min_matches={args.min_matches}, "
        f"representative=median)...",
        end=" ", flush=True,
    )
    scaffold_results = mine_scaffolds(
        workflows, patterns, target_ids, args.min_matches, coverage_threshold
    )
    ok       = sum(1 for r in scaffold_results.values() if r["status"] == "ok")
    skipped  = sum(1 for r in scaffold_results.values() if "insufficient" in r.get("status", ""))
    failed_s = sum(1 for r in scaffold_results.values() if r.get("status") == "generation_failed")
    print(f"{ok} generated  |  {skipped} skipped  |  {failed_s} failed")
    for pid, r in sorted(scaffold_results.items()):
        cf    = r.get("control_flow", "")
        count = r["match_count"]
        if r["status"] == "ok":
            types = [
                v.get("CustomTypeName", "?")
                for v in (r["scaffold"] or {}).values()
                if isinstance(v, dict)
            ]
            print(f"      v {pid} [{cf}]  {count} matches  ->  {types}")
        else:
            print(f"      - {pid} [{cf}]  {r['status']}")

    # -- Write files ---------------------------------------------------------
    if args.dry_run:
        print("\nDry run -- nothing written.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "patterns").mkdir(parents=True, exist_ok=True)

    (output_dir / "namespace_registry.json").write_text(
        json.dumps(ns_registry, indent=2), encoding="utf-8")
    (output_dir / "field_defaults.json").write_text(
        json.dumps(field_defaults, indent=2), encoding="utf-8")
    (output_dir / "activity_ranks.json").write_text(
        json.dumps(filtered_co, indent=2), encoding="utf-8")
    (output_dir / "enum_values.json").write_text(
        json.dumps(enum_values, indent=2), encoding="utf-8")
    (output_dir / "wiring_map.json").write_text(
        json.dumps(wiring_map, indent=2), encoding="utf-8")
    (output_dir / "patterns" / "scaffolds.json").write_text(
        json.dumps({
            "generated_from":         str(xml_dir),
            "total_workflows_parsed": len(workflows),
            "duplicates_skipped":     n_dupes,
            "matching":               "structural (zone-based)",
            "coverage_threshold":     coverage_threshold,
            "scaffolds":              scaffold_results,
        }, indent=2), encoding="utf-8")

    total_noise = sum(
        f.get("values_excluded_noise", 0)
        for v in enum_values.values() for f in v.values()
    )
    print(f"""
Written to {output_dir}/

  namespace_registry.json   ({len(ns_registry)} entries)
    -> Add to NAMESPACE_REGISTRY in serializer/xml_composer.py
    -> Remove confirmed types from UNCONFIRMED_NAMESPACE_ACTIVITIES in tools/annotation_tools.py

  field_defaults.json   ({total_df} fields across {len(field_defaults)} types)
    -> Config enum defaults for tools/build_tools.py

  activity_ranks.json   ({len(filtered_co)} pairs, per-workflow binary counts)
    -> Replaces data/activity_ranks.json directly
    -> Each workflow contributes at most 1 to any pair -- frequency_tier signal is clean

  enum_values.json   ({total_ef} fields, {total_ev} values kept, {total_noise} noise excluded)
    -> Load in tools/build_tools.py: replace "_value" placeholders in templates
    -> Pass valid values to StructureBuilder instruction per field
    -> SPOT-CHECK: verify no legitimate enum values ending in a digit were filtered

  wiring_map.json   ({len(corpus_wiring)} corpus wirings + {len(platform_rules)} platform rules)
    -> workflow_count = number of WORKFLOWS containing this wire (not loop iterations)
    -> pct_of_target  = fraction of workflows where target field has any var ref
    -> Entries with authoritative=true are platform rules, not corpus-derived

  patterns/scaffolds.json   ({ok} scaffolds, coverage>={coverage_threshold}, representative=median)
    -> Paste scaffold values into pattern_library.json:
         pattern["scaffold"] = scaffolds["scaffolds"]["p019"]["scaffold"]
""")


if __name__ == "__main__":
    main()