#!/usr/bin/env python3
"""
cluster_workflow_names.py

Clusters descriptive workflow filenames into draft task catalog entries.

Takes as input: the XML directory (filenames only; no XML parsing).
Produces as output:
  1. A human-readable report on stdout.
  2. draft_task_catalog.json — the candidate catalog ready for hand-curation.

Two clustering passes, based on the patterns observed in the corpus:

  A. Action clusters — for verb-first filenames (e.g. "Delete", "Create",
     "Monitor"). Cluster key = (verb, primary_domain_word).

  B. Domain clusters — for noun-first filenames (e.g. "NetBackup Servers
     Table", "VCenter HostInventory"). Cluster key = primary_domain_word.

The output JSON is a DRAFT. Each entry has the shape expected by a real
task catalog (task_id, display_name, workflow_count, example_filenames,
seed trigger_keywords) but fields like `category`, `description`, and
`typical_activities` are intentionally left empty for hand curation.

Usage:
    python cluster_workflow_names.py
    python cluster_workflow_names.py --xml-dir ./workflows_raw/xml
    python cluster_workflow_names.py --min-support 3 --out ./draft_task_catalog.json

Stdlib only.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# ─── Classification (mirrors analyze_workflow_names.py) ────────────────

GENERIC_EXACT = {
    "customworkflow", "workflow", "untitled", "test", "temp", "tmp",
    "globals", "main", "wf", "wf1", "copy", "backup", "old", "new",
    "logger", "template", "scratch", "debug", "placeholder", "misc",
    "junk", "draft", "sample", "default", "child", "parent",
}

CODED_PATTERNS = [
    re.compile(r"\bv\d+\b", re.I),
    re.compile(r"\b(wip|todo|draft)\b", re.I),
    re.compile(r"\bcopy\s*of\b", re.I),
    re.compile(r"\b(updated|final|latest)\b", re.I),
    re.compile(r"_\d{8}\b"),
    re.compile(r"_\d{6}\b"),
    re.compile(r"\btest\d+\b", re.I),
]

COMMON_VERBS = {
    # Original set
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
    "download", "upload", "revoke", "rotate",
    # Added after reviewing first-pass cluster output: these were being
    # treated as domain words, creating mirages like "domain_show",
    # "domain_display", "domain_migrate".
    "show", "display", "set", "return", "combine", "configure", "migrate",
    "collect", "engage", "recover", "acknowledge", "patch", "backup",
    "terminate", "execute", "calculate", "switch", "free", "complete",
    "handle", "manage", "resolve", "dispatch", "ingest", "emit",
    "register", "deregister", "promote", "demote", "enroll", "unenroll",
    "tag", "untag", "grant", "deny", "rename", "initialize", "shutdown",
    "reboot", "mount", "unmount",
}

# ─── Clustering-specific filters ───────────────────────────────────────

# Words that are grammatically necessary but carry no task meaning.
STOPWORDS = {
    # Function words
    "the", "a", "an", "and", "or", "to", "from", "for", "of", "in", "on",
    "at", "by", "with", "as", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "if", "then", "when",
    "where", "which", "all", "any", "each", "every", "some", "no", "not",
    # Generic adjective modifiers (not real domains)
    "new", "old", "latest", "current", "final", "draft", "specific",
    "generic", "common", "various", "default", "high", "low", "bare",
    "powered", "active", "inactive", "empty", "full", "auto", "manual",
    "custom", "dynamic", "automated",
    # Platform / framework artifacts seen in this corpus that created
    # false "domain_*" clusters on the first pass
    "imported", "workflow", "workflows", "child", "parent", "wf",
    "tool", "tools", "activity", "activitylist", "main", "worker", "workers",
    "top", "bottom",
    # Number words
    "one", "two", "three", "first", "second", "third",
}

# Words that are corpus noise — if they appear ANYWHERE in the filename's
# tokens, the file is skipped during clustering. This is catalog hygiene,
# removing scratch/author/test files that the descriptive filter let through.
NOISE_WORD_MARKERS = {
    "test", "testing", "tests", "mytest",
    "gary", "tamar",             # recurring author names in this corpus
    "tmp", "scratch",
}


def clean_filename(name: str) -> str:
    stem = re.sub(r"\.xml$", "", name, flags=re.I)
    stem = re.sub(r"^\d+_+", "", stem)
    return stem.strip()


def _plural_stem(word: str) -> str:
    """
    Naive plural stemmer. Merges snapshots/snapshot, files/file,
    accounts/account, categories/category. Does NOT stem words ending
    in -ss, -us, -is, -os, -as (status, analysis, process, etc.).
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith(
        ("ss", "us", "is", "os", "as", "ys", "hs")
    ):
        return word[:-1]
    return word


