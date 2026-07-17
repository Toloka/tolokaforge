"""Pydantic models for every vetchain_ops ledger entity.

Primary key field is named ``id`` on all models per the TolokaForge contract.

Policy traceability IDs preserved in docstrings:
  pol_dea_registration_required, pol_dea_registration_expired,
  pol_controlled_dispense_log, pol_schedule_ii_witness_required,
  pol_staff_suspended_blocked, pol_surg_credential_required,
  pol_surg_suite_availability, pol_surg_exotic_anesthesia_escalate,
  pol_surg_preop_consult_required, pol_lab_species_compat,
  pol_lab_fasting_hold, pol_lab_stat_off_hours_escalate,
  pol_lab_specimen_log, pol_repair_two_step_lifecycle,
  pol_repair_critical_loaner, pol_repair_vendor_reassign_blocked,
  pol_shift_credential_match, pol_shift_role_match,
  pol_shift_overtime_cap, pol_shift_float_authorization,
  pol_shift_no_eligible_staff_escalate, pol_shift_employment_active,
  pol_inventory_par_trigger, pol_inventory_cold_chain_hold,
  pol_inventory_backorder_escalate, pol_billing_tier1_threshold,
  pol_billing_tier2_threshold, pol_billing_insurance_preauth,
  pol_billing_documentation_always, pol_transfer_pharmacy_hold,
  pol_transfer_destination_vet, pol_transfer_record_migration,
  pol_vaccine_recall_lot_flag, pol_vaccine_recall_notification,
  pol_onboard_license_required, pol_onboard_pharmacy_access_dea,
  pol_onboard_credential_not_expired, pol_onboard_employment_active_required,
  pol_staff_on_leave_blocked
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# clinic
# ---------------------------------------------------------------------------
class Clinic(BaseModel):
    """Represents one of the 14 BluePine companion-animal clinic locations.

    Invariants:
      - id is unique across all clinics
      - status must be active for any new bookings or staff assignments
    """
    id: str
    name: str
    state: str
    address: str
    status: str  # active | suspended | closed


# ---------------------------------------------------------------------------
# staff_member
# ---------------------------------------------------------------------------
class StaffMember(BaseModel):
    """A veterinarian, vet tech, or support staff employed at one or more BluePine clinics.

    Invariants:
      - employment_status determines eligibility (pol_staff_suspended_blocked,
        pol_staff_on_leave_blocked, pol_shift_employment_active)
      - Only veterinarian role may hold a dea_registration_id
      - weekly_hours_worked must not exceed 40 after any shift assignment
        (pol_shift_overtime_cap)
      - float_authorized must be true for cross-clinic assignments
        (pol_shift_float_authorization)
    """
    id: str
    name: str
    role: str  # veterinarian | vet_tech | receptionist | ops_manager | ops_director | regional_staffing_coordinator
    primary_clinic_id: str
    employment_status: str  # active | on_leave | suspended | terminated
    float_authorized: bool
    weekly_hours_worked: float
    dea_registration_id: Optional[str] = None
    dea_registration_expiry: Optional[datetime] = None


# ---------------------------------------------------------------------------
# credential
# ---------------------------------------------------------------------------
class Credential(BaseModel):
    """A professional credential held by a staff member with an expiry date.

    Invariants:
      - status must be active and expiry_date >= REFERENCE_TIME for validity
        (pol_surg_credential_required, pol_dea_registration_required,
         pol_dea_registration_expired, pol_onboard_credential_not_expired)
      - Each staff_member may hold at most one active credential per type
    """
    id: str
    staff_id: str
    credential_type: str  # state_veterinary_license | surgical_certification | anesthesia_certification | exotic_species_certification | dea_registration | controlled_substance_handler
    credential_number: str
    issuing_authority: str
    issued_date: datetime
    expiry_date: datetime
    status: str  # active | expired | suspended | pending_renewal


# ---------------------------------------------------------------------------
# patient
# ---------------------------------------------------------------------------
class Patient(BaseModel):
    """A companion animal patient registered at a BluePine clinic.

    Invariants:
      - home_clinic_id must reference an active clinic
      - active_pharmacy_hold=true blocks inter-clinic transfer
        (pol_transfer_pharmacy_hold)
    """
    id: str
    name: str
    species: str  # canine | feline | avian | reptile | small_mammal | equine
    breed: Optional[str] = None
    owner_id: str
    home_clinic_id: str
    active_pharmacy_hold: bool
    fasting_confirmed: bool


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
class Client(BaseModel):
    """A pet owner or account holder who owns one or more patients at BluePine.

    Invariants:
      - contact_complete=true requires at least one of email or phone
      - Insured clients must have both insurance_provider and insurance_policy_number
        (pol_billing_insurance_preauth)
    """
    id: str
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    insurance_provider: Optional[str] = None
    insurance_policy_number: Optional[str] = None
    contact_complete: bool


# ---------------------------------------------------------------------------
# surgical_suite
# ---------------------------------------------------------------------------
class SurgicalSuite(BaseModel):
    """A physical surgical suite within a clinic that can be blocked for procedures.

    Invariants:
      - status=blocked means suite is reserved for a booking
        (pol_surg_suite_availability)
      - A suite may not be double-booked
    """
    id: str
    clinic_id: str
    name: str
    status: str  # available | blocked | maintenance
    anesthesia_protocols_supported: List[str]


# ---------------------------------------------------------------------------
# surgical_booking
# ---------------------------------------------------------------------------
class SurgicalBooking(BaseModel):
    """A scheduled surgical procedure linking patient, vet, suite, and anesthesia protocol.

    Invariants:
      - attending_vet must hold active surgical_certification
        (pol_surg_credential_required)
      - If anesthesia_protocol=exotic, attending_vet must hold exotic_species_certification
        (pol_surg_exotic_anesthesia_escalate)
      - status=scheduled requires preop_consult_id before procedure date
        (pol_surg_preop_consult_required)
    """
    id: str
    patient_id: str
    attending_vet_id: str
    suite_id: str
    procedure_code: str
    scheduled_start: datetime
    scheduled_end: datetime
    anesthesia_protocol: str
    status: str  # scheduled | preop_pending | in_progress | completed | cancelled
    preop_consult_id: Optional[str] = None


# ---------------------------------------------------------------------------
# pharmacy_item
# ---------------------------------------------------------------------------
class PharmacyItem(BaseModel):
    """A drug or pharmaceutical product stocked in central pharmacy.

    Invariants:
      - quantity_on_hand must not go negative after any dispense
      - dea_schedule determines witness and log requirements
        (pol_schedule_ii_witness_required, pol_controlled_dispense_log)
    """
    id: str
    name: str
    dea_schedule: str  # none | II | III | IV | V
    quantity_on_hand: float
    unit: str
    requires_cold_chain: bool


# ---------------------------------------------------------------------------
# dispense_request
# ---------------------------------------------------------------------------
class DispenseRequest(BaseModel):
    """A request to dispense a controlled or non-controlled substance from central pharmacy.

    Invariants:
      - status=refused requires refusal_reason
      - If dea_schedule=II then witness_staff_id must be set before dispensed
        (pol_schedule_ii_witness_required)
    """
    id: str
    requesting_vet_id: str
    pharmacy_item_id: str
    quantity_requested: float
    patient_id: str
    clinic_id: str
    witness_staff_id: Optional[str] = None
    status: str  # pending | approved | dispensed | refused | held_witness_required
    refusal_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# controlled_substance_log
# ---------------------------------------------------------------------------
class ControlledSubstanceLog(BaseModel):
    """Immutable audit log entry for every controlled substance dispense event.

    Invariants:
      - Log entries are immutable once created
      - dea_schedule=II entries must have witness_staff_id set
        (pol_controlled_dispense_log, pol_schedule_ii_witness_required)
    """
    id: str
    dispense_request_id: str
    pharmacy_item_id: str
    quantity_dispensed: float
    dispensing_vet_id: str
    witness_staff_id: Optional[str] = None
    dispensed_at: datetime
    dea_schedule: str


# ---------------------------------------------------------------------------
# lab_order
# ---------------------------------------------------------------------------
class LabOrder(BaseModel):
    """A reference lab panel order for a patient specimen.

    Invariants:
      - status=refused requires refusal_reason (pol_lab_species_compat)
      - species_compatibility_verified must be true before advancing past pending
      - If fasting_required=true then fasting_confirmed must be true before
        specimen_collected (pol_lab_fasting_hold)
    """
    id: str
    patient_id: str
    ordering_vet_id: str
    panel_code: str
    species_compatibility_verified: bool
    priority: str  # routine | stat
    fasting_required: bool
    fasting_confirmed: bool
    status: str  # pending | specimen_collected | in_progress | resulted | refused | held_fasting
    clinic_id: str
    ordered_at: datetime
    refusal_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# specimen_log
# ---------------------------------------------------------------------------
class SpecimenLog(BaseModel):
    """Audit record created when a patient specimen is collected for a lab order.

    Invariants: one specimen_log per lab_order per collection event
    (pol_lab_specimen_log)
    """
    id: str
    lab_order_id: str
    collected_at: datetime
    collected_by_staff_id: str
    specimen_type: str


# ---------------------------------------------------------------------------
# equipment
# ---------------------------------------------------------------------------
class Equipment(BaseModel):
    """A piece of clinic equipment subject to repair and loaner management.

    Invariants:
      - criticality=critical triggers loaner allocation and ops director escalation
        (pol_repair_critical_loaner)
    """
    id: str
    clinic_id: str
    name: str
    equipment_type: str
    criticality: str  # critical | high | medium | low
    status: str  # operational | under_repair | loaner_in_use | decommissioned


# ---------------------------------------------------------------------------
# repair_order
# ---------------------------------------------------------------------------
class RepairOrder(BaseModel):
    """A two-step lifecycle repair ticket for clinic equipment.

    Invariants:
      - status=assigned requires vendor_id (pol_repair_two_step_lifecycle)
      - criticality_snapshot=critical requires loaner_equipment_id or escalation_record_id
        (pol_repair_critical_loaner)
    """
    id: str
    equipment_id: str
    clinic_id: str
    status: str  # opened | assigned | in_repair | completed | escalated
    criticality_snapshot: str  # critical | high | medium | low
    vendor_id: Optional[str] = None
    loaner_equipment_id: Optional[str] = None
    opened_at: datetime
    assigned_at: Optional[datetime] = None
    escalation_record_id: Optional[str] = None


# ---------------------------------------------------------------------------
# shift
# ---------------------------------------------------------------------------
class Shift(BaseModel):
    """An open or filled work shift at a clinic requiring a specific role and credentials.

    Invariants:
      - status=filled requires assigned_staff_id
      - assigned staff must have role matching required_role (pol_shift_role_match)
      - assigned staff must hold required_credential_type (pol_shift_credential_match)
    """
    id: str
    clinic_id: str
    shift_start: datetime
    shift_end: datetime
    required_role: str  # veterinarian | vet_tech | receptionist
    required_credential_type: Optional[str] = None
    status: str  # open | filled | cancelled
    assigned_staff_id: Optional[str] = None
    hours_duration: float


# ---------------------------------------------------------------------------
# shift_coverage_request
# ---------------------------------------------------------------------------
class ShiftCoverageRequest(BaseModel):
    """A request to fill an open shift, tracking eligibility checks and outcome.

    Invariants:
      - status=refused requires refusal_reason
      - status=escalated requires escalation_record_id
        (pol_shift_no_eligible_staff_escalate)
    """
    id: str
    shift_id: str
    requested_staff_id: str
    status: str  # pending | approved | refused | escalated
    refusal_reason: Optional[str] = None
    escalation_record_id: Optional[str] = None


# ---------------------------------------------------------------------------
# inventory_item
# ---------------------------------------------------------------------------
class InventoryItem(BaseModel):
    """A consumable supply item tracked in central warehouse and per-clinic par levels.

    Invariants:
      - central_quantity_on_hand must not go negative
      - requires_cold_chain=true items require cold_transport_confirmed before
        fulfillment (pol_inventory_cold_chain_hold)
    """
    id: str
    name: str
    item_type: str
    central_quantity_on_hand: float
    requires_cold_chain: bool
    unit: str


# ---------------------------------------------------------------------------
# clinic_inventory
# ---------------------------------------------------------------------------
class ClinicInventory(BaseModel):
    """Tracks current stock level and par level for an inventory item at a clinic.

    Invariants:
      - quantity_on_hand < par_level indicates replenishment needed
        (pol_inventory_par_trigger)
    """
    id: str
    clinic_id: str
    inventory_item_id: str
    quantity_on_hand: float
    par_level: float


# ---------------------------------------------------------------------------
# replenishment_order
# ---------------------------------------------------------------------------
class ReplenishmentOrder(BaseModel):
    """A request to transfer consumable inventory from central warehouse to a clinic.

    Invariants:
      - status=fulfilled requires central_quantity_on_hand >= quantity_requested
      - status=held_cold_chain: cold_transport_confirmed=false and requires_cold_chain=true
        (pol_inventory_cold_chain_hold)
      - status=back_ordered triggers escalation_record
        (pol_inventory_backorder_escalate)
    """
    id: str
    clinic_id: str
    inventory_item_id: str
    quantity_requested: float
    status: str  # pending | fulfilled | back_ordered | held_cold_chain | escalated
    cold_transport_confirmed: bool
    escalation_record_id: Optional[str] = None


# ---------------------------------------------------------------------------
# invoice
# ---------------------------------------------------------------------------
class Invoice(BaseModel):
    """A client billing invoice for services rendered at a clinic.

    Invariants:
      - Insured clients require insurance_preauth_number before billing correction
        (pol_billing_insurance_preauth)
    """
    id: str
    client_id: str
    clinic_id: str
    patient_id: str
    total_amount_usd: float
    insurance_preauth_number: Optional[str] = None
    status: str  # open | adjusted | paid | voided


# ---------------------------------------------------------------------------
# billing_correction
# ---------------------------------------------------------------------------
class BillingCorrection(BaseModel):
    """A request to adjust a client invoice amount, subject to tiered approval thresholds.

    Invariants:
      - Record must be written regardless of approval outcome
        (pol_billing_documentation_always)
      - status=refused requires refusal_reason (pol_billing_insurance_preauth)
      - |adjustment| > 1000 requires approval_tier_required=director
        (pol_billing_tier2_threshold)
      - |adjustment| > 200 requires approval_tier_required=manager or director
        (pol_billing_tier1_threshold)
    """
    id: str
    invoice_id: str
    requested_by_staff_id: str
    adjustment_amount_usd: float
    reason: str
    approval_tier_required: str  # staff | manager | director
    status: str  # pending | approved | applied | refused | held_approval
    approved_by_staff_id: Optional[str] = None
    refusal_reason: Optional[str] = None
    insurance_preauth_verified: bool


# ---------------------------------------------------------------------------
# patient_transfer
# ---------------------------------------------------------------------------
class PatientTransfer(BaseModel):
    """Records the inter-clinic transfer of a patient including rebooking and pharmacy handoff.

    Invariants:
      - status=held_pharmacy requires patient.active_pharmacy_hold=true
        (pol_transfer_pharmacy_hold)
      - status=completed requires patient.home_clinic_id updated
        (pol_transfer_record_migration)
    """
    id: str
    patient_id: str
    source_clinic_id: str
    destination_clinic_id: str
    status: str  # initiated | records_migrated | appointments_rebooked | pharmacy_handed_off | completed | held_pharmacy | escalated
    initiated_at: datetime
    escalation_record_id: Optional[str] = None


# ---------------------------------------------------------------------------
# vaccine_lot
# ---------------------------------------------------------------------------
class VaccineLot(BaseModel):
    """A batch of vaccines from a specific manufacturer lot, subject to recall.

    Invariants:
      - recall_status=recalled requires recall_reason (pol_vaccine_recall_lot_flag)
      - All patient_vaccination records referencing a recalled lot must be flagged
    """
    id: str
    lot_number: str
    manufacturer: str
    vaccine_product: str
    recall_status: str  # active | recalled | quarantined
    recall_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# patient_vaccination
# ---------------------------------------------------------------------------
class PatientVaccination(BaseModel):
    """A record of a vaccine administered to a patient from a specific lot.

    Invariants:
      - recall_flagged must be true when associated vaccine_lot.recall_status=recalled
        (pol_vaccine_recall_lot_flag)
    """
    id: str
    patient_id: str
    vaccine_lot_id: str
    administered_at: datetime
    administered_by_staff_id: str
    clinic_id: str
    recall_flagged: bool


# ---------------------------------------------------------------------------
# client_notification_task
# ---------------------------------------------------------------------------
class ClientNotificationTask(BaseModel):
    """A task to notify a patient owner about a vaccine recall or other clinical event.

    Invariants:
      - status=pending_contact_info when client.contact_complete=false
        (pol_vaccine_recall_notification)
      - Tasks must be created for all affected patients regardless of contact completeness
    """
    id: str
    client_id: str
    patient_id: str
    task_type: str  # vaccine_recall | appointment_rebooking | billing_notice
    status: str  # pending | sent | failed | pending_contact_info
    related_record_id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# vaccine_recall_record
# ---------------------------------------------------------------------------
class VaccineRecallRecord(BaseModel):
    """Master recall event record linking a recalled lot to all exposure flags and notifications.

    Invariants:
      - patients_flagged_count must equal count of patient_vaccination records
        with recall_flagged=true for this lot (pol_vaccine_recall_lot_flag)
    """
    id: str
    vaccine_lot_id: str
    initiated_at: datetime
    initiated_by_staff_id: str
    patients_flagged_count: int
    notifications_created_count: int
    status: str  # in_progress | completed


# ---------------------------------------------------------------------------
# system_access_grant
# ---------------------------------------------------------------------------
class SystemAccessGrant(BaseModel):
    """A record granting a staff member access to a specific system module.

    Invariants:
      - central_pharmacy module requires active dea_registration or
        controlled_substance_handler (pol_onboard_pharmacy_access_dea)
      - practice_management access requires active state_veterinary_license for vet role
        (pol_onboard_license_required)
      - status=refused requires refusal_reason
    """
    id: str
    staff_id: str
    system_module: str  # practice_management | central_pharmacy | reference_lab | facilities_inventory | staffing
    granted_at: datetime
    granted_by_staff_id: str
    status: str  # active | revoked | refused
    refusal_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# appointment
# ---------------------------------------------------------------------------
class Appointment(BaseModel):
    """A scheduled patient appointment at a clinic.

    Invariants:
      - status=rebooked requires rebooked_appointment_id
        (pol_transfer_record_migration)
      - Only status=open appointments are subject to rebooking during transfer
    """
    id: str
    patient_id: str
    clinic_id: str
    appointment_type: str  # routine | preop_consult | follow_up | specialist
    scheduled_at: datetime
    status: str  # open | cancelled | completed | rebooked
    transfer_id: Optional[str] = None
    rebooked_appointment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# escalation_record
# ---------------------------------------------------------------------------
class EscalationRecord(BaseModel):
    """Written escalation record created whenever an operation is held for elevated approval.

    Invariants:
      - Escalation records are immutable once created; resolution updates status only
      - Every escalated operation must produce exactly one escalation_record
    """
    id: str
    operation_context: str
    escalated_to: str
    reason: str
    related_entity_type: str
    related_entity_id: str
    status: str  # open | approved | rejected | resolved
    created_at: datetime


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------
class AuditLog(BaseModel):
    """Immutable audit trail for all policy-significant operations.

    Invariants:
      - Audit log entries are immutable once created
      - Every refusal and escalation must produce an audit_log entry
    """
    id: str
    operation_id: str
    actor_staff_id: str
    outcome: str  # allowed | refused | escalated
    reason: Optional[str] = None
    related_entity_type: str
    related_entity_id: str
    timestamp: datetime
