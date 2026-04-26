"""
tools/telemetry.py

Phase 0 telemetry foundation. Provides:

  * log_event(event_type, payload)        — low-level event logger
  * log_error(stage, error_type, ...)     — error logger with optional state dump
  * log_outcome(tracking_token, worked)   — post-import outcome logger
  * dump_state(session_id, state)         — write a state snapshot, return filename
  * 24 typed factory functions            — one per event type from spec §13.2
  * SessionSweeper                        — background thread for abandon detection

DESIGN
  - Synchronous writes with swallowed errors. Telemetry never breaks user flow.
    A failed disk write is logged to stderr once and the call returns normally.
  - Daily JSONL files in logs/events/, logs/errors/, logs/outcomes/.
    State dumps are per-incident files in logs/state_dumps/.
  - JSON Schema validation runs at log time. Violations are logged to
    logs/errors/schema_violations.jsonl AND the original event still writes
    to its events file — we never lose data, even if the shape is off.
  - Session sweeper is an in-process daemon thread. Caller starts it explicitly.
    Adequate for single-instance internal beta. Multi-worker deployments will
    need to revisit (one sweeper per worker would emit duplicate abandon events).

ENV
  LOG_DIR    — root for log files. Defaults to "logs".
  DATA_DIR   — root for data files. Defaults to "/app/data". Schema lives at
               $DATA_DIR/telemetry_event_schema.json.

USAGE
  from tools import telemetry

  telemetry.log_session_start(session_id, prompt, user_agent="...")
  telemetry.log_decomposer_call(session_id, duration_ms=1234, mode="slot_filling")

  # Background abandon detection
  sweeper = telemetry.SessionSweeper(get_sessions=lambda: SESSIONS)
  sweeper.start()
  # ...
  sweeper.stop()

SMOKE TEST
  python -m tools.telemetry
  # Emits one of each event type, prints output paths.
"""

import json
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_ROOT    = Path(os.getenv("LOG_DIR",  "logs"))
DATA_ROOT   = Path(os.getenv("DATA_DIR", "/app/data"))
SCHEMA_PATH = DATA_ROOT / "telemetry_event_schema.json"

EVENTS_DIR       = LOG_ROOT / "events"
ERRORS_DIR       = LOG_ROOT / "errors"
OUTCOMES_DIR     = LOG_ROOT / "outcomes"
STATE_DUMPS_DIR  = LOG_ROOT / "state_dumps"
VIOLATIONS_FILE  = ERRORS_DIR / "schema_violations.jsonl"

