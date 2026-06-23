from __future__ import annotations

from pydantic import BaseModel, Field


class OSDPart(BaseModel):
    """One observed part of the object."""

    name: str = Field(
        ...,
        description=(
            "Short stable identifier — 'seat', 'front-left leg', 'lampshade'. "
            "Used by the Coder as a node id hint and by the Critic/Patcher as "
            "`target_node_id` for iterative refinement."
        ),
    )
    narrative: str = Field(
        ...,
        min_length=80,
        description=(
            "Two to five sentences covering shape, approximate size, position "
            "relative to the whole, color (hex if confident, else NL), "
            "material (NL phrasing that maps to PBR), and any modifier cue "
            "(bend/twist/taper). Written for a 3D artist to implement."
        ),
    )
    count_hint: str = Field(
        default="one",
        description=(
            "'one', 'four symmetric', 'two mirrored pairs', 'a single row of "
            "six'. Critical for the Coder — drives the instanced_group "
            "decision, so keep it as a structured field rather than burying "
            "it in the narrative."
        ),
    )
    motif_role: str | None = Field(
        default=None,
        description=(
            "Optional role hint — 'support', 'body', 'enclosure', "
            "'decoration'. Helps the Coder group related nodes into the "
            "right sub-group (legs_group, body_group, ...)."
        ),
    )


class OSD(BaseModel):
    """Object Structural Description."""

    object_type: str = Field(
        ...,
        description="Short lowercase noun — 'chair', 'goblet', 'bottle', 'car', 'lamp'.",
    )
    scene_brief: str = Field(
        ...,
        min_length=300,
        description=(
            "Markdown-formatted prose describing the whole object as if "
            "briefing a 3D artist. Expected sections (as ## headings inside "
            "the string):\n"
            "  ## Overall silhouette\n"
            "  ## Proportions\n"
            "  ## Materials and color palette\n"
            "  ## Layout and symmetry\n"
            "Typical length 600–2000 tokens. Coder reads this as the primary "
            "context — it is NOT a short summary."
        ),
    )
    parts: list[OSDPart] = Field(
        ...,
        description="Flat list of observed parts with per-part narratives.",
    )
    motif_hint: str | None = Field(
        default=None,
        description=(
            "If the object matches a known motif template (chair/table/"
            "bottle/mug/lamp/car/...), name it here. None if no clean match."
        ),
    )
    notes: str | None = Field(
        default=None,
        description="Overall caveats — occlusion, ambiguity, style cues. None if irrelevant.",
    )
