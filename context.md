# Affiliate Marketing Harness — Project Context

## What this is and why it exists
A personal money-making side project: an automated pipeline that finds **Amazon.ca affiliate
products** and then scours Reddit/Quora for **real people actively expressing the problem that
product solves**, so the owner can drop a helpful, non-spammy reply with their affiliate link.

The entire goal is to **automate the boring/slow parts of affiliate marketing**:
1. Product discovery (what to promote)
2. Lead finding (who might want it, and where they're posting about it)

Reply writing is deliberately left to the human — an LLM-drafted reply from a small local
model was tried and gutted (too robotic; a ban risk on Reddit). The harness's job is to
deliver the human a post URL + the person's situation + a copy-button for the affiliate link.

## Key constraint: runs entirely on a free local LLM
Everything LLM-related runs against **Ollama running a small local model** (gemma4:e4b-class)
— see `.env` (`OLLAMA_MODEL`, `OLLAMA_API_BASE`, default `http://localhost:11434`).
This is deliberate: **zero API cost**. The tradeoff is the model is small and inconsistent
at following "output strict JSON only" instructions. The harness compensates with
retry-on-unparseable logic everywhere an agent must return JSON, and reports (rather than
silently drops) candidates whose output stayed unusable.

## The web UI is the control panel (`web_ui.py`, Flask, port 5000)
Everything can be run from the dashboard; the CLI (`pipeline.py`) is an alternative.

- **Background job runner**: `JobManager` runs one job at a time in a daemon thread
  (single-job lock — the local model can't handle parallel jobs). Job stdout is captured
  via `contextlib.redirect_stdout` into a log list; the page polls `GET /status` every
  1.5s and renders a live log panel. Starting a job while busy returns 409.
- **Global actions** (`POST /run/discover`, `POST /run/all`): discover Amazon.ca
  best-sellers into `links.json`; run the full pipeline over every product.
  Discovery **appends** new products (never overwrites), skips ASINs already in
  links.json, and crawls randomly-ordered category best-seller pages (max 10 pages/run)
  so repeat runs surface different products. Associate tag comes from
  `AMAZON_ASSOCIATE_TAG` env (falls back to scanning existing data for a `tag=`);
  if none is found the job log warns loudly that links will earn nothing.
- **Per-product stage actions** (`POST /run/<action>/<idx>`, idx = position in links.json):
  - `queries` — stage 1: LLM generates pain-point search queries → `workspace.json`
  - `search` — stage 2: DDG search of staged queries, dedupe + drop already-saved leads →
    candidates into `workspace.json`
  - `score` — stage 3: LLM verdicts staged candidates; YES → `leads.json`; candidates are
    cleared from the workspace afterwards (judged either way, don't rescore)
  - `link` — all three stages chained for that product
  Stages depend on workspace state: running Search with no staged queries prints a
  friendly "run Queries first" message into the job log.
- **Products section**: one row per links.json entry with staged-state chips
  (N queries / N candidates / N leads), copy-affiliate-link button, and the four buttons.
- **Leads section**: leads grouped by product (group label = product title from links.json
  when matchable), status workflow new → opened → done, open-in-browser, delete
  (`POST /delete/<id>`), copy affiliate link.
- **Add product manually** (`POST /products/add`, JSON `{url, title?, description?}`):
  "➕ Add link" button in the Products header toggles an inline form. Amazon links get a
  clean tagged SiteStripe `affiliate_link` built via `build_amazon_affiliate_url`;
  non-Amazon affiliate links are stored as-is. Validates http(s) scheme, rejects exact
  duplicates (409). Appends to links.json. Returns 409 while a job is running.
- **Delete everywhere** (all POST, persist to the backing JSON files): single lead
  (`/delete/<id>`), whole lead group (`/leads/delete-group`, JSON body `{affiliate_link}`),
  product (`/products/delete/<idx>` — also drops its workspace entry; saved leads kept),
  staged queries/candidates (`/workspace/clear/<idx>`). Product delete and workspace clear
  return 409 while a job is running (jobs reference products by index).
  UI behavior: **lead and group deletes still confirm** (irreversible loss of scored
  leads); **product delete and clear-staged are immediate, no confirm** (per user request —
  products are cheap to re-add/re-discover).
- `workspace.json` (gitignored) holds per-product intermediate stage outputs keyed by
  product URL: `{ url: {"queries": [...], "candidates": [...]} }`.

## Pipeline flow (same code drives CLI and UI jobs)
1. **Query generation** (`pipeline.generate_queries` + `agents/query_agent`): 6-10 natural
   search queries describing a *person's problem* — not the product name. One retry with a
   sterner JSON instruction on parse failure.
2. **Search** (`pipeline.fetch_search_results` + `tools/search.py`): DuckDuckGo (`ddgs`)
   restricted to `site:reddit.com` / `site:quora.com`, 3-attempt exponential backoff,
   URL dedupe, snippets trimmed to 300 chars. `pipeline.filter_new_candidates()` drops
   anything already in `leads.json` *before* scoring. Cap: 4 queries × 2 platforms,
   20 candidates (CLI flags / UI constants).
3. **Scoring** (`pipeline.score_and_save` → `lead_scorer.score_candidates` +
   `agents/lead_scorer`): fresh ADK session per candidate via
   `agent_runner.run_agent_once()`, must return `{"verdict": "YES"/"NO", "situation"}`.
   Unparseable → one retry → counted in summary. Concurrency: `SCORER_CONCURRENCY` env or
   2. Every YES is written to `leads.json` immediately (interruption-safe).
4. **Link resolution** (`pipeline.resolve_link_entry`): handles legacy string URLs and
   object entries; builds tagged Amazon affiliate URLs via
   `tools/amazon_ca.build_amazon_affiliate_url` when the entry lacks one — and rebuilds
   stored Amazon links that are missing a `tag=` (defense against untagged data).
   Affiliate URLs use the SiteStripe text-link format:
   `https://www.amazon.ca/dp/ASIN?linkCode=ll1&tag=...&linkId=...&language=en_CA&ref_=as_li_ss_tl`.
   (amzn.to short links are impossible without a logged-in Associates session.)

## Repo layout
- `web_ui.py` — Flask control panel (see above). Routes: `/`, `/run/<action>[/<idx>]`,
  `/status`, `/open/<id>`, `/done/<id>`, `/delete/<id>` (POST), `/api/leads`.
  Runs with `use_reloader=False` (the reloader would duplicate job threads).
- `pipeline.py` — pipeline stages as importable functions + CLI: `--dry-run`, `--only TEXT`,
  `--limit N`, `--max-queries`, `--max-candidates`, `--concurrency`; end-of-run summary.
- `agent_runner.py` — shared `run_agent_once(agent, message, app_name)`; importing it
  silences LiteLLM's background logging workers (they error on interpreter exit).
- `lead_scorer.py` — per-candidate scoring with parse-retry, reports unparseable counts.
- `agents/query_agent/agent.py` — search-query JSON array generator.
- `agents/lead_scorer/agent.py` — single-candidate YES/NO + situation verdict.
- `agents/lead_finder/agent.py` — alternative batch-style filter (one call scores many
  results); not wired in, kept as an option.
- `tools/search.py` — DDG wrapper with retry/backoff; returns `[]` on total failure.
- `tools/amazon_ca.py` — Amazon.ca best-seller scraping, URL normalization,
  `build_amazon_affiliate_url()`, `discover_top_amazon_ca_products()`.
- `templates/index.html` — single-page dark dashboard: stats header, Pipeline Control
  panel (global buttons + live job log with spinner/status badge), Products rows with
  stage buttons + chips, lead groups. Vanilla JS: `runAction`, `poll` (1.5s), `copyText`,
  `deleteLead`; page reloads ~1.2s after a job finishes to refresh data.
- `agent.py` / `main.py` — standalone CLI chat loop for testing the local model; not part
  of the pipeline.
- `links.json` — input products `{ url, affiliate_link, description, title }` (legacy
  plain string or single bare object also tolerated). **Gitignored.**
- `leads.json` — output leads `{ id, affiliate_link, platform, title, url, snippet,
  situation, status (new/opened/done), found_at }`. **Gitignored.**
- `workspace.json` — staged per-product stage outputs. **Gitignored.**
- `README.md` — user-facing setup/usage docs.

## Environment / config (`.env`, gitignored)
- `OLLAMA_MODEL`, `OLLAMA_API_BASE` — local model config
- `AMAZON_ASSOCIATE_TAG` / `AMAZON_AFFILIATE_TAG` — Amazon associate tag
- `AMAZON_TOP_N` — products per discover run (default 5)
- `SCORER_CONCURRENCY` — parallel scoring sessions (default 2)

## Dependencies (`requirements.txt`)
`google-adk`, `litellm`, `python-dotenv`, `ddgs`, `flask`, `requests`, `beautifulsoup4`

## How to run
```
python web_ui.py     # http://localhost:5000 — the control panel (primary interface)
python pipeline.py   # CLI alternative for full runs / dry runs
python main.py       # manual agent chat (debug)
```

## Verified behavior (as of 2026-06-10)
- Compile/import checks, Flask route map, dashboard render against real data.
- Live via API + local Ollama: queries job (7 queries staged), search job (20 candidates
  staged, chips render), 409 busy-lock while a job runs. Score stage reuses the
  `score_and_save` path verified in a CLI dry run + earlier full runs.

## Known limitations / honest assessment
- The local model is the weakest link: query quality and verdict quality scale with model
  size. Retries fix *parse* failures, not *judgment* failures — gemma says YES to almost
  everything in broad niches (see existing leads.json).
- Amazon best-seller scraping is best-effort; breaks when Amazon changes markup.
- Old posts (2010-era Quora) still surface and score YES — no recency filtering yet.
- Job state is in-memory: restarting the Flask app forgets the last job's log (workspace
  and leads persist on disk).
- No automated test suite — verification is the manual checks listed above.

## Ideas for the next pass
- Scorer returns a 1-10 relevance score; sort/filter leads by confidence in the UI.
- Post recency filtering (parse dates from snippets; skip threads older than ~1-2 years).
- Editable queries in the UI before running Search (the workspace makes this easy).
- Per-product delete/add in the Products panel (currently hand-edit links.json).
- Track which posted replies got clicks (per-lead tag variants or a link shortener).
