from pydantic import BaseModel


class PromptItem(BaseModel):
    stem: str
    image_url: str


class GenerateRequest(BaseModel):
    prompts: list[PromptItem]
    seed: int = 42
