"""Register all vetchain_ops tools with the MCP registry.

Implements operations required by scenario sent_mut_001 (controlled_dispense family):
  - verify_dea_registration   (pol_dea_registration_required, pol_dea_registration_expired)
  - dispense_controlled_substance (pol_dea_registration_required, pol_dea_registration_expired,
                                   pol_schedule_ii_witness_required, pol_controlled_dispense_log,
                                   pol_staff_suspended_blocked)
  - log_controlled_dispense   (pol_controlled_dispense_log, pol_schedule_ii_witness_required)

REFERENCE_TIME = 2026-07-01T09:00:00
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import DomainToolRegistry, ToolError

REFERENCE_TIME = datetime.fromisoformat("2026-07-01T09:00:00")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(data: dict, table: str, entity_id: str, id_field: str = "id") -> dict:
    """Retrieve a record from a state table or raise ToolError."""
    records = data.get(table, [])
    for rec in records:
        if rec.get(id_field) == entity_id:
            return rec
    raise ToolError(f"{table} '{entity_id}' not found")


def _find(data: dict, table: str, **filters) -> list[dict]:
    """Return all records matching all supplied keyword filters."""
    results = []
    for rec in data.get(table, []):
        if all(rec.get(k) == v for k, v in filters.items()):
            results.append(rec)
    return results


def _new_id(data: dict, table: str, prefix: str) -> str:
    """Generate a deterministic sequential ID that does not collide."""
    existing = {rec.get("id", "") for rec in data.get(table, [])}
    i = 1
    while True:
        candidate = f"{prefix}_{i:04d}"
        if candidate not in existing:
            return candidate
        i += 1


def _append(data: dict, table: str, record: dict) -> None:
    """Append a record to a state table, creating the table if absent."""
    data.setdefault(table, []).append(record)


def _write_audit(
    data: dict,
    *,
    operation_id: str,
    actor_staff_id: str,
    outcome: str,
    related_entity_type: str,
    related_entity_id: str,
    reason: str | None = None,
) -> dict:
    """Append an immutable audit_log entry and return it."""
    entry = {
        "id": _new_id(data, "audit_log", "aud"),
        "operation_id": operation_id,
        "actor_staff_id": actor_staff_id,
        "outcome": outcome,
        "related_entity_type": related_entity_type,
        "related_entity_id": related_entity_id,
        "timestamp": REFERENCE_TIME.isoformat(),
    }
    if reason is not None:
        entry["reason"] = reason
    _append(data, "audit_log", entry)
    return entry


def _check_dea(data: dict, vet_id: str, actor_for_audit: str) -> tuple[bool, str | None]:
    """
    Check DEA registration for vet_id.
    Returns (valid: bool, failure_code: str | None).
    Writes audit_log on failure only (caller writes on success via dispense audit).
    """
    creds = _find(data, "credential", staff_id=vet_id, credential_type="dea_registration")
    if not creds:
        _write_audit(
            data,
            operation_id="verify_dea_registration",
            actor_staff_id=actor_for_audit,
            outcome="refused",
            related_entity_type="staff_member",
            related_entity_id=vet_id,
            reason="DEA_MISSING",
        )
        return False, "DEA_MISSING"

    cred = creds[0]
    # Check status and expiry
    expiry_str = cred.get("expiry_date", "")
    try:
        expiry_dt = datetime.fromisoformat(expiry_str)
    except (ValueError, TypeError):
        expiry_dt = None

    if cred.get("status") == "expired" or (expiry_dt is not None and expiry_dt < REFERENCE_TIME):
        _write_audit(
            data,
            operation_id="verify_dea_registration",
            actor_staff_id=actor_for_audit,
            outcome="refused",
            related_entity_type="staff_member",
            related_entity_id=vet_id,
            reason="DEA_EXPIRED",
        )
        return False, "DEA_EXPIRED"

    return True, None


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


def register_all(registry: DomainToolRegistry) -> None:
    """Register every vetchain_ops tool with *registry*."""

    # ------------------------------------------------------------------
    # verify_dea_registration
    # op: pol_dea_registration_required, pol_dea_registration_expired
    # ------------------------------------------------------------------
    @registry.tool(
        "Verify that a veterinarian holds a valid (active, non-expired) DEA registration "
        "credential before allowing a controlled substance dispense. "
        "Returns {valid: bool, failure_code: str|null}. "
        "Writes audit_log on failure. "
        "Policy: pol_dea_registration_required, pol_dea_registration_expired"
    )
    def verify_dea_registration(
        data: dict,
        vet_id: Annotated[str, Field(description="Staff member ID of the requesting veterinarian")],
    ) -> dict:
        """op:verify_dea_registration pol:pol_dea_registration_required pol:pol_dea_registration_expired"""
        vet = _get(data, "staff_member", vet_id)
        valid, code = _check_dea(data, vet_id, actor_for_audit=vet_id)
        return {"valid": valid, "failure_code": code}

    # ------------------------------------------------------------------
    # dispense_controlled_substance
    # op: pol_dea_registration_required, pol_dea_registration_expired,
    #     pol_schedule_ii_witness_required, pol_controlled_dispense_log,
    #     pol_staff_suspended_blocked
    # ------------------------------------------------------------------
    @registry.tool(
        "Process a controlled substance dispense from central pharmacy to a clinic, "
        "enforcing DEA validity, schedule-class witness requirements, "
        "and creating the controlled substance log. "
        "Policy: pol_dea_registration_required, pol_dea_registration_expired, "
        "pol_schedule_ii_witness_required, pol_controlled_dispense_log, pol_staff_suspended_blocked"
    )
    def dispense_controlled_substance(
        data: dict,
        dispense_request_id: Annotated[
            str, Field(description="ID of the dispense_request record (must have status=pending)")
        ],
    ) -> dict:
        """op:dispense_controlled_substance pol:pol_dea_registration_required pol:pol_dea_registration_expired pol:pol_schedule_ii_witness_required pol:pol_controlled_dispense_log pol:pol_staff_suspended_blocked"""
        req = _get(data, "dispense_request", dispense_request_id)

        if req.get("status") != "pending":
            raise ToolError(
                f"dispense_request '{dispense_request_id}' status must be pending "
                f"(got {req.get('status')!r})"
            )

        vet_id = req["requesting_vet_id"]
        vet = _get(data, "staff_member", vet_id)

        # pol_staff_suspended_blocked — employment_status must be active
        if vet.get("employment_status") != "active":
            req["status"] = "refused"
            req["refusal_reason"] = "VET_SUSPENDED"
            _write_audit(
                data,
                operation_id="dispense_controlled_substance",
                actor_staff_id=vet_id,
                outcome="refused",
                related_entity_type="dispense_request",
                related_entity_id=dispense_request_id,
                reason="VET_SUSPENDED",
            )
            return {"status": "refused", "reason": "VET_SUSPENDED"}

        # pol_dea_registration_required / pol_dea_registration_expired
        valid, code = _check_dea(data, vet_id, actor_for_audit=vet_id)
        if not valid:
            req["status"] = "refused"
            req["refusal_reason"] = code
            _write_audit(
                data,
                operation_id="dispense_controlled_substance",
                actor_staff_id=vet_id,
                outcome="refused",
                related_entity_type="dispense_request",
                related_entity_id=dispense_request_id,
                reason=code,
            )
            return {"status": "refused", "reason": code}

        item_id = req["pharmacy_item_id"]
        item = _get(data, "pharmacy_item", item_id)
        quantity_requested = req["quantity_requested"]

        # Stock check
        qty_on_hand = item.get("quantity_on_hand", 0)
        if qty_on_hand < quantity_requested:
            req["status"] = "refused"
            req["refusal_reason"] = "INSUFFICIENT_STOCK"
            _write_audit(
                data,
                operation_id="dispense_controlled_substance",
                actor_staff_id=vet_id,
                outcome="refused",
                related_entity_type="dispense_request",
                related_entity_id=dispense_request_id,
                reason="INSUFFICIENT_STOCK",
            )
            return {"status": "refused", "reason": "INSUFFICIENT_STOCK"}

        # pol_schedule_ii_witness_required
        dea_schedule = item.get("dea_schedule", "none")
        witness_id = req.get("witness_staff_id")
        if dea_schedule == "II" and not witness_id:
            req["status"] = "held_witness_required"
            _write_audit(
                data,
                operation_id="dispense_controlled_substance",
                actor_staff_id=vet_id,
                outcome="escalated",
                related_entity_type="dispense_request",
                related_entity_id=dispense_request_id,
                reason="WITNESS_REQUIRED",
            )
            return {"status": "held_witness_required", "reason": "WITNESS_REQUIRED"}

        # All checks pass — execute dispense
        item["quantity_on_hand"] = qty_on_hand - quantity_requested
        req["status"] = "dispensed"

        # pol_controlled_dispense_log — create immutable log entry
        log_entry = {
            "id": _new_id(data, "controlled_substance_log", "csl"),
            "dispense_request_id": dispense_request_id,
            "pharmacy_item_id": item_id,
            "quantity_dispensed": quantity_requested,
            "dispensing_vet_id": vet_id,
            "dispensed_at": REFERENCE_TIME.isoformat(),
            "dea_schedule": dea_schedule,
        }
        if witness_id:
            log_entry["witness_staff_id"] = witness_id

        _append(data, "controlled_substance_log", log_entry)

        _write_audit(
            data,
            operation_id="dispense_controlled_substance",
            actor_staff_id=vet_id,
            outcome="allowed",
            related_entity_type="dispense_request",
            related_entity_id=dispense_request_id,
        )

        return {
            "status": "dispensed",
            "controlled_substance_log_id": log_entry["id"],
            "quantity_remaining": item["quantity_on_hand"],
        }

    # ------------------------------------------------------------------
    # log_controlled_dispense
    # op: pol_controlled_dispense_log, pol_schedule_ii_witness_required
    # ------------------------------------------------------------------
    @registry.tool(
        "Create an immutable controlled_substance_log entry for a completed dispense event. "
        "Requires dispense_request.status=dispensed. "
        "For Schedule II drugs witness_staff_id must be present. "
        "Policy: pol_controlled_dispense_log, pol_schedule_ii_witness_required"
    )
    def log_controlled_dispense(
        data: dict,
        dispense_request_id: Annotated[
            str,
            Field(description="ID of the already-dispensed dispense_request record"),
        ],
    ) -> dict:
        """op:log_controlled_dispense pol:pol_controlled_dispense_log pol:pol_schedule_ii_witness_required"""
        req = _get(data, "dispense_request", dispense_request_id)

        if req.get("status") != "dispensed":
            raise ToolError(
                f"dispense_request '{dispense_request_id}' must have status=dispensed "
                f"(got {req.get('status')!r}); call dispense_controlled_substance first"
            )

        item_id = req["pharmacy_item_id"]
        item = _get(data, "pharmacy_item", item_id)
        dea_schedule = item.get("dea_schedule", "none")
        witness_id = req.get("witness_staff_id")

        if dea_schedule == "II" and not witness_id:
            raise ToolError("MISSING_WITNESS_FOR_SCHEDULE_II: witness_staff_id required for Schedule II")

        # Idempotency: if a log entry already exists for this request, return it
        existing = _find(data, "controlled_substance_log", dispense_request_id=dispense_request_id)
        if existing:
            return {"status": "already_logged", "controlled_substance_log_id": existing[0]["id"]}

        vet_id = req["requesting_vet_id"]
        log_entry = {
            "id": _new_id(data, "controlled_substance_log", "csl"),
            "dispense_request_id": dispense_request_id,
            "pharmacy_item_id": item_id,
            "quantity_dispensed": req["quantity_requested"],
            "dispensing_vet_id": vet_id,
            "dispensed_at": REFERENCE_TIME.isoformat(),
            "dea_schedule": dea_schedule,
        }
        if witness_id:
            log_entry["witness_staff_id"] = witness_id

        _append(data, "controlled_substance_log", log_entry)

        return {"status": "logged", "controlled_substance_log_id": log_entry["id"]}
