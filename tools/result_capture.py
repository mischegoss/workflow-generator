"""
tools/result_capture.py

Phase G2 — process-local result capture for ADK pipelines.

WHY THIS EXISTS
  ADK's session service treats LlmAgent output_keys differently from
  Python-stage state mutations. Output_keys go through the Event /
  state_delta mechanism and persist through get_session(). Python writes
  via `ctx.session.state[k] = v` mutate only the in-memory dict the
  agent sees during the run — those writes are LOST when callers later
  read state via get_session().

  This is fine for the CLI flow (main.py reads side-effect files from
  disk and only needs LlmAgent outputs from state). It is NOT fine for
  the HTTP gate-split flow because /build-activities needs to return
  enriched_workflow to the caller, and /generate-artifacts needs to
  consume annotation_result and validation_result. Both come from
  Python-stage writes by tools.pipeline_stages and
  tools.post_wirer_repair, neither of which is an LlmAgent output_key.

  Workaround: a process-local capture dict keyed by a UUID we pass into
  each pipeline run via initial_state["_capture_key"]. The pipeline
  writes its outputs via capture_result(); api.py reads them via
  pop_result() after the runner completes. Pop frees the entry so the
  dict doesn't leak.

  Single-process by design — same constraint as SessionStore. Multi-
  worker deployments will need a real cross-process channel (Redis
  hash, shared memory, etc.) but the contract here stays the same.

WHO CALLS WHAT
  Pipeline-side (in agents/pipeline.py and agents/api_pipelines.py):
    capture_result(_capture_key(ctx), enriched_workflow=..., ...)

  API-side (in api.py):
    captured = pop_result(capture_key)
    state = {**adk_state, **captured}

  Both modules import from tools.result_capture, avoiding the circular
  dependency that would arise if api_pipelines.py owned the dict and
  agents/pipeline.py needed to import it.

CLI COMPATIBILITY
  capture_result is a no-op when _capture_key is empty/missing in ctx
  state. main.py never sets _capture_key, so existing CLI runs see no
  behavior change — capture_result is called but stores nothing.
"""

# Module-level capture dict — keys are UUIDs, values are output dicts.
# Single-process; not thread-safe for concurrent same-key writes but
# api.py uses unique keys per request so contention can't happen.
_RESULTS: dict[str, dict] = {}


def capture_result(capture_key: str, **fields) -> None:
    """Pipeline-side. Merge fields into the capture entry. Idempotent —
    multiple calls accumulate. No-op when capture_key is empty (which is
    how CLI invocations stay unaffected: main.py doesn't seed a key, so
    _capture_key(ctx) returns "" and this function returns immediately)."""
    if not capture_key:
        return
    entry = _RESULTS.setdefault(capture_key, {})
    entry.update(fields)


def pop_result(capture_key: str) -> dict:
    """API-side. Retrieve and remove the captured outputs. Returns {}
    when the key is unknown — happens if the capture call happened in a
    different process or was never made (e.g. pipeline crashed before
    reaching its capture call site)."""
    return _RESULTS.pop(capture_key, {})


def _capture_key(ctx) -> str:
    """Helper to read the capture key from ADK session state. Returns
    empty string when absent, which makes capture_result() a safe no-op
    for non-API callers (main.py)."""
    try:
        return str(ctx.session.state.get("_capture_key") or "")
    except Exception:
        return ""