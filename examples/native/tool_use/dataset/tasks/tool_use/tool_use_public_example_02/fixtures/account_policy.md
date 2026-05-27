Enterprise account state policy

- High-risk account changes require staged transition (`active` -> `pending_review` -> `approved`).
- Every state transition must include an audit-log record with actor, timestamp, and rationale.
- Rollback procedure must be documented before irreversible actions.
