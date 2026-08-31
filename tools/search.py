import random
import time

from ddgs import DDGS

MAX_RETRIES = 3


def search_web(query: str, site: str = "reddit.com", max_results: int = 10) -> list[dict]:
    """Search the web for posts matching a query on a specific platform.

    Retries with exponential backoff — DuckDuckGo rate-limits aggressively.

    Args:
        query: The search query describing what kind of person or problem to find.
        site: The website to restrict results to (e.g. 'reddit.com', 'quora.com').
        max_results: Maximum number of results to fetch.

    Returns:
        A list of results, each with 'title', 'url', 'snippet', and 'platform' keys.
        Empty list if all retries fail.
    """
    full_query = f"site:{site} {query}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(full_query, max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", ""),
                        "platform": site,
                    })
            # Be polite to DDG
            time.sleep(1)
            return results
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"    ⚠ Search failed after {MAX_RETRIES} attempts ({site}): {e}")
                return []
            wait = 2 ** attempt + random.uniform(0, 1)
            print(f"    ⚠ Search hiccup ({e.__class__.__name__}), retrying in {wait:.0f}s...")
            time.sleep(wait)
    return []
