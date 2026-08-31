"""
lead_scorer.py — Per-lead session evaluator

Scores each candidate search result in a fresh ADK session via
agent_runner.run_agent_once(). Small local models frequently break the
"output only JSON" contract, so every candidate gets one retry with a
sterner instruction before being counted as unparseable (not silently
dropped — unparseable counts are reported in the run summary).

Import and call score_candidates() from pipeline.py.
"""

import asyncio
import json
import os
import re

from agent_runner import run_agent_once

APP_NAME = "lead_scorer"
DEFAULT_CONCURRENCY = int(os.getenv("SCORER_CONCURRENCY", "2"))

RETRY_SUFFIX = (
    '\n\nIMPORTANT: Reply with ONLY the JSON object '
    '{"verdict": "YES" or "NO", "situation": "..."} — no other text, '
    'no markdown, no explanation.'
)


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


def _build_prompt(result: dict, affiliate_link: str) -> str:
    return (
        f"Affiliate product URL: {affiliate_link}\n\n"
        f"Search result:\n"
        f"  Title:    {result.get('title', '')}\n"
        f"  URL:      {result.get('url', '')}\n"
        f"  Platform: {result.get('platform', '')}\n"
        f"  Snippet:  {result.get('snippet', '')[:300]}\n\n"
        f"Is this a worthwhile lead? Reply with the JSON object only."
    )


async def _score_one(scorer_agent, result: dict, affiliate_link: str) -> tuple[dict | None, bool]:
    """
    Score a single result in its own session.

    Returns (scored_result_or_None, parse_ok). scored_result is the input
    dict augmented with 'situation' when the verdict is YES.
    """
    prompt = _build_prompt(result, affiliate_link)

    try:
        response_text = await run_agent_once(scorer_agent, prompt, APP_NAME)
    except Exception as e:
        print(f"    [scorer] Error: {e}")
        return None, False

    verdict_data = _extract_verdict(response_text)

    if verdict_data is None:
        # One retry — local models often wrap JSON in prose on the first try.
        try:
            response_text = await run_agent_once(
                scorer_agent, prompt + RETRY_SUFFIX, APP_NAME
            )
            verdict_data = _extract_verdict(response_text)
        except Exception as e:
            print(f"    [scorer] Retry error: {e}")

    if verdict_data is None:
        print(f"    [scorer] Unparseable after retry: {response_text[:120]!r}")
        return None, False

    verdict = str(verdict_data.get("verdict", "")).strip().upper()
    situation = verdict_data.get("situation", "")

    if verdict == "YES":
        return {**result, "situation": situation}, True
    return None, True


async def score_candidates(
    results: list[dict],
    affiliate_link: str,
    on_yes=None,
    concurrency: int | None = None,
) -> list[dict]:
    """
    Score all candidate results, one fresh session each.

    Args:
        results: Raw search results from search_web().
        affiliate_link: The product URL being promoted.
        on_yes: Optional async callback called immediately when a YES is found.
                Signature: on_yes(lead: dict) -> None
        concurrency: Parallel scoring sessions (default: SCORER_CONCURRENCY
                     env var, falling back to 2 — local models choke above that).

    Returns:
        List of results that scored YES, with 'situation' field added.
    """
    from agents.lead_scorer.agent import root_agent as scorer_agent

    if concurrency is None:
        concurrency = DEFAULT_CONCURRENCY

    semaphore = asyncio.Semaphore(concurrency)
    approved = []
    unparseable = 0
    total = len(results)

    async def guarded_score(i: int, result: dict):
        nonlocal unparseable
        async with semaphore:
            url = result.get("url", "")[:60]
            print(f"  [{i}/{total}] Scoring: {url}...")
            scored, parse_ok = await _score_one(scorer_agent, result, affiliate_link)
            if scored:
                print(f"    ✓ YES — {scored.get('situation', '')[:80]}")
                approved.append(scored)
                if on_yes:
                    await on_yes(scored)
            elif parse_ok:
                print(f"    ✗ NO")
            else:
                unparseable += 1
                print(f"    ⚠ SKIPPED (model output unusable)")

    tasks = [guarded_score(i + 1, r) for i, r in enumerate(results)]
    await asyncio.gather(*tasks)

    if unparseable:
        print(f"  ⚠ {unparseable}/{total} candidate(s) skipped due to unusable model output")

    return approved
