from __future__ import annotations

import json
import re
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from config.settings import ProviderRoutingConfig
from llm.tools import Tool, ToolError, ToolResult
from logger_config import logger
from utils.retry import async_retry

T = TypeVar("T", bound=BaseModel)



def _preview_output(raw: str, limit: int = 4000) -> str:
    if len(raw) <= limit:
        return raw
    head = raw[: limit // 2]
    tail = raw[-(limit // 2) :]
    return f"{head}\n…(+{len(raw) - limit} chars truncated)…\n{tail}"


class SessionError(Exception):
    """Base class for session-level failures."""


class ToolLoopExceeded(SessionError):
    """The tool-call loop exceeded `max_tool_iters`."""


class ValidationExhausted(SessionError):
    """Pydantic validation of the final text failed `max_validation_retries` times."""

    def __init__(self, message: str, last_error: str, last_raw: str) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.last_raw = last_raw


class SessionAgent(Generic[T]):
    """
    Per-task LLM conversation with tool-call loop.
    """

    def __init__(
        self,
        task_id: str,
        actor: str,
        system_prompt: str,
        model: str,
        tools: list[Tool] | None = None,
        response_model: type[T] | None = None,
        *,
        client: Any,
        temperature: float = 0.0,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        repetition_penalty: float | None = None,
        seed: int | None = None,
        max_tokens: int = 4096,
        max_tool_iters: int = 8,
        max_validation_retries: int = 3,
        reasoning_effort: str | None = None,
        backend: str = "openrouter",
        providers: ProviderRoutingConfig | None = None,
    ) -> None:
        self.task_id = task_id
        self.actor = actor
        self.model = model
        self.response_model = response_model
        self.client = client
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.min_p = min_p
        self.presence_penalty = presence_penalty
        self.repetition_penalty = repetition_penalty
        self.seed = seed
        self.max_tokens = max_tokens
        self.max_tool_iters = max_tool_iters
        self.max_validation_retries = max_validation_retries
        self.reasoning_effort = reasoning_effort
        self.backend = backend
        self.providers = providers

        self._tools: dict[str, Tool] = {t.name: t for t in (tools or [])}
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    # Tools management
    def register_tool(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def _tools_openai(self) -> list[dict[str, Any]] | None:
        if not self._tools:
            return None
        return [t.to_openai_schema() for t in self._tools.values()]

    # Main loop
    async def run(self, user_content: str | list[dict[str, Any]]) -> T | str:
        """Run one user → assistant round (with tool-calls + validation retries)."""
        self.messages.append({"role": "user", "content": user_content})
        return await self._run_to_validated()

    async def _run_to_validated(self) -> T | str:
        last_raw = ""
        last_err = ""
        for attempt in range(self.max_validation_retries + 1):
            raw = await self._chat_with_tool_loop()
            if self.response_model is None:
                return raw
            try:
                parsed = self.response_model.model_validate_json(raw)
                return parsed
            except ValidationError as ve:
                last_raw = raw
                last_err = str(ve)
                logger.warning(
                    f"Session agent validation failed Task ID: {self.task_id} | Actor: {self.actor} | Attempt: {attempt} | Error: {last_err}"
                )
                if attempt >= self.max_validation_retries:
                    break
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Previous JSON failed Pydantic validation:\n"
                            f"{last_err}\n\n"
                            "Return ONLY the corrected JSON object, matching the schema."
                        ),
                    }
                )
        raise ValidationExhausted(
            f"Response model validation exhausted after "
            f"{self.max_validation_retries + 1} attempts | Last Error: {last_err} | Last Raw: {last_raw}",
            last_error=last_err,
            last_raw=last_raw,
        )

    async def _chat_with_tool_loop(self) -> str:
        """Inner loop: call model, dispatch tool calls, return final text."""
        for it in range(self.max_tool_iters + 1):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": self.messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self.top_p is not None:
                kwargs["top_p"] = self.top_p
            if self.presence_penalty is not None:
                kwargs["presence_penalty"] = self.presence_penalty
            if self.seed is not None:
                kwargs["seed"] = self.seed
            tools_schema = self._tools_openai()
            if tools_schema is not None:
                kwargs["tools"] = tools_schema
                kwargs["tool_choice"] = "auto"
            if self.response_model is not None:
                # Hint to Qwen/OpenRouter that we want raw JSON back.
                kwargs["response_format"] = {"type": "json_object"}
            if self.reasoning_effort:
                if self.backend == "vllm":
                    kwargs["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": True},
                    }
                else:
                    kwargs["extra_body"] = {
                        "reasoning": {"effort": self.reasoning_effort},
                    }
            if self.providers is not None and self.backend != "vllm":
                kwargs.setdefault("extra_body", {})["provider"] = (
                    self.providers.model_dump(exclude_none=True)
                )

            for _k, _v in (
                ("top_k", self.top_k),
                ("min_p", self.min_p),
                ("repetition_penalty", self.repetition_penalty),
            ):
                if _v is not None:
                    kwargs.setdefault("extra_body", {})[_k] = _v
                    
            async def _make_call(_attempt: int, _last_err: str | None):
                try:
                    return await self.client.chat.completions.create(**kwargs)
                except Exception as exc:
                    err_str = str(exc)
                    if "max_tokens" in err_str and "too large" in err_str:
                        m = re.search(
                            r"context length is (\d+) tokens and your request has (\d+) input",
                            err_str,
                        )
                        if m:
                            available = int(m.group(1)) - int(m.group(2)) - 256
                            if 256 < available < kwargs["max_tokens"]:
                                logger.warning(
                                    f"Session Task {self.task_id} | max_tokens capped "
                                    f"{kwargs['max_tokens']} → {available} (context overflow)"
                                )
                                kwargs["max_tokens"] = available
                                return await self.client.chat.completions.create(**kwargs)
                    raise

            response = await async_retry(_make_call, max_retries=2)
            choice = response.choices[0]
            msg = choice.message

            usage = getattr(response, "usage", None)
            if usage is not None:
                logger.info(
                    f"[TOKENS Actor: {self.actor} Task:{self.task_id} Iteration:{it}] | "
                    f"Prompt: {usage.prompt_tokens} | Completion: {usage.completion_tokens} | "
                    f"Total: {usage.total_tokens} | Finish Reason: {choice.finish_reason} | "
                    f"Max Tokens Cap: {kwargs.get('max_tokens')}"
                )

            tool_calls = getattr(msg, "tool_calls", None) or []
            raw_content = msg.content or ""
            if tool_calls:
                tc_summary = ", ".join(
                    f"{tc.function.name}({_preview_output(tc.function.arguments or '{}', 400)})"
                    for tc in tool_calls
                )
                logger.info(
                    f"Session output Task ID: {self.task_id} | Actor: {self.actor} | Iteration: {it} | Tool Calls: [{tc_summary}] | Content Characters: {len(raw_content)}"
                )
            # Append the assistant turn verbatim (content + any tool_calls).
            assistant_turn: dict[str, Any] = {
                "role": "assistant",
                "content": raw_content,
            }
            if tool_calls:
                assistant_turn["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            self.messages.append(assistant_turn)

            if not tool_calls:
                return raw_content.strip()

            for tc in tool_calls:
                await self._dispatch_tool_call(tc)

        raise ToolLoopExceeded(
            f"Tool-call loop exceeded max_tool_iters={self.max_tool_iters} "
            f"for Task ID: {self.task_id} | Actor: {self.actor}"
        )

    async def _dispatch_tool_call(self, tc: Any) -> None:
        name = tc.function.name
        raw_args = tc.function.arguments or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError as e:
            payload = ToolResult(content=f"tool args JSON parse error: {e}", ok=False).to_payload()
            self._append_tool_result(tc.id, name, payload)
            return

        tool = self._tools.get(name)
        if tool is None:
            payload = ToolResult(content=f"unknown tool: {name}", ok=False).to_payload()
            self._append_tool_result(tc.id, name, payload)
            return

        try:
            result = await tool.run(**args)
        except ToolError as e:
            payload = ToolResult(content=str(e), ok=False).to_payload()
        except Exception as e:
            logger.exception(
                f"Session agent tool crash Task ID: {self.task_id} | Actor: {self.actor} | Tool: {name}"
            )
            payload = ToolResult(content=f"{type(e).__name__}: {e}", ok=False).to_payload()
        else:
            if isinstance(result, ToolResult):
                payload = result.to_payload()
            else:
                payload = ToolResult(content=result, ok=True).to_payload()

        logger.info(
            f"Session tool Task ID: {self.task_id} | Actor: {self.actor} | Tool: {name} | OK: {payload.get('ok', True)}"
        )
        self._append_tool_result(tc.id, name, payload)

    def _append_tool_result(self, tool_call_id: str, name: str, payload: dict[str, Any]) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": json.dumps(payload, ensure_ascii=False),
            }
        )
