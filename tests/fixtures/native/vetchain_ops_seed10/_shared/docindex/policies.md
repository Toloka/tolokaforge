# BluePine Veterinary Partners – Operations Policy Compendium

**Reference Time:** 2026-07-01T09:00:00  
**Domain:** vetchain_ops

---

## SOP: Controlled Substance Dispensing (`sop_controlled_dispense`)

Covers dispensing DEA-scheduled drugs from central pharmacy.

### pol_dea_registration_required (outcome: refuse)
A controlled-substance dispense request must be refused if the requesting
veterinarian has no `dea_registration` credential on file.

### pol_dea_registration_expired (outcome: refuse)
A controlled-substance dispense request must be refused if the requesting
veterinarian's `dea_registration` credential has `expiry_date < 2026-07-01T09:00:00`
or `status=expired`.

### pol_schedule_ii_witness_required (outcome: escalate)
A dispense of a `pharmacy_item` with `dea_schedule=II` must be held with
`status=held_witness_required` if no `witness_staff_id` is recorded on the
`dispense_request`.

### pol_controlled_dispense_log (outcome: conditional)
Every successful controlled-substance dispense must produce an immutable
`controlled_substance_log` entry capturing drug, quantity, dispensing vet,
witness (if applicable), and timestamp.

---

## SOP: Surgical Suite Booking (`sop_surgical_booking`)

### pol_surg_credential_required (outcome: refuse)
A surgical suite booking must be refused if the attending veterinarian does not
hold an active `surgical_certification` credential with
`expiry_date >= 2026-07-01T09:00:00`.

### pol_surg_suite_availability (outcome: refuse)
A surgical suite booking must be refused if the requested suite already has
`status=blocked` for any overlapping time window.

### pol_surg_exotic_anesthesia_escalate (outcome: escalate)
If the required anesthesia_protocol is exotic and the attending vet does not
hold an active `exotic_species_certification`, the booking must be escalated
to the medical director.

### pol_surg_preop_consult_required (outcome: conditional)
Every surgical booking must have a pre-op consult appointment created and linked
before the booking status may advance.

---

## SOP: Lab Panel Ordering (`sop_lab_panel_order`)

### pol_lab_species_compat (outcome: refuse)
A lab panel order must be refused if the panel_code is not compatible with the
patient's species.

### pol_lab_fasting_hold (outcome: escalate)
If a lab panel requires fasting and `patient.fasting_confirmed=false`, the lab
order must be held at `status=held_fasting`.

### pol_lab_stat_off_hours_escalate (outcome: escalate)
A stat-priority lab order submitted outside reference lab operating hours must
be escalated to the on-call lab coordinator.

### pol_lab_specimen_log (outcome: conditional)
A `specimen_log` entry must be created for every lab order that advances to
`status=specimen_collected`.

---

## SOP: Equipment Repair (`sop_equipment_repair`)

### pol_repair_two_step_lifecycle (outcome: conditional)
A repair order must be opened (`status=opened`) before a vendor can be assigned.

### pol_repair_critical_loaner (outcome: escalate)
When a repair order is opened for critical-criticality equipment, a loaner must
be allocated if available; if no loaner is available, escalate to ops director.

### pol_repair_vendor_reassign_blocked (outcome: refuse)
A repair order with `status=assigned` may not have its vendor_id changed without
an approved escalation from the ops director.

---

## SOP: Shift Coverage (`sop_shift_coverage`)

### pol_shift_credential_match (outcome: refuse)
A shift coverage assignment must be refused if the proposed staff member does
not hold an active credential matching the shift's `required_credential_type`.

### pol_shift_role_match (outcome: refuse)
A shift coverage assignment must be refused if the proposed staff member's role
does not match the shift's `required_role`.

### pol_shift_overtime_cap (outcome: refuse)
A shift coverage assignment must be refused if adding the shift's `hours_duration`
to the staff member's `weekly_hours_worked` would exceed 40 hours.

### pol_shift_float_authorization (outcome: refuse)
A cross-clinic shift coverage assignment must be refused if the staff member
does not have `float_authorized=true`.

### pol_shift_no_eligible_staff_escalate (outcome: escalate)
If no staff member meets all constraints for an open shift, the coverage request
must be escalated to the regional staffing coordinator.

### pol_shift_employment_active (outcome: refuse)
A shift coverage assignment must be refused if the proposed staff member's
`employment_status` is not active.

