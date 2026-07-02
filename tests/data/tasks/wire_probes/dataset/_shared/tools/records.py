"""Generic 'records' sandbox tools for the wire-probe smoke set.

The tools are deliberately NEUTRAL (no domain flavour) and STUBBED: they only
echo what they received, so the probe is a clean wire-SHAPE test, not a
capability test. Each tool's argument schema carries one or more
quirk-eliciting shapes (typed dict-map, discriminated union + nested $ref,
Decimal, array + free-form container) for the smoke probe set.

NOTE: no ``from __future__ import annotations`` here on purpose - FastMCP builds
the tool argument model from the live function signature and must resolve the
model types (EntryValue / Item) as real objects, not stringified forward refs.
"""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, WithJsonSchema

from models import Block, DeepOrder, EntryValue, Item, RecordLine, TreeNode

from tolokaforge.core.tools_interface import DomainToolRegistry

# Hand-authored allOf schema (AND-composition of two object subschemas) for the
# allOf probe. Synthetic, not copied from any dataset.
_ALLOF_SCHEMA = {
    "description": "Object satisfying BOTH parts: a (string) AND b (integer).",
    "allOf": [
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
    ],
}


def _entry_keys(entries: Any) -> list[str]:
    """Echo entry keys without assuming typed-vs-dict coercion (stub-safe)."""
    if isinstance(entries, dict):
        return list(entries.keys())
    return []


