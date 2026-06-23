"""Scene Coder — OSD → final Three.js module.

`SceneCoderAgent` (in `agent.py`) emits JS directly from the OSD. Prompts
live in `prompts.py`; the shared Three.js primitive reference is in
`threejs_reference.py`.
"""
from modules.scene_coder.agent import SceneCoderAgent

__all__ = ["SceneCoderAgent"]
