"""
tools/retrieval_tools.py

Activity retrieval for the workflow generation pipeline.

Four layers applied in order, each independent and composable:

  Layer 1 — Intent map (deterministic)
      Maps Decomposer intent enum values directly to platform activities.
      Handles 12 of 14 intent values with 100% accuracy before any keyword
      work is done. Only intent="other" falls through to keyword matching.
      Zero error for mapped intents; eliminates vocabulary gap entirely for
      the most common step types.

  Layer 2 — IDF-weighted keyword scoring (replaces raw hit count)
      Replaces the flat keyword_hits count with TF-IDF-style scoring.
      Words that appear in few activity descriptions (high IDF) count more
      than words that appear everywhere (low IDF). Fixes score dilution:
      "ping" (IDF=5.8, df=2) beats "server" + "check" (both IDF=0, absent
      from any description). IDF weights computed once at load time from
      activity_list.txt.

  Layer 3 — Alias matching (vocabulary gap)
      A curated data file (data/activity_aliases.json) maps plain-language
      phrases to activity names. Checked as substring presence in the
      lowercased query. A matching alias adds ALIAS_BONUS to that activity's
      score. Handles cases where platform description vocabulary is unlike
      user language ("Allocates" vs "store", "Runs a Ping command" vs
      "check connectivity").

  Layer 4 — Exact name match boost
      If all significant (>=3-char) camelCase-split words of an activity name
      appear in the query, the score is multiplied by NAME_BOOST. Differentiates
      "Ping" (all name words match) from "PingLatency" (name word "latency"
      absent) when both contain "ping".

Post-scoring modulations (applied after Layers 1-4):

  Co-occurrence re-rank
      Boosts candidates that frequently appear with already-confirmed
      activities in the corpus.

  System bias (NEW)
      When the step's text identifies a target external system (via
      extract_system_from_step), candidates whose module_type matches
      that system get SYSTEM_MATCH_BOOST applied to combined_score, and
      candidates whose module_type is INTERNAL get INTERNAL_PENALTY.
      Reads module_type from the merged activity_frequency.json. No-op
      when the step does not identify a system — most steps won't.

Confidence gate:
      If the top candidate's combined_score is below CONFIDENCE_THRESHOLD
      after co-occurrence re-ranking and system bias, the step is marked
      UNCERTAIN with top-3 candidates passed through for StructureBuilder
      to judge.
"""

import csv
import json
import math
import os
import re
from collections import defaultdict
from typing import Annotated

from tools.system_extraction import extract_system_from_step

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_valid_activities:      set[str]  = set()
_activity_descriptions: dict[str, str] = {}

# Layer 2: IDF weights, computed once at load time
_idf:              dict[str, float]      = {}
_activity_tokens:  dict[str, set[str]]   = {}  # activity -> tokens in name+desc

# Layer 3: alias map loaded lazily
_alias_map:    dict[str, list[str]] | None = None  # activity -> [phrase, ...]
_alias_lookup: dict | None = None  # frozenset(words) -> activity

# Co-occurrence / frequency rank data
_activity_ranks: list | None          = None
_rank_lookup:    dict[str, int] | None = None
_rank_scores:    dict[str, int] | None = None

# System bias: activity name -> module_type from merged activity_frequency.json
_module_types: dict[str, str | None] | None = None

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

ALIAS_BONUS:          float = 6.0   # additive; intentionally large vs IDF scale
NAME_BOOST:           float = 2.5   # multiplier when all name words in query
CONFIDENCE_THRESHOLD: float = 0.35  # below this -> UNCERTAIN
_COOCCURRENCE_LAMBDA: float = 0.3

# System bias multipliers — applied to combined_score in retrieve_all_steps
# after co-occurrence re-rank, only when the step identifies a target system.
SYSTEM_MATCH_BOOST:   float = 1.5   # candidate.module_type == step.system
INTERNAL_PENALTY:     float = 0.7   # step has system, candidate is INTERNAL


# ---------------------------------------------------------------------------
# Layer 1 — Intent map
# ---------------------------------------------------------------------------

