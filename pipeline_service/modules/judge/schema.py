from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class JudgeVerdict(BaseModel):
    """Pairwise verdict over two candidate renders."""

    winner: Literal["A", "B"] = Field(
        description="Which candidate render matches the reference better."
    )
    reason: str = Field(
        default="",
        description="One- or two-sentence justification — what tipped it.",
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description=(
            "How confident the judge is. 0.5 = coin-flip / "
            "near-identical; 1.0 = clearly one is better."
        ),
    )
    decided_by: str = Field(
        default="",
        description="Which multi-stage stage decided the duel (e.g. 'S1', 'S2 consensus').",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Full per-stage diagnostic output from evaluate_duel (slim views).",
    )

    @field_validator("winner", mode="before")
    @classmethod
    def _coerce_winner(cls, v):
        if isinstance(v, str):
            s = v.strip().upper()
            if s in ("A", "B"):
                return s
        return v
