from __future__ import annotations

import asyncio
import io
from typing import Any

from logger_config import logger
from modules.judge.embedder_settings import EmbedderConfig


class DinoEmbedder:
    def __init__(self, config: EmbedderConfig) -> None:
        self.config = config
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._np: Any = None
        self._device: str | None = None
        self._disabled = False
        self._load_lock = asyncio.Lock()

   

    def _load_sync(self) -> bool:
        """Import deps + load the model on the calling thread. Returns success."""
        try:
            import numpy as np  # type: ignore
            import torch  # type: ignore
            from transformers import AutoImageProcessor, AutoModel  # type: ignore
        except Exception as exc:
            logger.warning(f"[DINO] dependencies unavailable ({exc!r}); S2BV disabled")
            return False

        device = self.config.device or ("cuda" if torch.cuda.is_available() else "cpu")
        from_pretrained_kwargs = {
            "revision": self.config.revision,
            "token": self.config.hf_token,
            "trust_remote_code": self.config.trust_remote_code,
        }
        try:
            processor = AutoImageProcessor.from_pretrained(
                self.config.model_id, **from_pretrained_kwargs
            )
            model = (
                AutoModel.from_pretrained(self.config.model_id, **from_pretrained_kwargs)
                .eval()
                .to(device)
            )
        except Exception as exc:
            logger.warning(
                f"[DINO] failed to load model {self.config.model_id!r} on {device}: "
                f"{exc!r}; S2BV disabled"
            )
            return False

        self._np = np
        self._torch = torch
        self._processor = processor
        self._model = model
        self._device = device
        logger.info(f"[DINO] loaded {self.config.model_id} on {device}")
        return True

    async def _ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        if self._disabled:
            return False
        async with self._load_lock:
            if self._model is not None:
                return True
            if self._disabled:
                return False
            ok = await asyncio.to_thread(self._load_sync)
            if not ok:
                self._disabled = True
            return ok

    def _embed_sync(self, images: list[bytes]) -> Any:
        """Return an (N, D) float32 L2-normalized numpy array for the given PNGs."""
        from PIL import Image  # type: ignore

        torch = self._torch
        pil = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
        vecs = []
        bs = max(1, self.config.batch_size)
        for i in range(0, len(pil), bs):
            batch = pil[i : i + bs]
            inputs = self._processor(images=batch, return_tensors="pt").to(self._device)
            with torch.inference_mode():
                out = self._model(**inputs)
            pooled = getattr(out, "pooler_output", None)
            if pooled is None:
                pooled = out.last_hidden_state[:, 0]  # CLS token
            pooled = torch.nn.functional.normalize(pooled, dim=-1)
            vecs.append(pooled.detach().to("cpu").float().numpy())
        return self._np.concatenate(vecs, axis=0)

    async def embed_reference(self, image: bytes) -> Any | None:
        if not image or not await self._ensure_loaded():
            return None
        try:
            arr = await asyncio.to_thread(self._embed_sync, [image])
            return arr[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DINO] reference embedding failed: {exc!r}")
            return None

    async def build_candidate_npz(
        self, ref_vec: Any | None, views: dict[str, bytes]
    ) -> bytes | None:
        if ref_vec is None or not views or not await self._ensure_loaded():
            return None
        names = [n for n, b in views.items() if b]
        if not names:
            return None
        try:
            arr = await asyncio.to_thread(self._embed_sync, [views[n] for n in names])
            payload = {"prompt": ref_vec}
            for n, vec in zip(names, arr):
                payload[f"view_{n}"] = vec
            buf = io.BytesIO()
            self._np.savez(buf, **payload)
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DINO] candidate embedding failed: {exc!r}")
            return None
