Issue summary

Billing reconciliation found occasional under-collection and over-collection on line items when tax is applied.

Observed failing cases from production replay:
- add_tax(100.0, 0.08) expected 108.0
- add_tax(19.99, 0.0825) expected 21.639175

Constraints
- Keep API compatibility with existing callers.
- Keep fix minimal and document one risky edge case.
