"""Notes CRUD tools — list and add."""

from models import get_notes

from tolokaforge.core.tools_interface import DomainToolRegistry


def register(registry: DomainToolRegistry) -> None:
    @registry.tool("List every stored note, newest first.")
    def list_notes(data: dict) -> list[dict]:
        return [n.model_dump() for n in reversed(get_notes(data))]

    @registry.tool("Save a new note. Returns the created note including its ID.")
    def add_note(data: dict, title: str, body: str) -> dict:
        notes = data.setdefault("notes", [])
        new_id = f"N-{len(notes) + 1:03d}"
        note = {"id": new_id, "title": title, "body": body}
        notes.append(note)
        return note
