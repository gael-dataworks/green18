from fastapi.responses import JSONResponse
from pipeline.state import MinerState


def _conflict(state: MinerState, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": msg, "current_status": state.status.value},
    )