"""Document-desk MCP server for the comparison-view reference pack.

Every write numbers its record by the table's length (``DOC-002``, ``COR-001``), the
way an application's sequence does, so a trajectory that files an extra draft files
the final document under a later id than the golden path does. The read tools log
each lookup. Both are what the task's ``comparison_view`` exists to see past.
"""

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import Field

from tolokaforge.core.tools_interface import ToolError, create_server

mcp, registry, TOOLS = create_server(__file__, "document-desk")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id(data: dict, table: str, prefix: str) -> str:
    return f"{prefix}-{len(data[table]) + 1:03d}"


def _find(data: dict, table: str, record_id: str, what: str) -> dict[str, Any]:
    record = next((row for row in data[table] if row["id"] == record_id), None)
    if record is None:
        raise ToolError(f"{what} '{record_id}' not found")
    return record


def _log_lookup(data: dict, subject: str) -> None:
    data["lookup_log"].append(
        {"id": _next_id(data, "lookup_log", "LK"), "subject": subject, "at": _now()}
    )


@registry.tool("Look a client up by id.")
def find_client(
    data: dict,
    client_id: Annotated[str, Field(description="Client identifier, e.g. 'C-1'")],
) -> dict:
    client = _find(data, "clients", client_id, "Client")
    _log_lookup(data, client_id)
    return dict(client)


@registry.tool("Look a filed document up by id.")
def lookup_document(
    data: dict,
    document_id: Annotated[str, Field(description="Document identifier, e.g. 'DOC-001'")],
) -> dict:
    document = _find(data, "documents", document_id, "Document")
    _log_lookup(data, document_id)
    return dict(document)


@registry.tool(
    "File a client's source document (an invoice, a receipt) as a final document. "
    "Returns the filed document with its generated id."
)
def file_document(
    data: dict,
    client_id: Annotated[str, Field(description="Client the document belongs to")],
    source_id: Annotated[str, Field(description="The source document's own number")],
    kind: Annotated[str, Field(description="Document kind, e.g. 'invoice'")],
) -> dict:
    _find(data, "clients", client_id, "Client")
    document = {
        "id": _next_id(data, "documents", "DOC"),
        "client_id": client_id,
        "source_id": source_id,
        "kind": kind,
        "status": "final",
        "created_at": _now(),
    }
    data["documents"].append(document)
    return dict(document)


@registry.tool("Supersede a document filed in error. It stays on file as superseded.")
def supersede_document(
    data: dict,
    document_id: Annotated[str, Field(description="Document to supersede")],
) -> dict:
    document = _find(data, "documents", document_id, "Document")
    document["status"] = "superseded"
    return dict(document)


@registry.tool("Place a payment hold for a client.")
def place_hold(
    data: dict,
    client_id: Annotated[str, Field(description="Client the hold is for")],
    amount: Annotated[float, Field(description="Amount held", gt=0)],
) -> dict:
    _find(data, "clients", client_id, "Client")
    hold = {
        "id": _next_id(data, "holds", "HOLD"),
        "client_id": client_id,
        "amount": amount,
        "status": "active",
        "created_at": _now(),
    }
    data["holds"].append(hold)
    return dict(hold)


@registry.tool("Release a payment hold. It stays on file as released.")
def release_hold(
    data: dict,
    hold_id: Annotated[str, Field(description="Hold to release")],
) -> dict:
    hold = _find(data, "holds", hold_id, "Hold")
    hold["status"] = "released"
    return dict(hold)


@registry.tool(
    "File a correction against a document, optionally citing the hold it concerns. "
    "Returns the filed correction with its generated id."
)
def file_correction(
    data: dict,
    document_id: Annotated[str, Field(description="Document the correction corrects")],
    reason: Annotated[str, Field(description="What is wrong and what it should be")],
    hold_id: Annotated[str | None, Field(description="Hold the correction concerns")] = None,
) -> dict:
    _find(data, "documents", document_id, "Document")
    if hold_id is not None:
        _find(data, "holds", hold_id, "Hold")
    correction = {
        "id": _next_id(data, "corrections", "COR"),
        "document_ref": document_id,
        "reason": reason,
        "hold_ref": hold_id,
        "created_at": _now(),
    }
    data["corrections"].append(correction)
    return dict(correction)


if __name__ == "__main__":
    mcp.run(transport="stdio")
