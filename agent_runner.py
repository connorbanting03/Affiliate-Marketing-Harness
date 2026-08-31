"""
agent_runner.py — Shared one-shot agent execution.

Import run_agent_once() anywhere an agent needs to answer a single message
in a fresh, throwaway session (pipeline.py, lead_scorer.py, web_ui.py).

Importing this module also silences LiteLLM's background logging workers,
which otherwise error on interpreter exit.
"""

import logging
import uuid

import litellm

litellm.success_callback = []
litellm.failure_callback = []
litellm._async_success_callback = []
litellm._async_failure_callback = []
litellm.suppress_debug_info = True
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
logging.getLogger("litellm").setLevel(logging.CRITICAL)

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai.types import Content, Part  # noqa: E402


async def run_agent_once(agent, message: str, app_name: str) -> str:
    """Send one message to an agent in a fresh session and return its reply text."""
    session_service = InMemorySessionService()
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name=app_name,
        user_id="harness",
        session_id=session_id,
    )
    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=session_service,
    )
    user_message = Content(role="user", parts=[Part(text=message)])
    response_text = ""
    async for event in runner.run_async(
        user_id="harness",
        session_id=session_id,
        new_message=user_message,
    ):
        if event.is_final_response():
            if event.content and event.content.parts:
                response_text = event.content.parts[0].text
    return response_text
