#!/usr/bin/env python3
"""
analyze_sequence_distribution.py

Measures how concentrated the activity co-occurrence distribution is in
mined_activity_sequences.json. Answers the question:

    "Is the head fat enough to support a task catalog?"

Decision rule on the 50th-ranked 3-gram:
    15+ times -> FAT HEAD, build the catalog (~40-80 tasks)
     6-14     -> MODERATE, catalog viable with care (~25-50 tasks)
     2-5      -> MARGINAL, use hybrid (categories + LLM-generated labels)
     1        -> TAIL-DOMINATED, skip catalog, categories-only preview

Usage:
    python analyze_sequence_distribution.py
    DATA_DIR=/path/to/data python analyze_sequence_distribution.py

Stdlib only. No deps.
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path


def load_sequences(path: Path) -> list[list[str]]:
    """Return [[activity, activity, ...], ...] — one list per workflow."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    sequences = []
    for entry in data:
        seq = entry.get("sequence")
        if isinstance(seq, list) and seq:
            sequences.append(seq)
    return sequences


def ngrams(seq: list[str], n: int):
    """Yield all n-grams from seq as tuples."""
    for i in range(len(seq) - n + 1):
        yield tuple(seq[i:i + n])


def count_ngrams(sequences: list[list[str]], n: int):
    """
    Count n-grams two ways:
      - instance_count: total occurrences across the corpus
      - workflow_count: number of distinct workflows it appears in
    """
    instance_counter = Counter()
    workflow_counter = Counter()
    total = 0
    for seq in sequences:
        seen = set()
        for gram in ngrams(seq, n):
            instance_counter[gram] += 1
            total += 1
            if gram not in seen:
                workflow_counter[gram] += 1
                seen.add(gram)
    return instance_counter, workflow_counter, total


def print_top(title, inst, wkfl, total, top_k=50):
    print()
    print("=" * 90)
    print(title)
    print("=" * 90)
    print(f"Unique n-grams:       {len(inst):,}")
    print(f"Total n-gram instances: {total:,}")
    if not inst:
        print("(none)")
        return
    top = inst.most_common(top_k)
    print()
    print(f"{'Rank':<5} {'Inst':<6} {'Wflw':<6} {'Cum%':<7} Sequence")
    print("-" * 90)
    cumulative = 0
    for rank, (gram, count) in enumerate(top, start=1):
        cumulative += count
        cum_pct = (cumulative / total) * 100 if total else 0
        seq_str = " \u2192 ".join(gram)
        print(f"{rank:<5} {count:<6} {wkfl[gram]:<6} {cum_pct:<6.1f}% {seq_str}")

    # Headline number for the decision
    if len(top) >= top_k:
        print()
        print(f"*** Rank-{top_k} frequency: {top[top_k - 1][1]} ***")


def interpret(inst3: Counter, top_k: int = 50) -> None:
    print()
    print("=" * 90)
    print("INTERPRETATION (based on 3-gram distribution)")
    print("=" * 90)
    top = inst3.most_common(top_k)
    if len(top) < top_k:
        print(f"Corpus has only {len(top)} distinct 3-grams — fewer than {top_k}.")
        print("Corpus is too small or too diverse for sequence-mined tasks.")
        print("=> Use categories-only preview, or hybrid with LLM-generated labels.")
        return
    kth = top[top_k - 1][1]
    print(f"The 50th most common 3-gram appears {kth} times.")
    print()
    if kth >= 15:
        print(">>> FAT HEAD — task catalog approach is well-supported.")
        print("    Mine tasks from the top 50-80 3-grams plus high-frequency 2-grams.")
        print("    Target: 40-80 task definitions.")
    elif kth >= 6:
        print(">>> MODERATE HEAD — task catalog viable with care.")
        print("    Mine tasks only from 3-grams appearing 5+ times.")
        print("    Target: 25-50 tasks. Plan a meaningful 'custom' fallback.")
    elif kth >= 2:
        print(">>> MARGINAL — static catalog will have a bloated tail.")
        print("    Recommendation: HYBRID. Categories deterministic (file #1),")
        print("    task labels LLM-generated per call. Skip the static catalog.")
    else:
        print(">>> TAIL-DOMINATED — task catalog is fragile.")
        print("    Recommendation: skip the catalog. Categories-only preview.")


def main():
    data_dir = os.getenv("DATA_DIR", "./data")
    path = Path(data_dir) / "mined_activity_sequences.json"
    if not path.exists():
        print(f"ERROR: {path} not found.", file=sys.stderr)
        print("Set DATA_DIR or run from the project root.", file=sys.stderr)
        sys.exit(1)

    sequences = load_sequences(path)
    if not sequences:
        print(f"ERROR: no sequences in {path}", file=sys.stderr)
        sys.exit(1)

    lens = [len(s) for s in sequences]
    print(f"Loaded {len(sequences)} workflow sequences from {path}")
    print(f"Workflow length: min={min(lens)}, max={max(lens)}, "
          f"mean={sum(lens)/len(lens):.1f}, median={sorted(lens)[len(lens)//2]}")

    # Run 2, 3, 4. 3 is the decisive one; 2 and 4 give shape context for free.
    results = {}
    for n in (2, 3, 4):
        inst, wkfl, total = count_ngrams(sequences, n)
        results[n] = (inst, wkfl, total)
        print_top(f"TOP 50 {n}-ACTIVITY SEQUENCES", inst, wkfl, total, top_k=50)

    interpret(results[3][0], top_k=50)


if __name__ == "__main__":
    main()