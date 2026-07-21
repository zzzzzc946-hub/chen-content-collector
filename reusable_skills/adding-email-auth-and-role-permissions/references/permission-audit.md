# Permission Audit

## Append-Only Event Record

Record security-relevant authorization decisions and permission changes in an append-only event store. Restrict direct update and delete access, and expose review through a read-only administrative path. Use immutable event identifiers, event time, correlation identifier, actor subject, target subject when applicable, action, resource class, outcome, reason code, and an approved before-and-after summary.

Capture both allowed and denied events for authentication-adjacent administration and authorization decisions that matter to security review. At minimum cover verification completion or failure classes, sign-in outcome classes, password reset completion, session revocation, role change, status change, history grant or revoke, Owner self-protection denial, last-Owner denial, and protected-resource allow or deny decisions where the product needs traceability.

## Redaction And Integrity

- Do not record passwords, verification codes, raw tokens, reset values, session identifiers, complete cookies, authorization headers, provider responses containing credentials, or unredacted sensitive request bodies.
- Prefer stable internal subject identifiers over email addresses. When an email is necessary for an authorized administration view, mask or minimize it according to the product's privacy policy.
- Store structured reason codes rather than raw exception messages. Preserve only the minimum debugging context needed for review.
- Append audit entries in the same transaction as a sensitive account mutation. For a denied action, append an immutable denial event without changing the target account.
- Control retention, read access, export, and deletion through an explicit audit policy. Do not let ordinary account deletion erase permission history when a retention obligation applies.

## Review And Verification

Review audit events by time, actor, target, action, outcome, and correlation identifier. Test that allowed and denied actions create distinguishable immutable entries, retries do not overwrite prior events, sensitive mutations fail or roll back when their required audit append cannot complete, and scans of audit output contain no credentials or complete cookies.
