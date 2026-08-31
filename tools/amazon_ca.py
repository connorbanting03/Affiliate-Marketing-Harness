import random
import re
import os
import time
import uuid
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BEST_SELLER_URLS = [
    "https://www.amazon.ca/Best-Sellers/zgbs",
    "https://www.amazon.ca/gp/bestsellers",
]

# Hard cap on pages fetched per discovery run (best-seller root + category pages)
MAX_PAGES_PER_RUN = 10

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _normalize_product_url(href: str) -> str | None:
    if not href or "/dp/" not in href:
        return None
    full = urljoin("https://www.amazon.ca", href)
    match = re.search(r"(https://www\.amazon\.ca/.+?/dp/[A-Z0-9]{10})", full)
    if match:
        return match.group(1)
    match = re.search(r"(https://www\.amazon\.ca/dp/[A-Z0-9]{10})", full)
    if match:
        return match.group(1)
    return None


def _extract_amazon_tag(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    tag_values = query.get("tag") or []
    return tag_values[0] if tag_values else None


def extract_asin(url: str) -> str | None:
    match = re.search(r"/dp/([A-Z0-9]{10})", url or "")
    if match:
        return match.group(1)
    return None


def build_amazon_affiliate_url(product_url: str, tag: str | None = None) -> str:
    """Build a SiteStripe-style text link: clean /dp/ASIN URL with the
    associate tag, matching what the SiteStripe toolbar's "Text" option emits.

    (amzn.to short links can't be generated here — they require a logged-in
    Associates session.)
    """
    affiliate_tag = (
        tag
        or _extract_amazon_tag(product_url)
        or os.getenv("AMAZON_ASSOCIATE_TAG")
        or os.getenv("AMAZON_AFFILIATE_TAG")
    )
    asin = extract_asin(product_url)
    if not asin:
        return product_url

    parsed = urlparse(product_url)
    domain = parsed.netloc or "www.amazon.ca"
    base_url = f"https://{domain}/dp/{asin}"
    if affiliate_tag:
        query = {
            "linkCode": "ll1",
            "tag": affiliate_tag,
            "linkId": uuid.uuid4().hex,
            "language": "en_CA",
            "ref_": "as_li_ss_tl",
        }
        return f"{base_url}?{urlencode(query)}"
    return base_url


def _build_description(title: str) -> str:
    return (
        "Amazon.ca best-seller product. "
        f"Item: {title}. "
        "Search for users asking for recommendations, comparisons, "
        "problems this product solves, and buying intent around this item type."
    )


def _extract_product_candidates(html: str) -> Iterable[dict]:
    soup = BeautifulSoup(html, "html.parser")
    seen = set()

    for anchor in soup.select("a[href*='/dp/']"):
        href = anchor.get("href", "")
        product_url = _normalize_product_url(href)
        if not product_url or product_url in seen:
            continue

        title = _clean_text(anchor.get_text(" ", strip=True))
        if not title:
            img = anchor.find("img")
            if img:
                title = _clean_text(img.get("alt", ""))

        if len(title) < 6:
            continue

        seen.add(product_url)
        yield {
            "url": product_url,
            "description": _build_description(title),
            "title": title,
        }


def _extract_category_urls(html: str) -> list[str]:
    """Pull category best-seller page URLs (.../zgbs/<category>) out of a page."""
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for anchor in soup.select("a[href*='/zgbs/']"):
        href = anchor.get("href", "")
        full = urljoin("https://www.amazon.ca", href).split("?")[0].split("#")[0]
        if "amazon.ca" in full and "/zgbs/" in full:
            urls.add(full.rstrip("/"))
    return list(urls)


def discover_top_amazon_ca_products(
    limit: int = 20,
    affiliate_tag: str | None = None,
    exclude_asins: set[str] | None = None,
) -> list[dict]:
    """Discover best-seller products, skipping ASINs already known.

    Walks the best-seller root pages first, then randomly-ordered category
    best-seller pages, until `limit` *new* products are found (or the page
    budget runs out). The random category order means repeat runs surface
    different products instead of the same top of the front page.
    """
    discovered_tag = (
        affiliate_tag
        or os.getenv("AMAZON_ASSOCIATE_TAG")
        or os.getenv("AMAZON_AFFILIATE_TAG")
    )

    products: list[dict] = []
    seen_asins = {a.upper() for a in (exclude_asins or set())}
    queue = list(BEST_SELLER_URLS)
    visited: set[str] = set()
    pages_fetched = 0

    while queue and pages_fetched < MAX_PAGES_PER_RUN and len(products) < limit:
        source_url = queue.pop(0)
        if source_url in visited:
            continue
        visited.add(source_url)

        try:
            response = requests.get(source_url, headers=_HEADERS, timeout=20)
            response.raise_for_status()
        except Exception:
            continue
        pages_fetched += 1

        # Queue category pages in random order so each run explores differently
        categories = _extract_category_urls(response.text)
        random.shuffle(categories)
        queue.extend(c for c in categories if c not in visited)

        for candidate in _extract_product_candidates(response.text):
            asin = extract_asin(candidate["url"])
            if not asin or asin in seen_asins:
                continue
            seen_asins.add(asin)
            products.append({
                "url": candidate["url"],
                "affiliate_link": build_amazon_affiliate_url(candidate["url"], tag=discovered_tag),
                "description": candidate["description"],
                "title": candidate["title"],
            })
            if len(products) >= limit:
                return products

        # Be polite between page fetches
        time.sleep(1)

    return products
