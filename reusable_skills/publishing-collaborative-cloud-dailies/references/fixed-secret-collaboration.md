# Fixed Secret Collaboration

## Credential Model

A fixed collaboration URL may contain a high-entropy bearer value, but storage retains only a slow, salted verifier and metadata such as scope, expiry, creation time, and revocation time. It is not a signing credential, API credential, or object-store credential, and must never be repurposed as `REPORT_SIGNING_SECRET`. Show the raw value only at creation or replacement. Never place it in application configuration, analytics, logs, referers, or database query output.

The URL itself is not a session. Its exchange endpoint verifies the value and creates a scoped, short-lived HttpOnly, Secure, SameSite session cookie. Report and item APIs authorize from the session scope, not by repeatedly accepting the URL value.

## Lifecycle

1. Create a value with a narrow report or collaboration scope and a replacement path.
2. Persist its verifier and audit metadata, never the raw value.
3. Exchange a valid unrevoked value for a restricted session.
4. Revoke by marking the verifier unavailable and invalidating matching sessions where the session store supports it.
5. Replace with a new value rather than attempting to restore an exposed one.

Avoid placing bearer values in paths that might be forwarded to third parties. Set `Referrer-Policy: no-referrer` on the exchange response and remove the value from browser history after exchange.

## Evidence

Verify that raw values are absent from persistence and logs, an exchange session has only its assigned scope, an expired or revoked value cannot create a session, and a revoked session loses access on its next authorization check.

Do not fix sharing by reusing the bearer value for signatures or storage access; create a separate server-held credential for each such function when the product needs one.