def words_from(name: str) -> list[str]:
    """
    Tokenize: split separators AND camelCase boundaries. Handles acronym
    boundaries like 'SCCMget' -> 'SCCM get'.
    """
    # lower→upper: getVm -> get Vm
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    # CAP-run→Mixed: SCCMget -> SCCM get
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return re.findall(r"[A-Za-z]{2,}", spaced.lower())


def classify(cleaned: str) -> str:
    if not cleaned:
        return "generic"
    if cleaned.lower().strip() in GENERIC_EXACT:
        return "generic"
    words = words_from(cleaned)
    if len(words) <= 1:
        return "generic"
    for pat in CODED_PATTERNS:
        if pat.search(cleaned):
            return "coded"
    if any(len(w) >= 4 for w in words):
        return "descriptive"
    return "generic"


# ─── Clustering ────────────────────────────────────────────────────────

def primary_domain_word(words: list[str]) -> str | None:
    """
    First token that isn't a stopword, verb, or too short (<3 chars).
    Returned in stemmed form so 'snapshots' and 'snapshot' cluster together.
    """
    for w in words:
        if w in STOPWORDS or w in COMMON_VERBS or len(w) < 3:
            continue
        return _plural_stem(w)
    return None


def secondary_domain_word(words: list[str], primary: str) -> str | None:
    """Second meaningful content word (after primary), for disambiguation."""
    seen_primary = False
    for w in words:
        stemmed = _plural_stem(w)
        if stemmed == primary and not seen_primary:
            seen_primary = True
            continue
        if w in STOPWORDS or w in COMMON_VERBS or len(w) < 3 or stemmed == primary:
            continue
        return stemmed
    return None


def has_noise_marker(words: list[str]) -> bool:
    """True if any token in the filename signals it's scratch/noise."""
    return any(w in NOISE_WORD_MARKERS for w in words)


def cluster_filenames(xml_files: list[Path]) -> dict:
    """
    Build (verb, domain) action clusters and domain-only clusters.
    """
    action: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    domain: dict[str, list[tuple[str, str]]] = defaultdict(list)

    stats = {
        "total":            0,
        "not_descriptive":  0,
        "skipped_noise":    0,
        "action_clustered": 0,
        "domain_clustered": 0,
        "unclustered":      0,
    }
    unclustered_samples: list[tuple[str, str]] = []

    for path in xml_files:
        stats["total"] += 1
        cleaned = clean_filename(path.name)
        if classify(cleaned) != "descriptive":
            stats["not_descriptive"] += 1
            continue

        words = words_from(cleaned)
        if not words:
            stats["not_descriptive"] += 1
            continue

        if has_noise_marker(words):
            stats["skipped_noise"] += 1
            continue

        first = words[0]

        if first in COMMON_VERBS:
            # Verb-first: cluster by (verb, primary domain word)
            dom = primary_domain_word(words[1:]) or "_generic"
            action[(first, dom)].append((path.name, cleaned))
            stats["action_clustered"] += 1
        else:
            dom = primary_domain_word(words)
            if dom is None:
                stats["unclustered"] += 1
                if len(unclustered_samples) < 20:
                    unclustered_samples.append((path.name, cleaned))
                continue
            domain[dom].append((path.name, cleaned))
            stats["domain_clustered"] += 1

    return {
        "action":  dict(action),
        "domain":  dict(domain),
        "stats":   stats,
        "unclustered_samples": unclustered_samples,
    }


