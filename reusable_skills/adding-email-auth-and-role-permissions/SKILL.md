---
name: adding-email-auth-and-role-permissions
description: Use when a future product optionally needs verified-email accounts, password sign-in, durable sessions, server-enforced Viewer Editor Owner capabilities, date-scoped access, Owner administration, or permission auditing.
---

# Adding Email Auth And Role Permissions

Use this optional module only when a product explicitly chooses account authentication and role authorization. It is reusable guidance, not evidence that any current product has enabled accounts, passwords, or an administration surface.

## Entry And Decision Order

1. Confirm that account access is in scope and record the product business time zone, allowed roles, resources, mutations, and any date-bound visibility rule.
2. Keep authentication, session handling, and authorization as separate server-side boundaries. Verify email before granting an active account or protected access.
3. Select a mature identity provider or proven password-hashing implementation; never design credential storage or encryption from scratch.
4. Derive every protected read and mutation from the authenticated identity and server-side account state. Treat client roles, dates, history flags, and hidden controls as untrusted.
5. Make permission changes transactional, re-authenticated, revocable, and auditable. Keep Owner self-protection and last-Owner protection in the same authoritative transaction.

## Inputs And Outputs

Inputs: an explicit decision to enable accounts, product business time zone, role capability matrix, date-access policy, session lifetime policy, and administration requirements.

Outputs: verified account state, server-revocable sessions, capability decisions for protected resources, explicit history grants when needed, protected Owner administration, and append-only permission evidence.

## Boundaries

- Do not imply an optional module is enabled by default, or that a particular product must use passwords or account administration.
- Do not store plaintext passwords, verification codes, raw tokens, complete cookies, or recoverable session credentials. Use a mature provider or strong password hash, and persist only hashed one-time verifier material where applicable.
- Do not infer authorization from a successful login, UI visibility, a client claim, or a role name alone. The server decides capabilities and date access for every protected path.
- Do not change `registered_at` or `access_start_date` while changing a role. Promotion to Editor or Owner must not silently reveal older records; use a distinct explicit, audited historical-access capability if a product requires it.
- Do not permit an Owner to demote, suspend, or delete their own account. The last active Owner needs an additional system-level invariant even when another administrator bypasses the UI.
- Do not place real domains, mailboxes, provider references, secrets, production values, or current-product configuration in this reusable module.

## Validation

- Verify an unverified email cannot reach protected data; verify codes and reset links are short-lived, one-time, hash-only at rest, and rate-limited.
- Verify both session-only and an explicitly selected persistent session use HttpOnly, Secure, and appropriate SameSite cookies, server revocation, and an absolute lifetime. Resetting a password revokes existing sessions.
- Exercise allow and deny decisions for Viewer, Editor, and Owner through server endpoints. Check list, detail, search, export, attachment, and mutation paths rather than UI controls alone.
- Verify the product business-day predicate: a verified account may see the full verification calendar day, but not an earlier day; later sign-ins and role changes preserve the original date boundary.
- Verify sensitive Owner changes atomically reject self-targeting and loss of the last active Owner, require recent re-authentication, revoke target sessions, and append an audit record without credentials.

## Read References When Needed

- For verification, password, reset, session, and persistent-login controls, read [email registration and sessions](references/email-registration-and-sessions.md).
- For capability checks and business-day visibility, read [role and date access](references/role-and-date-access.md).
- For Owner mutation invariants and transaction flow, read [Owner administration](references/owner-administration.md).
- For append-only audit fields, redaction, and review, read [permission audit](references/permission-audit.md).
