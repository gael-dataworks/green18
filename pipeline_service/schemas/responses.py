from typing import Any

from pydantic import BaseModel


class StatusResponse(BaseModel):
    status: str
    progress: int
    total: int
    payload: Any = None


class GenerateAccepted(BaseModel):
    accepted: int
