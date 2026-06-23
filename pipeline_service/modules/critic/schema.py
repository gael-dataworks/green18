from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class IssueKind(str, Enum):
    wrong_proportion  = "wrong_proportion"
    missing_part      = "missing_part"
    extra_part        = "extra_part"
    wrong_count       = "wrong_count"
    wrong_position    = "wrong_position"
    wrong_material    = "wrong_material"
    wrong_color       = "wrong_color"
    wrong_orientation = "wrong_orientation"


_ISSUE_KIND_ALIASES: dict[str, IssueKind] = {
    "missing_detail":     IssueKind.missing_part,
    "missing_feature":    IssueKind.missing_part,
    "missing_element":    IssueKind.missing_part,
    "extra_detail":       IssueKind.extra_part,
    "extra_feature":      IssueKind.extra_part,
    "extra_element":      IssueKind.extra_part,
    "wrong_size":         IssueKind.wrong_proportion,
    "wrong_dimension":    IssueKind.wrong_proportion,
    "wrong_dimensions":   IssueKind.wrong_proportion,
    "wrong_scale":        IssueKind.wrong_proportion,
    "wrong_aspect":       IssueKind.wrong_proportion,
    "wrong_aspect_ratio": IssueKind.wrong_proportion,
    "wrong_shape":        IssueKind.wrong_proportion,
    "wrong_form":         IssueKind.wrong_proportion,
    "wrong_design":       IssueKind.wrong_proportion,
    "wrong_geometry":     IssueKind.wrong_proportion,
    "wrong_texture":      IssueKind.wrong_material,
    "wrong_finish":       IssueKind.wrong_material,
    "wrong_surface":      IssueKind.wrong_material,
    "wrong_rotation":     IssueKind.wrong_orientation,
    "wrong_angle":        IssueKind.wrong_orientation,
    "wrong_placement":    IssueKind.wrong_position,
    "wrong_location":     IssueKind.wrong_position,
    "wrong_layout":       IssueKind.wrong_position,
}


class Severity(str, Enum):
    low    = "low"
    medium = "medium"
    high   = "high"


class Axis(str, Enum):
    x   = "x"
    y   = "y"
    z   = "z"
    all = "all"


class Issue(BaseModel):
    kind: IssueKind
    target_node_id: Optional[str] = None
    description: str = ""
    severity: Severity = Severity.medium

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, v: Any) -> Any:
        if isinstance(v, IssueKind):
            return v
        if isinstance(v, str):
            key = v.strip().lower().replace("-", "_").replace(" ", "_")
            if key in IssueKind._value2member_map_:
                return key
            if key in _ISSUE_KIND_ALIASES:
                return _ISSUE_KIND_ALIASES[key].value
            return IssueKind.missing_part.value
        return v


class CriticReport(BaseModel):
    overall_score: float = Field(ge=0.0, le=1.0)
    stop: bool = False
    issues: list[Issue] = Field(default_factory=list)
    matching_aspects: list[str] = Field(
        default_factory=list,
        description=(
            "Short phrases naming parts or aspects that already match the "
            "reference well (e.g. 'handle shape', 'overall silhouette', "
            "'body color'). Repair stage uses this as a preserve-list — "
            "explicit signal to the coder NOT to modify these parts, to "
            "prevent regression between iterations."
        ),
    )
