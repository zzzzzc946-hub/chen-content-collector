---
name: building-local-content-workbenches
description: Use when building a local content workbench with SQLite records, media downloads, daily selections, local playback, or recovery after local process interruption.
---

# Building Local Content Workbenches

Use this module after collection records exist. SQLite is the local business fact source; the filesystem holds owned media files, and the download queue and daily workbench are independent state machines or views over those facts.

## Entry And Decision Order

1. Define SQLite identities, ownership, and state transitions before creating queue, UI, or playback behavior.
2. Separate user- or Eagle-owned originals, tool-managed download copies, and cloud playback copies. A cleaner must never delete an original local file.
3. Build downloads as restartable tasks that update their own state without changing collection facts or daily selection.
4. Build the daily workbench as a view and curation layer over SQLite; it can create a publish handoff without treating media availability as the only valid content state.
5. Serve local media only through validated record-to-path mapping, with Range behavior and missing-file states explicit.

## Inputs And Outputs

Inputs: normalized collection records, selected media metadata, local operator actions, configured storage roots, and optional publish intent.

Outputs: SQLite-backed business records, independently recoverable download tasks, daily selections and export or publish handoff, and safely playable local media responses.

## Boundaries

- SQLite owns collection, curation, task, and daily-workbench facts; cloud records are downstream snapshots rather than replacements.
- Original user or Eagle files are read-only from the workbench's lifecycle perspective. Tool-owned downloads and disposable cloud copies have separate ownership and cleanup rules.
- Do not expose arbitrary filesystem paths through playback or download APIs.

## Validation

- Restart during queued, active, failed, and completed download states; verify recovery does not duplicate work or lose facts.
- Verify a missing file becomes a durable missing-file state without deleting its source record or daily selection.
- Verify a daily can retain text and source-link curation when optional local media is unavailable.
- Exercise whole-file and single-range playback, including `200`, `206`, and `416`, plus traversal and unmapped-path rejection.

## Read References When Needed

- For identities, ownership fields, and transaction boundaries, read [SQLite data model](references/sqlite-data-model.md).
- For leases, partial files, restart recovery, and cleanup ownership, read [download queue](references/download-queue.md).
- For daily selections, exports, and publish handoff, read [daily workbench](references/daily-workbench.md).
- For safe mapping, Range responses, and missing local media, read [local media playback](references/local-media-playback.md).
