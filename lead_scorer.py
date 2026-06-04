"""
lead_scorer.py — Per-lead session evaluator

Spins up a fresh ADK InMemorySession for each candidate search result,
asks the scorer agent to verdict YES/NO, then tears the session down.
Import and call score_candidates() from pipeline.py.
"""

import asyncio
import json
import re
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

APP_NAME = "lead_scorer"


def _extract_verdict(text: str) -> dict | None:
    """Parse {verdict, situation} JSON from the LLM response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


async def _score_one(scorer_agent, result: dict, affiliate_link: str) -> dict | None:
    """
    Spin up a fresh session, score a single result, tear down.
    Returns the result dict augmented with 'situation' if YES, else None.
    """
    session_service = InMemorySessionService()
    session_id = str(uuid.uuid4())

    await session_service.create_session(
        app_name=APP_NAME,
        user_id="scorer",
        session_id=session_id,
    )

    runner = Runner(
        agent=scorer_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    prompt = (
        f"Affiliate product URL: {affiliate_link}\n\n"
        f"Search result:\n"
        f"  Title:    {result.get('title', '')}\n"
        f"  URL:      {result.get('url', '')}\n"
        f"  Platform: {result.get('platform', '')}\n"
        f"  Snippet:  {result.get('snippet', '')[:300]}\n\n"
        f"Is this a worthwhile lead? Reply with the JSON object only."
    )

    user_message = Content(role="user", parts=[Part(text=prompt)])
    response_text = ""

    try:
        async for event in runner.run_async(
            user_id="scorer",
            session_id=session_id,
            new_message=user_message,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    response_text = event.content.parts[0].text
    except Exception as e:
        print(f"    [scorer] Error: {e}")
        return None
    finally:
        # Tear down — InMemorySessionService holds no external resources,
        # but we delete the session to free memory explicitly.
        try:
            await session_service.delete_session(
                app_name=APP_NAME,
                user_id="scorer",
                session_id=session_id,
            )
        except Exception:
            pass

    verdict_data = _extract_verdict(response_text)
    if not verdict_data:
        return None

    verdict = str(verdict_data.get("verdict", "")).strip().upper()
    situation = verdict_data.get("situation", "")

    if verdict == "YES":
        return {**result, "situation": situation}
    return None


async def score_candidates(
    results: list[dict],
    affiliate_link: str,
    on_yes=None,
    concurrency: int = 2,
) -> list[dict]:
    """
    Score all candidate results, one fresh session each.

    Args:
        results: Raw search results from search_web().
        affiliate_link: The product URL being promoted.
        on_yes: Optional async callback called immediately when a YES is found.
                Signature: on_yes(lead: dict) -> None
        concurrency: How many parallel scoring sessions to run at once.

    Returns:
        List of results that scored YES, with 'situation' field added.
    """
    from agents.lead_scorer.agent import root_agent as scorer_agent

    semaphore = asyncio.Semaphore(concurrency)
    approved = []
    total = len(results)

    async def guarded_score(i: int, result: dict):
        async with semaphore:
            url = result.get("url", "")[:60]
            print(f"  [{i}/{total}] Scoring: {url}...")
            scored = await _score_one(scorer_agent, result, affiliate_link)
            if scored:
                print(f"    ✓ YES — {scored.get('situation', '')[:80]}")
                approved.append(scored)
                if on_yes:
                    await on_yes(scored)
            else:
                print(f"    ✗ NO")

    tasks = [guarded_score(i + 1, r) for i, r in enumerate(results)]
    await asyncio.gather(*tasks)

    return approved
