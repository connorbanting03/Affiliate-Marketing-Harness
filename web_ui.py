"""
web_ui.py — Control panel for the affiliate lead-finding harness.

Everything the CLI pipeline does can be driven from here, per product or
all at once:
  • Discover products (Amazon.ca best-sellers → links.json)
  • Generate queries   (stage 1, saved to workspace.json)
  • Search             (stage 2, candidates saved to workspace.json)
  • Score              (stage 3, YES leads saved to leads.json)
  • Full pipeline      (all three stages, per product or every product)

Jobs run one at a time in a background thread; their stdout is captured
into a log the page polls via /status.
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for

import re

import pipeline
from tools.amazon_ca import discover_top_amazon_ca_products, extract_asin

app = Flask(__name__)

LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.json")
LINKS_FILE = os.path.join(os.path.dirname(__file__), "links.json")
WORKSPACE_FILE = os.path.join(os.path.dirname(__file__), "workspace.json")
AMAZON_TOP_N = int(os.getenv("AMAZON_TOP_N", "5"))
MAX_QUERIES = 4
MAX_CANDIDATES = 20

# Known affiliate network domains → human-readable brand names
_KNOWN_DOMAINS: dict[str, str] = {
    "shopify.pxf.io": "Shopify",
    "shareasale.com": "ShareASale",
    "impact.com": "Impact",
    "awin1.com": "Awin",
    "clickbank.net": "ClickBank",
    "amazon.com": "Amazon",
    "amazon.ca": "Amazon",
    "go.fiverr.com": "Fiverr",
    "convertkit.com": "ConvertKit",
    "bluehost.com": "Bluehost",
    "hostgator.com": "HostGator",
}


# ── Data access ──────────────────────────────────────────────────────────────

def get_link_label(url: str) -> str:
    try:
        host = urlparse(url).hostname or url
        host = host.removeprefix("www.")
        return _KNOWN_DOMAINS.get(host, host)
    except Exception:
        return url


def load_leads() -> list[dict]:
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_leads(leads: list[dict]) -> None:
    with open(LEADS_FILE, "w") as f:
        json.dump(leads, f, indent=2)


def load_links() -> list[dict | str]:
    links = []
    if os.path.exists(LINKS_FILE):
        with open(LINKS_FILE, "r") as f:
            try:
                links = json.load(f)
            except json.JSONDecodeError:
                return []
    # Tolerate a single bare object or URL string at the top level
    if isinstance(links, (dict, str)):
        links = [links]
    return links if isinstance(links, list) else []


def save_links(links: list[dict | str]) -> None:
    with open(LINKS_FILE, "w") as f:
        json.dump(links, f, indent=2)


def load_workspace() -> dict:
    """Per-product intermediate results, keyed by product URL:
    { url: {"queries": [...], "candidates": [...]} }"""
    if os.path.exists(WORKSPACE_FILE):
        with open(WORKSPACE_FILE, "r") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def save_workspace(ws: dict) -> None:
    with open(WORKSPACE_FILE, "w") as f:
        json.dump(ws, f, indent=2)


def find_product(affiliate_link: str) -> dict:
    """Look up the product behind an affiliate link in links.json (best effort)."""
    for entry in load_links():
        if isinstance(entry, str):
            if entry == affiliate_link:
                return {"title": "", "description": "", "url": entry}
            continue
        if affiliate_link in (entry.get("affiliate_link"), entry.get("url")):
            return entry
    return {}


def _entry_title(entry) -> str:
    if isinstance(entry, str):
        return entry
    return entry.get("title") or entry.get("url") or "Untitled product"


# ── Background job runner (one job at a time) ────────────────────────────────

class _LogWriter(io.TextIOBase):
    """File-like object that splits writes into log lines, echoing to stdout."""

    def __init__(self, manager: "JobManager"):
        self._manager = manager
        self._buf = ""

    def write(self, s: str) -> int:
        sys.__stdout__.write(s)
        sys.__stdout__.flush()
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._manager.append_log(line)
        return len(s)

    def flush(self) -> None:
        pass


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.job: dict | None = None  # current or most recent job

    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, name: str, target) -> bool:
        with self._lock:
            if self.is_busy():
                return False
            self.job = {
                "name": name,
                "status": "running",
                "log": [],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
            }
            self._thread = threading.Thread(
                target=self._run, args=(target,), daemon=True
            )
            self._thread.start()
            return True

    def append_log(self, line: str) -> None:
        with self._lock:
            if self.job is not None:
                self.job["log"].append(line)

    def snapshot(self) -> dict | None:
        with self._lock:
            if self.job is None:
                return None
            snap = dict(self.job)
            snap["log"] = snap["log"][-400:]
            return snap

    def _run(self, target) -> None:
        status = "done"
        try:
            with contextlib.redirect_stdout(_LogWriter(self)):
                target()
        except Exception:
            self.append_log(traceback.format_exc().strip())
            status = "error"
        with self._lock:
            self.job["status"] = status
            self.job["finished_at"] = datetime.now(timezone.utc).isoformat()


jobs = JobManager()


# ── Job bodies (run in the worker thread, prints go to the job log) ──────────

def _resolve_associate_tag() -> str | None:
    """Associate tag from env, falling back to any tagged link in existing data."""
    tag = os.getenv("AMAZON_ASSOCIATE_TAG") or os.getenv("AMAZON_AFFILIATE_TAG")
    if tag:
        return tag
    sources: list[str] = []
    for entry in load_links():
        if isinstance(entry, str):
            sources.append(entry)
        else:
            sources.extend(filter(None, [entry.get("affiliate_link"), entry.get("url")]))
    sources.extend(l.get("affiliate_link", "") for l in load_leads())
    for s in sources:
        match = re.search(r"[?&]tag=([\w-]+)", s)
        if match:
            return match.group(1)
    return None


def _job_discover():
    existing = load_links()
    known_asins = set()
    for entry in existing:
        for candidate in ([entry] if isinstance(entry, str)
                          else [entry.get("url"), entry.get("affiliate_link")]):
            asin = extract_asin(candidate or "")
            if asin:
                known_asins.add(asin)

    tag = _resolve_associate_tag()
    if tag:
        print(f"Using associate tag: {tag}")
    else:
        print("⚠ NO ASSOCIATE TAG FOUND — links will earn you nothing!")
        print("  Add AMAZON_ASSOCIATE_TAG=yourtag-20 to .env and rerun.")

    print(f"Discovering {AMAZON_TOP_N} new Amazon.ca best-sellers "
          f"(skipping {len(known_asins)} already in your list)...")
    new = discover_top_amazon_ca_products(
        limit=AMAZON_TOP_N, affiliate_tag=tag, exclude_asins=known_asins
    )
    if not new:
        print("⚠ No new products found — Amazon may be rate-limiting, or every "
              "discovered product is already in your list. Try again in a few minutes.")
        return

    save_links(existing + new)
    print(f"✓ Added {len(new)} new product(s) — {len(existing) + len(new)} total in links.json:")
    for l in new:
        print(f"  • {l['title'][:75]}")


def _job_queries(idx: int):
    entry = load_links()[idx]
    affiliate_link, description, url = pipeline.resolve_link_entry(entry)
    print(f"[1/3] Generating queries for: {_entry_title(entry)[:75]}")

    from agents.query_agent.agent import root_agent as query_agent
    queries = asyncio.run(
        pipeline.generate_queries(query_agent, affiliate_link, description)
    )
    if not queries:
        print("⚠ No usable queries after retry. Try again, or try a bigger model.")
        return

    ws = load_workspace()
    ws.setdefault(url, {})["queries"] = queries
    save_workspace(ws)
    print(f"✓ {len(queries)} queries saved to workspace:")
    for q in queries:
        print(f"  • {q}")
    print("\nNext step: Search.")


def _job_search(idx: int):
    entry = load_links()[idx]
    _, _, url = pipeline.resolve_link_entry(entry)
    ws = load_workspace()
    queries = ws.get(url, {}).get("queries") or []
    if not queries:
        print("⚠ No queries in workspace for this product — run “1. Queries” first.")
        return

    print(f"[2/3] Searching {min(len(queries), MAX_QUERIES)} of {len(queries)} queries "
          f"across {pipeline.PLATFORMS}...")
    candidates = pipeline.fetch_search_results(queries[:MAX_QUERIES], pipeline.PLATFORMS)
    print(f"  ✓ Got {len(candidates)} unique result(s)")
    candidates = pipeline.filter_new_candidates(candidates)[:MAX_CANDIDATES]

    ws.setdefault(url, {})["candidates"] = candidates
    save_workspace(ws)

    if not candidates:
        print("⚠ Nothing new to score — all results already saved or no results.")
        return
    print(f"✓ {len(candidates)} candidate(s) saved to workspace:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i:>2}. [{c.get('platform', '?')}] {c.get('title', '')[:70]}")
    print("\nNext step: Score.")


def _job_score(idx: int):
    entry = load_links()[idx]
    affiliate_link, _, url = pipeline.resolve_link_entry(entry)
    ws = load_workspace()
    candidates = ws.get(url, {}).get("candidates") or []
    if not candidates:
        print("⚠ No candidates in workspace for this product — run “2. Search” first.")
        return

    print(f"[3/3] Scoring {len(candidates)} candidate(s), one session each...")
    scored = asyncio.run(pipeline.score_and_save(candidates, affiliate_link))

    # Candidates are now judged either way — clear them so they aren't rescored
    ws[url]["candidates"] = []
    save_workspace(ws)
    print(f"\n✓ {len(scored)} lead(s) scored YES and saved to leads.json.")


def _job_link(idx: int):
    entry = load_links()[idx]
    affiliate_link, description, url = pipeline.resolve_link_entry(entry)
    if not url:
        print("✗ Entry has no URL.")
        return

    from agents.query_agent.agent import root_agent as query_agent
    leads = asyncio.run(
        pipeline.process_link(affiliate_link, description=description, query_agent=query_agent)
    )
    print(f"\n✓ Done — {len(leads)} new lead(s) for this product.")


def _job_all():
    links = load_links()
    if not links:
        print("No products in links.json — run Discover Products first.")
        return

    from agents.query_agent.agent import root_agent as query_agent

    async def run():
        total = 0
        for idx, entry in enumerate(links, 1):
            print(f"\n[Link {idx}/{len(links)}]")
            affiliate_link, description, url = pipeline.resolve_link_entry(entry)
            if not url:
                print("  ✗ No URL found. Skipping.")
                continue
            try:
                leads = await pipeline.process_link(
                    affiliate_link, description=description, query_agent=query_agent
                )
                total += len(leads)
            except Exception as e:
                print(f"  ✗ Error processing link: {e}. Moving on…")
        print(f"\n{'='*60}")
        print(f"✓ Full run complete. {total} new lead(s), "
              f"{len(load_leads())} total in leads.json.")

    asyncio.run(run())


# ── Routes ───────────────────────────────────────────────────────────────────

_GLOBAL_ACTIONS = {
    "discover": ("Discover products", _job_discover),
    "all": ("Full pipeline — all products", _job_all),
}

_PRODUCT_ACTIONS = {
    "queries": ("Generate queries", _job_queries),
    "search": ("Search", _job_search),
    "score": ("Score", _job_score),
    "link": ("Full pipeline", _job_link),
}


@app.route("/run/<action>", methods=["POST"])
@app.route("/run/<action>/<int:idx>", methods=["POST"])
def run_action(action: str, idx: int | None = None):
    if idx is None:
        if action not in _GLOBAL_ACTIONS:
            return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 404
        name, fn = _GLOBAL_ACTIONS[action]
        target = fn
    else:
        if action not in _PRODUCT_ACTIONS:
            return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 404
        links = load_links()
        if not 0 <= idx < len(links):
            return jsonify({"ok": False, "error": "Product index out of range"}), 404
        label, fn = _PRODUCT_ACTIONS[action]
        name = f"{label} — {_entry_title(links[idx])[:60]}"
        target = lambda: fn(idx)  # noqa: E731

    if not jobs.start(name, target):
        return jsonify({"ok": False, "error": "A job is already running — wait for it to finish."}), 409
    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify({"busy": jobs.is_busy(), "job": jobs.snapshot()})


def _group_leads(leads: list[dict]) -> list[dict]:
    order = {"new": 0, "opened": 1, "done": 2}
    sorted_leads = sorted(leads, key=lambda l: order.get(l.get("status", "new"), 99))

    groups_map: dict[str, list[dict]] = {}
    for lead in sorted_leads:
        link = lead.get("affiliate_link", "Unknown")
        groups_map.setdefault(link, []).append(lead)

    groups = []
    for link, group_leads in groups_map.items():
        product = find_product(link)
        label = product.get("title") or get_link_label(link)
        groups.append({
            "label": label,
            "affiliate_link": link,
            "leads": group_leads,
            "stats": {
                "total": len(group_leads),
                "new": sum(1 for l in group_leads if l.get("status", "new") == "new"),
                "opened": sum(1 for l in group_leads if l.get("status") == "opened"),
                "done": sum(1 for l in group_leads if l.get("status") == "done"),
            },
        })
    return groups


def _build_products() -> list[dict]:
    ws = load_workspace()
    leads = load_leads()
    products = []
    for idx, entry in enumerate(load_links()):
        affiliate_link, _, url = pipeline.resolve_link_entry(entry)
        stage = ws.get(url, {})
        products.append({
            "idx": idx,
            "title": _entry_title(entry),
            "url": url,
            "affiliate_link": affiliate_link,
            "query_count": len(stage.get("queries") or []),
            "candidate_count": len(stage.get("candidates") or []),
            "lead_count": sum(1 for l in leads if l.get("affiliate_link") == affiliate_link),
        })
    return products


@app.route("/")
def index():
    leads = load_leads()
    stats = {
        "total": len(leads),
        "new": sum(1 for l in leads if l.get("status", "new") == "new"),
        "opened": sum(1 for l in leads if l.get("status") == "opened"),
        "done": sum(1 for l in leads if l.get("status") == "done"),
    }
    return render_template(
        "index.html",
        groups=_group_leads(leads),
        stats=stats,
        products=_build_products(),
        job=jobs.snapshot(),
        busy=jobs.is_busy(),
    )


@app.route("/open/<lead_id>")
def open_lead(lead_id: str):
    leads = load_leads()
    for lead in leads:
        if lead["id"] == lead_id:
            if lead.get("status") == "new":
                lead["status"] = "opened"
                save_leads(leads)
            if lead.get("url"):
                webbrowser.open(lead["url"])
            break
    return redirect(url_for("index"))


@app.route("/done/<lead_id>")
def mark_done(lead_id: str):
    leads = load_leads()
    for lead in leads:
        if lead["id"] == lead_id:
            lead["status"] = "done"
            save_leads(leads)
            break
    return redirect(url_for("index"))


@app.route("/delete/<lead_id>", methods=["POST"])
def delete_lead(lead_id: str):
    leads = load_leads()
    remaining = [l for l in leads if l.get("id") != lead_id]
    if len(remaining) != len(leads):
        save_leads(remaining)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Lead not found"}), 404


@app.route("/leads/delete-group", methods=["POST"])
def delete_group():
    """Delete every lead belonging to one affiliate link (one group in the UI)."""
    data = request.get_json(silent=True) or {}
    link = data.get("affiliate_link")
    if not link:
        return jsonify({"ok": False, "error": "affiliate_link required"}), 400
    leads = load_leads()
    remaining = [l for l in leads if l.get("affiliate_link") != link]
    removed = len(leads) - len(remaining)
    if removed:
        save_leads(remaining)
    return jsonify({"ok": True, "removed": removed})


@app.route("/products/add", methods=["POST"])
def add_product():
    """Manually add an affiliate link (or product URL) to links.json.

    Accepts any link — Amazon links get a clean tagged SiteStripe URL built
    from the configured associate tag; non-Amazon affiliate links are stored
    as-is. A description is optional but strongly improves query quality.
    """
    if jobs.is_busy():
        return jsonify({"ok": False, "error": "Wait for the running job to finish first."}), 409
    data = request.get_json(silent=True) or {}
    link = (data.get("url") or "").strip()
    if not link:
        return jsonify({"ok": False, "error": "A URL is required."}), 400
    if not re.match(r"^https?://", link):
        return jsonify({"ok": False, "error": "URL must start with http:// or https://"}), 400

    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()

    if "amazon." in link:
        affiliate_link = pipeline.build_amazon_affiliate_url(link)
    else:
        affiliate_link = link

    entry = {
        "url": link,
        "affiliate_link": affiliate_link,
        "title": title or link,
        "description": description,
    }

    links = load_links()
    # Skip exact duplicates (same product URL or same affiliate link)
    for existing in links:
        ex_url = existing if isinstance(existing, str) else (existing.get("url") or existing.get("affiliate_link") or "")
        if ex_url == link or (isinstance(existing, dict) and existing.get("affiliate_link") == affiliate_link):
            return jsonify({"ok": False, "error": "That link is already in your product list."}), 409

    links.append(entry)
    save_links(links)
    return jsonify({"ok": True})


@app.route("/products/delete/<int:idx>", methods=["POST"])
def delete_product(idx: int):
    """Remove a product from links.json (saved leads are kept)."""
    if jobs.is_busy():
        # Jobs reference products by position — don't shift indices mid-run
        return jsonify({"ok": False, "error": "Wait for the running job to finish first."}), 409
    links = load_links()
    if not 0 <= idx < len(links):
        return jsonify({"ok": False, "error": "Product index out of range"}), 404
    entry = links.pop(idx)
    save_links(links)
    # Drop its staged workspace data too
    _, _, url = pipeline.resolve_link_entry(entry)
    ws = load_workspace()
    if url in ws:
        del ws[url]
        save_workspace(ws)
    return jsonify({"ok": True})


@app.route("/workspace/clear/<int:idx>", methods=["POST"])
def clear_workspace(idx: int):
    """Clear a product's staged queries/candidates without touching the product."""
    if jobs.is_busy():
        return jsonify({"ok": False, "error": "Wait for the running job to finish first."}), 409
    links = load_links()
    if not 0 <= idx < len(links):
        return jsonify({"ok": False, "error": "Product index out of range"}), 404
    _, _, url = pipeline.resolve_link_entry(links[idx])
    ws = load_workspace()
    if url in ws:
        del ws[url]
        save_workspace(ws)
    return jsonify({"ok": True})


@app.route("/api/leads")
def api_leads():
    return jsonify(load_leads())


if __name__ == "__main__":
    print("Starting Affiliate Lead UI at http://localhost:5000")
    # use_reloader=False: the reloader's watcher process would duplicate job threads
    app.run(debug=True, port=5000, use_reloader=False)