def build_catalog(clusters: dict, min_support: int) -> dict:
    """
    Flatten raw cluster maps into catalog-entry dicts.

    Three output lists:
      - verb_rollups:  per-verb summary (all "create" workflows together,
                       with domain-word breakdown) — useful when (verb, dom)
                       pairs are small but the verb is common
      - action_tasks:  (verb, domain) clusters with support >= min_support
      - domain_tasks:  noun-first clusters with support >= min_support
    """
    verb_rollups = []
    action_tasks = []
    domain_tasks = []

    # ─── Verb roll-ups ─────────────────────────────────────────────────
    verb_to_files: dict[str, list[tuple[str, str]]] = defaultdict(list)
    verb_to_domains: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (verb, dom), files in clusters["action"].items():
        verb_to_files[verb].extend(files)
        verb_to_domains[verb][dom] += len(files)

    for verb, files in sorted(verb_to_files.items(), key=lambda kv: -len(kv[1])):
        if len(files) < min_support:
            continue
        dom_breakdown = dict(sorted(
            verb_to_domains[verb].items(), key=lambda kv: -kv[1]
        ))
        verb_rollups.append({
            "verb":              verb,
            "workflow_count":    len(files),
            "distinct_domains":  len(dom_breakdown),
            "domain_breakdown":  dom_breakdown,
            "example_filenames": [f[0] for f in files],
        })

    # ─── Fine-grained action tasks: (verb, domain) pairs ───────────────
    for (verb, dom), files in sorted(
        clusters["action"].items(), key=lambda kv: -len(kv[1])
    ):
        if len(files) < min_support:
            continue
        has_domain = dom != "_generic"
        display = f"{verb.capitalize()} {dom}" if has_domain else verb.capitalize()
        task_id = f"{verb}_{dom}" if has_domain else verb
        triggers = [verb]
        if has_domain:
            triggers.extend([dom, f"{verb} {dom}"])
        action_tasks.append({
            "task_id":            task_id,
            "display_name":       display,
            "description":        "",            # hand-fill
            "category":           "",            # hand-fill
            "verb":               verb,
            "domain_word":        dom if has_domain else None,
            "workflow_count":     len(files),
            "example_filenames":  [f[0] for f in files],
            "typical_activities": [],            # filled by enrich_catalog_activities.py
            "trigger_keywords":   triggers,
            "cluster_type":       "action",
        })

    # ─── Domain tasks (noun-first) ─────────────────────────────────────
    for dom, files in sorted(
        clusters["domain"].items(), key=lambda kv: -len(kv[1])
    ):
        if len(files) < min_support:
            continue
        display = f"{dom.capitalize()} workflows"
        task_id = f"domain_{dom}"
        domain_tasks.append({
            "task_id":            task_id,
            "display_name":       display,
            "description":        "",
            "category":           "",
            "verb":               None,
            "domain_word":        dom,
            "workflow_count":     len(files),
            "example_filenames":  [f[0] for f in files],
            "typical_activities": [],
            "trigger_keywords":   [dom],
            "cluster_type":       "domain",
        })

    return {
        "verb_rollups": verb_rollups,
        "action_tasks": action_tasks,
        "domain_tasks": domain_tasks,
    }


