from __future__ import annotations

from abc import ABC, abstractmethod

from pipeline.task import PipelineTask


class BaseModule(ABC):

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @abstractmethod
    async def process(self, task: PipelineTask) -> PipelineTask | None:
        raise NotImplementedError