---

## SOP: Inventory Replenishment (`sop_inventory_replenish`)

### pol_inventory_par_trigger (outcome: conditional)
A replenishment order may only be created when
`clinic_inventory.quantity_on_hand < clinic_inventory.par_level`.

### pol_inventory_cold_chain_hold (outcome: escalate)
Fulfillment of a replenishment order for a cold-chain item must be held at
`status=held_cold_chain` if `cold_transport_confirmed=false`.

### pol_inventory_backorder_escalate (outcome: escalate)
If central warehouse quantity_on_hand is zero at fulfillment time, the
replenishment order must transition to `status=back_ordered` and an
`escalation_record` created for the supply chain manager.

---

## SOP: Billing Correction (`sop_billing_correction`)

### pol_billing_tier1_threshold (outcome: escalate)
A billing correction with `|adjustment_amount_usd| > 200` submitted by a
non-manager staff member must be escalated to a manager.

### pol_billing_tier2_threshold (outcome: escalate)
A billing correction with `|adjustment_amount_usd| > 1000` must be held for
director approval regardless of who submits it.

### pol_billing_insurance_preauth (outcome: refuse)
A billing correction for an insured client must be refused if
`insurance_preauth_number` is absent on the invoice; the `billing_correction`
record must still be written with `status=refused`.

### pol_billing_documentation_always (outcome: conditional)
A `billing_correction` record must be written and an `audit_log` entry created
for every correction attempt including those that are refused or held.

---

## SOP: Patient Transfer (`sop_patient_transfer`)

### pol_transfer_pharmacy_hold (outcome: conditional)
An inter-clinic patient transfer must be held at `status=held_pharmacy` if
`patient.active_pharmacy_hold=true`.

### pol_transfer_destination_vet (outcome: escalate)
An inter-clinic patient transfer must be escalated to the medical director if
the destination clinic has no active veterinarian able to treat the patient's
species.

### pol_transfer_record_migration (outcome: conditional)
All patient records must be migrated and `patient.home_clinic_id` must be
updated to the destination clinic as part of a completed transfer.

---

## SOP: Vaccine Lot Recall (`sop_vaccine_recall`)

### pol_vaccine_recall_lot_flag (outcome: conditional)
When a vaccine recall is processed, every `patient_vaccination` record
referencing the recalled lot must have `recall_flagged` set to `true` regardless
of which clinic administered the vaccine.

### pol_vaccine_recall_notification (outcome: conditional)
A `client_notification_task` must be created for every patient owner affected
by a vaccine recall; if `client.contact_complete=false`, the task status is
`pending_contact_info`.

---

## SOP: New Provider Onboarding (`sop_new_provider_onboarding`)

### pol_onboard_license_required (outcome: refuse)
A `system_access_grant` for `practice_management` to a veterinarian must be
refused if the staff member does not have an active `state_veterinary_license`.

### pol_onboard_pharmacy_access_dea (outcome: refuse)
A `system_access_grant` for `central_pharmacy` must be refused if the staff
member does not hold an active `dea_registration` or
`controlled_substance_handler` credential.

### pol_onboard_credential_not_expired (outcome: refuse)
Registering a credential whose `expiry_date < 2026-07-01T09:00:00` must be
refused during new provider onboarding.

### pol_onboard_employment_active_required (outcome: refuse)
System access grants and credential registrations may only be processed for
staff members with `employment_status=active`.

---

## Cross-Cutting Policies

### pol_staff_suspended_blocked (outcome: refuse)
A staff member with `employment_status=suspended` must be blocked from surgical
bookings, shift assignments, dispense requests, and all credentialed operations.

### pol_staff_on_leave_blocked (outcome: refuse)
A staff member with `employment_status=on_leave` must be blocked from shift
coverage assignments.

---

## Domain Constants

| Constant | Value |
|---|---|
| REFERENCE_TIME | 2026-07-01T09:00:00 |
| OVERTIME_WEEKLY_CAP_HOURS | 40 |
| BILLING_TIER1_THRESHOLD_USD | 200 |
| BILLING_TIER2_THRESHOLD_USD | 1000 |
| LOANER_CRITICALITY_THRESHOLD | critical |
| SCHEDULE_II_WITNESS_REQUIRED | true |
| PREOP_CONSULT_REQUIRED | true |
| BACKORDER_ESCALATION_STOCK_LEVEL | 0 |
