---
name: publishing-collaborative-cloud-dailies
description: Use when publishing local daily selections to a shared cloud report with private media, collaborator access, retention, or responsive daily views.
---

# Publishing Collaborative Cloud Dailies

Treat cloud data as a published collaboration snapshot. Local collection and curation facts remain authoritative; cloud media is an authorized, removable playback copy.

## Entry And Decision Order

1. Define the approved daily identity and immutable snapshot boundary before UI, upload, or access work. Repeating an approved publish request must return the same snapshot, not create a second daily.
2. Separate report metadata, disposable media copies, publishing-device credentials, and collaborator sessions. Give each its own lifecycle and least-privilege boundary.
3. Use one private object store and an authorization-aware proxy by default. Add another cloud, cold backup, or public CDN only when the user explicitly requires it.
4. Upload resumable media copies with content integrity checks; serve private objects only through the proxy.
5. Apply the product's specified business time zone and retention window exactly. Retain media by upload-completion time, while retaining historical report content and original source links permanently.
6. Build the workbench as a responsive collaboration view over the snapshot. Never make cloud playback availability the condition for retaining a report item.

## Inputs And Outputs

Inputs: approved local daily selections, stable source links, optional removable media copies, publishing intent, collaboration policy, and an explicit business time zone.

Outputs: one idempotent immutable daily snapshot, authorized collaboration access, resumable verified media copies, current-day playback decisions, retained historical source links, and a responsive daily workbench.

## Boundaries

- Do not let cloud edits overwrite local collection or curation facts.
- Store a publishing-device credential only in an operating-system credential store; persist a verifier or credential identifier, never the credential itself.
- Keep private media private. A browser receives an authorized proxied response, not storage credentials or a durable object URL.
- A fixed collaboration URL is a high-entropy bearer credential, not a signing credential, API credential, or storage credential. Store only its verifier, exchange it for a scoped HttpOnly session, and make revocation immediate; never turn it into `REPORT_SIGNING_SECRET`.
- Do not represent a planned route or product-specific deployment as an already released feature. This module describes reusable patterns.
- This module has no deletion permission for any local source, original, download, cache, or user-owned file. Cleanup is idempotent and retryable and may delete only its own expired cloud playback copies.

## Validation

- Publish the same approved daily twice and verify one snapshot identity, stable item identities, and no duplicated report.
- Interrupt an upload, resume it, and verify the final SHA-256 before publication; reject unauthorized, malformed, and unsatisfiable Range requests.
- Confirm that a revoked collaboration credential cannot exchange a new session, cannot become a signing or storage credential, and has no raw value in configuration, logs, or persistence.
- Test the specified business-day boundary and retention cutoff using the configured time zone and upload completion timestamp. Retrying, viewing, or republishing must not extend the window; expiry removes only the cloud copy while the historical report retains metadata and source link without a player.
- Exercise desktop and narrow mobile layouts with empty, uploading, current-day, and historical states.

## Common Mistakes

- Never treat a successful cloud upload as permission to delete a local cache or original; this module cannot delete local files.
- Never retain an expired temporary copy because another historical report references it; retain the metadata and source link, not the playback copy.
- Never replace a specified time zone or 72-hour window with a generic 180- or 365-day policy, or add multi-cloud and public delivery for free-storage convenience.

## Read References When Needed

- For daily identity, write-once fields, and publish recovery, read [idempotent snapshot publisher](references/idempotent-snapshot-publisher.md).
- For chunking, resume state, integrity, private proxying, Range, and cleanup, read [resumable secure media](references/resumable-secure-media.md).
- For fixed collaboration credentials and session exchange, read [fixed secret collaboration](references/fixed-secret-collaboration.md).
- For business-day and retention decisions, read [current day and retention](references/current-day-and-retention.md).
- For responsive workbench states and behavior, read [responsive daily workbench](references/responsive-daily-workbench.md).
