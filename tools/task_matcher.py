"""
tools/task_matcher.py

Phase 1a — task matching. Path B variant: atomic tasks only, no slot
extraction, no scaffold lookup. Output is for Gate 1 legibility and for
downstream telemetry; the Decomposer (Phase 1b) does not consume matcher
output in Path B (stays in freeform mode per spec §6.3).

INPUT:  prompt string
OUTPUT: ordered list of matched tasks with confidence, plus unmatched
        segments for taxonomy-gap telemetry

ALGORITHM
  1. Load all (phrase → task_id) mappings from data/task_match_phrases.json
  2. Sort phrases by length desc — longest matches consume their character
     range first, preventing 'check if running' from also matching 'check if'
  3. For each phrase, find all word-boundary substring matches in the prompt
  4. Resolve overlaps: longer phrases win, mark their ranges as consumed
  5. Aggregate surviving matches by task_id, compute confidence per task
  6. Order tasks by first occurrence in prompt
  7. Return remaining unconsumed text spans as unmatched_segments

CONFIDENCE FORMULA
  base 0.6 + 0.15 per matched phrase, capped at 1.0:
    1 phrase  → 0.75
    2 phrases → 0.90
    3+ phrases → 1.00
  Tasks with zero matched phrases don't appear in output.

WORD BOUNDARY MATCHING
  Each phrase is matched as a regex with \\b on each side, after escaping.
  'ping' matches 'ping it' but not 'shipping'. Multi-word phrases like
  'for each' work because the boundary check is at phrase start/end, not
  between internal words.

ENV
  DATA_DIR — root for data files. Defaults to "/app/data".

USAGE
  from tools.task_matcher import match_tasks

  result = match_tasks(
      prompt="For each server, ping it. If down, send an email.",
      telemetry_session_id="abc123",  # optional; None disables telemetry
  )
  for task in result.tasks:
      print(task["task_id"], task["confidence"])

SMOKE TEST
  python -m tools.task_matcher
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from tools import telemetry
except Exception:
    telemetry = None  # graceful: matcher works without telemetry installed


DATA_DIR     = Path(os.getenv("DATA_DIR", "/app/data"))
PHRASES_PATH = DATA_DIR / "task_match_phrases.json"


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

CONFIDENCE_BASE      = 0.60
CONFIDENCE_PER_HIT   = 0.15
CONFIDENCE_MAX       = 1.00
MIN_UNMATCHED_LENGTH = 8     # ignore tiny leftover spans like ', and '


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PhraseMatch:
    """Single phrase hit. Internal."""
    task_id: str
    phrase:  str
    start:   int
    end:     int


@dataclass(frozen=True)
class MatchResult:
    """Returned by match_tasks. Stable shape — telemetry and downstream
    callers depend on these field names."""
    tasks:              list   # [{task_id, confidence, evidence_phrases, first_position}]
    fallback:           str | None   # None when ≥1 task matched; "no_task_match" otherwise
    unmatched_segments: list   # leftover text spans for taxonomy gap analysis


# ---------------------------------------------------------------------------
# Module-level state — phrases loaded once
# ---------------------------------------------------------------------------

_phrases_loaded:    bool = False
_phrase_to_tasks:   dict[str, list[str]] = {}   # phrase → [task_ids] (multiple OK)
_phrase_regex:      list[tuple[str, re.Pattern]] = []   # sorted long→short
_total_phrase_count: int = 0


def _load_phrases() -> None:
    """Loads phrases once and pre-compiles regex per phrase. Graceful on
    failure: matcher returns empty results with a stderr warning rather
    than crashing."""
    global _phrases_loaded, _phrase_to_tasks, _phrase_regex, _total_phrase_count
    if _phrases_loaded:
        return
    _phrases_loaded = True
    try:
        with open(PHRASES_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"[task_matcher] WARNING: phrases file not loadable at "
              f"{PHRASES_PATH}: {e}. Matcher will return empty results.",
              file=sys.stderr)
        return

    # Build phrase → task map. A phrase mapping to multiple tasks is allowed
    # (rare) and propagates to all of them.
    for key, value in doc.items():
        if key.startswith("_") or not isinstance(value, list):
            continue
        task_id = key
        for phrase in value:
            if not isinstance(phrase, str) or not phrase.strip():
                continue
            phrase_lower = phrase.lower().strip()
            _phrase_to_tasks.setdefault(phrase_lower, []).append(task_id)

    # Pre-compile regex per unique phrase, with word boundaries
    for phrase in _phrase_to_tasks:
        # \b doesn't quite work with phrases starting/ending in non-word
        # characters, but our phrases are all alphanumeric, so this is fine.
        pattern = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
        _phrase_regex.append((phrase, pattern))

    # Sort longest first so 'check if running' beats 'check if'
    _phrase_regex.sort(key=lambda x: -len(x[0]))
    _total_phrase_count = len(_phrase_regex)


# ---------------------------------------------------------------------------
# Matching internals
# ---------------------------------------------------------------------------

def _find_all_matches(prompt: str) -> list[_PhraseMatch]:
    """Returns every phrase hit in the prompt. May contain overlaps —
    resolve_overlaps handles those next."""
    out: list[_PhraseMatch] = []
    for phrase, pattern in _phrase_regex:
        for m in pattern.finditer(prompt):
            for task_id in _phrase_to_tasks[phrase]:
                out.append(_PhraseMatch(
                    task_id=task_id, phrase=phrase,
                    start=m.start(), end=m.end(),
                ))
    return out


def _resolve_overlaps(matches: list[_PhraseMatch]) -> list[_PhraseMatch]:
    """Greedy longest-first selection. Walk matches sorted by phrase length
    desc, keep each one only if it doesn't overlap an already-kept match."""
    matches_by_length = sorted(
        matches, key=lambda m: (-(m.end - m.start), m.start)
    )
    kept: list[_PhraseMatch] = []
    consumed: list[tuple[int, int]] = []   # ranges already taken
    for m in matches_by_length:
        if any(not (m.end <= s or m.start >= e) for s, e in consumed):
            continue
        kept.append(m)
        consumed.append((m.start, m.end))
    return kept