def register(registry: DomainToolRegistry) -> None:
    @registry.tool(
        "Update a record: set named entries (a map of key -> {amount}) and "
        "replace the record's tags."
    )
    def update_record(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-1001"])],
        entries: Annotated[
            dict[str, EntryValue],
            Field(
                description="Map of entry key -> {amount}.",
                examples=[{"example-key": {"amount": 5}}],
            ),
        ],
        tags: Annotated[
            list[str], Field(description="Tags to set on the record (replaces existing).")
        ],
    ) -> dict:
        # Non-scoring stub: echo the received shape so the wire form is observable.
        return {
            "ok": True,
            "record_id": record_id,
            "entry_keys": _entry_keys(entries),
            "tags": list(tags) if isinstance(tags, list) else tags,
        }

    @registry.tool(
        "Create an item. `item` is a discriminated union on `kind` (note | task)."
    )
    def create_item(
        data: dict,
        item: Annotated[Item, Field(description="The item to create.")],
    ) -> dict:
        item_repr = item.model_dump() if hasattr(item, "model_dump") else item
        return {"ok": True, "item": item_repr}

    @registry.tool("Set the unit price of a record.")
    def set_price(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-1001"])],
        amount: Annotated[
            Decimal, Field(ge=0, description="Unit price.", examples=["19.99"])
        ],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "amount": str(amount)}

    @registry.tool(
        "Attach free-form metadata to a record. `metadata` is an open object "
        "(any string keys -> any values)."
    )
    def attach_metadata(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-1001"])],
        metadata: Annotated[
            dict[str, Any],
            Field(
                description="Free-form metadata: arbitrary string keys to values.",
                examples=[{"source": "import", "priority": 2}],
            ),
        ],
    ) -> dict:
        keys = list(metadata.keys()) if isinstance(metadata, dict) else []
        return {"ok": True, "record_id": record_id, "metadata_keys": keys}

    @registry.tool(
        "Set line items on a record. `lines` is a map of SKU -> {qty, unit_price}."
    )
    def set_line_items(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-2001"])],
        lines: Annotated[
            dict[str, RecordLine],
            Field(
                description="Map of SKU -> {qty, unit_price}.",
                examples=[{"SKU-EXAMPLE": {"qty": 1, "unit_price": "1.00"}}],
            ),
        ],
    ) -> dict:
        skus = list(lines.keys()) if isinstance(lines, dict) else []
        return {"ok": True, "record_id": record_id, "skus": skus}

    @registry.tool(
        "Create a new record. Returns the created record's id, which later "
        "calls must reference."
    )
    def create_record(data: dict) -> dict:
        # Stub returns a fixed, non-guessable id the model must thread into the
        # next call (lifecycle-threading; a placeholder-id quirk is caught here).
        return {"ok": True, "record_id": "REC-7F3A9"}

    # ---- Tier-1 quirk probes (synthetic, hand-authored) ----

    @registry.tool("Look up information about a topic. Side-effect-free.")
    def get_info(
        data: dict,
        topic: Annotated[str, Field(description="Topic to look up.", examples=["alpha"])],
    ) -> dict:
        return {"ok": True, "topic": topic, "info": f"(info about {topic})"}

    @registry.tool("Save a free-text note on a record.")
    def write_note(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-3001"])],
        text: Annotated[
            str, Field(description="Note text (may contain newlines, quotes, unicode).")
        ],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "len": len(text) if isinstance(text, str) else 0}

    @registry.tool("Get the current system status. Takes no parameters.")
    def get_status(data: dict) -> dict:
        return {"ok": True, "status": "green"}

    @registry.tool("Set a record's priority. `level` is one of low | medium | high.")
    def set_priority(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-3002"])],
        level: Annotated[Literal["low", "medium", "high"], Field(description="Priority level.")],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "level": str(level)}

    @registry.tool(
        "Set a quota: `codes` must be exactly 2 UNIQUE codes; `amount` must be "
        ">= 10 and a multiple of 5."
    )
    def set_quota(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-3003"])],
        codes: Annotated[
            list[str],
            Field(min_length=2, max_length=2, json_schema_extra={"uniqueItems": True},
                  description="Exactly 2 unique codes."),
        ],
        amount: Annotated[int, Field(ge=10, multiple_of=5, description="Amount, >=10, multiple of 5.")],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "codes": codes, "amount": amount}

    @registry.tool("Link a record to an external reference (UUID + ISO-8601 timestamp).")
    def link_ref(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-3004"])],
        ref_id: Annotated[
            str, Field(json_schema_extra={"format": "uuid"}, description="External reference UUID.")
        ],
        when: Annotated[
            str, Field(json_schema_extra={"format": "date-time"}, description="Timestamp, ISO 8601.")
        ],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "ref_id": ref_id, "when": when}

    @registry.tool(
        "Submit a payload that must satisfy BOTH parts of an allOf schema "
        "(a: string AND b: integer)."
    )
    def submit_payload(
        data: dict,
        payload: Annotated[dict, WithJsonSchema(_ALLOF_SCHEMA)],
    ) -> dict:
        return {"ok": True, "keys": list(payload.keys()) if isinstance(payload, dict) else None}

    @registry.tool("Configure a connection. Flat schema: host and port only (no wrapper).")
    def configure(
        data: dict,
        host: Annotated[str, Field(description="Host.", examples=["db.internal"])],
        port: Annotated[int, Field(description="Port.", examples=[5432])],
    ) -> dict:
        return {"ok": True, "host": host, "port": port}

    @registry.tool(
        "Run a command with positional arguments. Note: this tool legitimately "
        "has a field named 'arguments' (collision bait for over-eager unwrapping)."
    )
    def run_command(
        data: dict,
        command: Annotated[str, Field(description="Command.", examples=["deploy"])],
        arguments: Annotated[list[str], Field(description="Positional CLI arguments.")],
    ) -> dict:
        return {"ok": True, "command": command, "argc": len(arguments) if isinstance(arguments, list) else 0}

    # ---- complexity bump: deep nesting + multi-hop/fan-in threading ----

    @registry.tool("Submit an order. `order` is a deeply-nested object (customer.address + line items).")
    def submit_order(
        data: dict,
        order: Annotated[DeepOrder, Field(description="The order (nested object).")],
    ) -> dict:
        o = order.model_dump() if hasattr(order, "model_dump") else order
        return {"ok": True, "order_id": o.get("order_id") if isinstance(o, dict) else None}

    @registry.tool(
        "Add a section to a record. Returns the new section id, which later calls must reference."
    )
    def add_section(
        data: dict,
        record_id: Annotated[str, Field(description="Record id from create_record.", examples=["REC-7F3A9"])],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "section_id": "SEC-3B1C"}

    @registry.tool(
        "Finalize a record. Requires BOTH the record id AND the section id returned by the earlier calls."
    )
    def finalize_record(
        data: dict,
        record_id: Annotated[str, Field(description="Record id from create_record.", examples=["REC-7F3A9"])],
        section_id: Annotated[str, Field(description="Section id from add_section.", examples=["SEC-3B1C"])],
    ) -> dict:
        return {"ok": True, "record_id": record_id, "section_id": section_id, "finalized": True}

    @registry.tool("Submit a tree. `root` is a recursive node whose children are themselves nodes.")
    def submit_tree(
        data: dict,
        root: Annotated[TreeNode, Field(description="Root node (recursive: children are nodes).")],
    ) -> dict:
        def count(n):
            n = n if isinstance(n, dict) else (n.model_dump() if hasattr(n, "model_dump") else {})
            return 1 + sum(count(c) for c in (n.get("children") or []))
        return {"ok": True, "node_count": count(root)}

    @registry.tool(
        "Submit content blocks. `blocks` is a heterogeneous array: each element is "
        "either a text block or an image block (discriminated on `kind`)."
    )
    def submit_blocks(
        data: dict,
        blocks: Annotated[list[Block], Field(min_length=1, description="Polymorphic array of text|image blocks.")],
    ) -> dict:
        kinds = [
            (b.get("kind") if isinstance(b, dict) else getattr(b, "kind", None)) for b in blocks
        ] if isinstance(blocks, list) else []
        return {"ok": True, "kinds": kinds}
