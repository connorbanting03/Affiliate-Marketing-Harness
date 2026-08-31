"""
pipeline.py — Affiliate Marketing Lead Finder Pipeline

Usage:
    python pipeline.py                      # process every link in links.json
    python pipeline.py --dry-run            # queries + search only, no scoring/saving
    python pipeline.py --only carhartt      # only links whose URL/title matches
    python pipeline.py --limit 2            # stop after 2 links
    python pipeline.py --max-candidates 10  # score fewer candidates per link
    python pipeline.py --concurrency 1      # parallel scoring sessions

Flow per affiliate link:
  1. query_agent    → LLM generates search queries for the product
  2. fetch_search_results() → DuckDuckGo searches each query (plain Python)
  3. lead_scorer.py → fresh ADK session per candidate, scores YES/NO
  4. Approved leads saved to leads.json immediately as they're found
  5. Review at http://localhost:5000 (python web_ui.py)
"""

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from agent_runner import run_agent_once  # noqa: E402  (also silences litellm)
from tools.amazon_ca import build_amazon_affiliate_url  # noqa: E402
from tools.search import search_web  # noqa: E402
from lead_scorer import score_candidates  # noqa: E402

load_dotenv()

LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.json")
LINKS_FILE = os.path.join(os.path.dirname(__file__), "links.json")
PLATFORMS = ["reddit.com", "quora.com"]

QUERY_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Output ONLY a JSON array of strings like "
    '["query one", "query two"] — no other text, no markdown.'
)


def load_leads() -> list[dict]:
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r") as f:
            raw = f.read().strip()
            if raw:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    print("WARN: leads.json is not valid JSON. Starting with no leads.")
    else:
        save_leads([])
    return []


def save_leads(leads: list[dict]) -> None:
    with open(LEADS_FILE, "w") as f:
        json.dump(leads, f, indent=2)


def save_links(links: list[dict | str]) -> None:
    with open(LINKS_FILE, "w") as f:
        json.dump(links, f, indent=2)


def load_links() -> list:
    links = []
    if os.path.exists(LINKS_FILE):
        with open(LINKS_FILE, "r") as f:
            raw = f.read().strip()
            if raw:
                try:
                    links = json.loads(raw)
                except json.JSONDecodeError:
                    print("WARN: links.json is not valid JSON. Starting with no links.")
    else:
        save_links([])

    # Tolerate a single bare object or URL string at the top level
    if isinstance(links, (dict, str)):
        links = [links]
    if not isinstance(links, list):
        print("WARN: links.json has an unexpected shape. Starting with no links.")
        links = []
    return links


def extract_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


async def generate_queries(query_agent, affiliate_link: str, description: str | None) -> list[str]:
    """Ask the query agent for search queries, retrying once if it returns junk."""
    if description:
        query_prompt = (
            f"Analyze this affiliate product and generate search queries "
            f"to find people who need it:\n\n"
            f"Product URL: {affiliate_link}\n"
            f"Description: {description}"
        )
    else:
        query_prompt = (
            f"Analyze this affiliate product URL and generate search queries "
            f"to find people who need this product:\n\n{affiliate_link}"
        )

    for attempt in (1, 2):
        prompt = query_prompt if attempt == 1 else query_prompt + QUERY_RETRY_SUFFIX
        response = await run_agent_once(query_agent, prompt, "query_agent")
        queries = extract_json(response)
        if isinstance(queries, list) and queries:
            return [str(q) for q in queries if isinstance(q, str) and q.strip()]
        print(f"  ⚠ Attempt {attempt}: could not parse queries from: {response[:120]!r}")

    return []


def resolve_link_entry(link_entry) -> tuple[str, str | None, str]:
    """Resolve a links.json entry to (affiliate_link, description, url).

    Handles legacy string URLs and object entries; builds a tagged Amazon
    affiliate URL when the entry doesn't carry one already.
    """
    if isinstance(link_entry, str):
        affiliate_url = link_entry
        description = None
        link_entry = {}
    else:
        affiliate_url = link_entry.get("url", "")
        description = link_entry.get("description")

    affiliate_link = affiliate_url
    stored = link_entry.get("affiliate_link") or ""
    if stored and ("tag=" in stored or "amazon." not in stored):
        affiliate_link = stored
    elif "amazon." in (stored or affiliate_url):
        # Stored link is missing the associate tag (or absent) — rebuild it
        affiliate_link = build_amazon_affiliate_url(stored or affiliate_url)
    elif stored:
        affiliate_link = stored
    return affiliate_link, description, affiliate_url


def filter_new_candidates(candidates: list[dict]) -> list[dict]:
    """Drop candidates whose URL is already saved in leads.json."""
    existing_urls = {l["url"] for l in load_leads()}
    fresh = [c for c in candidates if c.get("url") and c["url"] not in existing_urls]
    skipped = len(candidates) - len(fresh)
    if skipped:
        print(f"  ↻ {skipped} candidate(s) already in leads.json — skipping those")
    return fresh


