"""Shared LLM session + tool-use primitives for Agent Flow v2.

Three pieces:
  - `session.SessionAgent`     — per-task conversation with a tool-call loop,
                                 built on `AsyncOpenAI` (OpenRouter-compatible).
  - `tools.Tool`               — base for tools agents can call; dispatched
                                 inside `SessionAgent.run()`.
  - `session_store.SessionStore` — in-memory `{task_id → {actor → SessionAgent}}`,
                                 evicted on terminal task state.
"""
from llm.session import SessionAgent, SessionError, ToolLoopExceeded, ValidationExhausted
from llm.session_store import SessionStore
from llm.tools import Tool, ToolError, ToolResult

__all__ = [
    "SessionAgent",
    "SessionError",
    "ToolLoopExceeded",
    "ValidationExhausted",
    "SessionStore",
    "Tool",
    "ToolError",
    "ToolResult",
]
