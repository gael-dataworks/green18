"""Scene Planner — IMG → OSD.

Exposes the `OSD` Pydantic schema; the session-based agent lives in
`agent.py` and is wired into the pipeline graph by `pipeline_factory`.
"""
from modules.scene_planner.schema import OSD, OSDPart

__all__ = ["OSD", "OSDPart"]
