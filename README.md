# Affiliate Marketing Harness

An automated lead-finding machine for Amazon affiliate marketing that runs **100% free on a
local LLM** (Ollama). It finds products worth promoting, then scours Reddit and Quora for
real people actively asking for help with the exact problem that product solves — so the
only thing left for you to do is post a helpful reply with your affiliate link.

```
┌─────────────┐   ┌──────────────┐   ┌────────────┐   ┌─────────────┐   ┌──────────────┐
│  Discover    │ → │  Generate     │ → │  Search     │ → │  Score with  │ → │  Review       │
│  products    │   │  pain-point   │   │  Reddit /   │   │  local LLM   │   │  leads in     │
│  (Amazon.ca) │   │  queries (LLM)│   │  Quora (DDG)│   │  YES / NO    │   │  web UI       │
└─────────────┘   └──────────────┘   └────────────┘   └─────────────┘   └──────────────┘
```

Every stage can be run from the web UI — individually per product, or the whole pipeline
end to end — with a live log so you can watch what the model is doing.

## Quick start

1. **Install Ollama and pull a model** (anything small that fits your machine):
   ```bash
   ollama pull gemma3:4b
   ```

2. **Set up Python** (3.10+):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure `.env`:**
   ```bash
   OLLAMA_MODEL=ollama/gemma3:4b
   OLLAMA_API_BASE=http://localhost:11434
   AMAZON_ASSOCIATE_TAG=yourtag-20      # your Amazon Associates tag
   AMAZON_TOP_N=5                       # products per discovery run
   SCORER_CONCURRENCY=2                 # parallel LLM scoring sessions
   ```

4. **Run it:**
   ```bash
   python web_ui.py        # open http://localhost:5000 — everything runs from here
   ```

## The workflow (all from the web UI)

The dashboard at `http://localhost:5000` is the control panel. Jobs run one at a time in
the background with a live log panel.

1. **Discover products** — scrapes Amazon.ca best-sellers and saves them (with your
   affiliate tag baked in) to `links.json`. You can also hand-edit `links.json` —
   entries look like:
   ```json
   { "url": "https://www.amazon.ca/dp/XXXXXXXXXX", "title": "...", "description": "..." }
   ```
   A plain URL string also works; Amazon affiliate links are built automatically.

2. **Run the stages** — each product row has buttons for every stage, so you can run them
   one at a time and inspect the output between steps, or hit **▶ Run** for the whole
   chain:
   - **1. Queries** — the LLM generates 6-10 search queries phrased the way a frustrated
     person would post ("overwhelmed by my to-do list", not the product name). Saved to
     `workspace.json` — chips on the product row show what's staged.
   - **2. Search** — runs those queries against Reddit + Quora via DuckDuckGo, dedupes,
     and drops anything already in your leads. Candidates saved to `workspace.json`.
   - **3. Score** — the LLM verdicts each candidate: *is this a real person with a problem
     this product solves?* Every YES is saved to `leads.json` immediately, so you can kill
     a run anytime without losing progress.

   **▶ Run Full Pipeline** at the top does all stages for every product in `links.json`.

3. **Review & reply** — leads are grouped by product below the control panel:
   - **Open Lead** — opens the Reddit/Quora post in your browser (and marks it opened)
   - **Copy affiliate link** — one-click clipboard, on every product row and lead group
   - **Done / ✕** — track what you've handled, delete junk leads

   Writing the actual reply is deliberately left to you — a genuine, human reply is the
   one thing a small local model can't fake, and the thing that keeps your accounts alive.

## CLI (optional — everything is also in the UI)

```
python pipeline.py                      # full run over links.json
python pipeline.py --dry-run            # queries + search only — see candidates without scoring
python pipeline.py --only carhartt      # only links matching this text
python pipeline.py --limit 2            # first 2 links only
python pipeline.py --max-queries 6      # search more of the generated queries (default 4)
python pipeline.py --max-candidates 10  # score fewer candidates per link (default 20)
python pipeline.py --concurrency 1      # if your machine struggles with 2 parallel sessions
```

`--dry-run` is the cheap way to sanity-check a new product before committing to a long
scoring run.

## Project layout

| File | Role |
|---|---|
| `web_ui.py` | Flask control panel (port 5000) — run every stage, watch logs, review leads |
| `pipeline.py` | Pipeline stages + CLI — queries → search → score → save |
| `lead_scorer.py` | Per-candidate YES/NO scoring with retry on bad model output |
| `agent_runner.py` | Shared one-shot agent session helper |
| `agents/query_agent/` | Generates pain-point search queries |
| `agents/lead_scorer/` | Verdicts a single candidate |
| `agents/lead_finder/` | Batch-style candidate filter (kept as an alternative, unused) |
| `tools/search.py` | DuckDuckGo search with retry/backoff |
| `tools/amazon_ca.py` | Amazon.ca best-seller scraping + affiliate URL builder |
| `agent.py` / `main.py` | Standalone CLI chat loop for testing the local model — not part of the pipeline |
| `links.json` | Input: products to promote (gitignored — personal data) |
| `leads.json` | Output: approved leads (gitignored — personal data) |
| `workspace.json` | Staged per-product queries/candidates from UI runs (gitignored) |

## A note on disclosure

When you post replies with your affiliate link, disclose it ("affiliate link"). Amazon
Associates and the FTC both require disclosure, and undisclosed affiliate links are the
fastest way to get banned from a subreddit — honest replies that genuinely help are also
the ones that actually convert.

## Troubleshooting

- **"Could not parse queries" / candidates skipped** — small local models are bad at
  strict JSON. The harness already retries once with a sterner prompt; if it still happens
  constantly, try a slightly bigger model (`gemma3:12b`, `qwen3:8b`).
- **Searches return nothing** — DuckDuckGo rate-limits. The search tool retries with
  backoff, but if you hammer it, wait a few minutes.
- **Discover finds nothing** — Amazon changed their best-seller page layout, or you're
  rate-limited. Try again later; the scraper is best-effort by design.
- **A job seems stuck** — watch the live log panel; scoring 20 candidates on a small local
  model legitimately takes 10-20 minutes. Free has a price. Jobs run one at a time on
  purpose — the model can't handle more.