def _aggregate_by_task(matches: list[_PhraseMatch]) -> list[dict]:
    """Group by task_id, compute confidence, sort by first occurrence."""
    by_task: dict[str, list[_PhraseMatch]] = {}
    for m in matches:
        by_task.setdefault(m.task_id, []).append(m)

    out = []
    for task_id, hits in by_task.items():
        n = len(hits)
        confidence = min(CONFIDENCE_MAX,
                         CONFIDENCE_BASE + n * CONFIDENCE_PER_HIT)
        first_position = min(h.start for h in hits)
        evidence = sorted({h.phrase for h in hits})
        out.append({
            "task_id":          task_id,
            "confidence":       round(confidence, 3),
            "evidence_phrases": evidence,
            "first_position":   first_position,
        })
    out.sort(key=lambda t: t["first_position"])
    return out


def _compute_unmatched_segments(prompt: str,
                                matches:  list[_PhraseMatch]) -> list[str]:
    """Returns text spans not covered by any kept match, longer than
    MIN_UNMATCHED_LENGTH. Useful for taxonomy gap analysis — surfacing
    chunks of user prompts that no task knew how to label."""
    if not matches:
        return [prompt.strip()] if prompt.strip() else []
    consumed = sorted([(m.start, m.end) for m in matches])
    out = []
    cursor = 0
    for s, e in consumed:
        if s > cursor:
            seg = prompt[cursor:s].strip(" \n\t.,;:-")
            if len(seg) >= MIN_UNMATCHED_LENGTH:
                out.append(seg)
        cursor = max(cursor, e)
    if cursor < len(prompt):
        seg = prompt[cursor:].strip(" \n\t.,;:-")
        if len(seg) >= MIN_UNMATCHED_LENGTH:
            out.append(seg)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def match_tasks(
    prompt:               str,
    telemetry_session_id: str | None = None,
) -> MatchResult:
    """
    Match a prompt against the task taxonomy.

    telemetry_session_id, when provided, causes the matcher to emit
    task_match_attempted and task_match_result events. Pass None to disable
    (CLI / smoke test contexts).
    """
    _load_phrases()

    if not isinstance(prompt, str):
        prompt = str(prompt or "")

    if telemetry is not None and telemetry_session_id:
        try:
            telemetry.log_task_match_attempted(
                telemetry_session_id,
                prompt_length=len(prompt),
                total_phrases_scanned=_total_phrase_count,
            )
        except Exception as e:
            print(f"[task_matcher] WARNING: telemetry failed: {e}",
                  file=sys.stderr)

    raw_matches  = _find_all_matches(prompt)
    kept_matches = _resolve_overlaps(raw_matches)
    tasks        = _aggregate_by_task(kept_matches)
    unmatched    = _compute_unmatched_segments(prompt, kept_matches)
    fallback     = None if tasks else "no_task_match"

    result = MatchResult(
        tasks=tasks,
        fallback=fallback,
        unmatched_segments=unmatched,
    )

    if telemetry is not None and telemetry_session_id:
        try:
            top_confidence = max((t["confidence"] for t in tasks), default=None)
            telemetry.log_task_match_result(
                telemetry_session_id,
                tasks=tasks,
                confidence=top_confidence,
                fallback=fallback,
                unmatched_segments=unmatched,
            )
        except Exception as e:
            print(f"[task_matcher] WARNING: telemetry failed: {e}",
                  file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """Exercises typical patterns. Verifies ordering, overlap resolution,
    fallback, and unmatched-segment extraction.

    Test cases revised after path-b-1 smoke run:
      - 'longest-first wins' now uses a prompt where the long phrase
        actually appears contiguously ('check if running' as one phrase,
        not split by 'the service is')
      - 'database + email pattern' uses 'for each' explicitly to actually
        exercise iterate_rows; the original 'if any row' is a SQL
        existence check, not iteration
    """
    print("[smoke] DATA_DIR =", DATA_DIR.resolve())
    print("[smoke] phrases =", PHRASES_PATH.resolve())
    print()

    # (label, prompt, expected_task_ids_in_order, expect_fallback)
    cases = [
        (
            "monitor + email pattern",
            "Every morning, check a list of servers from an Excel file. "
            "For each server, ping it. If the ping fails, send an email "
            "to the admin with the server name.",
            ["read_file", "iterate_rows", "query_system_state",
             "branch_decision", "send_email"],
            None,
        ),
        (
            "ITSM + Slack pattern",
            "When a new ServiceNow incident is created with high priority, "
            "post a message to the on-call Slack channel.",
            ["query_itsm", "send_notification"],
            None,
        ),
        (
            "database + iterate + email pattern",
            "Run a SQL query against the orders database. For each failed "
            "order, email the operations team.",
            ["query_database", "iterate_rows", "send_email"],
            None,
        ),
        (
            "no match → fallback",
            "Make my computer go faster please thanks",
            [],
            "no_task_match",
        ),
        (
            "longest-first wins",
            "Check if running on the host.",
            ["query_system_state"],   # 'check if running' wins over 'check if'
            None,
        ),
    ]

    failures = 0
    for label, prompt, expected_ids, expected_fallback in cases:
        result = match_tasks(prompt)
        actual_ids = [t["task_id"] for t in result.tasks]

        ids_match      = actual_ids == expected_ids
        fallback_match = result.fallback == expected_fallback
        ok             = ids_match and fallback_match

        marker = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{marker}] {label}")
        if not ok:
            print(f"        expected ids:      {expected_ids}")
            print(f"        actual ids:        {actual_ids}")
            print(f"        expected fallback: {expected_fallback}")
            print(f"        actual fallback:   {result.fallback}")
            if result.tasks:
                print(f"        evidence:")
                for t in result.tasks:
                    print(f"          {t['task_id']}: {t['evidence_phrases']}")

    # Verify confidence formula
    print()
    print("[smoke] confidence formula:")
    r = match_tasks("send email")  # 1 phrase
    if r.tasks and r.tasks[0]["confidence"] == 0.75:
        print("  [PASS] 1 phrase → 0.75")
    else:
        failures += 1
        print(f"  [FAIL] 1 phrase expected 0.75, got "
              f"{r.tasks[0]['confidence'] if r.tasks else 'no match'}")

    r = match_tasks("send email and email the admin")  # 2 phrases, same task
    if r.tasks and r.tasks[0]["confidence"] == 0.90:
        print("  [PASS] 2 phrases → 0.90")
    else:
        failures += 1
        print(f"  [FAIL] 2 phrases expected 0.90, got "
              f"{r.tasks[0]['confidence'] if r.tasks else 'no match'}")

    # Verify unmatched segments surface
    print()
    print("[smoke] unmatched segments:")
    r = match_tasks(
        "send an email when the rocket is ready to launch tomorrow"
    )
    print(f"  matched tasks: {[t['task_id'] for t in r.tasks]}")
    print(f"  unmatched:     {r.unmatched_segments}")
    if any("rocket" in seg for seg in r.unmatched_segments):
        print("  [PASS] novel vocabulary surfaces in unmatched_segments")
    else:
        failures += 1
        print("  [FAIL] expected 'rocket' in unmatched")

    # Verify graceful behavior with empty prompt
    print()
    r = match_tasks("")
    if not r.tasks and r.fallback == "no_task_match":
        print("  [PASS] empty prompt → no_task_match")
    else:
        failures += 1
        print(f"  [FAIL] empty prompt expected no_task_match, got {r}")

    print()
    total = len(cases) + 4
    print(f"[smoke] {total - failures}/{total} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(_smoke_test())