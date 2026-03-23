import os
import asyncio
import time
import random

os.environ["DATA_DIR"] = "./data"

from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("FULL PIPELINE TEST — Server Ping Workflow")
print("=" * 60)

api_key = os.getenv("GOOGLE_API_KEY", "")
if not api_key:
    print("ERROR: GOOGLE_API_KEY not set in .env")
    exit(1)

# Unique name for this import — guaranteed fresh every run
import_name = f"WF_{int(time.time())}_{random.randint(1000, 9999)}"
print(f"\nImport file name this run: {import_name}")
print("Running pipeline — 60-120 seconds...\n")

from main import run

prompt = (
    "Create a workflow that loops through a list of servers stored in a memory table "
    "and pings each server. The table has one column called 'server' containing server "
    "hostnames or IP addresses. For each server, if the ping succeeds display a success "
    "message in the logs. If the ping fails, display a failure message showing the server "
    "name and the ping result."
)

xml_content, chat_response = asyncio.run(run(prompt))

print("=" * 60)
print("PIPELINE OUTPUT:")
print("=" * 60)
print(chat_response)
print()

if xml_content:
    # Stamp the guaranteed-unique import name and pnumber directly into the XML.
    # ComposerAgent already generated a name/pnumber — we override them here
    # so every test run produces a distinctly named importable file.
    import re
    import_pnumber = str(random.randint(50000, 99999))

    xml_content = re.sub(
        r'(<WorkflowInfo[^>]*\s)Pnumber="[^"]*"',
        lambda m: m.group(1) + f'Pnumber="{import_pnumber}"',
        xml_content,
        count=1,
    )
    xml_content = re.sub(
        r'(<WorkflowInfo[^>]*\s)Name="[^"]*"',
        lambda m: m.group(1) + f'Name="{import_name}"',
        xml_content,
        count=1,
    )

    # Write directly to project root — no output/pipeline directory involved
    root_path = f"./{import_name}.xml"
    with open(root_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    print(f"IMPORT THIS FILE: {root_path}")
    print(f"File size: {len(xml_content)} bytes")
    print()
    print(xml_content[:2000])
else:
    print("ERROR: Pipeline did not produce XML output.")
    print("Check pipeline output above for errors.")