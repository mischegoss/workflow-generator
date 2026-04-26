#!/usr/bin/env python3
"""
G2 acceptance test — runs all four endpoints in sequence and reports.
No shell quoting issues since everything is in one Python file.

Usage:
    python3 g2_acceptance.py
    # uvicorn must be running on localhost:8000

Exits 0 on full success, 1 on any failure.
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8000"
PROMPT = "For each server in serverTable, ping it and email admin if down"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body.decode("utf-8", "replace")}


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def banner(s):
    print()
    print("=" * 60)
    print(s)
    print("=" * 60)


failures = 0

# ---- /health ----------------------------------------------------------
banner("/health")
status, body = post("/health", {}) if False else (lambda: get("/health"))()
try:
    body = json.loads(body) if isinstance(body, bytes) else body
except Exception:
    pass
print(f"  status: {status}")
print(f"  body:   {body}")
if status != 200:
    failures += 1

# ---- /plan ------------------------------------------------------------
banner("/plan")
t0 = time.time()
status, body = post("/plan", {"prompt": PROMPT})
print(f"  status:        {status}  ({time.time()-t0:.1f}s)")
if status != 200:
    print(f"  body:          {body}")
    failures += 1
    print()
    print(f"FAILED: cannot continue without /plan succeeding")
    sys.exit(1)

sid        = body.get("session_id")
step_count = body.get("step_count")
state      = body.get("state")
print(f"  session_id:    {sid}")
print(f"  step_count:    {step_count}")
print(f"  state:         {state}")
print(f"  preview snip:  {body.get('preview', '')[:80]}...")

# ---- /build-activities ------------------------------------------------
banner("/build-activities")
t0 = time.time()
status, body = post("/build-activities", {"session_id": sid})
print(f"  status:           {status}  ({time.time()-t0:.1f}s)")
if status != 200:
    print(f"  body:             {body}")
    failures += 1
    print()
    print(f"FAILED: cannot continue without /build-activities succeeding")
    sys.exit(1)

print(f"  activity_count:   {body.get('activity_count')}")
print(f"  state:            {body.get('state')}")

# ---- /generate-artifacts ----------------------------------------------
banner("/generate-artifacts")
t0 = time.time()
status, body = post("/generate-artifacts", {"session_id": sid})
print(f"  status:           {status}  ({time.time()-t0:.1f}s)")
if status != 200:
    print(f"  body:             {json.dumps(body, indent=2)[:500]}")
    failures += 1
    print()
    print(f"FAILED: /generate-artifacts did not succeed")
    sys.exit(1)

token  = body.get("tracking_token", "")
url    = body.get("download_url", "")
fname  = body.get("output_filename", "")
print(f"  tracking_token:   {token}")
print(f"  output_filename:  {fname}")
print(f"  download_url:     {url}")
print(f"  retried:          {body.get('retried')}")
print(f"  retry_reason:     {body.get('retry_reason')}")
print(f"  state:            {body.get('state')}")

# ---- /download --------------------------------------------------------
banner("/download")
status, raw = get(url)
print(f"  status:        {status}")
print(f"  bytes:         {len(raw)}")
if status == 200:
    try:
        wf = json.loads(raw)
        print(f"  workflow name: {wf.get('name', '?')}")
        print(f"  pnumber:       {wf.get('pnumber', '?')}")
    except Exception as e:
        print(f"  could not parse downloaded JSON: {e}")
        failures += 1
else:
    failures += 1

# ---- /outcome ---------------------------------------------------------
banner("/outcome")
status, body = post(f"/outcome/{token}", {"worked": True, "notes": "G2 acceptance test"})
print(f"  status: {status}")
print(f"  body:   {body}")
if status != 200:
    failures += 1

# ---- summary ----------------------------------------------------------
print()
print("=" * 60)
print(f"FINAL: {'ALL PASSED ✓' if failures == 0 else f'{failures} failure(s)'}")
print("=" * 60)
print()
print(f"To verify telemetry events for this session, run:")
print(f"  TODAY=$(date -u +%Y-%m-%d)")
print(f"  grep -F '\"session_id\": \"{sid}\"' logs/events/$TODAY.jsonl | \\")
print(f"    python3 -c 'import json,sys,collections; c=collections.Counter()")
print(f"  [c.update([json.loads(l)[\"_event_type\"]]) for l in sys.stdin]")
print(f"  [print(f\"  {{v:3d}}  {{k}}\") for k,v in sorted(c.items())]'")

sys.exit(failures)