SESSION_TIMEOUT_SEC        = 4  * 60 * 60   # 4 hours
SESSION_SWEEP_INTERVAL_SEC = 15 * 60        # 15 minutes


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_schema_cache: dict | None = None
_schema_load_attempted: bool = False
_init_lock = threading.Lock()
_dirs_ready = False
_stderr_warned: set = set()  # deduplicate stderr warnings


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    """Idempotent. Creates log directories on first write call."""
    global _dirs_ready
    if _dirs_ready:
        return
    with _init_lock:
        if _dirs_ready:
            return
        for d in (EVENTS_DIR, ERRORS_DIR, OUTCOMES_DIR, STATE_DUMPS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        _dirs_ready = True


def _load_schema() -> dict:
    """
    Loads the event schema once. On failure, returns empty dict and disables
    validation (events still log). Logs warning to stderr exactly once.
    """
    global _schema_cache, _schema_load_attempted
    if _schema_load_attempted:
        return _schema_cache or {}
    with _init_lock:
        if _schema_load_attempted:
            return _schema_cache or {}
        _schema_load_attempted = True
        try:
            with open(SCHEMA_PATH, encoding="utf-8") as f:
                doc = json.load(f)
            _schema_cache = doc.get("events", {})
        except Exception as e:
            _warn_once("schema_load",
                       f"[telemetry] Schema file not loadable at {SCHEMA_PATH}: {e}. "
                       "Validation disabled — all events will write unchecked.")
            _schema_cache = {}
        return _schema_cache


def _warn_once(key: str, message: str) -> None:
    if key in _stderr_warned:
        return
    _stderr_warned.add(key)
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Mini JSON Schema validator (stdlib-only)
# ---------------------------------------------------------------------------
#
# Handles the subset used by telemetry_event_schema.json:
#   - type (single string or list of strings)
#   - required (list of field names)
#   - properties (per-field schemas, recursive)
#   - const (exact value match)
#   - enum (list of allowed values)
#
# Unknown fields in payload are tolerated; only declared properties are checked
# against their sub-schemas. Missing required fields and type mismatches fail.

_PY_TYPES_FOR_JSON_TYPE: dict = {
    "string":  (str,),
    "integer": (int,),
    "number":  (int, float),  # JSON Schema: integer is a subtype of number
    "boolean": (bool,),
    "array":   (list,),
    "object":  (dict,),
    "null":    (type(None),),
}


def _types_match(value: Any, json_type: str | list) -> bool:
    if isinstance(json_type, list):
        return any(_types_match(value, t) for t in json_type)
    expected = _PY_TYPES_FOR_JSON_TYPE.get(json_type)
    if expected is None:
        return True  # unknown type keyword — don't fail
    # bool is a subtype of int in Python; reject when json_type is "integer" or "number"
    if json_type in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _validate_against_schema(value: Any, schema: dict) -> str | None:
    """Returns None on pass, or a human-readable error string."""
    if "const" in schema and value != schema["const"]:
        return f"expected const {schema['const']!r}, got {value!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"expected one of {schema['enum']!r}, got {value!r}"
    if "type" in schema and not _types_match(value, schema["type"]):
        return f"expected type {schema['type']!r}, got {type(value).__name__}"
    if isinstance(value, dict) and "properties" in schema:
        for field, sub_schema in schema["properties"].items():
            if field in value:
                err = _validate_against_schema(value[field], sub_schema)
                if err:
                    return f"{field}: {err}"
        for required in schema.get("required", []):
            if required not in value:
                return f"missing required field {required!r}"
    return None


def _validate_event(event_type: str, payload: dict) -> str | None:
    """Returns None on pass, or a human-readable error string."""
    schema_map = _load_schema()
    if not schema_map:
        return None  # validation disabled
    event_schema = schema_map.get(event_type)
    if event_schema is None:
        return f"unknown event_type {event_type!r}"
    return _validate_against_schema(payload, event_schema)


# ---------------------------------------------------------------------------
# Low-level append
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a single record as a JSON line. Swallows all errors with a
    one-time stderr warning per failure category."""
    try:
        _ensure_dirs()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        _warn_once(f"append_fail:{path}",
                   f"[telemetry] Append to {path} failed: {e}. "
                   "Subsequent failures to the same path are silent.")


# ---------------------------------------------------------------------------
# Public — low-level
# ---------------------------------------------------------------------------

def log_event(event_type: str, payload: dict) -> None:
    """
    Low-level event logger. Used by typed factories below; can also be called
    directly when adding ad-hoc instrumentation that isn't worth a factory.

    Adds _event_type and _timestamp automatically. Validates against schema;
    on violation, writes to schema_violations.jsonl AND continues to write the
    event to the events log so data is never lost.
    """
    record = {
        "_event_type": event_type,
        "_timestamp":  _now_iso(),
        **payload,
    }
    err = _validate_event(event_type, record)
    if err:
        _append_jsonl(VIOLATIONS_FILE, {
            "_event_type":   "schema_violation",
            "_timestamp":    _now_iso(),
            "violated_type": event_type,
            "error":         err,
            "payload":       record,
        })
    _append_jsonl(EVENTS_DIR / _today_filename(), record)


def log_error(stage:         str,
              error_type:    str,
              error_message: str,
              session_id:    str | None    = None,
              state:         dict | None   = None,
              exception:     BaseException | None = None) -> None:
    """
    Logs a pipeline error. Always emits a generation_failed event AND writes a
    detailed error record (with stack trace if exception given) to the errors
    log. Optionally dumps state to logs/state_dumps/ and references the dump
    filename in the event.
    """
    state_dump_filename = None
    if state is not None and session_id is not None:
        state_dump_filename = dump_state(session_id, state)

    # Detailed record — full stack, full state ref
    error_record = {
        "_event_type":         "error",
        "_timestamp":          _now_iso(),
        "session_id":          session_id,
        "stage":               stage,
        "error_type":          error_type,
        "error_message":       error_message,
        "state_dump_filename": state_dump_filename,
        "traceback":           "".join(traceback.format_exception(exception)) if exception else None,
    }
    _append_jsonl(ERRORS_DIR / _today_filename(), error_record)

    # Public event — schema-validated subset
    log_event("generation_failed", {
        "session_id":          session_id or "unknown",
        "stage":               stage,
        "error_type":          error_type,
        "error_message":       error_message,
        "state_dump_filename": state_dump_filename,
    })


def log_outcome(tracking_token: str, worked: bool, notes: str = "") -> None:
    """
    Post-import outcome from the user. Writes to logs/outcomes/ AND emits an
    outcome_reported event (so it's queryable in the same place as other
    events).
    """
    record = {
        "_event_type":    "outcome_reported",
        "_timestamp":     _now_iso(),
        "tracking_token": tracking_token,
        "worked":         worked,
        "notes":          notes,
    }
    _append_jsonl(OUTCOMES_DIR / _today_filename(), record)
    log_event("outcome_reported", {
        "tracking_token": tracking_token,
        "worked":         worked,
        "notes":          notes,
    })


def dump_state(session_id: str, state: dict) -> str:
    """
    Writes a state snapshot to logs/state_dumps/<session_id>_<timestamp>.json
    and returns the bare filename (not a full path). Caller embeds the returned
    filename in error events for later retrieval.
    """
    _ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{session_id}_{ts}.json"
    path = STATE_DUMPS_DIR / filename
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, default=str, ensure_ascii=False, indent=2)
    except Exception as e:
        _warn_once(f"state_dump_fail:{path}",
                   f"[telemetry] State dump to {path} failed: {e}.")
        return ""
    return filename


# ---------------------------------------------------------------------------
# Public — typed factories (one per event_type in spec §13.2)
# ---------------------------------------------------------------------------
#
# Each factory takes the fields the spec declares and forwards to log_event.
# Optional fields default to sensible empty values rather than None where the
# schema accepts both, to keep downstream jq queries simpler.

def log_session_start(session_id: str, prompt: str, user_agent: str = "") -> None:
    log_event("session_start", {
        "session_id":    session_id,
        "prompt":        prompt,
        "prompt_length": len(prompt),
        "user_agent":    user_agent,
    })


def log_session_abandon(session_id: str, last_state: str,
                        time_since_last_activity_sec: float) -> None:
    log_event("session_abandon", {
        "session_id":                  session_id,
        "last_state":                  last_state,
        "time_since_last_activity_sec": time_since_last_activity_sec,
    })


def log_session_reset(session_id: str, from_state: str, reason: str) -> None:
    log_event("session_reset", {
        "session_id": session_id,
        "from_state": from_state,
        "reason":     reason,
    })


def log_session_complete(session_id: str, total_duration_sec: float,
                         final_state: str, total_llm_calls: int) -> None:
    log_event("session_complete", {
        "session_id":         session_id,
        "total_duration_sec": total_duration_sec,
        "final_state":        final_state,
        "total_llm_calls":    total_llm_calls,
    })


def log_task_match_attempted(session_id: str, prompt_length: int,
                             total_phrases_scanned: int) -> None:
    log_event("task_match_attempted", {
        "session_id":            session_id,
        "prompt_length":         prompt_length,
        "total_phrases_scanned": total_phrases_scanned,
    })


def log_task_match_result(session_id: str, tasks: list,
                          confidence: float | None = None,
                          fallback: str | None = None,
                          unmatched_segments: list | None = None) -> None:
    log_event("task_match_result", {
        "session_id":         session_id,
        "tasks":              tasks,
        "confidence":         confidence,
        "fallback":           fallback,
        "unmatched_segments": unmatched_segments or [],
    })


def log_gate1_preview_shown(session_id: str, step_count: int,
                            decomposition_json: dict | None = None,
                            task_match_result:  dict | None = None,
                            workflow_summary:   str  = "") -> None:
    log_event("gate1_preview_shown", {
        "session_id":         session_id,
        "decomposition_json": decomposition_json,
        "task_match_result":  task_match_result,
        "step_count":         step_count,
        "workflow_summary":   workflow_summary,
    })


def log_gate1_approved(session_id: str, time_to_decision_sec: float,
                       retry_count: int) -> None:
    log_event("gate1_approved", {
        "session_id":           session_id,
        "time_to_decision_sec": time_to_decision_sec,
        "retry_count":          retry_count,
    })


def log_gate1_rejected(session_id: str, feedback_text: str,
                       time_to_decision_sec: float, retry_count: int) -> None:
    log_event("gate1_rejected", {
        "session_id":           session_id,
        "feedback_text":        feedback_text,
        "time_to_decision_sec": time_to_decision_sec,
        "retry_count":          retry_count,
    })


def log_task_pattern_composed(session_id: str, task_id: str,
                              scaffold_pattern_id: str,
                              slots_filled: int, slots_unfilled: int,
                              composition_duration_ms: float | None = None) -> None:
    payload = {
        "session_id":          session_id,
        "task_id":             task_id,
        "scaffold_pattern_id": scaffold_pattern_id,
        "slots_filled":        slots_filled,
        "slots_unfilled":      slots_unfilled,
    }
    if composition_duration_ms is not None:
        payload["composition_duration_ms"] = composition_duration_ms
    log_event("task_pattern_composed", payload)


def log_task_slot_filled(session_id: str, task_id: str,
                         slots_filled: int, slots_null: int) -> None:
    log_event("task_slot_filled", {
        "session_id":   session_id,
        "task_id":      task_id,
        "slots_filled": slots_filled,
        "slots_null":   slots_null,
    })


def log_gate2_preview_shown(session_id: str,
                            activity_list: list | None = None,
                            data_flow:     dict | list | None = None,
                            placeholders:  list | None = None,
                            warnings:      list | None = None,
                            mermaid_snapshot: str | None = None,
                            task_groupings: dict | list | None = None) -> None:
    log_event("gate2_preview_shown", {
        "session_id":       session_id,
        "activity_list":    activity_list  or [],
        "data_flow":        data_flow,
        "placeholders":     placeholders   or [],
        "warnings":         warnings       or [],
        "mermaid_snapshot": mermaid_snapshot,
        "task_groupings":   task_groupings,
    })


def log_gate2_approved(session_id: str, time_to_decision_sec: float,
                       retry_count: int) -> None:
    log_event("gate2_approved", {
        "session_id":           session_id,
        "time_to_decision_sec": time_to_decision_sec,
        "retry_count":          retry_count,
    })


def log_gate2_edit(session_id: str, step_id: str,
                   from_activity: str, to_activity: str,
                   was_in_candidates_list: bool = False,
                   mermaid_snapshot: str | None = None) -> None:
    log_event("gate2_edit", {
        "session_id":             session_id,
        "step_id":                step_id,
        "from_activity":          from_activity,
        "to_activity":            to_activity,
        "was_in_candidates_list": was_in_candidates_list,
        "mermaid_snapshot":       mermaid_snapshot,
    })


def log_gate2_placeholder_resolved(session_id: str, step_id: str,
                                   chosen_activity: str,
                                   candidate_rank: int | None = None,
                                   mermaid_snapshot: str | None = None) -> None:
    log_event("gate2_placeholder_resolved", {
        "session_id":       session_id,
        "step_id":          step_id,
        "chosen_activity":  chosen_activity,
        "candidate_rank":   candidate_rank,
        "mermaid_snapshot": mermaid_snapshot,
    })


def log_gate2_rejected(session_id: str, feedback_text: str,
                       time_to_decision_sec: float, retry_count: int,
                       mermaid_snapshot: str | None = None) -> None:
    log_event("gate2_rejected", {
        "session_id":           session_id,
        "feedback_text":        feedback_text,
        "time_to_decision_sec": time_to_decision_sec,
        "retry_count":          retry_count,
        "mermaid_snapshot":     mermaid_snapshot,
    })


def log_decomposer_call(session_id: str, duration_ms: float, mode: str,
                        prompt_tokens: int | None = None,
                        output_tokens: int | None = None) -> None:
    log_event("decomposer_call", {
        "session_id":    session_id,
        "duration_ms":   duration_ms,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "mode":          mode,
    })


def log_deterministic_middle(session_id: str, duration_ms: float,
                             stage_timings: dict | None = None,
                             warnings:      list | None = None) -> None:
    log_event("deterministic_middle", {
        "session_id":    session_id,
        "duration_ms":   duration_ms,
        "stage_timings": stage_timings,
        "warnings":      warnings or [],
    })


def log_wirer_call(session_id: str, duration_ms: float,
                   prompt_tokens: int  | None = None,
                   output_tokens: int  | None = None,
                   fields_filled: int  | list | None = None) -> None:
    log_event("wirer_call", {
        "session_id":    session_id,
        "duration_ms":   duration_ms,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "fields_filled": fields_filled,
    })


def log_correction_fired(session_id: str, reason: str,
                         errors: list | None = None,
                         resolved_after_correction: bool | None = None) -> None:
    log_event("correction_fired", {
        "session_id":                session_id,
        "reason":                    reason,
        "errors":                    errors or [],
        "resolved_after_correction": resolved_after_correction,
    })


def log_validation_result(session_id: str, status: str,
                          errors: list | None = None,
                          verify_notes: list | None = None) -> None:
    log_event("validation_result", {
        "session_id":   session_id,
        "status":       status,
        "errors":       errors       or [],
        "verify_notes": verify_notes or [],
    })


def log_xml_downloaded(filename: str, tracking_token: str,
                       file_size_bytes: int,
                       session_id: str | None = None) -> None:
    log_event("xml_downloaded", {
        "session_id":      session_id,
        "filename":        filename,
        "tracking_token":  tracking_token,
        "file_size_bytes": file_size_bytes,
    })


def log_outcome_reported(tracking_token: str, worked: bool,
                         notes: str = "") -> None:
    """
    Convenience wrapper that calls log_outcome (which writes to BOTH the
    outcomes log and emits the event). Provided for symmetry with the other
    typed factories — most call sites should use log_outcome directly.
    """
    log_outcome(tracking_token, worked, notes)


def log_generation_failed(session_id: str, stage: str,
                          error_type: str, error_message: str,
                          state_dump_filename: str | None = None) -> None:
    log_event("generation_failed", {
        "session_id":          session_id,
        "stage":               stage,
        "error_type":          error_type,
        "error_message":       error_message,
        "state_dump_filename": state_dump_filename,
    })


# ---------------------------------------------------------------------------
# Session sweeper
# ---------------------------------------------------------------------------

class SessionSweeper:
    """
    Background thread that scans an in-memory session store and emits
    session_abandon events for sessions that exceeded the activity timeout.

    The store is accessed via a callable so this class doesn't need to know
    the session-store implementation. The callable should return a dict
    keyed by session_id with values exposing at minimum:
      - last_activity_at: float (unix epoch seconds)
      - state:            str   (current state machine label)

    Sweeper marks abandoned sessions by adding "_abandon_logged: True" to the
    session entry, so subsequent sweeps don't re-emit the event for the same
    session. Callers that delete sessions on completion don't need this guard.

    Stop is cooperative — calling stop() signals the thread; join happens in
    stop() so callers don't need to do it themselves.
    """

    def __init__(self,
                 get_sessions: Callable[[], dict],
                 timeout_sec:  float = SESSION_TIMEOUT_SEC,
                 interval_sec: float = SESSION_SWEEP_INTERVAL_SEC) -> None:
        self._get_sessions = get_sessions
        self._timeout_sec  = timeout_sec
        self._interval_sec = interval_sec
        self._stop_event   = threading.Event()
        self._thread:      threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="telemetry-session-sweeper",
            daemon=True,
        )
        self._thread.start()

    def stop(self, join_timeout_sec: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_sec)

    def sweep_once(self) -> int:
        """Public for tests. Returns count of sessions marked abandoned."""
        try:
            sessions = self._get_sessions() or {}
        except Exception as e:
            _warn_once("sweeper_get_sessions",
                       f"[telemetry] SessionSweeper get_sessions failed: {e}")
            return 0

        now   = time.time()
        count = 0
        for sid, entry in list(sessions.items()):
            try:
                if entry.get("_abandon_logged"):
                    continue
                last_at = float(entry.get("last_activity_at", 0))
                if last_at <= 0:
                    continue
                idle = now - last_at
                if idle >= self._timeout_sec:
                    last_state = str(entry.get("state", "unknown"))
                    log_session_abandon(sid, last_state, idle)
                    entry["_abandon_logged"] = True
                    count += 1
            except Exception as e:
                _warn_once(f"sweeper_session:{sid}",
                           f"[telemetry] SessionSweeper failed on {sid}: {e}")
                continue
        return count

    def _run(self) -> None:
        # Initial small delay so app startup doesn't compete with sweeper init
        if self._stop_event.wait(min(5.0, self._interval_sec)):
            return
        while not self._stop_event.is_set():
            try:
                self.sweep_once()
            except Exception as e:
                _warn_once("sweeper_loop",
                           f"[telemetry] SessionSweeper loop error: {e}")
            if self._stop_event.wait(self._interval_sec):
                return


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """
    Emits one of each event type using a fake session_id, then prints output
    paths. Run via:  python -m tools.telemetry
    """
    sid = f"smoke_{uuid.uuid4().hex[:8]}"
    print(f"[smoke] session_id = {sid}")
    print(f"[smoke] LOG_ROOT   = {LOG_ROOT.resolve()}")
    print(f"[smoke] schema     = {SCHEMA_PATH.resolve()}")
    print()

    log_session_start(sid, prompt="smoke test prompt", user_agent="cli")
    log_task_match_attempted(sid, prompt_length=18, total_phrases_scanned=42)
    log_task_match_result(sid,
                          tasks=[{"task_id": "demo", "confidence": 0.9}],
                          confidence=0.9,
                          fallback=None,
                          unmatched_segments=[])
    log_decomposer_call(sid, duration_ms=1234.5, mode="slot_filling",
                        prompt_tokens=100, output_tokens=200)
    log_task_slot_filled(sid, task_id="demo", slots_filled=3, slots_null=1)
    log_gate1_preview_shown(sid, step_count=4, workflow_summary="demo summary")
    log_gate1_approved(sid, time_to_decision_sec=12.3, retry_count=0)
    log_task_pattern_composed(sid, task_id="demo", scaffold_pattern_id="p019",
                              slots_filled=3, slots_unfilled=1,
                              composition_duration_ms=4.2)
    log_deterministic_middle(sid, duration_ms=89.0,
                             stage_timings={"retrieval": 12.0, "fragments": 3.0},
                             warnings=[])
    log_gate2_preview_shown(sid,
                            activity_list=[{"step_id": "s1", "activity": "Ping"}],
                            placeholders=[],
                            warnings=[],
                            mermaid_snapshot="graph TD; A-->B")
    log_gate2_edit(sid, step_id="s1", from_activity="Ping",
                   to_activity="TraceRoute", was_in_candidates_list=True)
    log_gate2_placeholder_resolved(sid, step_id="s2",
                                   chosen_activity="SendEmail", candidate_rank=1)
    log_gate2_approved(sid, time_to_decision_sec=8.0, retry_count=1)
    log_wirer_call(sid, duration_ms=2200.0, prompt_tokens=400,
                   output_tokens=800, fields_filled=12)
    log_correction_fired(sid, reason="validation_error",
                         errors=["xName collision"], resolved_after_correction=True)
    log_validation_result(sid, status="valid", errors=[], verify_notes=[])
    log_xml_downloaded(filename="wf_demo.xml", tracking_token="tok_abc",
                       file_size_bytes=4321, session_id=sid)
    log_outcome("tok_abc", worked=True, notes="smoke")
    log_session_reset(sid, from_state="GATE1", reason="user_clicked_start_over")
    log_session_complete(sid, total_duration_sec=42.0,
                         final_state="DONE", total_llm_calls=2)
    log_gate1_rejected(sid, feedback_text="wrong tasks",
                       time_to_decision_sec=5.0, retry_count=1)
    log_gate2_rejected(sid, feedback_text="wrong activity",
                       time_to_decision_sec=4.0, retry_count=2)
    log_session_abandon(sid, last_state="GATE2", time_since_last_activity_sec=14401)

    try:
        raise RuntimeError("intentional smoke-test exception")
    except RuntimeError as e:
        log_error(stage="smoke", error_type="RuntimeError",
                  error_message=str(e), session_id=sid,
                  state={"smoke": True, "session_id": sid},
                  exception=e)

    today = _today_filename()
    print("[smoke] done. Inspect:")
    print(f"  {EVENTS_DIR / today}")
    print(f"  {ERRORS_DIR / today}")
    print(f"  {OUTCOMES_DIR / today}")
    print(f"  {STATE_DUMPS_DIR}/   (per-incident files)")
    print()
    print("[smoke] schema violations (should be empty if all factories match schema):")
    print(f"  {VIOLATIONS_FILE}")


if __name__ == "__main__":
    _smoke_test()