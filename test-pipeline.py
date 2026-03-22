import os
import asyncio
import time
import random
import re
os.environ["DATA_DIR"] = "./data"
os.environ["OUTPUT_DIR"] = "./output/pipeline"
os.makedirs("./output/pipeline", exist_ok=True)

from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("FULL PIPELINE TEST — Certificate Expiry Workflow")
print("=" * 60)

api_key = os.getenv("ANTHROPIC_API_KEY", "")
if not api_key or api_key == "sk-ant-":
    print("ERROR: ANTHROPIC_API_KEY not set in .env")
    exit(1)

# Unique name for this run
forced_name = f"WF_{int(time.time())}_{random.randint(1000, 9999)}"
print(f"\nWorkflow name this run: {forced_name}")
print("Running pipeline — 60-120 seconds...\n")

from main import run

prompt = (
    "Create a workflow that stores expiration dates for security certificates "
    "and sends an email 5 days before a security certificate will expire. "
    "The workflow should loop through each certificate, calculate the days "
    "remaining until expiration, and if 5 or fewer days remain send a reminder email."
)

result = asyncio.run(run(prompt))

print("=" * 60)
print("PIPELINE OUTPUT:")
print("=" * 60)
print(result)
print()

# Find the latest file in the pipeline output dir
output_dir = "./output/pipeline"
xml_files = [f for f in os.listdir(output_dir) if f.endswith(".xml")]
xml_files.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)

if xml_files:
    latest_path = os.path.join(output_dir, xml_files[0])
    with open(latest_path, encoding="utf-8") as f:
        xml_content = f.read()

    # Force the unique name into the XML
    xml_content = re.sub(r'Name="[^"]*"', f'Name="{forced_name}"', xml_content, count=1)
    xml_content = re.sub(r'Pnumber="[^"]*"', f'Pnumber="{random.randint(50000,99999)}"', xml_content, count=1)

    # Write ONLY to project root — this is the file to import
    root_path = f"./{forced_name}.xml"
    with open(root_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    print(f"IMPORT THIS FILE: {root_path}")
    print(f"File size: {len(xml_content)} bytes")
    print()
    print(xml_content[:2000])
else:
    print("ERROR: No XML file found in ./output/pipeline/")
    print("Check pipeline output above for errors.")