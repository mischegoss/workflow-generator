#!/usr/bin/env python3
"""
analyze_workflow_names.py

Measures how descriptive the real workflow corpus FILENAMES are.

Why filenames? The XOML-level <SequentialWorkflowActivity x:Name="..."> is
always "CustomWorkflow" (platform default) and provides no task signal.
The filename is where authors put human vocabulary.

Answers: "Are workflow filenames descriptive enough to ground a task taxonomy?"

Decision rule on % descriptive filenames:
    60%+ -> STRONG, cluster filenames into ~40 task names, reliable ground truth
    30-60 -> USABLE, use as seed + validate against sequences, + second-pass on
             activity Description fields
    <30  -> WEAK, filenames cannot anchor the catalog; use sequence-mined skeleton
             with LLM-generated labels (the hybrid)

Usage:
    python analyze_workflow_names.py
    python analyze_workflow_names.py --xml-dir ./workflows_raw/xml
    python analyze_workflow_names.py --sample 30

Stdlib only.
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path


# Single-word filenames that carry no task information.
# Even if long, these don't describe what the workflow does.
GENERIC_EXACT = {
    "customworkflow", "workflow", "untitled", "test", "temp", "tmp",
    "globals", "main", "wf", "wf1", "copy", "backup", "old", "new",
    "logger", "template", "scratch", "debug", "placeholder", "misc",
    "junk", "draft", "sample", "default", "child", "parent",
}

# Markers indicating engineering/versioning noise rather than task description.
# Hit any of these and we classify as "coded" (usable but noisy).
CODED_PATTERNS = [
    re.compile(r"\bv\d+\b", re.I),            # v1, v2, v3
    re.compile(r"\b(wip|todo|draft)\b", re.I),
    re.compile(r"\bcopy\s*of\b", re.I),
    re.compile(r"\b(updated|final|latest)\b", re.I),
    re.compile(r"_\d{8}\b"),                  # _20241204 (date)
    re.compile(r"_\d{6}\b"),                  # _241204
    re.compile(r"\btest\d*\b", re.I),
]

# Action verbs commonly starting a descriptive workflow name.
# Used as a secondary signal: "% of names starting with a verb" tells us
# how task-like the naming convention is.
COMMON_VERBS = {
    "create", "delete", "update", "get", "send", "check", "monitor", "list",
    "sync", "import", "export", "compare", "copy", "move", "run", "find",
    "notify", "alert", "add", "remove", "convert", "process", "generate",
    "validate", "approve", "reject", "start", "stop", "restart", "reset",
    "read", "write", "fetch", "query", "search", "filter", "parse",
    "extract", "load", "save", "restore", "clean", "clear", "scan",
    "retrieve", "deploy", "enable", "disable", "install", "uninstall",
    "purge", "archive", "refresh", "lock", "unlock", "assign", "close",
    "open", "attach", "detach", "build", "trigger", "publish", "schedule",
    "report", "audit", "verify", "onboard", "offboard", "provision",
}


def clean_filename(name: str) -> str:
    """
    Strip the numeric ID prefix and extension.

    Examples:
        "9_Monitor windows event log.xml"                  -> "Monitor windows event log"
        "8309__Parent_ - NEW Dynamic AD GROUP MGMT.xml"   -> "Parent_ - NEW Dynamic AD GROUP MGMT"
        "900_getVmNameFromIP.xml"                          -> "getVmNameFromIP"
        "CustomWorkflow.xml"                               -> "CustomWorkflow"
    """
    stem = re.sub(r"\.xml$", "", name, flags=re.I)
    # Strip leading "N_" or "N__" where N is digits (e.g. "9_", "8309__")
    stem = re.sub(r"^\d+_+", "", stem)
    return stem.strip()


def words_from(name: str) -> list[str]:
    """
    Extract lowercase word tokens (2+ chars) from a name. Splits on
    separators AND on camelCase boundaries so 'getVmNameFromIP' becomes
    ['get', 'vm', 'name', 'from', 'ip'].
    """
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return re.findall(r"[A-Za-z]{2,}", spaced.lower())


def classify(cleaned: str) -> str:
    """Return one of: 'descriptive', 'coded', 'generic'."""
    if not cleaned:
        return "generic"

    low = cleaned.lower().strip()
    if low in GENERIC_EXACT:
        return "generic"

    words = words_from(cleaned)
    if len(words) <= 1:
        # Single-word (or no-word) names aren't descriptive enough.
        return "generic"

    # Coded markers take priority — the name has information but is polluted.
    for pat in CODED_PATTERNS:
        if pat.search(cleaned):
            return "coded"

    # 2+ words with at least one 4+-char word → descriptive
    if any(len(w) >= 4 for w in words):
        return "descriptive"

    # 2+ short words (all <4 chars) → not enough substance
    return "generic"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure how descriptive workflow filenames are."
    )
    parser.add_argument(
        "--xml-dir",
        default="./workflows_raw/xml",
        help="Directory containing workflow XML files. Searched recursively. "
             "(default: ./workflows_raw/xml)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help="Number of example names to print per bucket (default: 20)",
    )
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        # Try a couple of common fallback paths before giving up
        for alt in (Path("./workflows_raw"),
                    Path("./data/workflows_raw"),
                    Path("./data/workflows_raw/xml")):
            if alt.exists():
                print(f"Note: {args.xml_dir} not found, using {alt}", file=sys.stderr)
                xml_dir = alt
                break
        else:
            print(f"ERROR: {args.xml_dir} not found. Pass --xml-dir <path>.",
                  file=sys.stderr)
            return 1

    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files found under {xml_dir}", file=sys.stderr)
        return 1

    print(f"Found {len(xml_files)} XML files under {xml_dir}")
    print()

    buckets: dict[str, list[tuple[str, str]]] = {
        "descriptive": [], "coded": [], "generic": []
    }
    first_words: Counter = Counter()
    follow_words: Counter = Counter()

    for path in xml_files:
        cleaned = clean_filename(path.name)
        bucket = classify(cleaned)
        buckets[bucket].append((path.name, cleaned))

        words = words_from(cleaned)
        if words:
            first_words[words[0]] += 1
            for w in words[1:]:
                if len(w) >= 3:
                    follow_words[w] += 1

    total = len(xml_files)

    # ── Bucket distribution ──────────────────────────────────────────────────
    print("=" * 90)
    print("FILENAME CLASSIFICATION")
    print("=" * 90)
    for b in ("descriptive", "coded", "generic"):
        n = len(buckets[b])
        pct = n / total * 100
        print(f"  {b:<12} {n:>5}  ({pct:>5.1f}%)")
    print()

    # ── Samples per bucket ───────────────────────────────────────────────────
    for bucket in ("descriptive", "coded", "generic"):
        entries = buckets[bucket]
        if not entries:
            continue
        print("=" * 90)
        print(f"SAMPLE: {bucket.upper()} "
              f"({min(args.sample, len(entries))} of {len(entries)})")
        print("=" * 90)
        # Stride sampling so we don't just see alphabetical A-names
        step = max(1, len(entries) // args.sample)
        shown = entries[::step][:args.sample]
        for fname, cleaned in shown:
            fname_short = fname if len(fname) <= 50 else fname[:47] + "..."
            print(f"  {cleaned!r:<60}  <- {fname_short}")
        print()

    # ── First-word frequency (verb analysis) ─────────────────────────────────
    print("=" * 90)
    print("TOP FIRST WORDS (after stripping numeric ID prefix)")
    print("=" * 90)
    verb_count = 0
    top_firsts = first_words.most_common(25)
    for word, count in top_firsts:
        is_verb = word in COMMON_VERBS
        marker = "VERB" if is_verb else "    "
        print(f"  [{marker}] {word:<20} {count:>5}")
        if is_verb:
            verb_count += count
    total_firsts = sum(first_words.values())
    all_verb_total = sum(c for w, c in first_words.items() if w in COMMON_VERBS)
    verb_pct = (all_verb_total / total_firsts * 100) if total_firsts else 0
    print()
    print(f"  First word is an action verb in "
          f"{all_verb_total}/{total_firsts} ({verb_pct:.1f}%) of files")
    print()

    # ── Following-word frequency (domain analysis) ────────────────────────────
    print("=" * 90)
    print("TOP FOLLOWING WORDS (domain / target vocabulary)")
    print("=" * 90)
    for word, count in follow_words.most_common(25):
        print(f"  {word:<25} {count:>5}")
    print()

    # ── Interpretation ───────────────────────────────────────────────────────
    n_desc = len(buckets["descriptive"])
    n_coded = len(buckets["coded"])
    pct_desc = n_desc / total * 100
    pct_coded = n_coded / total * 100
    pct_usable = (n_desc + n_coded) / total * 100

    print("=" * 90)
    print("INTERPRETATION")
    print("=" * 90)
    print(f"Descriptive filenames:   {pct_desc:.1f}%  ({n_desc} files)")
    print(f"Coded but meaningful:    {pct_coded:.1f}%  ({n_coded} files)")
    print(f"Usable total:            {pct_usable:.1f}%")
    print(f"First word is a verb:    {verb_pct:.1f}%")
    print()
    if pct_desc >= 60:
        print(">>> STRONG. Filenames are reliable ground truth for task vocabulary.")
        print("    Next step: cluster the descriptive filenames into ~40 task names.")
        print("    The 'top first words' list above is already a draft of your verb axis;")
        print("    the 'top following words' list is your domain axis.")
    elif pct_desc >= 30:
        print(">>> USABLE. Filenames give a real signal but not enough alone.")
        print("    Next step: seed task names from descriptive filenames, then validate")
        print("    against the top sequence-mined n-grams from your other script.")
        print("    Consider a second pass mining activity Description fields in XML")
        print("    for the remaining ~40-50% where filenames don't carry signal.")
    else:
        print(">>> WEAK. Filenames cannot anchor the task catalog by themselves.")
        print("    Options:")
        print("    - Mine activity-level Description / Subject / ValueToDisplay fields")
        print("      from inside the XML (harder, but that's where the language is)")
        print("    - Skip the name-based catalog entirely; use the sequence-mined")
        print("      skeleton with LLM-generated labels per call (the hybrid)")

    return 0


if __name__ == "__main__":
    sys.exit(main())