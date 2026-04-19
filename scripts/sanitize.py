import re
import os

# Root directory (run from repo root)
ROOT_DIR = "."

# File types to scan
TARGET_EXTENSIONS = (".py",)

# Regex patterns for common secrets
PATTERNS = {
    "OPENAI_API_KEY": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "SLACK_TOKEN": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "GENERIC_LONG_TOKEN": re.compile(r"[A-Za-z0-9_\-]{32,}"),
    "AZURE_SECRET": re.compile(r"[A-Za-z0-9]{40,}"),
}

REPLACEMENTS = {
    "OPENAI_API_KEY": "REMOVED_OPENAI_KEY",
    "SLACK_TOKEN": "REMOVED_SLACK_TOKEN",
    "GENERIC_LONG_TOKEN": "REMOVED_SECRET",
    "AZURE_SECRET": "REMOVED_AZURE_SECRET",
}


def sanitize_file(filepath):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    original_content = content
    changes = []

    for key, pattern in PATTERNS.items():
        matches = pattern.findall(content)
        if matches:
            content = pattern.sub(REPLACEMENTS[key], content)
            changes.append((key, len(matches)))

    if content != original_content:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    return changes


def main():
    total_files_changed = 0

    for root, _, files in os.walk(ROOT_DIR):
        # Skip .git directory
        if ".git" in root:
            continue

        for file in files:
            if file.endswith(TARGET_EXTENSIONS):
                path = os.path.join(root, file)
                changes = sanitize_file(path)

                if changes:
                    total_files_changed += 1
                    print(f"\nUpdated: {path}")
                    for change_type, count in changes:
                        print(f"  - {change_type}: {count} replacements")

    print(f"\nDone. Files modified: {total_files_changed}")


if __name__ == "__main__":
    main()