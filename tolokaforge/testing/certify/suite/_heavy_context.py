"""A heavy agentic context for capability probes that need one.

Some route defects only show under load: a long policy system prompt, a
dozen-plus nested tool schemas and several prior turns with tool results.
``z-ai/glm-5.3`` via OpenRouter thinks normally at ``effort=medium`` on a
short prompt yet degrades to zero reasoning tokens - and drops mandated
tool fields - once the context looks like a real evaluation turn
(2026-08-24/25). Probes that exercise reasoning under a requested effort
therefore run against THIS context rather than a one-liner.

Everything here is original and synthetic - a fictional field-service
company, its policy, its tools and one support conversation. Nothing is
copied from any evaluation task pack (this repository is public). The
shape mirrors what an evaluation turn carries: ~4k tokens of policy plus
~7k tokens of tool schemas (22 tools, four to six levels deep; ~11k prompt
tokens on the wire), ten prior messages, and a
final user turn that requires a create call carrying a field the policy
mandates be copied from an earlier lookup (:data:`MANDATED_FIELD`).

Public surface: :data:`SYSTEM_PROMPT`, :data:`TOOLS`, :data:`MESSAGES`,
:data:`CREATE_TOOL`, :data:`MANDATED_FIELD`, :data:`EXPECTED_ACCOUNT_ID`,
:func:`mandated_field_present`.
"""

from __future__ import annotations

import json
from typing import Any

from tolokaforge.core.models import Message, MessageRole, ToolCall

__all__ = [
    "CREATE_TOOL",
    "EXPECTED_ACCOUNT_ID",
    "MANDATED_FIELD",
    "MESSAGES",
    "SYSTEM_PROMPT",
    "TOOLS",
    "mandated_field_present",
]

CREATE_TOOL = "helpdesk_create_record"
MANDATED_FIELD = "account_id"
EXPECTED_ACCOUNT_ID = "ACC-77120"

# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------


