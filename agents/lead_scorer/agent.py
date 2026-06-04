import os
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "ollama/gemma4:e4b")
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
os.environ["OLLAMA_API_BASE"] = OLLAMA_API_BASE

INSTRUCTION = """You are a lead quality scorer for affiliate marketing.

You will receive a single search result and an affiliate product URL.
Decide whether this result represents a real person who is actively experiencing a 
problem that the affiliate product could genuinely solve.

Respond with ONLY this JSON object and nothing else:
{
  "verdict": "YES" or "NO",
  "situation": "One sentence explaining why this person needs the product, or why they don't qualify."
}

Score YES if:
- A real person is asking for help, venting frustration, or describing a specific problem
- The problem is clearly something the product addresses
- The post is from Reddit, Quora, or a forum (not an ad, article, or generic page)

Score NO if:
- It is a news article, listicle, or marketing page
- No specific person's problem is expressed
- The product would not plausibly help them
"""

root_agent = Agent(
    name="lead_scorer_agent",
    model=LiteLlm(model=OLLAMA_MODEL),
    instruction=INSTRUCTION,
)
