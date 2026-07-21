# Email Registration And Sessions

## Registration And Verification

Treat an email address as unverified until a provider-backed verification flow succeeds. Create the active account and any date-access baseline only after that success. Return generic responses where account enumeration would be harmful.

- Use a mature identity provider for email delivery and password authentication when available. Otherwise use a maintained password-hashing library with a modern adaptive hash such as Argon2id, per-password salts, and provider or library defaults that can be upgraded.
- Never write plaintext passwords, reversible password material, verification codes, raw reset links, raw tokens, or complete cookies to storage, logs, telemetry, support exports, or audit records.
- Generate verification and reset values with a cryptographically secure generator. Persist only a hash plus subject, purpose, expiry, consumed timestamp, and attempt metadata. Accept each value once, expire it quickly, and rate-limit requests and attempts by account, address, IP, and other appropriate abuse signals.
- Use the same verification guarantees for an address change. Do not treat an unverified replacement address as an authorization change.

## Password Sign-In And Reset

Use generic sign-in failures that do not disclose whether an address exists. Apply rate limits and escalating abuse controls to sign-in and reset requests. A reset flow must verify control of the email, consume the one-time reset value, update the password through the approved provider or hash library, and revoke all existing sessions before the new password is accepted for protected access.

## Session Policy

Keep a session record or provider session identifier that the server can revoke. Bind authorization to current server-side account status and capability state on each protected request or through a short, revocable validation interval.

- Make persistent login an explicit user choice, not a default and not a request to store a password.
- Use HttpOnly, Secure, and an appropriate SameSite setting for browser session cookies. Keep cookie scope as narrow as the deployment allows.
- Give both session-only and persistent sessions an idle policy and an absolute expiry. Persistent login may extend convenience within its absolute cap; it must not become an unbounded credential.
- Rotate session identifiers at authentication, privilege elevation, and other risk boundaries. Revoke sessions at logout, suspension, password reset, and a security-sensitive permission change where the target must reauthenticate.
- Require recent re-authentication for password changes and sensitive administration actions. Do not rely only on a long-lived session for these actions.

## Verification Checks

Test verified and unverified account states, expired and replayed verification values, rate limits, generic failures, session-only versus persistent expiry, explicit logout, password reset session revocation, suspension, and forced re-authentication.
