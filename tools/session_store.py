"""
tools/session_store.py

Phase G2 — server-side session store. In-memory implementation of the §13.1
schema. Used by api.py to persist gate state between HTTP requests.

DESIGN
  - Plain dict, lock-protected. Single-process, single-worker. Multi-worker
    deployments will need to swap this for Redis or similar; the public API
    is small and stable enough to abstract behind a Protocol later.
  - Session entry tracks both the §13.1 user-facing fields AND internal
    pipeline artifacts (activity_manifest, pattern_match, workflow_json,
    output_result) needed to seed the next ADK runner. Keeping them in one
    dict simplifies the gate flow — each endpoint reads its prerequisites
    and writes its outputs into the same entry.
  - Tracking tokens get a separate inverse index (tracking_token -> dict)
    so /outcome/{tracking_token} can resolve in O(1) without scanning
    sessions. Each token entry holds session_id and output_filename for
    cross-reference.
  - register_activity() bumps last_activity_at on every endpoint touch so
    SessionSweeper sees fresh sessions as alive.
  - increment_prompt_count() is called on each successful /plan to enforce
    the 50-prompt-per-session ceiling via the prompt_validator.

GATE STATE TRANSITIONS
  state field labels track where the user is in the gate workflow. These
  are the labels SessionSweeper emits in session_abandon events.

    INIT                  — session created but no /plan yet (transient)
    AWAITING_GATE1        — /plan returned, user reviewing preview
    AWAITING_GATE2        — /build-activities returned, user reviewing activities
    AWAITING_GATE3        — /generate-artifacts returned, user reviewing artifacts
    DONE                  — outcome reported
    FAILED                — terminal failure (validation, retry exhausted)

PROTOCOL CONFORMANCE
  Implements the SessionStore protocol from tools.prompt_validator so checks
  5 (session freshness) and 6 (prompt limit) work against the live store.

USAGE
  store = SessionStore()
  sid = store.create_session(prompt="...")
  store.update(sid, decomposition={...}, state="AWAITING_GATE1")
  entry = store.get(sid)

  # Register a tracking token after successful /generate-artifacts:
  token = store.register_tracking_token(sid, output_filename="wf_x.json")

  # Look up by token from /outcome:
  ref = store.lookup_tracking_token(token)
  # ref == {"session_id": ..., "output_filename": ...} or None

SMOKE TEST
  python -m tools.session_store
"""

import threading
import time
import uuid

from tools.prompt_validator import SessionMetadata


# 4-hour timeout matches §13.1 and prompt_validator.SESSION_TIMEOUT_SEC
SESSION_TIMEOUT_SEC = 4 * 60 * 60


def _empty_session_entry(prompt: str) -> dict:
    """Returns a fresh entry matching §13.1 schema plus internal pipeline
    artifact slots. None values are placeholders so .get() returns falsy
    consistently across uninitialized stages."""
    now = time.time()
    return {
        # §13.1 user-facing
        "session_id":           "",   # filled by create_session
        "prompt":               prompt,
        "original_prompt":      prompt,
        "retry_count_stage1":   0,
        "retry_count_stage2":   0,

        "task_match_result":    None,   # Phase 1a output
        "decomposition":        None,   # Phase 1b output
        "composed_skeleton":    None,   # Phase 2a — Path B leaves null
        "enriched_workflow":    None,   # Phase 2b output

        "mermaid_snapshot":     None,
        "tracking_tokens":      [],

        "created_at":           now,
        "last_activity_at":     now,

        # Internal — pipeline artifacts needed to seed next ADK runner
        "activity_manifest":    None,
        "pattern_match":        None,
        "workflow_json":        None,
        "annotation_result":    None,
        "validation_result":    None,
        "output_result":        None,

        # Internal — gate timing for time_to_decision_sec computation
        "gate1_shown_at":       None,
        "gate2_shown_at":       None,
        "gate3_shown_at":       None,

        # Internal — counter for prompt_validator check 6
        "prompt_count":         0,

        # Internal — gate state machine label (for sweeper abandon events)
        "state":                "INIT",

        # Internal — sweeper bookkeeping
        "_abandon_logged":      False,
    }


