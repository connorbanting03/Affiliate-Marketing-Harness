import time
from ddgs import DDGS


def search_web(query: str, site: str = "reddit.com") -> list[dict]:
    """Search the web for posts matching a query on a specific platform.

    Args:
        query: The search query describing what kind of person or problem to find.
        site: The website to restrict results to (e.g. 'reddit.com', 'quora.com').

    Returns:
        A list of results, each with 'title', 'url', and 'snippet' keys.
    """
    full_query = f"site:{site} {query}"
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(full_query, max_results=10):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                    "platform": site,
                })
        # Be polite to DDG
        time.sleep(1)
    except Exception as e:
        results = [{"title": "Search error", "url": "", "snippet": str(e), "platform": site}]
    return results