# ─── Reporting ─────────────────────────────────────────────────────────

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def print_report(catalog: dict, clusters: dict, min_support: int) -> None:
    stats = clusters["stats"]

    print("=" * 90)
    print("CLUSTERING STATS")
    print("=" * 90)
    print(f"  Total files:           {stats['total']}")
    print(f"  Not descriptive:       {stats['not_descriptive']}  (excluded)")
    print(f"  Noise markers:         {stats['skipped_noise']}  (excluded)")
    print(f"  Action-clustered:      {stats['action_clustered']}")
    print(f"  Domain-clustered:      {stats['domain_clustered']}")
    print(f"  Unclustered:           {stats['unclustered']}")
    print()

    # ─── Verb rollups ──────────────────────────────────────────────────
    rollups = catalog["verb_rollups"]
    print("=" * 90)
    print(f"VERB ROLL-UPS — {len(rollups)} verbs with >= {min_support} workflows")
    print("=" * 90)
    if rollups:
        print(f"  {'#':<5} {'Verb':<15} {'Domains':<8} Top domain breakdown")
        print("  " + "-" * 88)
        for r in rollups:
            top3 = list(r["domain_breakdown"].items())[:4]
            breakdown = ", ".join(
                f"{d}={n}" for d, n in top3 if d != "_generic"
            )
            if "_generic" in r["domain_breakdown"]:
                breakdown = (f"bare={r['domain_breakdown']['_generic']}, "
                             + breakdown) if breakdown else \
                            f"bare={r['domain_breakdown']['_generic']}"
            print(f"  {r['workflow_count']:<5} {r['verb']:<15} "
                  f"{r['distinct_domains']:<8} {_truncate(breakdown, 55)}")
    print()

    # ─── Fine-grained action and domain tasks ──────────────────────────
    for heading, key in [("ACTION TASKS (verb + domain pair)", "action_tasks"),
                         ("DOMAIN TASKS (noun-first)",         "domain_tasks")]:
        tasks = catalog[key]
        print("=" * 90)
        print(f"{heading} — min_support={min_support}, {len(tasks)} clusters")
        print("=" * 90)
        if not tasks:
            print("  (none — try lower --min-support)")
        else:
            print(f"  {'#':<5} {'Task ID':<34} {'Display':<30} Examples")
            print("  " + "-" * 88)
            for t in tasks:
                ex = _truncate(", ".join(t["example_filenames"][:2]), 38)
                print(f"  {t['workflow_count']:<5} {t['task_id']:<34} "
                      f"{_truncate(t['display_name'], 30):<30} {ex}")
        print()

    if clusters["unclustered_samples"]:
        print("=" * 90)
        print("UNCLUSTERED SAMPLES (up to 20)")
        print("=" * 90)
        for fname, cleaned in clusters["unclustered_samples"]:
            print(f"  {_truncate(cleaned, 50):<50} <- {_truncate(fname, 40)}")
        print()

    n_rollups = len(catalog["verb_rollups"])
    n_action = len(catalog["action_tasks"])
    n_domain = len(catalog["domain_tasks"])

    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"  Verb roll-ups:        {n_rollups}")
    print(f"  (verb,domain) pairs:  {n_action}")
    print(f"  Domain-only tasks:    {n_domain}")
    print(f"  TOTAL FINE-GRAINED:   {n_action + n_domain}")
    print()
    print("HOW TO READ THIS:")
    print("  - Verb roll-ups tell you the high-level shape: e.g. 'create' has")
    print("    31 workflows spread across 12 distinct domains → candidate for")
    print("    12 separate create_<domain> catalog entries OR one generic")
    print("    'create' task with the domain filled per-prompt.")
    print("  - The (verb, domain) pairs are the ones with enough support to")
    print("    stand as their own catalog entries.")
    print("  - Domain-only tasks are for noun-first workflows (e.g. VCenter,")
    print("    NetBackup) where the task is really 'work with <product>'.")
    print()
    print("NEXT STEPS after reviewing draft_task_catalog.json:")
    print("  1. For each verb with many domains (distinct_domains > 5): decide")
    print("     whether to make one task per domain or one generic verb task.")
    print("  2. Delete noise clusters (obvious non-tasks).")
    print("  3. Merge synonyms (delete_user + remove_user → one task).")
    print("  4. Fill 'category' for each keeper (map to your category taxonomy).")
    print("  5. Fill 'typical_activities' from sequence-mined data — open the")
    print("     example_filenames and look up their sequences in")
    print("     mined_activity_sequences.json.")
    print("  6. Expand 'trigger_keywords' with natural phrasings / aliases.")


# ─── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cluster descriptive workflow filenames into draft task catalog entries."
    )
    parser.add_argument(
        "--xml-dir", default="./workflows_raw/xml",
        help="Workflow XML directory (searched recursively). "
             "Default: ./workflows_raw/xml",
    )
    parser.add_argument(
        "--min-support", type=int, default=2,
        help="Minimum workflow count to keep a (verb, domain) cluster. "
             "Default: 2. Verb roll-ups use this same threshold.",
    )
    parser.add_argument(
        "--out", default="./draft_task_catalog.json",
        help="Output path (default: ./draft_task_catalog.json)",
    )
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        for alt in (Path("./workflows_raw"),
                    Path("./data/workflows_raw"),
                    Path("./data/workflows_raw/xml")):
            if alt.exists():
                print(f"Note: {args.xml_dir} not found, using {alt}",
                      file=sys.stderr)
                xml_dir = alt
                break
        else:
            print(f"ERROR: {args.xml_dir} not found", file=sys.stderr)
            return 1

    xml_files = sorted(xml_dir.rglob("*.xml"))
    if not xml_files:
        print(f"ERROR: no .xml files under {xml_dir}", file=sys.stderr)
        return 1

    print(f"Found {len(xml_files)} XML files under {xml_dir}\n")

    clusters = cluster_filenames(xml_files)
    catalog = build_catalog(clusters, args.min_support)
    print_report(catalog, clusters, args.min_support)

    out_doc = {
        "metadata": {
            "source_dir":     str(xml_dir),
            "total_files":    len(xml_files),
            "min_support":    args.min_support,
            "verb_count":     len(catalog["verb_rollups"]),
            "action_count":   len(catalog["action_tasks"]),
            "domain_count":   len(catalog["domain_tasks"]),
        },
        "verb_rollups":  catalog["verb_rollups"],
        "action_tasks":  catalog["action_tasks"],
        "domain_tasks":  catalog["domain_tasks"],
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(out_doc, indent=2), encoding="utf-8")
    print(f"Wrote draft catalog: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())