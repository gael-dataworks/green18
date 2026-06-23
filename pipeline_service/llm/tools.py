from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Awaitable


class ToolError(Exception):
    """Raised by a Tool.run() to signal a recoverable failure."""


@dataclass
class ToolResult:
    """Structured result from a Tool.run() call."""

    content: Any
    ok: bool = True

    def to_payload(self) -> dict[str, Any]:
        if self.ok:
            return {"result": self.content}
        return {"error": self.content}


class Tool:
    """Base class for a session-agent tool."""

    name: str = ""
    description: str = ""
    params_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_openai_schema(self) -> dict[str, Any]:
        """OpenAI Chat Completions `tools[i]` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


class FunctionTool(Tool):
    """Convenience wrapper to turn a plain async function into a Tool."""

    def __init__(
        self,
        name: str,
        description: str,
        params_schema: dict[str, Any],
        fn: Callable[..., Awaitable[Any]],
    ) -> None:
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self._fn = fn

    async def run(self, **kwargs: Any) -> Any:
        return await self._fn(**kwargs)
