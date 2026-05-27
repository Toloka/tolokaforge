"""Read-only Pydantic models for notes state entities."""

from pydantic import BaseModel, ConfigDict, Field

_cfg = ConfigDict(extra="ignore", frozen=True)


class Note(BaseModel):
    model_config = _cfg

    id: str = Field(description="Note identifier, e.g. 'N-001'")
    title: str
    body: str


def get_notes(data: dict) -> list[Note]:
    return [Note(**n) for n in data.get("notes", [])]
