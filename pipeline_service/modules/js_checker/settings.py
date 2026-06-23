from pydantic import BaseModel


class JSCheckerConfig(BaseModel):
    execution_timeout_ms: int = 5000
    node_binary: str = "node"
