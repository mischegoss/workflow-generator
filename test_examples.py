"""
test_10_workflows.py
====================
Runs all 10 test prompts from data/test_prompts.json through the pipeline
and collects JSON + XML output files into a review/ folder.

Usage:
    python test_10_workflows.py
    python test_10_workflows.py --prompts data/test_prompts.json
    python test_10_workflows.py --ids t01 t03 t07   # run specific tests only

Output:
    review/
        t01_linear_ftp/
            prompt.txt
            workflow.json
            workflow.xml
            result.txt          (pass/fail + notes)
        t02_...
        ...
        summary.txt             (overall results table)
"""

import os
import asyncio
import json
import pathlib
import subprocess
import sys
import argparse
import time

os.environ["DATA_DIR"] = "./data"

from dotenv import load_dotenv
load_dotenv()

REVIEW_DIR = pathlib.Path("review")


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--prompts", default="data/test_prompts.json")
parser.add_argument("--ids", nargs="*", help="Run only these test IDs (e.g. t01 t03)")
parser.add_argument("--output-dir", default="review")
args = parser.parse_args()

REVIEW_DIR = pathlib.Path(args.output_dir)
REVIEW_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Load test prompts
# ---------------------------------------------------------------------------

prompts_path = pathlib.Path(args.prompts)
if not prompts_path.exists():
    print(f"ERROR: Test prompts file not found: {prompts_path}")
    sys.exit(1)

all_tests = json.loads(prompts_path.read_text())
if args.ids:
    tests = [t for t in all_tests if t["id"] in args.ids]
    if not tests:
        print(f"ERROR: No tests matched IDs {args.ids}")
        sys.exit(1)
else:
    tests = all_tests

print("=" * 60)
print(f"WORKFLOW GENERATOR — 10-PROMPT TEST SUITE")
print("=" * 60)
print(f"Running {len(tests)} test(s). Output → {REVIEW_DIR}/")
print()

api_key = os.getenv("GOOGLE_API_KEY", "")
if not api_key:
    print("ERROR: GOOGLE_API_KEY not set in .env")
    sys.exit(1)

from main import run

# ---------------------------------------------------------------------------
# Run each test
# ---------------------------------------------------------------------------

results = []

for i, test in enumerate(tests, 1):
    test_id    = test["id"]
    prompt     = test["prompt"]
    cf         = test["control_flow"]
    source     = test.get("file", "")
    rationale  = test.get("rationale", "")

    # Folder name: t01_while_ifelse etc
    folder_name = f"{test_id}_{cf}"
    test_dir = REVIEW_DIR / folder_name
    test_dir.mkdir(exist_ok=True)

    print(f"[{i}/{len(tests)}] {test_id} — {cf}")
    print(f"  Source: {source}")
    print(f"  Prompt: {prompt[:80]}...")

    # Write prompt for reviewer
    (test_dir / "prompt.txt").write_text(
        f"Test ID:      {test_id}\n"
        f"Control flow: {cf}\n"
        f"Source file:  {source}\n"
        f"Rationale:    {rationale}\n"
        f"\n"
        f"PROMPT:\n{prompt}\n",
        encoding="utf-8",
    )

    start = time.time()
    status = "FAIL"
    notes  = []

    try:
        json_path_str, chat_response = asyncio.run(run(prompt))
        elapsed = round(time.time() - start, 1)

        if not json_path_str:
            notes.append("Pipeline produced no output.")
            notes.append(f"Chat response: {chat_response[:300]}")
        else:
            json_path = pathlib.Path(json_path_str)

            # Copy JSON
            if json_path.exists():
                dest_json = test_dir / "workflow.json"
                dest_json.write_bytes(json_path.read_bytes())
                notes.append(f"JSON: {json_path.stat().st_size} bytes")
            else:
                notes.append(f"JSON file missing at {json_path}")

            # Convert to XML
            xml_result = subprocess.run(
                [sys.executable, "convert_to_xml.py", str(json_path)],
                capture_output=True, text=True,
            )
            xml_path = json_path.with_suffix(".xml")

            if xml_result.returncode == 0 and xml_path.exists():
                dest_xml = test_dir / "workflow.xml"
                dest_xml.write_bytes(xml_path.read_bytes())
                notes.append(f"XML: {xml_path.stat().st_size} bytes — VALID")
                status = "PASS"
            else:
                notes.append("XML conversion FAILED")
                if xml_result.stderr:
                    notes.append(xml_result.stderr.strip()[:200])

            # Extract verify notes from JSON for reviewer
            try:
                wf_data = json.loads(json_path.read_text())
                verify_items = [
                    item for item in wf_data.get("placeholder_summary", [])
                    if item.get("kind") == "verify"
                ]
                if verify_items:
                    notes.append(f"{len(verify_items)} VERIFY note(s) for reviewer:")
                    for item in verify_items:
                        notes.append(f"  [{item.get('activity','?')}] {item.get('message','')[:120]}")
            except Exception:
                pass

    except Exception as e:
        elapsed = round(time.time() - start, 1)
        notes.append(f"Exception: {e}")

    # Write result.txt
    result_lines = [
        f"Test ID:  {test_id}",
        f"Status:   {status}",
        f"Elapsed:  {elapsed}s",
        f"",
        "Notes:",
    ] + [f"  {n}" for n in notes]
    (test_dir / "result.txt").write_text("\n".join(result_lines), encoding="utf-8")

    symbol = "✓" if status == "PASS" else "✗"
    print(f"  {symbol} {status}  ({elapsed}s)")
    for n in notes[:3]:
        print(f"     {n}")
    print()

    results.append({
        "id": test_id, "cf": cf, "status": status,
        "elapsed": elapsed, "notes": notes,
    })

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

passed  = sum(1 for r in results if r["status"] == "PASS")
failed  = len(results) - passed

summary_lines = [
    "=" * 60,
    "TEST SUITE SUMMARY",
    "=" * 60,
    f"Total: {len(results)}  |  Passed: {passed}  |  Failed: {failed}",
    "",
    f"{'ID':<6} {'CF':<14} {'STATUS':<6} {'TIME':>6}  NOTES",
    "-" * 60,
]
for r in results:
    first_note = r["notes"][0] if r["notes"] else ""
    summary_lines.append(
        f"{r['id']:<6} {r['cf']:<14} {r['status']:<6} {r['elapsed']:>5}s  {first_note[:45]}"
    )

summary_lines += [
    "",
    f"Output files are in: {REVIEW_DIR}/",
    "Each subfolder contains: prompt.txt, workflow.json, workflow.xml, result.txt",
]

summary_text = "\n".join(summary_lines)
print(summary_text)
(REVIEW_DIR / "summary.txt").write_text(summary_text, encoding="utf-8")
print(f"\nSummary written to {REVIEW_DIR}/summary.txt")