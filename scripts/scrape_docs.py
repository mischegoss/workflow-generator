#!/usr/bin/env python3
"""
scrape_docs_categories.py

Crawls https://help.resolve.io/actions/Activity-Repository/ to produce a
vendor-authoritative mapping:
    activity_display_name → {category, subcategory, url}

Output: data/docs_categories_source.json

Two-pass crawl:
  1. Fetch the root Activity Repository index to enumerate top-level categories.
  2. For each category page, extract direct child links. Children are either
     leaf activity pages OR subcategory landing pages (distinguished by
     'introduction-to-' / '-overview' / '-management' slug patterns OR
     re-nested structure). Subcategory pages are fetched for their leaves.

Robustness choices:
  - Uses html.parser from stdlib (no lxml/bs4 dependency).
  - Only collects links inside <main>/<article> to avoid sidebar noise.
  - Falls back to all links if main/article isn't detected.
  - Polite fixed delay between requests (default 0.5s).
  - Skips pages whose display name matches overview/introduction markers.

Run once, or re-run when the docs are updated. Expected runtime: ~60 seconds.

Usage:
    python scrape_docs_categories.py
    python scrape_docs_categories.py --delay 1.0 --output ./out.json

Stdlib only.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin


BASE_URL  = "https://help.resolve.io"
INDEX_URL = "https://help.resolve.io/actions/Activity-Repository/"

# Phrases that indicate an overview/index page (not an activity itself).
# Matched case-insensitively against the link's display text.
SKIP_PHRASES = {
    "overview", "introduction to", "list of activities",
    "activity repository", "what's new", "getting started",
    "home", "previous", "next", "skip to main content",
    "documentation", "actions", "pro", "express", "barista",
    "training", "support", "automation exchange",
    "menu", "ask rani", "blog", "linkedin", "youtube",
    "contact us", "privacy policy", "terms of use",
    "learning hub", "discover resolve", "insights",
    "support portal", "resolve actions documentation home",
    "automation use cases", "building your workflow",
    "creating self service forms", "developing custom activities",
    "product navigation", "support and troubleshooting",
}


# ─── HTML extractor ────────────────────────────────────────────────────

class MainContentLinkExtractor(HTMLParser):
    """
    Collect (href, text, kind) triples from <a> elements.

    `kind` is derived from the anchor's subtitle text (Docusaurus card
    convention):
      "folder"  — subtitle contains "N items" (this is a subcategory)
      "leaf"    — subtitle contains "Activity Description"
      "unknown" — neither pattern matched; caller must fall back to URL

    `main_links` contains only links inside <main>/<article>; `all_links`
    contains every link (sidebar + footer + body). Use `all_links` for
    category discovery on the root page (sidebar has the authoritative
    full list of categories) and `main_links` for page content parsing.

    If the anchor wraps both an <h1-4> heading AND subtitle text, the
    heading alone is used as the display text (stripping subtitles like
    "Activity Description" and "N items").
    """
    _HEADING_TAGS = {"h1", "h2", "h3", "h4"}
    _ITEMS_PAT = re.compile(r"\s+\d+\s+items?\s*$", re.IGNORECASE)
    _DESC_PAT  = re.compile(r"\s+Activity Description\s*$", re.IGNORECASE)

    def __init__(self):
        super().__init__()
        self.main_links: list[tuple[str, str, str]] = []
        self.all_links:  list[tuple[str, str, str]] = []
        self._in_main = 0
        self._a_depth = 0
        self._href: str | None = None
        self._full_buf:    list[str] = []
        self._heading_buf: list[str] = []
        self._in_heading = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("main", "article"):
            self._in_main += 1
        elif t == "a":
            if self._a_depth == 0:
                self._href = next(
                    (v for k, v in attrs if k.lower() == "href"), None
                )
                self._full_buf = []
                self._heading_buf = []
                self._in_heading = 0
            self._a_depth += 1
        elif t in self._HEADING_TAGS and self._a_depth > 0:
            self._in_heading += 1

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._HEADING_TAGS and self._a_depth > 0 and self._in_heading > 0:
            self._in_heading -= 1
        elif t == "a" and self._a_depth > 0:
            self._a_depth -= 1
            if self._a_depth == 0 and self._href:
                # Detect kind from the FULL anchor text (before stripping)
                full = re.sub(r"\s+", " ", "".join(self._full_buf).strip())
                if self._ITEMS_PAT.search(full):
                    kind = "folder"
                elif self._DESC_PAT.search(full):
                    kind = "leaf"
                else:
                    kind = "unknown"

                # Build clean display text: prefer heading, else strip subtitles
                raw = ("".join(self._heading_buf).strip()
                       or "".join(self._full_buf).strip())
                text = re.sub(r"\s+", " ", raw)
                text = re.sub(r"^[^\w\(]+\s*", "", text).strip()   # leading emoji
                text = self._DESC_PAT.sub("", text).strip()
                text = self._ITEMS_PAT.sub("", text).strip()

                if text:
                    link = (self._href, text, kind)
                    self.all_links.append(link)
                    if self._in_main > 0:
                        self.main_links.append(link)
                self._href = None
                self._full_buf = []
                self._heading_buf = []
                self._in_heading = 0
        elif t in ("main", "article") and self._in_main > 0:
            self._in_main -= 1

    def handle_data(self, data):
        if self._a_depth > 0:
            self._full_buf.append(data)
            if self._in_heading > 0:
                self._heading_buf.append(data)


# ─── Fetching ──────────────────────────────────────────────────────────

def fetch(url: str, delay: float, max_retries: int = 2) -> str | None:
    """Return page text, or None on failure. Polite delay before each request."""
    time.sleep(delay)
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CategoryBootstrap/1.0 (docs scrape)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (URLError, HTTPError) as e:
            if attempt < max_retries:
                print(f"  retry {attempt+1}/{max_retries}: {url}  ({e})",
                      file=sys.stderr)
                time.sleep(2)
            else:
                print(f"  FAILED: {url}  ({e})", file=sys.stderr)
                return None


def extract_main_links(html_text: str) -> list[tuple[str, str, str]]:
    """
    Return (url, display_text, kind) from main/article content.
    Falls back to all links if no main/article present.
    Use this for parsing category pages where we want body content only.
    """
    if not html_text:
        return []
    parser = MainContentLinkExtractor()
    try:
        parser.feed(html_text)
    except Exception as e:
        print(f"  HTML parse error: {e}", file=sys.stderr)
    return parser.main_links or parser.all_links


def extract_all_links(html_text: str) -> list[tuple[str, str, str]]:
    """
    Return (url, display_text, kind) from every <a> on the page including
    sidebar and footer. Use this for the root index where the sidebar has
    the authoritative list of ALL categories (the main content sometimes
    lags behind).
    """
    if not html_text:
        return []
    parser = MainContentLinkExtractor()
    try:
        parser.feed(html_text)
    except Exception as e:
        print(f"  HTML parse error: {e}", file=sys.stderr)
    return parser.all_links


# ─── URL classification ────────────────────────────────────────────────

def is_category_url(url: str) -> bool:
    """Top-level category, e.g. /actions/category/active-directory/"""
    return bool(re.match(r"^https?://help\.resolve\.io/actions/category/[^/]+/?$", url))


def is_activity_repo_url(url: str) -> bool:
    """Any page under the Activity-Repository tree."""
    return "/actions/Activity-Repository/" in url or \
           "/actions/activity-repository/" in url


def looks_like_landing(url: str, text: str) -> bool:
    """
    Fallback URL-based heuristic: is this a subcategory landing page?
    Used ONLY when the subtitle-based `kind` signal is "unknown".

    Uses positional matching (startswith/endswith/exact) rather than
    substring `in`, because activity slugs often contain landing-marker
    words mid-slug (e.g. "REMOVED_SECRET"
    contains "-management" but is a leaf activity, not a landing page).
    """
    if is_skip_text(text):
        return True
    last = url.rstrip("/").split("/")[-1].lower()

    # Prefix: subcategory intros in Docusaurus structure
    if last.startswith("introduction-to-"):
        return True
    # Suffix: category/subcategory overview landing pages
    if last.endswith("-overview"):
        return True
    # Exact/suffix: specific known-landing slugs
    if last.endswith("-management") and last.count("-") <= 1:
        # Matches "password-management" but NOT "xch-management-shell"
        # or "REMOVED_SECRET"
        return True
    if last == "list-of-activities-overview":
        return True
    return False


def is_skip_text(text: str) -> bool:
    low = text.lower().strip()
    if not low or len(low) < 2:
        return True
    return any(p == low or p in low for p in SKIP_PHRASES)


# ─── Normalization for later matching ──────────────────────────────────

def normalize(name: str) -> str:
    """Collapse to match key: lowercase, alphanumeric only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ─── Main crawl ────────────────────────────────────────────────────────

