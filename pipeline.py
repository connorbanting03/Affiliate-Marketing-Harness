"""
pipeline.py — Affiliate Marketing Lead Finder Pipeline

Usage:
    python pipeline.py

Flow per affiliate link:
  1. query_agent    → LLM generates search queries for the product
  2. fetch_search_results() → DuckDuckGo searches each query (plain Python)
  3. lead_scorer.py → fresh ADK session per candidate, scores YES/NO, tears down
  4. review_leads() → human reviews each approved lead one at a time
  5. Approved leads saved to leads.json
"""

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import litellm

# Prevent LiteLLM from spawning LoggingWorker background tasks that error on exit
litellm.success_callback = []
litellm.failure_callback = []
litellm._async_success_callback = []
litellm._async_failure_callback = []
litellm.suppress_debug_info = True
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
logging.getLogger("litellm").setLevel(logging.CRITICAL)

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

sys.path.insert(0, os.path.dirname(__file__))
from tools.search import search_web  # noqa: E402
from lead_scorer import score_candidates  # noqa: E402

load_dotenv()

LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.json")
LINKS_FILE = os.path.join(os.path.dirname(__file__), "links.json")
PLATFORMS = ["reddit.com", "quora.com"]


def load_leads() -> list[dict]:
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r") as f:
            return json.load(f)
    return []


def save_leads(leads: list[dict]) -> None:
    with open(LEADS_FILE, "w") as f:
        json.dump(leads, f, indent=2)


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


async def run_agent_once(agent, message: str, app_name: str) -> str:
    session_service = InMemorySessionService()
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=session_id,
    )
    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=session_service,
    )
    user_message = Content(role="user", parts=[Part(text=message)])
    response_text = ""
    async for event in runner.run_async(
        user_id="pipeline",
        session_id=session_id,
        new_message=user_message,
    ):
        if event.is_final_response():
            if event.content and event.content.parts:
                response_text = event.content.parts[0].text
    return response_text


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


async def process_link(affiliate_link: str, description: str | None = None, query_agent=None) -> list[dict]:
    print(f"\n{'='*60}")
    print(f"Processing: {affiliate_link}")
    if description:
        print(f"Description: {description}")
    print(f"{'='*60}")

    # Step 1: LLM generates queries
    print("\n[1/3] Generating search queries...")
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
    query_response = await run_agent_once(query_agent, query_prompt, "query_agent")
    print(f"  Raw response: {query_response[:200]}")

    queries = extract_json(query_response)
    if not isinstance(queries, list) or len(queries) == 0:
        print("  ⚠ Could not parse queries. Skipping this link.")
        return []
    print(f"  ✓ Got {len(queries)} queries")

    # Step 2: Search in Python
    print(f"\n[2/3] Searching {min(len(queries), 4)} queries across {PLATFORMS}...")
    candidates = fetch_search_results(queries[:4], PLATFORMS)
    print(f"  ✓ Got {len(candidates)} unique candidates")

    if not candidates:
        print("  ⚠ No candidates found. Skipping.")
        return []

    # Cap at 20 to keep scoring time reasonable
    candidates = candidates[:20]

    # Step 3: Score each candidate in its own ADK session, saving YES hits immediately
    print(f"\n[3/3] Scoring {len(candidates)} candidates (one session each)...")

    existing_leads = load_leads()
    existing_urls = {l["url"] for l in existing_leads}

    async def on_yes(result: dict):
        """Write each YES lead to disk the moment it's confirmed."""
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

    scored = await score_candidates(candidates, affiliate_link, on_yes=on_yes, concurrency=2)
    print(f"  ✓ {len(scored)} candidate(s) scored YES (all saved to leads.json as found)")

    # Return scored leads so review_leads() can still present them at the end
    leads = []
    for r in scored:
        if not r.get("url"):
            continue
        leads.append({
            "id": "",  # already saved with id; placeholder here for review display
            "affiliate_link": affiliate_link,
            "platform": r.get("platform", "unknown"),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "situation": r.get("situation", ""),
            "status": "new",
            "found_at": datetime.now(timezone.utc).isoformat(),
        })
    return leads


async def main():
    from agents.query_agent.agent import root_agent as query_agent

    if not os.path.exists(LINKS_FILE):
        print(f"ERROR: {LINKS_FILE} not found.")
        return

    with open(LINKS_FILE, "r") as f:
        links = json.load(f)

    if not links:
        print("No links found in links.json.")
        return

    print(f"Found {len(links)} affiliate link(s) to process.")

    existing_leads = load_leads()
    existing_urls = {lead["url"] for lead in existing_leads}

    for idx, link_entry in enumerate(links, 1):
        print(f"\n[Link {idx}/{len(links)}]")
        
        # Handle both string URLs (legacy) and object format {url, description}
        if isinstance(link_entry, str):
            affiliate_url = link_entry
            description = None
        else:
            affiliate_url = link_entry.get("url", "")
            description = link_entry.get("description")
        
        if not affiliate_url:
            print("  ✗ No URL found. Skipping.")
            continue
        
        try:
            leads = await process_link(affiliate_url, description=description, query_agent=query_agent)
        except KeyboardInterrupt:
            print(f"\n  ⚠ Skipped (interrupted). Moving to next link…")
            continue
        except Exception as e:
            print(f"\n  ✗ Error processing link: {e}. Moving to next link…")
            continue

        if not leads:
            print("  No leads scored YES for this link.")
            continue

        print(f"  ✓ {len(leads)} lead(s) saved — review them at http://localhost:5000")

    total_now = len(load_leads())
    print(f"\n✓ Done. {total_now} total lead(s) in leads.json")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nPipeline stopped by user.")

