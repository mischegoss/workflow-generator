import os
import asyncio
os.environ["DATA_DIR"] = "./data"
os.environ["OUTPUT_DIR"] = "./output"

from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("FULL PIPELINE TEST — Certificate Expiry Workflow")
print("=" * 60)

api_key = os.getenv("ANTHROPIC_API_KEY", "")
if not api_key or api_key == "sk-ant-":
    print("ERROR: ANTHROPIC_API_KEY not set in .env")
    exit(1)

print("\nAPI key found — running full pipeline...")
print("This will take 60-120 seconds...\n")

from main import run

prompt = (
    "Create a workflow that stores expiration dates for security certificates "
    "and sends an email 5 days before a security certificate will expire. "
    "The workflow should loop through each certificate, calculate the days "
    "remaining until expiration, and if 5 or fewer days remain send a reminder email."
)

print(f"PROMPT: {prompt}")
print()

result = asyncio.run(run(prompt))

print("=" * 60)
print("PIPELINE OUTPUT:")
print("=" * 60)
print(result)
print()

# Find the output file
output_dir = "./output"
xml_files = [f for f in os.listdir(output_dir) if f.endswith(".xml")]
xml_files.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)

if xml_files:
    latest = xml_files[0]
    latest_path = os.path.join(output_dir, latest)
    print("=" * 60)
    print(f"LATEST XML FILE: {latest_path}")
    print("=" * 60)
    with open(latest_path) as f:
        xml_content = f.read()
    print(xml_content[:3000])
    if len(xml_content) > 3000:
        print(f"\n... ({len(xml_content)} total chars)")

    # Also copy to project root for easy access
    root_copy = f"./pipeline_output_{latest}"
    with open(root_copy, "w") as f:
        f.write(xml_content)
    print(f"\nCopied to project root: {root_copy}")
    print("Import this file into Resolve Actions.")
else:
    print("No XML file found in ./output/")
    print("Pipeline may have returned an error — check output above.")