import os
import asyncio
import json
import pathlib
import sys

os.environ["DATA_DIR"] = "./data"

from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("FULL PIPELINE TEST — Server Ping Workflow")
print("=" * 60)

api_key = os.getenv("GOOGLE_API_KEY", "")
if not api_key:
    print("ERROR: GOOGLE_API_KEY not set in .env")
    sys.exit(1)

print("Running pipeline — 60-120 seconds...\n")

from main import run

prompt = (
    "Create a workflow that loops through a list of servers stored in a memory table "
    "and pings each server. The table has one column called 'server' containing server "
    "hostnames or IP addresses. For each server, if the ping succeeds display a success "
    "message in the logs. If the ping fails, display a failure message showing the server "
    "name and the ping result."
)

# Pipeline now returns (json_file_path | None, chat_response)
json_path, chat_response = asyncio.run(run(prompt))

print("=" * 60)
print("PIPELINE OUTPUT:")
print("=" * 60)
print(chat_response)
print()

if not json_path:
    print("ERROR: Pipeline did not produce a JSON file.")
    print("Check pipeline output above for errors.")
    sys.exit(1)

json_path = pathlib.Path(json_path)
if not json_path.exists():
    print(f"ERROR: JSON file not found at {json_path}")
    sys.exit(1)

print(f"JSON written: {json_path}  ({json_path.stat().st_size} bytes)")

# ── Convert to XML using convert_to_xml.py ─────────────────────────────────
# convert_to_xml.py validates both the outer TotalExport wrapper and the
# inner Xoml string before writing. If either fails it exits with code 3.
print("\nConverting to XML...")

import subprocess
result = subprocess.run(
    [sys.executable, "convert_to_xml.py", str(json_path)],
    capture_output=True,
    text=True,
)
print(result.stdout.strip())
if result.returncode != 0:
    print("ERROR: XML conversion failed.")
    if result.stderr:
        print(result.stderr.strip())
    sys.exit(result.returncode)

# convert_to_xml.py writes the .xml next to the .json by default
xml_path = json_path.with_suffix(".xml")
if not xml_path.exists():
    print(f"ERROR: Expected XML file not found at {xml_path}")
    sys.exit(1)

xml_content = xml_path.read_text(encoding="utf-8")

print(f"\nIMPORT THIS FILE: {xml_path}")
print(f"File size: {len(xml_content)} bytes")
print()
print(xml_content[:2000])