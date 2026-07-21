# Resumable Secure Media

## Upload Contract

Treat a cloud object as a removable playback copy. The default is one private object store behind the authorized proxy; do not add multi-cloud replication, cold backup, or a public CDN unless the user explicitly requires it. Create a durable upload record before sending parts, with declared size, allowed MIME type, chunk size, content digest, and owner scope. Upload parts by ordinal number so a client can query completed parts after interruption and resend only missing parts.

Completion must assemble or finalize only the recorded parts, verify the declared SHA-256, and atomically change the media record from incomplete to playable. A digest mismatch leaves it non-playable and retryable. Do not expose a local source path in the upload contract.

## Authorized Playback

Keep objects private. The media endpoint must first validate report access, current-day policy, and any short-lived media session, then proxy `GET` or `HEAD` to the object store. Forward only valid byte ranges and preserve the semantics required for seeking:

| Request outcome | Response |
| --- | --- |
| No Range | `200` with full representation when allowed. |
| One satisfiable range | `206` with correct `Content-Range` and length. |
| Unsatisfiable range | `416` with total length. |
| Multiple ranges or malformed syntax | Reject or use a deliberately documented policy; do not improvise concatenation. |

Do not return storage credentials, a public bucket URL, or a long-lived signed object URL to the browser.

## Cleanup

Schedule cleanup from `upload_completed_at`, not a report date or request creation time. Do not change that instant on retry, view, metadata edit, or republish. When the fixed retention window expires, delete the cloud-owned playback copy even if a historical report or manifest still references its metadata; retain the report, metadata reference, and original source link instead. A cleanup job records each deletion outcome and is safe to repeat after a timeout or partial failure. It has no permission to address, unlink, or delete any local source, original, cache, download, or user-owned file.

## Evidence

Interrupt between parts and resume. Verify digest mismatch rejection, authorized seek behavior, unauthorized rejection, expiry despite a remaining historical manifest reference, retryable cleanup failure, and preservation of every local file and historical source link.
