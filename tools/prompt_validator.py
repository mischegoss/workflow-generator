"""
tools/prompt_validator.py

Phase A: deterministic prompt validation. Six checks per spec §4. No LLM calls.

CHECKS
  1. Length              — ≥10 chars initial, ≥5 feedback
  2. Character diversity — ≥5 distinct alpha chars, unique/total ratio ≥ 0.3
  3. Consonant run       — no 6+ consecutive consonants
  4. Domain keyword      — ≥1 word from data/prompt_validation_keywords.json
  5. Session freshness   — last activity within SESSION_TIMEOUT_SEC
  6. Prompt-per-session  — ≤ MAX_PROMPTS_PER_SESSION

Per spec, checks 1-4 run on backend AND frontend (the same six-check logic
ships to the browser as a duplicate guard against round-trip latency).
Checks 5-6 are backend-only — frontend doesn't have the session store.

DESIGN
  - Pure function. validate_prompt returns a ValidationResult; never mutates
    state, never emits telemetry, never raises on invalid input. Callers
    decide what to log and what to surface.
  - Short-circuits on first failure. Length is checked first because the
    other checks are uninteresting if the prompt is too short.
  - Session-aware checks (5, 6) are skipped when session_id or session_store
    is None. CLI / smoke-test contexts get checks 1-4 only.

STEM MATCH (check 4)
  Per spec §4 the keyword check is "≥1 from 60-word vocabulary." Implementation
  is word-boundary tokenize the prompt, then test each token against the
  keyword set with a small suffix tolerance: a token matches a keyword if
  token == keyword + sfx for any sfx in SUFFIXES. SUFFIXES covers the common
  English inflections that show up in real prompts ("rows", "monitoring",
  "filtered"). Substring matching without word boundaries was rejected because
  it falsely accepts "browser" as matching "row".

ENV
  DATA_DIR — root for data files. Defaults to "/app/data".

USAGE
  from tools.prompt_validator import validate_prompt, MemorySessionStore

  result = validate_prompt(
      prompt="Send an email when a server fails to ping",
      is_feedback=False,
      session_id="abc123",
      session_store=MemorySessionStore(...),
  )
  if not result.valid:
      print(result.message)

SMOKE TEST
  python -m tools.prompt_validator
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
KEYWORDS_PATH = DATA_DIR / "prompt_validation_keywords.json"

MIN_LENGTH_INITIAL  = 10
MIN_LENGTH_FEEDBACK = 5

MIN_DISTINCT_CHARS  = 5
MIN_DIVERSITY_RATIO = 0.3

MAX_CONSONANT_RUN   = 5   # fail at 6+ consecutive consonants

SESSION_TIMEOUT_SEC      = 4 * 60 * 60
MAX_PROMPTS_PER_SESSION  = 50

# Stem-match suffix tolerance for check 4. Order does not matter; each suffix
# is tried independently. Empty string ensures exact-match works.
_KEYWORD_SUFFIXES = ("", "s", "es", "ed", "ing", "er", "ers")

# Vowels for consonant-run check. Y is treated as a consonant (the spec
# doesn't specify; treating y as consonant is more aggressive at catching
# keyboard mashing like "qwrtyp" without rejecting normal English).
_VOWELS = frozenset("aeiou")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationResult:
    """Returned by validate_prompt. Use .valid for the boolean and .message
    for the user-facing reason on failure."""
    valid:         bool
    failed_check:  str | None = None   # one of: length, diversity,
                                        # consonant_run, domain_keyword,
                                        # session_freshness, prompt_limit
    message:       str  = ""           # user-facing
    details:       dict = field(default_factory=dict)


@dataclass
class SessionMetadata:
    """Subset of session state the validator needs for checks 5 and 6.
    Production session store should expose get_session_metadata()."""
    last_activity_at: float   # unix epoch seconds
    prompt_count:     int


class SessionStore(Protocol):
    """Minimal interface the validator depends on. Implementations live
    elsewhere (api.py will have the real one); this validator only reads."""
    def get_session_metadata(self, session_id: str) -> SessionMetadata | None: ...


# ---------------------------------------------------------------------------
# Module-level state — keyword set loaded once
# ---------------------------------------------------------------------------

_keyword_match_set: frozenset | None = None
_keywords_load_attempted: bool = False


def _load_keywords() -> frozenset:
    """Load keywords once and pre-expand into a stem-match set. On failure,
    returns an empty set and disables check 4 (warns once via stderr).
    Behavior parallels telemetry's schema-load fallback: graceful degradation
    rather than hard failure."""
    global _keyword_match_set, _keywords_load_attempted
    if _keywords_load_attempted:
        return _keyword_match_set or frozenset()
    _keywords_load_attempted = True
    try:
        with open(KEYWORDS_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        keywords = doc.get("keywords", [])
        expanded = {
            (kw + sfx).lower()
            for kw in keywords
            for sfx in _KEYWORD_SUFFIXES
            if isinstance(kw, str) and kw
        }
        _keyword_match_set = frozenset(expanded)
    except Exception as e:
        import sys
        print(f"[prompt_validator] WARNING: keywords file not loadable at "
              f"{KEYWORDS_PATH}: {e}. Domain-keyword check (4) disabled.",
              file=sys.stderr)
        _keyword_match_set = frozenset()
    return _keyword_match_set


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_length(prompt: str, is_feedback: bool) -> ValidationResult:
    minimum = MIN_LENGTH_FEEDBACK if is_feedback else MIN_LENGTH_INITIAL
    stripped = prompt.strip()
    if len(stripped) < minimum:
        kind = "Feedback" if is_feedback else "Prompt"
        return ValidationResult(
            valid=False,
            failed_check="length",
            message=(f"{kind} is too short. "
                     f"Please use at least {minimum} characters."),
            details={"length": len(stripped), "minimum": minimum},
        )
    return ValidationResult(valid=True)


def _check_diversity(prompt: str, is_feedback: bool) -> ValidationResult:
    """Counts only alpha characters. Whitespace and punctuation don't count
    toward diversity — otherwise 'aaaa bbbb cccc' would look diverse on the
    space."""
    alpha = [c.lower() for c in prompt if c.isalpha()]
    if not alpha:
        return ValidationResult(
            valid=False,
            failed_check="diversity",
            message=("Prompt doesn't contain any letters. "
                     "Please describe your workflow in plain language."),
            details={"alpha_count": 0},
        )
    distinct = len(set(alpha))
    ratio    = distinct / len(alpha)
    if distinct < MIN_DISTINCT_CHARS or ratio < MIN_DIVERSITY_RATIO:
        return ValidationResult(
            valid=False,
            failed_check="diversity",
            message=("Prompt doesn't have enough variety. "
                     "Please describe your workflow in plain language."),
            details={
                "distinct_chars":  distinct,
                "diversity_ratio": round(ratio, 2),
                "min_distinct":    MIN_DISTINCT_CHARS,
                "min_ratio":       MIN_DIVERSITY_RATIO,
            },
        )
    return ValidationResult(valid=True)


def _check_consonant_run(prompt: str, is_feedback: bool) -> ValidationResult:
    """Scans for runs of consecutive consonants. Non-letter characters reset
    the run counter, so 'abc 123 def' is two separate runs."""
    run = 0
    longest = 0
    for ch in prompt.lower():
        if ch.isalpha() and ch not in _VOWELS:
            run += 1
            longest = max(longest, run)
            if run > MAX_CONSONANT_RUN:
                return ValidationResult(
                    valid=False,
                    failed_check="consonant_run",
                    message=("Prompt looks like keyboard mashing — too many "
                             "consecutive consonants. Please describe your "
                             "workflow in plain language."),
                    details={"longest_run": longest,
                             "max_allowed": MAX_CONSONANT_RUN},
                )
        else:
            run = 0
    return ValidationResult(valid=True)


_TOKEN_RE = re.compile(r"[a-z]+")


def _check_domain_keyword(prompt: str, is_feedback: bool) -> ValidationResult:
    match_set = _load_keywords()
    if not match_set:
        # Graceful degradation: if we can't load keywords, don't reject.
        # The stderr warning at load time tells operators to fix it.
        return ValidationResult(valid=True)
    tokens = _TOKEN_RE.findall(prompt.lower())
    matched = [t for t in tokens if t in match_set]
    if matched:
        return ValidationResult(
            valid=True,
            details={"matched_keywords": matched[:5]},  # cap for log size
        )
    return ValidationResult(
        valid=False,
        failed_check="domain_keyword",
        message=("Prompt doesn't mention any workflow-related terms. "
                 "Please describe what you want the workflow to do — for "
                 "example, 'send an email when a server fails to ping'."),
        details={"token_count": len(tokens)},
    )


def _check_session_freshness(session: SessionMetadata) -> ValidationResult:
    age = time.time() - session.last_activity_at
    if age > SESSION_TIMEOUT_SEC:
        return ValidationResult(
            valid=False,
            failed_check="session_freshness",
            message=("This session has been inactive too long. "
                     "Please start a new session."),
            details={"age_sec":          int(age),
                     "timeout_sec":      SESSION_TIMEOUT_SEC},
        )
    return ValidationResult(valid=True)


def _check_prompt_limit(session: SessionMetadata) -> ValidationResult:
    if session.prompt_count >= MAX_PROMPTS_PER_SESSION:
        return ValidationResult(
            valid=False,
            failed_check="prompt_limit",
            message=("This session has reached the maximum number of prompts. "
                     "Please start a new session."),
            details={"prompt_count": session.prompt_count,
                     "max_allowed":  MAX_PROMPTS_PER_SESSION},
        )
    return ValidationResult(valid=True)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_prompt(
    prompt:        str,
    is_feedback:   bool                  = False,
    session_id:    str | None            = None,
    session_store: SessionStore | None   = None,
) -> ValidationResult:
    """
    Run all six checks in spec order, short-circuiting on the first failure.

    Checks 5 and 6 require both session_id AND session_store. If either is
    None, the session-aware checks are skipped (validator runs in CLI /
    test mode).
    """
    if not isinstance(prompt, str):
        return ValidationResult(
            valid=False,
            failed_check="length",
            message="Prompt must be a string.",
            details={"type": type(prompt).__name__},
        )

    # Checks 1-4: pure prompt content
    content_checks = (
        _check_length,
        _check_diversity,
        _check_consonant_run,
        _check_domain_keyword,
    )
    for check in content_checks:
        result = check(prompt, is_feedback)
        if not result.valid:
            return result

    # Checks 5-6: session-aware
    if session_id and session_store is not None:
        try:
            session = session_store.get_session_metadata(session_id)
        except Exception as e:
            # Don't fail validation on store errors — that's an operational
            # problem, not a user error. Log via stderr and pass.
            import sys
            print(f"[prompt_validator] WARNING: session_store lookup failed "
                  f"for {session_id}: {e}", file=sys.stderr)
            session = None
        if session is not None:
            for check in (_check_session_freshness, _check_prompt_limit):
                result = check(session)
                if not result.valid:
                    return result

    return ValidationResult(valid=True, message="ok")


# ---------------------------------------------------------------------------
# Reference session store (in-memory)
# ---------------------------------------------------------------------------

class MemorySessionStore:
    """
    Reference implementation. Backs onto a plain dict. api.py will replace
    this with whatever it actually uses; the validator only needs the
    SessionStore protocol.

    The store also exposes register_activity() and increment_prompt_count()
    so the smoke test (and api.py during development) can update sessions
    without needing a separate harness. The validator itself never calls
    these — it only reads via get_session_metadata().
    """

    def __init__(self, sessions: dict | None = None) -> None:
        self._sessions: dict[str, dict] = sessions if sessions is not None else {}

    def get_session_metadata(self, session_id: str) -> SessionMetadata | None:
        entry = self._sessions.get(session_id)
        if entry is None:
            return None
        return SessionMetadata(
            last_activity_at=float(entry.get("last_activity_at", 0.0)),
            prompt_count=int(entry.get("prompt_count", 0)),
        )

    def register_activity(self, session_id: str) -> None:
        entry = self._sessions.setdefault(session_id, {
            "created_at":       time.time(),
            "last_activity_at": time.time(),
            "prompt_count":     0,
        })
        entry["last_activity_at"] = time.time()

    def increment_prompt_count(self, session_id: str) -> int:
        entry = self._sessions.setdefault(session_id, {
            "created_at":       time.time(),
            "last_activity_at": time.time(),
            "prompt_count":     0,
        })
        entry["prompt_count"] = int(entry.get("prompt_count", 0)) + 1
        entry["last_activity_at"] = time.time()
        return entry["prompt_count"]

    @property
    def sessions(self) -> dict:
        return self._sessions


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """Exercises all six checks plus a positive case. Returns 0 on success
    (all expected outcomes matched), 1 on failure."""
    print("[smoke] DATA_DIR =", DATA_DIR.resolve())
    print("[smoke] keywords =", KEYWORDS_PATH.resolve())
    print()

    cases = [
        # (label, prompt, is_feedback, expect_valid, expect_failed_check)
        ("valid prompt",
         "Send an email when a server fails to ping",
         False, True, None),

        ("valid feedback",
         "use sql instead",
         True, True, None),

        ("too short (initial)",
         "ping",
         False, False, "length"),

        ("too short (feedback)",
         "no",
         True, False, "length"),

        ("low diversity",
         "aaaaaaaaaaaaaaa",
         False, False, "diversity"),

        ("consonant run",
         "qwrtypsdfgh send an email",
         False, False, "consonant_run"),

        ("no domain keyword",
         "the quick brown fox jumps over the lazy dog",
         False, False, "domain_keyword"),

        ("plural via stem-match (rows)",
         "count the rows in the table",
         False, True, None),

        ("ing form via stem-match (monitoring)",
         "monitoring the database for errors",
         False, True, None),
    ]

    failures = 0
    for label, prompt, is_feedback, expect_valid, expect_check in cases:
        result = validate_prompt(prompt, is_feedback=is_feedback)
        ok = (result.valid == expect_valid and
              (expect_check is None or result.failed_check == expect_check))
        marker = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{marker}] {label}")
        if not ok:
            print(f"        expected valid={expect_valid} "
                  f"failed_check={expect_check}")
            print(f"        got      valid={result.valid} "
                  f"failed_check={result.failed_check}")
            print(f"        message  {result.message}")

    # Session-aware cases
    print()
    print("[smoke] session-aware checks:")
    store = MemorySessionStore()

    # Stale session
    store._sessions["stale"] = {
        "created_at":       time.time() - 5 * 60 * 60,
        "last_activity_at": time.time() - 5 * 60 * 60,
        "prompt_count":     1,
    }
    r = validate_prompt("Send an email when something happens",
                        session_id="stale", session_store=store)
    ok = (not r.valid and r.failed_check == "session_freshness")
    print(f"  [{'PASS' if ok else 'FAIL'}] stale session detected")
    if not ok:
        failures += 1

    # Over prompt limit
    store._sessions["chatty"] = {
        "created_at":       time.time(),
        "last_activity_at": time.time(),
        "prompt_count":     MAX_PROMPTS_PER_SESSION,
    }
    r = validate_prompt("Send an email when something happens",
                        session_id="chatty", session_store=store)
    ok = (not r.valid and r.failed_check == "prompt_limit")
    print(f"  [{'PASS' if ok else 'FAIL'}] prompt limit detected")
    if not ok:
        failures += 1

    # Fresh session passes
    store.register_activity("fresh")
    r = validate_prompt("Send an email when something happens",
                        session_id="fresh", session_store=store)
    ok = r.valid
    print(f"  [{'PASS' if ok else 'FAIL'}] fresh session passes")
    if not ok:
        failures += 1

    # Session-store error doesn't crash the validator
    class BrokenStore:
        def get_session_metadata(self, sid):
            raise RuntimeError("simulated store outage")
    r = validate_prompt("Send an email when something happens",
                        session_id="anything", session_store=BrokenStore())
    ok = r.valid  # should fall through to valid
    print(f"  [{'PASS' if ok else 'FAIL'}] broken store doesn't crash validator")
    if not ok:
        failures += 1

    print()
    print(f"[smoke] {len(cases) + 4 - failures}/{len(cases) + 4} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_smoke_test())