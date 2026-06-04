import os
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "ollama/gemma4:e4b")
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
os.environ["OLLAMA_API_BASE"] = OLLAMA_API_BASE

INSTRUCTION = """You output only raw JSON arrays. Never output prose, explanations, or markdown.

You will receive a list of search results and an affiliate product URL.
Identify results where a real person expresses a problem the product would solve.

Output format — start with [ and end with ] with no other text:
[
  {
    "title": "post title",
    "url": "https://...",
    "snippet": "excerpt",
    "platform": "reddit.com",
    "situation": "One sentence: why this person needs the product."
  }
]

If no results qualify, output: []
"""

root_agent = Agent(
    name="lead_finder_agent",
    model=LiteLlm(model=OLLAMA_MODEL),
    instruction=INSTRUCTION,
)