class SessionStore:
    """
    Thread-safe in-memory session store. Implements the SessionStore protocol
    from tools.prompt_validator. Provides create/get/update/delete plus a
    tracking-token inverse index.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._tokens:   dict[str, dict] = {}   # token -> {session_id, output_filename}
        self._lock = threading.RLock()

    # ----- core session ops ------------------------------------------------

    def create_session(self, prompt: str) -> str:
        """Create a new session entry. Returns the generated session_id."""
        sid = f"sess_{uuid.uuid4().hex[:12]}"
        with self._lock:
            entry = _empty_session_entry(prompt)
            entry["session_id"] = sid
            self._sessions[sid] = entry
        return sid

    def get(self, session_id: str) -> dict | None:
        """Return a shallow copy of the session entry, or None if not found.
        Returning a copy prevents callers from mutating internal state by
        reference; updates must go through update()."""
        with self._lock:
            entry = self._sessions.get(session_id)
            return dict(entry) if entry is not None else None

    def update(self, session_id: str, **fields) -> bool:
        """Merge fields into the session entry and bump last_activity_at.
        Returns True if the session existed and was updated, False otherwise.

        Unknown fields are tolerated — internal pipeline artifacts are passed
        through as-is to keep the API simple. The §13.1 schema fields and
        internal slots are listed in _empty_session_entry; the test for
        "is this a real field" happens by convention, not by validation."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return False
            entry.update(fields)
            entry["last_activity_at"] = time.time()
            entry["_abandon_logged"]  = False   # reset — fresh activity
            return True

    def delete(self, session_id: str) -> bool:
        """Drop a session and any associated tracking tokens. Returns True
        if the session existed."""
        with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry is None:
                return False
            for token in entry.get("tracking_tokens", []):
                self._tokens.pop(token, None)
            return True

    def register_activity(self, session_id: str) -> bool:
        """Bump last_activity_at without mutating other fields. Used by
        endpoints that don't change session content (e.g. /download)."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return False
            entry["last_activity_at"] = time.time()
            entry["_abandon_logged"]  = False
            return True

    def increment_prompt_count(self, session_id: str) -> int:
        """Increment and return the new count. Used by /plan to enforce
        the per-session prompt limit via prompt_validator check 6."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return 0
            entry["prompt_count"] = int(entry.get("prompt_count", 0)) + 1
            entry["last_activity_at"] = time.time()
            return entry["prompt_count"]

    # ----- prompt_validator.SessionStore protocol --------------------------

    def get_session_metadata(self, session_id: str) -> SessionMetadata | None:
        """Implements the SessionStore protocol from prompt_validator.
        Returns the subset of session state checks 5+6 need."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            return SessionMetadata(
                last_activity_at=float(entry.get("last_activity_at", 0.0)),
                prompt_count=int(entry.get("prompt_count", 0)),
            )

    # ----- tracking-token index --------------------------------------------

    def register_tracking_token(self, session_id: str,
                                output_filename: str) -> str | None:
        """Generate a UUID v4 tracking token, store it in the session's
        tracking_tokens list, and add an inverse index entry. Returns the
        token, or None if the session doesn't exist."""
        token = f"tok_{uuid.uuid4().hex}"
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            entry.setdefault("tracking_tokens", []).append(token)
            entry["last_activity_at"] = time.time()
            self._tokens[token] = {
                "session_id":      session_id,
                "output_filename": output_filename,
            }
        return token

    def lookup_tracking_token(self, token: str) -> dict | None:
        """Return {session_id, output_filename} for the token, or None."""
        with self._lock:
            ref = self._tokens.get(token)
            return dict(ref) if ref is not None else None

    # ----- sweeper interface -----------------------------------------------

    @property
    def sessions(self) -> dict:
        """Return a shallow snapshot of all sessions, for the
        SessionSweeper. Sweeper expects entries with last_activity_at,
        state, and _abandon_logged keys — all present in
        _empty_session_entry. Returning a copy of the dict (but with
        live entry references) lets the sweeper mark _abandon_logged
        in place, which is the contract telemetry.SessionSweeper expects."""
        with self._lock:
            return dict(self._sessions)

    def gc_expired(self) -> int:
        """Hard-delete sessions older than SESSION_TIMEOUT_SEC. Distinct from
        SessionSweeper, which only emits abandon events without deleting.
        Caller may invoke this periodically to reclaim memory.
        Returns count of sessions deleted."""
        cutoff = time.time() - SESSION_TIMEOUT_SEC
        deleted = 0
        with self._lock:
            for sid in list(self._sessions.keys()):
                entry = self._sessions[sid]
                if entry.get("last_activity_at", 0) < cutoff:
                    for token in entry.get("tracking_tokens", []):
                        self._tokens.pop(token, None)
                    del self._sessions[sid]
                    deleted += 1
        return deleted


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    print("[smoke] tools.session_store")
    failures = 0

    store = SessionStore()

    # create + get
    sid = store.create_session(prompt="ping a server and email admin")
    entry = store.get(sid)
    if entry is None:
        failures += 1
        print("  [FAIL] create_session/get round-trip")
    else:
        print(f"  [PASS] create_session -> {sid}")
        # Verify §13.1 fields
        for f in ["session_id", "prompt", "original_prompt", "retry_count_stage1",
                  "retry_count_stage2", "task_match_result", "decomposition",
                  "composed_skeleton", "enriched_workflow", "mermaid_snapshot",
                  "tracking_tokens", "created_at", "last_activity_at"]:
            if f not in entry:
                failures += 1
                print(f"  [FAIL] missing §13.1 field: {f}")
        if not failures:
            print("  [PASS] §13.1 schema fields all present")

    # update
    ok = store.update(sid, decomposition={"steps": [{"step_id": "s1"}]},
                      state="AWAITING_GATE1")
    if ok and store.get(sid)["decomposition"]["steps"][0]["step_id"] == "s1":
        print("  [PASS] update")
    else:
        failures += 1
        print("  [FAIL] update")

    # update on missing session returns False
    if not store.update("nonexistent", foo="bar"):
        print("  [PASS] update on missing session returns False")
    else:
        failures += 1
        print("  [FAIL] update should return False on missing session")

    # tracking token round-trip
    token = store.register_tracking_token(sid, output_filename="wf.json")
    ref   = store.lookup_tracking_token(token)
    if ref and ref["session_id"] == sid and ref["output_filename"] == "wf.json":
        print(f"  [PASS] tracking token round-trip: {token[:16]}...")
    else:
        failures += 1
        print("  [FAIL] tracking token round-trip")
    if token in store.get(sid).get("tracking_tokens", []):
        print("  [PASS] tracking token added to session")
    else:
        failures += 1
        print("  [FAIL] tracking token not in session list")

    # SessionStore protocol — get_session_metadata
    meta = store.get_session_metadata(sid)
    if meta and meta.prompt_count == 0:
        print("  [PASS] get_session_metadata (protocol)")
    else:
        failures += 1
        print("  [FAIL] get_session_metadata")

    # increment_prompt_count
    n = store.increment_prompt_count(sid)
    if n == 1 and store.get_session_metadata(sid).prompt_count == 1:
        print("  [PASS] increment_prompt_count")
    else:
        failures += 1
        print("  [FAIL] increment_prompt_count")

    # delete also removes tokens
    store.delete(sid)
    if store.get(sid) is None and store.lookup_tracking_token(token) is None:
        print("  [PASS] delete cascades to tokens")
    else:
        failures += 1
        print("  [FAIL] delete should remove session and its tokens")

    # gc_expired
    sid2 = store.create_session(prompt="another prompt to test gc")
    # Force expiry by mutating last_activity_at directly (acceptable in test)
    with store._lock:
        store._sessions[sid2]["last_activity_at"] = time.time() - SESSION_TIMEOUT_SEC - 100
    deleted = store.gc_expired()
    if deleted == 1 and store.get(sid2) is None:
        print("  [PASS] gc_expired removes stale sessions")
    else:
        failures += 1
        print(f"  [FAIL] gc_expired returned {deleted}, session still present: "
              f"{store.get(sid2) is not None}")

    # Sweeper interface — sessions property
    sid3 = store.create_session(prompt="for sweeper test")
    snapshot = store.sessions
    if sid3 in snapshot:
        print("  [PASS] sessions snapshot includes live session")
    else:
        failures += 1
        print("  [FAIL] sessions snapshot")

    # Sweeper marks _abandon_logged in entry — verify entries are live refs
    snapshot[sid3]["_abandon_logged"] = True
    if store.get(sid3)["_abandon_logged"]:
        print("  [PASS] sessions snapshot exposes live entry refs (sweeper contract)")
    else:
        failures += 1
        print("  [FAIL] sessions snapshot must expose live refs for sweeper")

    print()
    print(f"[smoke] {failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_smoke_test())