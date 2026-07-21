# Idempotent Snapshot Publisher

## Contract

An approved local daily is published as one immutable cloud snapshot. Define a stable idempotency key from the daily business identity and approved revision identity, not from a request timestamp or retry attempt.

Persist the key, snapshot identifier, immutable item identities, publish state, and failure detail in one transaction or equivalent durable state transition. A retry must return the existing snapshot or resume its unfinished work; it must not create a second report.

## Suggested States

`prepared -> uploading -> ready_to_publish -> published` is sufficient when each transition is durable. Record failure as recoverable state with an error summary and a retry boundary. Do not mark a report published until required snapshot data and declared media outcomes are complete.

## Fields And Ownership

| Field | Rule |
| --- | --- |
| Daily identity | Stable for the configured business date and report scope. |
| Approved revision identity | Changes only for an explicitly approved replacement snapshot. |
| Item identity | Stable within the snapshot; preserve source link and curation text. |
| Idempotency key | Unique for the intended snapshot; enforced by storage. |
| Publish result | Stores report identity and state, never a local source path or raw credential. |

Cloud edits can create collaboration revisions only where the product permits them. They do not rewrite the upstream local fact source.

## Evidence

Submit the same request twice, including after a process restart. Verify one report identity, no duplicate items, preserved source links, and deterministic recovery from every unfinished state.
