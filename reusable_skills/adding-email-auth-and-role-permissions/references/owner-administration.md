# Owner Administration

## Protected Mutation Model

Keep account administration on server-only endpoints. Resolve actor identity and current account state on the server, load the target inside a transaction, and make the authorization decision from the authoritative capability matrix. Do not accept an actor role, target state, or prior value from the client as proof.

For any role, status, deletion, or historical-access mutation:

1. Require a current Owner capability and recent re-authentication.
2. Lock or otherwise serialize the target account and active-Owner set inside one transaction.
3. Reject an Owner targeting themselves for demotion, suspension, or deletion, even if other Owners exist.
4. Reject any mutation that would leave no active Owner. Enforce this as a database or service invariant, not only a UI rule.
5. Apply the requested, allowed field changes without altering `registered_at` or `access_start_date`.
6. Revoke the target account's active sessions when the action changes access, role, status, or historical scope, then append the audit event before commit.

Use one transaction for the decision, mutation, session revocation marker, and audit append. If a provider cannot transact its session store with the account database, commit a durable revocation version within the authorization transaction and require future session checks to compare it before granting protected access.

## Administrative Boundaries

- Let Owners change only product-supported target states and roles. Keep system bootstrap, recovery, and break-glass processes outside ordinary self-service administration and document their stricter control separately.
- Do not make a role change perform a hidden history grant. Historical access is a distinct mutation with its own confirmation, re-authentication, session revocation, and audit evidence.
- Make a failed mutation leave the displayed and persisted state unchanged. Return an authorization-safe reason suitable for the administrator without exposing private target details.
- Do not allow bulk operations to bypass self-targeting or last-Owner checks; evaluate each target and the final Owner count transactionally.

## Verification Checks

Test allowed administrative changes, denied non-Owner attempts, denied Owner self-demotion, self-suspension, and self-deletion, last-active-Owner protection under concurrent requests, recent re-authentication expiry, target-session rejection after a successful change, rollback on an audit write failure, and unchanged registration-date visibility after every role mutation.
