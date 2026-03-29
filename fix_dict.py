#!/usr/bin/env python3
"""
Patch tools/annotation_tools.py — fix _ensure_dict to handle:
  1. Fenced JSON with trailing text (```json\n{...}\n```\n\nNote: ...)
  2. JSON with trailing prose  ({...}\n\nNote: ...)
  3. Prose before JSON  (Here is...\n{...})

Run from repo root: python3 fix_ensure_dict.py
"""
import pathlib, sys

path = pathlib.Path("tools/annotation_tools.py")
if not path.exists():
    print("ERROR: tools/annotation_tools.py not found. Run from repo root.")
    sys.exit(1)

src = path.read_text(encoding="utf-8")

OLD = '''def _ensure_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.split("\\n", 1)[-1]
            if text.endswith("```"):
                text = text[: text.rfind("```")]
            text = text.strip()
        try:
            result = _json.loads(text)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {}'''

NEW = '''def _ensure_dict(value) -> dict:
    """
    Safely converts LLM string output to dict.
    Handles: clean JSON, markdown-fenced JSON, trailing prose after JSON,
    prose before JSON, fenced JSON with trailing notes.
    Falls back to {} only for genuinely unparseable content (truncated, invalid).
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}

    text = value.strip()

    # Try 1: direct parse (clean output — most common case)
    try:
        result = _json.loads(text)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # Try 2: strip markdown fence (handles trailing text after closing fence)
    if text.startswith("```"):
        text2 = text.split("\\n", 1)[-1].strip()
        # Strip closing fence even when followed by trailing text
        fence_end = text2.rfind("\\n```")
        if fence_end >= 0:
            text2 = text2[:fence_end].strip()
        elif text2.endswith("```"):
            text2 = text2[: text2.rfind("```")].strip()
        try:
            result = _json.loads(text2)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        text = text2

    # Try 3: extract JSON object by matching braces
    # Handles: prose before JSON, trailing notes after closing brace,
    # fenced JSON with trailing text after the fence.
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escape_next = False
        end = -1
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\\\" and in_string:
                escape_next = True
                continue
            if ch == \'"\':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > start:
            try:
                result = _json.loads(text[start:end + 1])
                if isinstance(result, dict):
                    return result
            except Exception:
                pass

    return {}'''

if OLD not in src:
    print("ERROR: Target _ensure_dict not found — already patched or file differs.")
    print("       Check tools/annotation_tools.py manually.")
    sys.exit(1)

patched = src.replace(OLD, NEW, 1)
path.write_text(patched, encoding="utf-8")
print(f"✓ Patched {path}")
print("  _ensure_dict now handles trailing text and prose-before-JSON formats.")