INTENT_TO_ACTIVITY: dict[str, str] = {
    "get_date":            "GetDate",
    "format_date":         "GetDate",
    "count_rows":          "GetRowsCount",
    "get_cell":            "GetCellValue",
    "set_variable":        "MemorySet",
    "display":             "DisplayValue",
    "send_email":          "SendEmail",
    "initialize_variable": "MemorySet",
    "exit_loop":           "ExitWhile",
    "date_difference":     "DateDifference",
    "create_table":        "CreateMemoryTable",
    "query_servicenow":    "SNGetRecord",
    "loop":                "WhileActivity",
    # "branch"  -> CONTROL_FLOW via control_flow="ifelse"
    # "other"   -> keyword matching
}


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Lowercase tokens >= 3 chars from text."""
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _split_camel(name: str) -> list[str]:
    """Split camelCase name into lowercase words >= 3 chars."""
    return [w.lower() for w in re.sub(r"([A-Z])", r" \1", name).split()
            if len(w) >= 3]


def load_activity_list() -> None:
    """
    Loads activity_list.txt and computes IDF weights (Layer 2).
    Safe to call multiple times — no-op if already loaded.
    """
    global _valid_activities, _activity_descriptions, _idf, _activity_tokens
    if _valid_activities:
        return

    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_list.txt")

    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            name = row["name"].strip()
            desc = row.get("description", "").strip()
            _valid_activities.add(name)
            _activity_descriptions[name] = desc
            rows.append({"name": name, "desc": desc})

    N = len(rows)
    df: dict[str, int] = defaultdict(int)
    for row in rows:
        tokens = _tokenize(f"{row['name']} {row['desc']}")
        _activity_tokens[row["name"]] = tokens
        for t in tokens:
            df[t] += 1

    _idf = {t: math.log(N / d) for t, d in df.items()}
    print(f"[retrieval] Loaded {len(_valid_activities)} activities, "
          f"{len(_idf)} IDF tokens.")


def _load_alias_map() -> tuple[dict, dict]:
    """Loads data/activity_aliases.json lazily."""
    global _alias_map, _alias_lookup
    if _alias_map is not None:
        return _alias_map, _alias_lookup

    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_aliases.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        _alias_map = {k: v for k, v in raw.items() if not k.startswith("_")}
    except FileNotFoundError:
        print(f"[retrieval] Warning: activity_aliases.json not found — Layer 3 disabled.")
        _alias_map = {}

    # Pre-tokenize each phrase using the same 3+ char filter as _tokenize,
    # so the subset check query_word_set >= phrase_words is consistent.
    _alias_lookup = {}
    for activity, phrases in _alias_map.items():
        for phrase in phrases:
            words = frozenset(re.findall(r"[a-z]{3,}", phrase.lower()))
            if words and words not in _alias_lookup:
                _alias_lookup[words] = activity

    print(f"[retrieval] Loaded {len(_alias_map)} alias entries, "
          f"{len(_alias_lookup)} phrase word-sets.")
    return _alias_map, _alias_lookup


def _load_rank_data() -> tuple[dict, dict]:
    """Loads activity_ranks.json and builds rank lookup dicts."""
    global _activity_ranks, _rank_lookup, _rank_scores
    if _rank_lookup is not None:
        return _rank_lookup, _rank_scores

    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_ranks.json")
    try:
        with open(path, encoding="utf-8") as f:
            _activity_ranks = json.load(f)
    except Exception:
        _activity_ranks = []

    freq: dict[str, int] = {}
    for pair in _activity_ranks:
        a1 = pair.get("activity", "")
        a2 = pair.get("next", "")
        f  = pair.get("rank", 0)
        if a1:
            freq[a1] = freq.get(a1, 0) + f
        if a2:
            freq[a2] = freq.get(a2, 0) + f

    sorted_acts = sorted(freq.items(), key=lambda x: -x[1])
    _rank_lookup = {name: idx for idx, (name, _) in enumerate(sorted_acts)}
    _rank_scores = {name: score for name, score in sorted_acts}

    print(f"[retrieval] Loaded rank data for {len(_rank_lookup)} activities.")
    return _rank_lookup, _rank_scores


def _load_module_types() -> dict[str, str | None]:
    """
    Loads the activity name -> module_type lookup from the merged
    activity_frequency.json. Safe to call multiple times — no-op if
    already loaded. Returns an empty dict if the file is missing or
    if no entries carry module_type (i.e. merge has not been run yet).
    """
    global _module_types
    if _module_types is not None:
        return _module_types

    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_frequency.json")

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("individual_activities", [])
        _module_types = {
            entry["activity"]: entry.get("module_type")
            for entry in entries
            if entry.get("activity")
        }
    except Exception as e:
        print(f"[retrieval] Warning: could not load module_types from {path}: {e}")
        _module_types = {}

    enriched = sum(1 for v in _module_types.values() if v)
    print(f"[retrieval] Loaded module_type for {enriched} of {len(_module_types)} activities.")
    if enriched == 0 and _module_types:
        print(f"[retrieval] Warning: no module_type values present — "
              f"run scripts/merge_activity_info.py to enrich the catalog.")
    return _module_types


# ---------------------------------------------------------------------------
# Per-activity scoring (Layers 2, 3, 4)
# ---------------------------------------------------------------------------

def _score_candidates(query: str, alias_map: dict) -> list[dict]:
    """
    Score all activities against the query using Layers 2, 3, and 4.
    Returns top-10 sorted by raw_score descending.
    """
    query_lower  = query.lower()
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    # Max possible IDF normaliser: sum of IDF for all query tokens
    max_possible = sum(_idf.get(t, 0.0) for t in query_tokens) or 1.0

    candidates = []
    for name, desc in _activity_descriptions.items():

        # Layer 2: IDF-weighted score, normalised to [0, 1]
        act_tokens = _activity_tokens.get(name, set())
        idf_raw    = sum(_idf.get(t, 0.0) for t in query_tokens if t in act_tokens)
        idf_norm   = idf_raw / max_possible

        # Layer 3: alias bonus — word-set containment (order-independent)
        # Each alias phrase is pre-tokenized to a frozenset; it matches if all
        # its words appear anywhere in the query (regardless of order/position).
        alias = 0.0
        query_word_set = _tokenize(query)  # reuse IDF tokenizer (>=3-char words)
        for phrase_words, act_name in (_alias_lookup or {}).items():
            if act_name == name and phrase_words <= query_word_set:
                alias = ALIAS_BONUS
                break

        # Layer 4: name match boost (multiplicative on idf_norm)
        name_words = _split_camel(name)
        boosted    = bool(name_words and all(w in query_lower for w in name_words))
        boost      = NAME_BOOST if boosted else 1.0

        raw = idf_norm * boost + alias
        if raw > 0:
            candidates.append({
                "activity_name": name,
                "description":   desc,
                "keyword_hits":  raw,       # kept for backward compat
                "idf_norm":      round(idf_norm, 4),
                "alias_match":   alias > 0,
                "name_boosted":  boosted,
            })

    return sorted(candidates, key=lambda x: x["keyword_hits"], reverse=True)[:10]


# ---------------------------------------------------------------------------
# Co-occurrence re-ranking
# ---------------------------------------------------------------------------

def _frequency_tier(activity_name: str, rank_scores: dict) -> str:
    score = rank_scores.get(activity_name, 0)
    if score == 0:
        return "low"
    max_score = max(rank_scores.values()) if rank_scores else 1
    ratio = score / max_score
    if ratio >= 0.4:
        return "high"
    if ratio >= 0.1:
        return "medium"
    return "low"


def _rerank_candidates(
    candidates:           list[dict],
    confirmed_activities: set[str],
    rank_scores:          dict,
) -> list[dict]:
    """Re-ranks using co-occurrence with already-confirmed activities."""
    if not candidates:
        return candidates

    max_hits  = max(c["keyword_hits"] for c in candidates) or 1
    max_score = max(rank_scores.values()) if rank_scores else 1

    def cooc(name: str) -> float:
        if not _activity_ranks or not confirmed_activities:
            return rank_scores.get(name, 0) / max_score
        total = sum(
            pair.get("rank", 0)
            for pair in _activity_ranks
            if (pair.get("activity") == name and pair.get("next") in confirmed_activities)
            or (pair.get("next") == name and pair.get("activity") in confirmed_activities)
        )
        return (total / max_score) if total else (rank_scores.get(name, 0) / max_score)

    for c in candidates:
        semantic = c["keyword_hits"] / max_hits
        c["combined_score"] = round(
            semantic + _COOCCURRENCE_LAMBDA * cooc(c["activity_name"]), 4
        )

    return sorted(candidates, key=lambda x: x["combined_score"], reverse=True)


# ---------------------------------------------------------------------------
# System bias
# ---------------------------------------------------------------------------

def _apply_system_bias(
    candidates:    list[dict],
    step_system:   str | None,
    module_types:  dict[str, str | None],
) -> list[dict]:
    """
    Applies multiplicative system bias to combined_score and re-sorts.

    No-op when step_system is None (most steps don't target a specific
    external system). Tags each candidate with system_match (True when
    candidate's module_type matches step_system, False when it's INTERNAL
    while step_system is set, None otherwise) for debugging visibility.
    """
    if not candidates or not step_system:
        for c in candidates:
            c.setdefault("system_match", None)
        return candidates

    for c in candidates:
        cand_module = module_types.get(c["activity_name"])
        if cand_module == step_system:
            c["combined_score"] = round(c["combined_score"] * SYSTEM_MATCH_BOOST, 4)
            c["system_match"]   = True
        elif cand_module == "INTERNAL":
            c["combined_score"] = round(c["combined_score"] * INTERNAL_PENALTY, 4)
            c["system_match"]   = False
        else:
            # Unknown module_type, or matches a different external system —
            # leave the score alone, mark as neither boost nor penalty
            c["system_match"] = None

    return sorted(candidates, key=lambda x: x["combined_score"], reverse=True)


# ---------------------------------------------------------------------------
# Public tool: retrieve_activities
# ---------------------------------------------------------------------------

def retrieve_activities(
    query: Annotated[str, "Natural language description of what this step needs to do"],
) -> list[dict]:
    """
    Returns activity candidates for a query using Layers 2, 3, and 4.
    Layer 1 (intent map) is applied in retrieve_all_steps which has the
    step's intent field; this function is the keyword-matching path only.
    """
    load_activity_list()
    alias_map, _ = _load_alias_map()
    return _score_candidates(query, alias_map)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_activity(
    activity_name: Annotated[str, "Activity name to validate against the confirmed list"],
) -> dict:
    """Hard gate — confirms the activity name exists in activity_list.txt."""
    load_activity_list()
    if activity_name in _valid_activities:
        return {"valid": True, "activity_name": activity_name}
    close = [n for n in _valid_activities if activity_name.lower() in n.lower()][:3]
    return {
        "valid":         False,
        "activity_name": activity_name,
        "reason":        f"'{activity_name}' not found in confirmed activity list.",
        "suggestions":   close,
    }


def get_activity_description(
    activity_name: Annotated[str, "Activity name to look up"],
) -> str:
    """Returns the description for a known activity, or empty string if not found."""
    load_activity_list()
    return _activity_descriptions.get(activity_name, "")


# ---------------------------------------------------------------------------
# Public entry point: retrieve_all_steps
# ---------------------------------------------------------------------------

def retrieve_all_steps(
    steps: Annotated[list, "Full list of step dicts from decomposition"],
) -> list[dict]:
    """
    Retrieves and validates activity candidates for ALL steps in one call.

    Resolution order per step:
      0. Control-flow scaffold  (intent=branch, control_flow=ifelse/parallel)
      1. Intent map             Layer 1 — deterministic, no keyword work
      2. Scored keyword match   Layers 2-4 — IDF + alias + name boost
      3. Co-occurrence re-rank  boosts platform-common activities
      4. System bias            boost module-matched candidates, penalize
                                INTERNAL when step targets external system
      5. Confidence gate        below CONFIDENCE_THRESHOLD -> UNCERTAIN

    status values:
      CONTROL_FLOW  — structural scaffold, no activity retrieved
      INTENT_MATCH  — resolved via intent map, high confidence
      MATCHED       — keyword pipeline, confidence >= threshold
      UNCERTAIN     — keyword pipeline, confidence < threshold; top-3 included
      UNAVAILABLE   — no candidates found
    """
    load_activity_list()
    alias_map, _   = _load_alias_map()
    _, rank_scores = _load_rank_data()
    module_types   = _load_module_types()

    CONTROL_FLOW_INTENTS = {"branch", "parallel"}
    CONTROL_FLOW_CF      = {"ifelse", "parallel"}

    manifest: list[dict] = []
    confirmed_activities: set[str] = set()

    for step in steps:
        step_id      = step.get("step_id", "")
        description  = step.get("description", "")
        intent       = step.get("intent", "")
        control_flow = step.get("control_flow", "linear")

        # ── Control-flow scaffold ────────────────────────────────────────────
        if control_flow in CONTROL_FLOW_CF or intent in CONTROL_FLOW_INTENTS:
            manifest.append({
                "step_id":           step_id,
                "query":             description,
                "candidates":        [],
                "selected_activity": "IfElseActivity",
                "status":            "CONTROL_FLOW",
                "frequency_tier":    "high",
            })
            confirmed_activities.add("IfElseActivity")
            continue

        # ── Layer 1: intent map ──────────────────────────────────────────────
        if intent in INTENT_TO_ACTIVITY:
            mapped = INTENT_TO_ACTIVITY[intent]
            if mapped in _valid_activities:
                tier = _frequency_tier(mapped, rank_scores)
                manifest.append({
                    "step_id":           step_id,
                    "query":             description,
                    "candidates":        [{"activity_name": mapped,
                                          "combined_score": 1.0,
                                          "resolution":     "intent_map"}],
                    "selected_activity": mapped,
                    "status":            "INTENT_MATCH",
                    "frequency_tier":    tier,
                    "intent_used":       intent,
                })
                confirmed_activities.add(mapped)
                continue

        # ── Layers 2-4: scored keyword matching ──────────────────────────────
        raw_candidates = _score_candidates(description, alias_map)

        if not raw_candidates:
            manifest.append({
                "step_id":           step_id,
                "query":             description,
                "candidates":        [],
                "selected_activity": None,
                "status":            "UNAVAILABLE",
                "frequency_tier":    "low",
            })
            continue

        # Co-occurrence re-rank, then system bias
        reranked    = _rerank_candidates(raw_candidates, confirmed_activities, rank_scores)
        step_system = extract_system_from_step(step)
        reranked    = _apply_system_bias(reranked, step_system, module_types)

        top      = reranked[0]
        top_name = top["activity_name"]

        if not validate_activity(top_name)["valid"]:
            manifest.append({
                "step_id":           step_id,
                "query":             description,
                "candidates":        reranked[:3],
                "selected_activity": None,
                "status":            "UNAVAILABLE",
                "frequency_tier":    "low",
            })
            continue

        confidence = top.get("combined_score", 0.0)
        tier       = _frequency_tier(top_name, rank_scores)

        candidates_out = [
            {
                "activity_name":  c["activity_name"],
                "combined_score": c.get("combined_score", 0.0),
                "alias_match":    c.get("alias_match", False),
                "name_boosted":   c.get("name_boosted", False),
                "system_match":   c.get("system_match"),
            }
            for c in reranked[:3]
        ]

        entry = {
            "step_id":           step_id,
            "query":             description,
            "candidates":        candidates_out,
            "selected_activity": top_name,
            "frequency_tier":    tier,
            "confidence":        round(confidence, 4),
        }
        if step_system:
            entry["step_system"] = step_system

        if confidence < CONFIDENCE_THRESHOLD:
            # Best guess included but flagged — don't add to confirmed_activities
            entry["status"] = "UNCERTAIN"
            manifest.append(entry)
        else:
            entry["status"] = "MATCHED"
            manifest.append(entry)
            confirmed_activities.add(top_name)

    return manifest
