from __future__ import annotations

from typing import Callable

from llm.session import SessionAgent


class SessionStore:
    """
    In-memory store of per-task, per-actor SessionAgents.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], SessionAgent] = {}

    def get_or_create(
        self,
        task_id: str,
        actor: str,
        factory: Callable[[str, str], SessionAgent],
    ) -> SessionAgent:
        key = (task_id, actor)
        sess = self._sessions.get(key)
        if sess is None:
            sess = factory(task_id, actor)
            self._sessions[key] = sess
        return sess

    def get(self, task_id: str, actor: str) -> SessionAgent | None:
        return self._sessions.get((task_id, actor))

    def evict(self, task_id: str) -> int:
        """Drop every session for this task. Returns count evicted."""
        keys = [k for k in self._sessions if k[0] == task_id]
        for k in keys:
            del self._sessions[k]
        return len(keys)

    def evict_actor(self, task_id: str, actor: str) -> bool:
        """Drop a single session by (task_id, actor). Returns True if removed."""
        return self._sessions.pop((task_id, actor), None) is not None

    def clear(self) -> int:
        """Drop every session. Returns count cleared."""
        n = len(self._sessions)
        self._sessions.clear()
        return n

    def rename_actor(self, task_id: str, from_actor: str, to_actor: str) -> bool:
        """Promote a per-candidate session to the canonical actor name.

        Used by multigen: after the judge picks a winner among `coder#k0`,
        `coder#k1`, ... we rename the winner's session to plain `coder` so
        refinement can call `session_store.get(task_id, "coder")`. If a
        previous session occupies the target slot, it is overwritten.
        """
        sess = self._sessions.pop((task_id, from_actor), None)
        if sess is None:
            return False
        sess.actor = to_actor
        self._sessions[(task_id, to_actor)] = sess
        return True

    def __len__(self) -> int:
        return len(self._sessions)