def classify_child(url: str, text: str, kind: str) -> str:
    """
    Decide whether a child link from a category page is a folder (subcategory
    landing we need to recurse into) or a leaf (activity).

    Primary signal: `kind` from the subtitle — "N items" => folder,
    "Activity Description" => leaf. These are authoritative because the
    docs themselves emit them.

    Fallback (kind=="unknown"): URL-based heuristic via looks_like_landing.
    """
    if kind == "folder":
        return "folder"
    if kind == "leaf":
        return "leaf"
    # Unknown subtitle — fall back to URL heuristic
    if looks_like_landing(url, text):
        return "folder"
    return "leaf"


def crawl(index_url: str, delay: float) -> list[dict]:
    activities: list[dict] = []
    visited: set[str] = set()

    print(f"Fetching index: {index_url}")
    root_html = fetch(index_url, delay)
    if not root_html:
        return activities

    # Category discovery: use ALL links on root (sidebar has the
    # full 51 categories, while main content sometimes only shows 44-46)
    root_all = extract_all_links(root_html)

    category_links: list[tuple[str, str]] = []
    seen = set()
    for href, text, _kind in root_all:
        abs_url = urljoin(index_url, href)
        if not is_category_url(abs_url):
            continue
        if abs_url in seen:
            continue
        if is_skip_text(text):
            continue
        seen.add(abs_url)
        category_links.append((abs_url, text))

    print(f"  Found {len(category_links)} top-level categories\n")

    for cat_url, cat_name in category_links:
        if cat_url in visited:
            continue
        visited.add(cat_url)

        print(f"[{cat_name}]")
        cat_html = fetch(cat_url, delay)
        if not cat_html:
            continue

        # Use MAIN content only on category pages — sidebar would leak
        # cross-category links into this category's child list
        child_links = extract_main_links(cat_html)

        local_seen = set()
        for href, text, kind in child_links:
            abs_url = urljoin(cat_url, href)
            if abs_url in local_seen:
                continue
            local_seen.add(abs_url)

            if not is_activity_repo_url(abs_url):
                continue
            if is_skip_text(text):
                continue

            role = classify_child(abs_url, text, kind)

            if role == "folder":
                if abs_url in visited:
                    continue
                visited.add(abs_url)
                print(f"  > {text}  (subcategory, kind={kind})")
                sub_html = fetch(abs_url, delay)
                if not sub_html:
                    continue
                sub_links = extract_main_links(sub_html)
                sub_seen = set()
                sub_leaves = 0
                for sub_href, sub_text, sub_kind in sub_links:
                    sub_url = urljoin(abs_url, sub_href)
                    if sub_url in sub_seen:
                        continue
                    sub_seen.add(sub_url)
                    if not is_activity_repo_url(sub_url):
                        continue
                    if is_skip_text(sub_text):
                        continue
                    sub_role = classify_child(sub_url, sub_text, sub_kind)
                    if sub_role == "folder":
                        continue  # deeper nesting not expected
                    activities.append({
                        "display":     sub_text,
                        "category":    cat_name,
                        "subcategory": text,
                        "url":         sub_url,
                        "match_key":   normalize(sub_text),
                    })
                    sub_leaves += 1
                if sub_leaves == 0:
                    # Subcategory yielded no leaves — likely misclassified.
                    # Record the "subcategory" itself as a leaf activity to
                    # avoid losing data from false-positive folder detection.
                    print(f"    (0 leaves — reclassifying as leaf)",
                          file=sys.stderr)
                    activities.append({
                        "display":     text,
                        "category":    cat_name,
                        "subcategory": None,
                        "url":         abs_url,
                        "match_key":   normalize(text),
                    })
            else:
                # Direct leaf activity
                activities.append({
                    "display":     text,
                    "category":    cat_name,
                    "subcategory": None,
                    "url":         abs_url,
                    "match_key":   normalize(text),
                })

    return activities


