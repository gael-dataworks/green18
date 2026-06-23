from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from pipeline.state import ServiceState
from pydantic import BaseModel, ConfigDict


class ProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str                       
    ok: bool
    state: ServiceState
    error: Optional[str] = None

    @classmethod
    def running(cls, m: str) -> "ProbeResult":
        return cls(name=m, ok=True, state=ServiceState.RUNNING)

    @classmethod
    def starting(cls, m: str, reason: str) -> "ProbeResult":
        return cls(name=m, ok=False, state=ServiceState.STARTING, error=reason)

    @classmethod
    def down(cls, m: str, reason: str) -> "ProbeResult":
        return cls(name=m, ok=False, state=ServiceState.DOWN, error=reason)

    def __str__(self) -> str:
        err = f" | {self.error}" if self.error else ""
        return f"{self.name} | {self.state.value}{err}"
