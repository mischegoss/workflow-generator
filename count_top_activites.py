"""
count_top_activities.py
=======================
Counts individual activity frequency AND adjacent pair frequency across
the full XML corpus.

Two metrics per activity:
  workflow_count  — number of distinct workflows containing this activity
  instance_count  — total instances across all workflows

workflow_count is the primary signal. It's not inflated by large looping
workflows that repeat one activity many times.

For pairs, adjacency means the two activities appear consecutively in the
document-order sequence of leaf activities (containers stripped).

Run from the repo root:
    python count_top_activities.py

Or specify a different XML directory:
    python count_top_activities.py --xml-dir /path/to/workflows_raw/xml

Output:
  - Prints top 30 individual activities and top 30 pairs to terminal
  - Writes full results to data/activity_frequency.json
"""

import argparse
import html
import json
import re
from collections import Counter
from pathlib import Path


# Structural containers — excluded from individual and pair counts.
# These are scaffolding, not discrete operations.
STRUCTURAL = {
    "SequentialWorkflowActivity",
    "SequenceActivity",
    "IfElseActivity",
    "IfElseBranchActivity",
    "WhileActivity",
    "ParallelActivity",
    "UserGroup",
    "ForEachActivity",
    "ExitWhile",
    "ReturnValue",
    "WorkflowInfo",
}

# Matches any activity tag that has an x:Name attribute (i.e. a real activity
# node, not a bare XML element). Group 1 = activity TypeName.
ACTIVITY_RE = re.compile(r'<([A-Z][A-Za-z0-9_]+)\s[^>]*x:Name="[^"]*"')


def load_xoml(path: Path) -> str | None:
    """Load and double-unescape an XML file to get the raw XOML string."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        return html.unescape(html.unescape(raw))
    except Exception:
        return None


def extract_leaf_sequence(xoml: str) -> list[str]:
    """
    Return the document-order sequence of non-structural activity TypeNames.
    This is the flat sequence used for pair extraction — containers stripped,
    leaf activities only, in the order they appear in the XML.
    """
    return [
        m.group(1)
        for m in ACTIVITY_RE.finditer(xoml)
        if m.group(1) not in STRUCTURAL
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Count top individual activities and adjacent pairs in corpus"
    )
    parser.add_argument(
        "--xml-dir",
        default="./workflows_raw/xml",
        help="Path to directory containing workflow XML files (default: ./workflows_raw/xml)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="How many top entries to print for both tables (default: 30)",
    )
    parser.add_argument(
        "--output",
        default="./data/activity_frequency.json",
        help="Where to write the full results JSON (default: ./data/activity_frequency.json)",
    )
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        print(f"ERROR: Directory not found: {xml_dir}")
        return

    xml_files = sorted(xml_dir.glob("*.xml"))
    print(f"Found {len(xml_files)} XML files in {xml_dir}")
    print("Processing...", flush=True)

    # Individual activity counts
    wf_count: Counter  = Counter()   # binary: does this workflow contain this activity?
    inst_count: Counter = Counter()  # total instances across all workflows

    # Pair counts (adjacent in document order, per-workflow binary)
    pair_wf_count: Counter  = Counter()   # how many workflows contain this pair?
    pair_inst_count: Counter = Counter()  # total adjacent pair occurrences

    n_ok = 0
    n_fail = 0

    for i, fpath in enumerate(xml_files):
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(xml_files)}...", flush=True)

        xoml = load_xoml(fpath)
        if not xoml:
            n_fail += 1
            continue

        seq = extract_leaf_sequence(xoml)
        if not seq:
            n_fail += 1
            continue

        n_ok += 1

        # Individual counts
        for act in set(seq):
            wf_count[act] += 1
        for act in seq:
            inst_count[act] += 1

        # Adjacent pair counts
        seen_pairs = set()
        for j in range(len(seq) - 1):
            pair = (seq[j], seq[j + 1])
            pair_inst_count[pair] += 1
            if pair not in seen_pairs:
                pair_wf_count[pair] += 1
                seen_pairs.add(pair)

    print(f"\nParsed: {n_ok} OK  |  {n_fail} failed/empty/skipped")
    print(f"Unique activity types:  {len(wf_count)}")
    print(f"Unique adjacent pairs:  {len(pair_wf_count)}")
    print()

    # ── Individual activities table ──────────────────────────────────────────
    sorted_acts = sorted(
        wf_count.keys(),
        key=lambda a: (-wf_count[a], -inst_count[a])
    )

    print(f"TOP {args.top} INDIVIDUAL ACTIVITIES  (by workflow count, out of {n_ok})")
    print("-" * 70)
    print(f"{'#':>3}  {'Activity':<38} {'Workflows':>9}  {'%':>5}  {'Instances':>9}")
    print("-" * 70)
    for i, act in enumerate(sorted_acts[:args.top], 1):
        wc  = wf_count[act]
        ic  = inst_count[act]
        pct = round(100 * wc / n_ok, 1) if n_ok else 0
        print(f"{i:>3}. {act:<38} {wc:>9}  {pct:>4.1f}%  {ic:>9}")
    print("-" * 70)
    print()

    # ── Adjacent pairs table ─────────────────────────────────────────────────
    sorted_pairs = sorted(
        pair_wf_count.keys(),
        key=lambda p: (-pair_wf_count[p], -pair_inst_count[p])
    )

    print(f"TOP {args.top} ADJACENT PAIRS  (by workflow count, out of {n_ok})")
    print("-" * 85)
    print(f"{'#':>3}  {'Activity A':<30}  {'Activity B':<30} {'Workflows':>9}  {'%':>5}")
    print("-" * 85)
    for i, (a, b) in enumerate(sorted_pairs[:args.top], 1):
        wc  = pair_wf_count[(a, b)]
        pct = round(100 * wc / n_ok, 1) if n_ok else 0
        print(f"{i:>3}. {a:<30}  {b:<30} {wc:>9}  {pct:>4.1f}%")
    print("-" * 85)
    print()

    # ── Write JSON ───────────────────────────────────────────────────────────
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    act_results = [
        {
            "activity":       act,
            "workflow_count": wf_count[act],
            "workflow_pct":   round(100 * wf_count[act] / n_ok, 1) if n_ok else 0,
            "instance_count": inst_count[act],
        }
        for act in sorted_acts
    ]

    pair_results = [
        {
            "activity_a":     a,
            "activity_b":     b,
            "workflow_count": pair_wf_count[(a, b)],
            "workflow_pct":   round(100 * pair_wf_count[(a, b)] / n_ok, 1) if n_ok else 0,
            "instance_count": pair_inst_count[(a, b)],
        }
        for (a, b) in sorted_pairs
    ]

    output_data = {
        "total_workflows":       n_ok,
        "unique_activity_types": len(wf_count),
        "unique_pairs":          len(pair_wf_count),
        "individual_activities": act_results,
        "adjacent_pairs":        pair_results,
    }

    output_path.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
    print(f"Full results written to: {output_path}")
    print(f"  {len(act_results)} activity types")
    print(f"  {len(pair_results)} adjacent pairs")


if __name__ == "__main__":
    main()