# ─── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape Resolve Actions docs for activity taxonomy."
    )
    parser.add_argument("--index",  default=INDEX_URL)
    parser.add_argument("--output", default="./data/docs_categories_source.json")
    parser.add_argument("--delay",  type=float, default=0.5,
                        help="Seconds between requests (default: 0.5)")
    args = parser.parse_args()

    activities = crawl(args.index, args.delay)

    # Dedupe by match_key — if an activity appears in multiple categories,
    # first occurrence wins (usually the canonical one)
    by_match: dict[str, dict] = {}
    for a in activities:
        k = a["match_key"]
        if k and k not in by_match:
            by_match[k] = a
    final = list(by_match.values())

    # Summary
    cat_counts: dict[str, int] = {}
    subcat_counts = 0
    for a in final:
        cat_counts[a["category"]] = cat_counts.get(a["category"], 0) + 1
        if a.get("subcategory"):
            subcat_counts += 1

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  Total unique activities: {len(final)}")
    print(f"  Categories:              {len(cat_counts)}")
    print(f"  Activities with subcat:  {subcat_counts}")
    print()
    print("  Per-category count:")
    for c, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {c:<42} {n:>4}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc = {
        "metadata": {
            "source_url":         args.index,
            "total_activities":   len(final),
            "num_categories":     len(cat_counts),
        },
        "activities": final,
    }
    out_path.write_text(
        json.dumps(out_doc, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"\nWrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())