async def score_and_save(
    candidates: list[dict], affiliate_link: str, concurrency: int | None = None
) -> list[dict]:
    """Score candidates, persisting every YES to leads.json the moment it's confirmed."""
    existing_urls = {l["url"] for l in load_leads()}

    async def on_yes(result: dict):
        if not result.get("url") or result["url"] in existing_urls:
            return
        lead = {
            "id": str(uuid.uuid4()),
            "affiliate_link": affiliate_link,
            "platform": result.get("platform", "unknown"),
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "snippet": result.get("snippet", ""),
            "situation": result.get("situation", ""),
            "status": "new",
            "found_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_urls.add(lead["url"])
        current = load_leads()
        current.append(lead)
        save_leads(current)
        print(f"    💾 Saved: {lead['title'][:60]}")

    return await score_candidates(
        candidates, affiliate_link, on_yes=on_yes, concurrency=concurrency
    )


def fetch_search_results(queries: list[str], platforms: list[str]) -> list[dict]:
    all_results = []
    seen_urls = set()
    for query in queries:
        for platform in platforms:
            print(f"  Searching {platform}: {query[:60]}...")
            results = search_web(query=query, site=platform)
            for r in results:
                if r.get("url") and r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    # Trim snippet now so scorer gets clean input
                    if r.get("snippet"):
                        r["snippet"] = r["snippet"][:300]
                    all_results.append(r)
    return all_results


async def process_link(
    affiliate_link: str,
    description: str | None = None,
    query_agent=None,
    args: argparse.Namespace | None = None,
) -> list[dict]:
    print(f"\n{'='*60}")
    print(f"Processing: {affiliate_link}")
    if description:
        print(f"Description: {description}")
    print(f"{'='*60}")

    # Step 1: LLM generates queries
    print("\n[1/3] Generating search queries...")
    queries = await generate_queries(query_agent, affiliate_link, description)
    if not queries:
        print("  ⚠ No usable queries after retry. Skipping this link.")
        return []
    print(f"  ✓ Got {len(queries)} queries")

    # Step 2: Search in Python
    max_queries = args.max_queries if args else 4
    print(f"\n[2/3] Searching {min(len(queries), max_queries)} queries across {PLATFORMS}...")
    candidates = fetch_search_results(queries[:max_queries], PLATFORMS)
    print(f"  ✓ Got {len(candidates)} unique candidates")

    if not candidates:
        print("  ⚠ No candidates found. Skipping.")
        return []

    # Skip anything already saved — don't waste slow local-LLM time re-scoring it
    candidates = filter_new_candidates(candidates)
    if not candidates:
        print("  ⚠ All candidates already saved. Skipping.")
        return []

    # Cap to keep scoring time reasonable
    max_candidates = args.max_candidates if args else 20
    candidates = candidates[:max_candidates]

    if args and args.dry_run:
        print(f"\n[3/3] DRY RUN — would score {len(candidates)} candidate(s):")
        for i, c in enumerate(candidates, 1):
            print(f"  {i:>2}. [{c.get('platform', '?')}] {c.get('title', '')[:70]}")
            print(f"      {c.get('url', '')}")
        return []

    # Step 3: Score each candidate in its own ADK session, saving YES hits immediately
    print(f"\n[3/3] Scoring {len(candidates)} candidates (one session each)...")
    concurrency = args.concurrency if args else None
    scored = await score_and_save(candidates, affiliate_link, concurrency=concurrency)
    print(f"  ✓ {len(scored)} candidate(s) scored YES (all saved to leads.json as found)")
    return scored


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Affiliate lead-finding pipeline")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate queries and search, but skip scoring and saving",
    )
    parser.add_argument(
        "--only", metavar="TEXT", default=None,
        help="Only process links whose URL or title contains TEXT (case-insensitive)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process at most N links",
    )
    parser.add_argument(
        "--max-queries", type=int, default=4, metavar="N",
        help="Max generated queries to search per link (default: 4)",
    )
    parser.add_argument(
        "--max-candidates", type=int, default=20, metavar="N",
        help="Max candidates to score per link (default: 20)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None, metavar="N",
        help="Parallel scoring sessions (default: SCORER_CONCURRENCY env var or 2)",
    )
    return parser.parse_args()


def _link_matches(link_entry, needle: str) -> bool:
    needle = needle.lower()
    if isinstance(link_entry, str):
        return needle in link_entry.lower()
    haystack = " ".join(
        str(link_entry.get(k, "")) for k in ("url", "affiliate_link", "title", "description")
    )
    return needle in haystack.lower()


async def main():
    args = parse_args()

    from agents.query_agent.agent import root_agent as query_agent

    links = load_links()

    if args.only:
        links = [l for l in links if _link_matches(l, args.only)]
        if not links:
            print(f"No links match --only '{args.only}'.")
            return

    if args.limit:
        links = links[:args.limit]

    if not links:
        print("No links found in links.json. Add links manually or use the web UI to discover products.")
        return

    print(f"Found {len(links)} affiliate link(s) to process.")
    if args.dry_run:
        print("DRY RUN — no scoring, nothing will be saved.")

    total_yes = 0
    errors = 0

    for idx, link_entry in enumerate(links, 1):
        print(f"\n[Link {idx}/{len(links)}]")

        affiliate_link, description, affiliate_url = resolve_link_entry(link_entry)

        if not affiliate_url:
            print("  ✗ No URL found. Skipping.")
            continue

        try:
            leads = await process_link(
                affiliate_link, description=description, query_agent=query_agent, args=args
            )
            total_yes += len(leads)
        except KeyboardInterrupt:
            print(f"\n  ⚠ Skipped (interrupted). Moving to next link…")
            continue
        except Exception as e:
            errors += 1
            print(f"\n  ✗ Error processing link: {e}. Moving to next link…")
            continue

    print(f"\n{'='*60}")
    if args.dry_run:
        print("✓ Dry run complete. Nothing was scored or saved.")
    else:
        total_now = len(load_leads())
        print(f"✓ Done. {total_yes} new lead(s) this run, {total_now} total in leads.json")
        if total_yes:
            print(f"  Review them: python web_ui.py → http://localhost:5000")
    if errors:
        print(f"⚠ {errors} link(s) hit errors — scroll up for details.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nPipeline stopped by user.")
