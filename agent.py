import os
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "ollama/gemma4:e4b")
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")

os.environ["OLLAMA_API_BASE"] = OLLAMA_API_BASE

agent = Agent(
    name="local_ollama_agent",
    model=LiteLlm(model=OLLAMA_MODEL),
    instruction="You are a helpful assistant. Answer questions clearly and concisely.",
)
