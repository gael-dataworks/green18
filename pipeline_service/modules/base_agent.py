from __future__ import annotations

from typing import Any

from config.settings import ActorConfig
from openai import AsyncOpenAI

class BaseAgent:
    def __init__(self, client: AsyncOpenAI, settings: ActorConfig) -> None:
        self.client = client
        self.model = settings.model
        self.max_tokens = settings.max_tokens
        self.seed = settings.seed
        self.temperature = settings.temperature
        self.top_p = settings.top_p
        self.top_k = settings.top_k
        self.min_p = settings.min_p
        self.presence_penalty = settings.presence_penalty
        self.repetition_penalty = settings.repetition_penalty
        self.backend = "openrouter" if settings.client == "openrouter" else "vllm"
        self.providers = settings.providers