def _s(type_: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": description, **extra}


def _obj(description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _tool(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


_ADDRESS = _obj(
    "A postal address as recorded in the account system.",
    {
        "line_1": _s("string", "Street and number."),
        "line_2": _s("string", "Unit, floor or building, when present."),
        "city": _s("string", "City or locality."),
        "region": _s("string", "State, province or region code."),
        "postal_code": _s("string", "Postal code exactly as stored, including spaces."),
        "country": _s("string", "ISO 3166-1 alpha-2 country code."),
    },
    ["line_1", "city", "country"],
)

_CONTACT = _obj(
    "A person attached to an account, site or record.",
    {
        "name": _s("string", "Full name as stored."),
        "role": _s(
            "string",
            "Role at the account: site_manager, technician, accounts_payable, security, other.",
            enum=["site_manager", "technician", "accounts_payable", "security", "other"],
        ),
        "email": _s("string", "Work e-mail address."),
        "phone": _s("string", "Phone number in E.164 form."),
        "preferred_channel": _s(
            "string", "How this contact wants to be reached.", enum=["email", "phone", "portal"]
        ),
        "address": _ADDRESS,
    },
    ["name", "role"],
)

_LINE_ITEM = _obj(
    "One line of a request, order or record - a part, a labour block or a fee.",
    {
        "kind": _s("string", "What the line is.", enum=["part", "labour", "fee", "credit"]),
        "sku": _s("string", "Catalogue SKU for parts; empty for labour and fees."),
        "description": _s("string", "Human-readable description of the line."),
        "quantity": _s("number", "Quantity; hours for labour lines."),
        "unit_price": _s("number", "Unit price in the account's billing currency."),
        "warranty": _obj(
            "Warranty coverage that applies to this line, if any.",
            {
                "covered": _s("boolean", "Whether the warranty covers this line."),
                "contract_id": _s("string", "Service contract the coverage comes from."),
                "expires_on": _s("string", "ISO date the coverage expires."),
            },
            ["covered"],
        ),
    },
    ["kind", "description", "quantity"],
)

_RECORD_DETAILS = _obj(
    "Type-specific payload of a helpdesk record. Populate the branch that matches `table`.",
    {
        "summary": _s("string", "One-line summary shown in list views. Max 120 characters."),
        "narrative": _s(
            "string", "Full description in the requester's own words, then the agent's findings."
        ),
        "severity": _s(
            "string",
            "Business impact.",
            enum=["s1_outage", "s2_degraded", "s3_minor", "s4_question"],
        ),
        "category": _s(
            "string",
            "Primary category.",
            enum=["equipment", "billing", "access", "safety", "scheduling", "other"],
        ),
        "subcategory": _s("string", "Free-text refinement of the category."),
        "site_id": _s("string", "Site the record concerns, when it concerns one."),
        "asset_tags": _s(
            "array",
            "Asset tags involved, exactly as printed on the equipment.",
            items=_s("string", "One asset tag."),
        ),
        "lines": _s("array", "Cost or credit lines attached to the record.", items=_LINE_ITEM),
        "requested_by": _CONTACT,
        "due_by": _s("string", "ISO 8601 timestamp the requester needs a resolution by."),
        "attachments": _s(
            "array",
            "Attachment references already uploaded to the portal.",
            items=_obj(
                "One attachment.",
                {
                    "attachment_id": _s("string", "Portal attachment id."),
                    "label": _s("string", "Display label."),
                },
                ["attachment_id"],
            ),
        ),
    },
    ["summary", "narrative", "severity", "category"],
)

_RECORD = _obj(
    "A helpdesk record: the envelope every table shares plus the type-specific details.",
    {
        "table": _s(
            "string",
            "Which helpdesk table receives the record.",
            enum=["complaints", "service_requests", "billing_disputes", "access_requests"],
        ),
        MANDATED_FIELD: _s(
            "string",
            "The account the record is filed under. MANDATORY: copy it verbatim from the account lookup earlier in the conversation; never invent, derive or omit it.",
        ),
        "priority": _s("string", "Queue priority.", enum=["p1", "p2", "p3", "p4"]),
        "assignment_group": _s(
            "string",
            "Group that will work the record.",
            enum=["field_ops", "billing", "site_security", "dispatch", "quality"],
        ),
        "details": _RECORD_DETAILS,
        "linked_records": _s(
            "array", "Ids of related records in any table.", items=_s("string", "A record id.")
        ),
        "tags": _s("array", "Free-form tags.", items=_s("string", "One tag.")),
    },
    ["table", MANDATED_FIELD, "priority", "details"],
)

TOOLS: list[dict[str, Any]] = [
    _tool(
        "people_get_employee",
        "Look up an employee of the company by id or badge number. Returns identity, department, manager and employment status.",
        _obj(
            "Employee lookup.",
            {
                "employee_id": _s("string", "Employee id such as E-10432."),
                "badge": _s("string", "Badge number, used when the employee id is unknown."),
            },
            [],
        ),
    ),
    _tool(
        "people_get_timecard",
        "Return an employee's timecard for a pay period, with every shift and its approval state.",
        _obj(
            "Timecard lookup.",
            {
                "employee_id": _s("string", "Employee id."),
                "period_start": _s("string", "ISO date the pay period starts."),
                "period_end": _s("string", "ISO date the pay period ends."),
            },
            ["employee_id", "period_start", "period_end"],
        ),
    ),
    _tool(
        "people_submit_time_correction",
        "Submit a correction to a timecard shift. Requires the manager's approval reference.",
        _obj(
            "Time correction.",
            {
                "employee_id": _s("string", "Employee id."),
                "shift_date": _s("string", "ISO date of the shift."),
                "correction": _obj(
                    "What to change.",
                    {
                        "clock_in": _s("string", "Corrected clock-in, ISO time."),
                        "clock_out": _s("string", "Corrected clock-out, ISO time."),
                        "reason": _s("string", "Why the correction is needed."),
                        "approval_ref": _s("string", "Manager approval reference."),
                    },
                    ["reason", "approval_ref"],
                ),
            },
            ["employee_id", "shift_date", "correction"],
        ),
    ),
    _tool(
        "people_get_leave_balance",
        "Return an employee's leave balances by type.",
        _obj("Leave balance.", {"employee_id": _s("string", "Employee id.")}, ["employee_id"]),
    ),
    _tool(
        "fleet_get_vehicle",
        "Return a fleet vehicle's record: registration, assigned technician, service history, open defects.",
        _obj(
            "Vehicle lookup.",
            {
                "vehicle_id": _s("string", "Fleet id such as V-208."),
                "registration": _s("string", "Registration plate, when the fleet id is unknown."),
            },
            [],
        ),
    ),
    _tool(
        "fleet_list_vehicles",
        "List fleet vehicles matching filters.",
        _obj(
            "Vehicle filter.",
            {
                "depot": _s("string", "Depot code."),
                "status": _s(
                    "string",
                    "Availability.",
                    enum=["available", "in_service", "in_repair", "retired"],
                ),
                "assigned_to": _s("string", "Employee id of the assigned technician."),
            },
            [],
        ),
    ),
    _tool(
        "fleet_report_defect",
        "Report a defect on a fleet vehicle. Creates a defect record and, for safety-critical defects, grounds the vehicle.",
        _obj(
            "Defect report.",
            {
                "vehicle_id": _s("string", "Fleet id."),
                "defect": _obj(
                    "The defect.",
                    {
                        "component": _s(
                            "string",
                            "Component affected.",
                            enum=[
                                "brakes",
                                "tyres",
                                "lights",
                                "steering",
                                "body",
                                "electrics",
                                "other",
                            ],
                        ),
                        "description": _s("string", "What was observed."),
                        "safety_critical": _s("boolean", "Whether the vehicle must be grounded."),
                        "reported_by": _CONTACT,
                    },
                    ["component", "description", "safety_critical"],
                ),
            },
            ["vehicle_id", "defect"],
        ),
    ),
    _tool(
        "facilities_search_work_orders",
        "Search facilities work orders by site, status, asset or free text.",
        _obj(
            "Work-order search.",
            {
                "site_id": _s("string", "Site id."),
                "status": _s(
                    "string",
                    "Work-order status.",
                    enum=["open", "scheduled", "in_progress", "on_hold", "closed"],
                ),
                "asset_tag": _s("string", "Asset tag."),
                "query": _s("string", "Free-text search over title and notes."),
                "limit": _s("integer", "Maximum rows to return, 1-50."),
            },
            [],
        ),
    ),
    _tool(
        "facilities_get_work_order",
        "Return one work order with its full history.",
        _obj(
            "Work-order lookup.",
            {"work_order_id": _s("string", "Work order id such as WO-55021.")},
            ["work_order_id"],
        ),
    ),
    _tool(
        "facilities_create_work_order",
        "Create a facilities work order.",
        _obj(
            "New work order.",
            {
                "site_id": _s("string", "Site id."),
                "title": _s("string", "Short title."),
                "description": _s("string", "What needs doing and why."),
                "asset_tags": _s("array", "Assets involved.", items=_s("string", "Asset tag.")),
                "priority": _s("string", "Priority.", enum=["p1", "p2", "p3", "p4"]),
                "requested_by": _CONTACT,
                "lines": _s("array", "Estimated cost lines.", items=_LINE_ITEM),
            },
            ["site_id", "title", "description", "priority"],
        ),
    ),
    _tool(
        "facilities_update_work_order",
        "Update fields on an existing work order.",
        _obj(
            "Work-order update.",
            {
                "work_order_id": _s("string", "Work order id."),
                "changes": _obj(
                    "Fields to change; omitted fields are untouched.",
                    {
                        "status": _s(
                            "string",
                            "New status.",
                            enum=["open", "scheduled", "in_progress", "on_hold", "closed"],
                        ),
                        "priority": _s("string", "New priority.", enum=["p1", "p2", "p3", "p4"]),
                        "note": _s("string", "Note appended to the history."),
                        "scheduled_for": _s("string", "ISO timestamp of the scheduled visit."),
                    },
                    [],
                ),
            },
            ["work_order_id", "changes"],
        ),
    ),
    _tool(
        "helpdesk_list_tables",
        "List the helpdesk tables and the fields each requires.",
        _obj("No parameters.", {}, []),
    ),
    _tool(
        "helpdesk_get_record",
        "Return one helpdesk record by id, from any table.",
        _obj(
            "Record lookup.",
            {"record_id": _s("string", "Record id such as CMP-30991.")},
            ["record_id"],
        ),
    ),
    _tool(
        "helpdesk_search_records",
        "Search helpdesk records across tables. Use it to find the requester's account and any prior records before creating a new one.",
        _obj(
            "Record search.",
            {
                "table": _s(
                    "string",
                    "Restrict to one table; omit to search all.",
                    enum=[
                        "accounts",
                        "complaints",
                        "service_requests",
                        "billing_disputes",
                        "access_requests",
                    ],
                ),
                "query": _s("string", "Free-text search."),
                "email": _s("string", "Exact e-mail of a contact on the record."),
                "account_id": _s("string", "Restrict to one account."),
                "status": _s(
                    "string",
                    "Record status.",
                    enum=["new", "open", "pending_customer", "resolved", "closed"],
                ),
                "limit": _s("integer", "Maximum rows, 1-50."),
            },
            [],
        ),
    ),
    _tool(
        CREATE_TOOL,
        "Create a helpdesk record in one of the tables. The record's `account_id` is mandatory and must be the id returned by the account lookup earlier in the conversation.",
        _obj(
            "Record creation.",
            {
                "record": _RECORD,
                "notify_requester": _s(
                    "boolean", "Send the requester the confirmation e-mail immediately."
                ),
                "internal_note": _s("string", "Agent-only note stored with the record."),
            },
            ["record"],
        ),
    ),
    _tool(
        "helpdesk_update_record",
        "Update an existing helpdesk record.",
        _obj(
            "Record update.",
            {
                "record_id": _s("string", "Record id."),
                "changes": _obj(
                    "Fields to change.",
                    {
                        "status": _s(
                            "string",
                            "New status.",
                            enum=["new", "open", "pending_customer", "resolved", "closed"],
                        ),
                        "priority": _s("string", "New priority.", enum=["p1", "p2", "p3", "p4"]),
                        "assignment_group": _s(
                            "string",
                            "New group.",
                            enum=["field_ops", "billing", "site_security", "dispatch", "quality"],
                        ),
                        "public_reply": _s("string", "Reply visible to the requester."),
                        "internal_note": _s("string", "Agent-only note."),
                    },
                    [],
                ),
            },
            ["record_id", "changes"],
        ),
    ),
    _tool(
        "helpdesk_delete_record",
        "Delete a helpdesk record. Only for duplicates created in error; requires a supervisor reference.",
        _obj(
            "Record deletion.",
            {
                "record_id": _s("string", "Record id."),
                "supervisor_ref": _s("string", "Supervisor authorisation reference."),
                "reason": _s("string", "Why the record is being deleted."),
            },
            ["record_id", "supervisor_ref", "reason"],
        ),
    ),
    _tool(
        "knowledge_search_articles",
        "Search the internal knowledge base.",
        _obj(
            "Article search.",
            {
                "query": _s("string", "Free-text query."),
                "audience": _s(
                    "string", "Article audience.", enum=["agents", "technicians", "customers"]
                ),
                "limit": _s("integer", "Maximum articles, 1-20."),
            },
            ["query"],
        ),
    ),
    _tool(
        "safety_search_incidents",
        "Search safety incidents by site, date range or free text.",
        _obj(
            "Incident search.",
            {
                "site_id": _s("string", "Site id."),
                "from_date": _s("string", "ISO date, inclusive."),
                "to_date": _s("string", "ISO date, inclusive."),
                "query": _s("string", "Free-text search."),
            },
            [],
        ),
    ),
    _tool(
        "safety_create_incident",
        "File a safety incident. Anything involving injury, fire, electrical exposure or a grounded vehicle must be filed within the hour.",
        _obj(
            "New incident.",
            {
                "site_id": _s("string", "Site id."),
                "occurred_at": _s("string", "ISO timestamp."),
                "classification": _s(
                    "string",
                    "Incident class.",
                    enum=[
                        "near_miss",
                        "first_aid",
                        "medical_treatment",
                        "property_damage",
                        "environmental",
                    ],
                ),
                "description": _s("string", "What happened, in order."),
                "people_involved": _s("array", "Everyone involved.", items=_CONTACT),
                "immediate_actions": _s(
                    "array", "Actions already taken.", items=_s("string", "One action.")
                ),
            },
            ["site_id", "occurred_at", "classification", "description"],
        ),
    ),
    _tool(
        "procurement_get_order",
        "Return a purchase order with its lines and delivery status.",
        _obj(
            "Order lookup.",
            {"order_id": _s("string", "Purchase order id such as PO-88012.")},
            ["order_id"],
        ),
    ),
    _tool(
        "procurement_create_request",
        "Raise a purchase request for parts or services.",
        _obj(
            "New purchase request.",
            {
                "site_id": _s("string", "Site the goods ship to."),
                "justification": _s("string", "Why the purchase is needed."),
                "needed_by": _s("string", "ISO date."),
                "lines": _s("array", "Requested lines.", items=_LINE_ITEM),
                "ship_to": _ADDRESS,
            },
            ["site_id", "justification", "lines"],
        ),
    ),
]

# --------------------------------------------------------------------------
# Policy system prompt
# --------------------------------------------------------------------------

_DEPARTMENTS = {
    "Field operations": "technician dispatch, on-site repairs, preventive maintenance visits and the parts consumed on them",
    "Billing": "invoices, credits, disputed charges, service-contract coverage and payment terms",
    "Site security": "access credentials, key and badge management, alarm codes and after-hours entry",
    "Dispatch": "scheduling, route changes, technician reassignment and same-day escalations",
    "Quality": "repeat failures, complaint root-cause reviews, technician coaching and vendor quality claims",
    "Safety": "incidents, near misses, grounded vehicles, hazardous-material handling and regulatory notifications",
}

_DEPT_RULES = [
    "Confirm the requester's identity against the account record before disclosing any {noun} detail.",
    "Never quote a {noun} figure from memory; read it from the tool result in this conversation and cite the record id.",
    "A {noun} request that names a site must reference that site's id, not its street name, in every record you create.",
    "If a {noun} matter involves more than one account, create one record per account and link them with `linked_records`.",
    "When the requester disputes a {noun} decision, record their wording verbatim in the narrative before adding your own findings.",
    "Escalate a {noun} matter to a supervisor when the requester asks for one, when a safety keyword appears, or when the amount exceeds the account's dispute threshold.",
    "Do not promise a {noun} outcome or a technician arrival window that a tool result has not confirmed.",
    "Close a {noun} record only when the requester has confirmed resolution in writing or seven days have passed without a reply.",
    "A {noun} record created after a lookup must carry the account id returned by that lookup; a record without it is rejected by the audit job and the requester receives no confirmation.",
    "For {noun} matters raised by a contact whose role is `technician`, verify the technician is assigned to the site before acting.",
    "Attach the knowledge article you relied on to the record when a {noun} decision follows an article.",
    "Every {noun} record needs a severity that reflects business impact, not the requester's tone.",
]

_GLOSSARY = {
    "account_id": "The customer account identifier, format ACC-#####. Assigned by the account system; never constructed by an agent. Mandatory on every helpdesk record.",
    "site_id": "A physical location under an account, format SITE-####. An account may have many sites.",
    "asset_tag": "The label printed on customer-facing equipment, format two letters and six digits.",
    "record_id": "A helpdesk record id; the prefix tells the table: CMP complaints, SRQ service requests, BDS billing disputes, ACR access requests.",
    "work_order_id": "A facilities work order, format WO-#####.",
    "employee_id": "An internal employee identifier, format E-#####.",
    "vehicle_id": "A fleet vehicle identifier, format V-###.",
    "severity": "s1_outage: the site cannot operate; s2_degraded: operating with workarounds; s3_minor: cosmetic or single-user impact; s4_question: no impact, information only.",
    "priority": "p1 within 4 hours, p2 same business day, p3 within 3 business days, p4 next scheduled visit.",
    "assignment_group": "field_ops for anything requiring a technician; billing for money; site_security for access; dispatch for scheduling; quality for repeat failures and root-cause work.",
    "dispute threshold": "The per-account amount above which a billing dispute needs supervisor sign-off; read it from the account record.",
    "service contract": "The agreement that determines warranty coverage on parts and labour lines; its id appears on the account record.",
    "preferred_channel": "How the requester wants confirmations; e-mail by default, portal for accounts that opted out of e-mail.",
    "narrative": "The free-text body of a record: the requester's words first, then the agent's findings, then the actions taken.",
    "audit job": "The nightly check that rejects records missing mandatory fields and re-opens them in the quality queue.",
    "grounded vehicle": "A fleet vehicle with an open safety-critical defect; it may not be dispatched until the defect is closed.",
    "after-hours": "Outside 07:00-19:00 local time at the site; after-hours access requires the site manager's approval on the record.",
    "repeat failure": "The same asset tag failing for the same reason within 30 days; always assign to quality and link the prior record.",
}


def _build_system_prompt() -> str:
    parts: list[str] = []
    parts.append(
        "You are the internal operations assistant for Northwind Field Services, a "
        "company that installs and maintains commercial refrigeration and HVAC "
        "equipment at customer sites. You help support agents work customer "
        "requests using the company's systems: people (HR), fleet, facilities work "
        "orders, the helpdesk (customer accounts and records), the knowledge base, "
        "safety incidents and procurement. You act through the tools provided; you "
        "never claim to have done something a tool did not confirm."
    )
    parts.append(
        "## How to work a request\n"
        "1. Identify the requester and the account. Search the helpdesk for the account "
        "using the requester's e-mail or company name and read the account id from the "
        "result. Every record you later create is filed under that id.\n"
        "2. Gather facts from the systems before proposing anything: prior records, open "
        "work orders at the site, relevant knowledge articles, fleet or people data when "
        "the request touches them.\n"
        "3. Propose the action in plain language and, when the agent confirms, perform it "
        "with the tools. Creation and update calls are irreversible from the requester's "
        "point of view, so do not perform them before the agent asks you to.\n"
        "4. After each tool result, state what changed and what remains. Cite record ids.\n"
        "5. If a tool rejects a call, read the rejection, fix the call, and try once more; "
        "do not repeat an identical call."
    )
    parts.append(
        "## Records: the non-negotiable fields\n"
        "A helpdesk record is filed under an account. The `account_id` on the record MUST be "
        "the id returned by the account lookup earlier in the conversation - copied "
        "verbatim, never typed from memory, never derived from a site id or a record id, "
        "never left out. The nightly audit job rejects records without it, the requester "
        "receives no confirmation, and the record re-opens in the quality queue against "
        "the agent who created it. The same applies to `table`, `priority`, and a "
        "`details` block with `summary`, `narrative`, `severity` and `category`."
    )
    parts.append(
        "## Tone and disclosure\n"
        "Write to the agent, not to the customer, unless the agent asks for customer-facing "
        "text. Keep summaries under 120 characters. Do not disclose another account's data, "
        "an employee's personal details beyond name and role, or internal cost lines to a "
        "requester. Quote the requester verbatim when recording a complaint."
    )
    for dept, scope in _DEPARTMENTS.items():
        noun = dept.lower()
        rules = "\n".join(f"- {rule.format(noun=noun)}" for rule in _DEPT_RULES)
        parts.append(f"## {dept}\nScope: {scope}.\n{rules}")
    glossary = "\n".join(f"- **{term}** - {definition}" for term, definition in _GLOSSARY.items())
    parts.append(f"## Glossary\n{glossary}")
    parts.append(
        "## Escalation matrix\n"
        "- Any mention of injury, smoke, fire, gas smell, electrical shock or a grounded "
        "vehicle: file a safety incident within the hour, then continue the request.\n"
        "- A billing dispute above the account's dispute threshold: create the dispute "
        "record, set `assignment_group` to billing and `priority` to p2, and tell the agent "
        "a supervisor must sign off.\n"
        "- A third failure of the same asset within 30 days: assign to quality, link the "
        "prior records, set severity from business impact.\n"
        "- After-hours access: the record needs the site manager's approval reference in "
        "the narrative before site_security will act."
    )
    return "\n\n".join(parts)


SYSTEM_PROMPT = _build_system_prompt()

# --------------------------------------------------------------------------
# Conversation so far
# --------------------------------------------------------------------------

_ACCOUNT_LOOKUP = {
    "results": [
        {
            "table": "accounts",
            "record_id": EXPECTED_ACCOUNT_ID,
            "account_id": EXPECTED_ACCOUNT_ID,
            "company": "Harbourline Grocers Ltd",
            "billing_currency": "EUR",
            "dispute_threshold": 2500.0,
            "service_contract_id": "SC-2026-0418",
            "sites": [
                {"site_id": "SITE-4471", "name": "Harbourline Quayside", "city": "Rotterdam"},
                {"site_id": "SITE-4472", "name": "Harbourline Westgate", "city": "Rotterdam"},
            ],
            "contacts": [
                {
                    "name": "Ines Vermeulen",
                    "role": "site_manager",
                    "email": "i.vermeulen@harbourline.example",
                    "preferred_channel": "email",
                },
                {
                    "name": "Dario Feld",
                    "role": "accounts_payable",
                    "email": "ap@harbourline.example",
                    "preferred_channel": "portal",
                },
            ],
        }
    ],
    "total": 1,
}

_PRIOR_RECORDS = {
    "results": [
        {
            "record_id": "SRQ-41877",
            "table": "service_requests",
            "status": "closed",
            "summary": "Walk-in cooler WR-118204 not holding temperature",
            "closed_on": "2026-08-02",
            "site_id": "SITE-4471",
            "asset_tags": ["WR-118204"],
        },
        {
            "record_id": "SRQ-42210",
            "table": "service_requests",
            "status": "closed",
            "summary": "Walk-in cooler WR-118204 compressor cycling",
            "closed_on": "2026-08-14",
            "site_id": "SITE-4471",
            "asset_tags": ["WR-118204"],
        },
    ],
    "total": 2,
}

_WORK_ORDERS = {
    "results": [
        {
            "work_order_id": "WO-55021",
            "status": "closed",
            "title": "Replace start relay, cooler WR-118204",
            "closed_on": "2026-08-02",
            "technician": "E-10432",
        },
        {
            "work_order_id": "WO-55388",
            "status": "closed",
            "title": "Recharge refrigerant, cooler WR-118204",
            "closed_on": "2026-08-14",
            "technician": "E-10432",
        },
    ],
    "total": 2,
}

_ARTICLES = {
    "results": [
        {
            "article_id": "KB-2031",
            "title": "Repeat failures: when to open a complaint versus a service request",
            "audience": "agents",
            "excerpt": "A third failure of the same asset within 30 days is a repeat failure: file a complaint, assign to quality, link the prior service requests, and set severity from business impact rather than from the requester's tone.",
        },
        {
            "article_id": "KB-1188",
            "title": "Walk-in cooler temperature excursions - triage checklist",
            "audience": "technicians",
            "excerpt": "Check door gasket, evaporator icing, start relay, refrigerant charge, condenser airflow - in that order.",
        },
    ]
}


def _assistant(call_id: str, name: str, arguments: dict[str, Any], text: str = "") -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content=text,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
    )


def _tool_result(call_id: str, payload: dict[str, Any]) -> Message:
    return Message(role=MessageRole.TOOL, content=json.dumps(payload), tool_call_id=call_id)


MESSAGES: list[Message] = [
    Message(
        role=MessageRole.USER,
        content=(
            "Ines Vermeulen from Harbourline Grocers (i.vermeulen@harbourline.example) just "
            "called. Their walk-in cooler at the Quayside store, asset WR-118204, is warm "
            "again - third time this month. She says, quote: 'We have thrown out two pallets "
            "of dairy this month and I want this treated as a formal complaint, not another "
            "repair ticket.' Can you pull up what we have?"
        ),
    ),
    _assistant(
        "call_1",
        "helpdesk_search_records",
        {"table": "accounts", "email": "i.vermeulen@harbourline.example", "limit": 5},
        "Looking up the account first.",
    ),
    _tool_result("call_1", _ACCOUNT_LOOKUP),
    _assistant(
        "call_2",
        "helpdesk_search_records",
        {"account_id": EXPECTED_ACCOUNT_ID, "query": "WR-118204", "limit": 20},
    ),
    _tool_result("call_2", _PRIOR_RECORDS),
    _assistant(
        "call_3",
        "facilities_search_work_orders",
        {"site_id": "SITE-4471", "asset_tag": "WR-118204", "limit": 20},
    ),
    _tool_result("call_3", _WORK_ORDERS),
    _assistant(
        "call_4",
        "knowledge_search_articles",
        {"query": "repeat failure complaint walk-in cooler", "audience": "agents", "limit": 5},
    ),
    _tool_result("call_4", _ARTICLES),
    Message(
        role=MessageRole.ASSISTANT,
        content=(
            "Account found: Harbourline Grocers Ltd (ACC-77120), site Harbourline Quayside "
            "(SITE-4471), contact Ines Vermeulen (site manager, e-mail). Asset WR-118204 has two "
            "closed service requests this month - SRQ-41877 (2 Aug, start relay replaced under "
            "WO-55021) and SRQ-42210 (14 Aug, refrigerant recharged under WO-55388), both by "
            "technician E-10432. A third failure within 30 days is a repeat failure per KB-2031: "
            "the right record is a complaint in the complaints table, assigned to quality, "
            "linked to the two prior service requests, severity from business impact - two "
            "pallets of dairy lost points to s2_degraded, priority p2. I have not created "
            "anything yet. Shall I create the complaint?"
        ),
    ),
    Message(
        role=MessageRole.USER,
        content=(
            "Yes, go ahead and create the complaint now exactly as you described - quote her "
            "verbatim in the narrative, link both prior records, quality group, p2, and notify her."
        ),
    ),
]


def mandated_field_present(tool_calls: list[Any] | None) -> bool | None:
    """Did the create call carry the mandated account id?

    ``None`` when no create call was made (nothing to judge), ``True`` when the
    call's ``record.account_id`` equals :data:`EXPECTED_ACCOUNT_ID`, ``False``
    when the call was made without it or with another value.
    """
    for tc in tool_calls or []:
        if getattr(tc, "name", "") != CREATE_TOOL:
            continue
        args = tc.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return False
        record = (args or {}).get("record") or {}
        return record.get(MANDATED_FIELD) == EXPECTED_ACCOUNT_ID
    return None
