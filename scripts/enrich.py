#!/usr/bin/env python3
"""
enrich_catalog_activities.py (lift-scored version)

Enriches draft_task_catalog.json with activity analysis, using LIFT rather
than raw containment to rank activities. This fixes the connective-tissue
problem in the first version: MemorySet/GetCellValue/DisplayValue appear
in ~80% of all workflows so they dominate raw containment rankings but
carry no task-level signal. Lift asks the right question — "is this
activity disproportionately common in THIS task vs the corpus?"

  lift = task_containment_rate / corpus_containment_rate
      1.0  → equally common here and everywhere (no signal)
      2.0  → 2x more common in this task than corpus average
      5.0+ → highly distinctive of this task

Inclusion rules for typical_activities:
  - lift >= --min-lift (default 1.5)
  - task_containment >= --min-containment (default 0.5)
  - appears in at least 2 workflows absolute
  - at most --max-activities per task

Each task also gets a confidence label based on how many of its example
workflows have sequence data:
  high:     >= 8 sequences — typical_activities is reliable
  medium:   4-7 sequences — approximate, sanity-check against examples
  low:      2-3 sequences — best-guess; inspect example_filenames directly
  very_low: 0-1 sequences — no useful analysis possible

Usage:
    python enrich_catalog_activities.py
    python enrich_catalog_activities.py --min-lift 2.0 --min-containment 0.4

Stdlib only.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


STRUCTURAL_ACTIVITIES = {
    "SequentialWorkflowActivity", "SequenceActivity", "WhileActivity",
    "IfElseActivity", "IfElseBranchActivity", "ParallelActivity",
    "UserGroup", "ForEachActivity", "ExitWhile", "ReturnValue",
    "Continue", "WorkflowInfo",
}


def compute_corpus_containment(
    seq_by_file: dict[str, list[str]],
) -> tuple[dict[str, int], int]:
    """
    One pass over the full sequences file: count how many workflows
    contain each non-structural activity at least once.
    """
    containment: Counter = Counter()
    total = 0
    for seq in seq_by_file.values():
        total += 1
        seen = set()
        for act in seq:
            if act in STRUCTURAL_ACTIVITIES:
                continue
            seen.add(act)
        for act in seen:
            containment[act] += 1
    return dict(containment), total


def confidence_label(n_sequences: int) -> str:
    if n_sequences >= 8:
        return "high"
    if n_sequences >= 4:
        return "medium"
    if n_sequences >= 2:
        return "low"
    return "very_low"


def analyze_task(
    example_files: list[str],
    seq_by_file: dict[str, list[str]],
    corpus_containment: dict[str, int],
    corpus_total: int,
    min_lift: float,
    min_containment: float,
    max_activities: int,
) -> dict:
    """
    For each activity appearing in the task's example workflows, compute
    lift relative to corpus-wide containment, then select typical_activities
    by the threshold rules.
    """
    found: list[list[str]] = []
    missing: list[str] = []
    for fname in example_files:
        seq = seq_by_file.get(fname)
        if seq is None:
            missing.append(fname)
        else:
            found.append(seq)

    n_task = len(found)
    result = {
        "typical_activities": [],
        "confidence":         confidence_label(n_task),
        "examples_analyzed":  n_task,
        "examples_missing":   len(missing),
        "missing_files":      missing[:5],
        "activities":         [],
    }
    if n_task == 0:
        return result

    task_containment: Counter = Counter()
    task_total_instances: Counter = Counter()
    for seq in found:
        seen = set()
        for act in seq:
            if act in STRUCTURAL_ACTIVITIES:
                continue
            task_total_instances[act] += 1
            seen.add(act)
        for act in seen:
            task_containment[act] += 1

    entries = []
    for act, t_cnt in task_containment.items():
        task_rate = t_cnt / n_task
        corpus_cnt = corpus_containment.get(act, 0)
        corpus_rate = corpus_cnt / corpus_total if corpus_total else 0.0
        if corpus_rate > 0:
            lift = task_rate / corpus_rate
        else:
            # Activity exists in task but not corpus baseline — max distinctive
            lift = 999.0
        entries.append({
            "activity":               act,
            "lift":                   round(lift, 2),
            "task_containment_pct":   round(task_rate * 100, 1),
            "corpus_containment_pct": round(corpus_rate * 100, 1),
            "task_workflows":         t_cnt,
            "total_instances":        task_total_instances[act],
            "avg_per_workflow":       round(task_total_instances[act] / n_task, 1),
        })

    # Rank by lift desc, tiebreak by task_containment desc, avg_per_wf desc
    entries.sort(key=lambda e: (
        -e["lift"], -e["task_containment_pct"], -e["avg_per_workflow"]
    ))

    typical = []
    min_abs = 2  # absolute floor: activity must appear in >= 2 workflows
    for e in entries:
        if len(typical) >= max_activities:
            break
        if (e["lift"] >= min_lift
                and e["task_containment_pct"] >= min_containment * 100
                and e["task_workflows"] >= min_abs):
            typical.append(e["activity"])

    result["typical_activities"] = typical
    result["activities"]         = entries[:15]  # top 15 by lift for inspection
    return result


def enrich_catalog(
    catalog: dict,
    seq_by_file: dict[str, list[str]],
    corpus_containment: dict[str, int],
    corpus_total: int,
    min_lift: float,
    min_containment: float,
    max_activities: int,
) -> dict:
    stats = {"tasks_high": 0, "tasks_medium": 0,
             "tasks_low": 0,  "tasks_very_low": 0,
             "typical_populated": 0, "typical_empty": 0,
             "total_missing_files": 0}
    for list_key in ("verb_rollups", "action_tasks", "domain_tasks"):
        for task in catalog.get(list_key, []):
            examples = task.get("example_filenames", [])
            if not examples:
                task["activity_analysis"] = {
                    "confidence":       "very_low",
                    "examples_analyzed": 0,
                    "activities":       [],
                }
                task["typical_activities"] = []
                stats["tasks_very_low"] += 1
                stats["typical_empty"] += 1
                continue

            analysis = analyze_task(
                examples, seq_by_file,
                corpus_containment, corpus_total,
                min_lift, min_containment, max_activities,
            )
            task["typical_activities"] = analysis["typical_activities"]
            task["activity_analysis"] = {
                "confidence":         analysis["confidence"],
                "examples_analyzed":  analysis["examples_analyzed"],
                "examples_missing":   analysis["examples_missing"],
                "missing_files":      analysis["missing_files"],
                "activities":         analysis["activities"],
            }
            stats[f"tasks_{analysis['confidence']}"] += 1
            if analysis["typical_activities"]:
                stats["typical_populated"] += 1
            else:
                stats["typical_empty"] += 1
            stats["total_missing_files"] += analysis["examples_missing"]
    return stats


def print_report(
    catalog: dict, stats: dict, corpus_total: int,
    min_lift: float, min_containment: float,
) -> None:
    print("=" * 95)
    print(f"CORPUS BASELINE: {corpus_total} workflows")
    print(f"THRESHOLDS: lift >= {min_lift}, "
          f"task_containment >= {int(min_containment * 100)}%, "
          f">= 2 absolute workflows")
    print("=" * 95)
    print()

    print("=" * 95)
    print("CONFIDENCE DISTRIBUTION")
    print("=" * 95)
    print(f"  high (>= 8 seq):      {stats['tasks_high']}  (reliable)")
    print(f"  medium (4-7 seq):     {stats['tasks_medium']}  (approximate)")
    print(f"  low (2-3 seq):        {stats['tasks_low']}  (inspect manually)")
    print(f"  very_low (0-1 seq):   {stats['tasks_very_low']}  (no analysis)")
    print(f"  typical populated:    {stats['typical_populated']}")
    print(f"  typical empty:        {stats['typical_empty']}")
    print(f"  missing file lookups: {stats['total_missing_files']}")
    print()

    all_tasks = (catalog.get("action_tasks", [])
                 + catalog.get("domain_tasks", []))
    by_conf: dict[str, list] = {"high": [], "medium": [], "low": []}
    for t in all_tasks:
        conf = t.get("activity_analysis", {}).get("confidence", "very_low")
        if conf in by_conf:
            by_conf[conf].append(t)
    for k in by_conf:
        by_conf[k].sort(key=lambda t: -t.get("workflow_count", 0))
    samples = by_conf["high"][:6] + by_conf["medium"][:4] + by_conf["low"][:3]

    print("=" * 95)
    print("SAMPLE ENRICHMENTS (mixed confidence)")
    print("=" * 95)
    for t in samples:
        analysis = t.get("activity_analysis", {})
        n = analysis.get("examples_analyzed", 0)
        conf = analysis.get("confidence", "?")
        typical = set(t.get("typical_activities", []))
        print(f"\n  {t['task_id']}  "
              f"({t['workflow_count']} wf, {n} with sequences, {conf})")
        entries = analysis.get("activities", [])[:8]
        if not entries:
            print("    (no activity data)")
            continue
        print(f"    {'Activity':<32} {'Lift':>6}  {'Task%':>6}  {'Corp%':>6}  {'Avg':>5}")
        for e in entries:
            mark = "*" if e["activity"] in typical else " "
            lift_disp = ">100" if e["lift"] > 100 else f"{e['lift']:.2f}"
            print(f"  {mark} {e['activity']:<32} "
                  f"{lift_disp:>6}  {e['task_containment_pct']:>5.1f}%  "
                  f"{e['corpus_containment_pct']:>5.1f}%  "
                  f"{e['avg_per_workflow']:>5.1f}")

    print()
    print("  (* = included in typical_activities)")
    print()
    print("=" * 95)
    print("HOW TO READ THIS")
    print("=" * 95)
    print("  Lift > 1.0 : activity is more common in this task than in the corpus.")
    print("  Lift < 1.0 : activity is LESS common here than average — connective tissue.")
    print("  Lift = 999 : activity doesn't appear in corpus baseline — maximally rare.")
    print()
    print("  For HIGH-confidence tasks, trust typical_activities.")
    print("  For MEDIUM, sanity-check against example_filenames.")
    print("  For LOW, typical_activities is a guess — look at examples directly.")
    print()
    print("  Tune with:")
    print(f"    --min-lift        (current: {min_lift})  higher = stricter")
    print(f"    --min-containment (current: {min_containment})  higher = stricter")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enrich draft task catalog with lift-scored activities."
    )
    parser.add_argument("--catalog", default="./draft_task_catalog.json")
    parser.add_argument(
        "--sequences", default="./data/mined_activity_sequences.json"
    )
    parser.add_argument("--out", default="./draft_task_catalog_enriched.json")
    parser.add_argument(
        "--min-lift", type=float, default=1.5,
        help="Min lift for typical_activities (default: 1.5)",
    )
    parser.add_argument(
        "--min-containment", type=float, default=0.5,
        help="Min task containment fraction (default: 0.5)",
    )
    parser.add_argument(
        "--max-activities", type=int, default=8,
        help="Max activities per task (default: 8)",
    )
    args = parser.parse_args()

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        print(f"ERROR: catalog {catalog_path} not found.", file=sys.stderr)
        return 1
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    seq_path = Path(args.sequences)
    if not seq_path.exists():
        alt = Path("./mined_activity_sequences.json")
        if alt.exists():
            seq_path = alt
            print(f"Note: using {alt}", file=sys.stderr)
        else:
            print(f"ERROR: sequences {seq_path} not found.", file=sys.stderr)
            return 1

    seq_raw = json.loads(seq_path.read_text(encoding="utf-8"))
    seq_by_file: dict[str, list[str]] = {}
    for entry in seq_raw:
        f = entry.get("file")
        s = entry.get("sequence")
        if f and isinstance(s, list):
            seq_by_file[f] = s

    n_action = len(catalog.get("action_tasks", []))
    n_domain = len(catalog.get("domain_tasks", []))
    print(f"Loaded catalog: {catalog_path.name} "
          f"({n_action} action + {n_domain} domain tasks)")
    print(f"Loaded sequences: {seq_path.name} ({len(seq_by_file)} workflows)")

    corpus_containment, corpus_total = compute_corpus_containment(seq_by_file)
    print(f"Computed corpus baseline: "
          f"{len(corpus_containment)} distinct activities "
          f"across {corpus_total} workflows\n")

    stats = enrich_catalog(
        catalog, seq_by_file, corpus_containment, corpus_total,
        args.min_lift, args.min_containment, args.max_activities,
    )

    out_path = Path(args.out)
    out_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    print_report(catalog, stats, corpus_total,
                 args.min_lift, args.min_containment)
    print(f"\nWrote enriched catalog: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())