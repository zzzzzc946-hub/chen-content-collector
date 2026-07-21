# SQLite Data Model

## Source Of Truth

SQLite is the local business fact source for collected content, curation, download tasks, daily selections, settings, and local media references. Store stable identities and state transitions in SQLite; use the filesystem for bytes, not as the only place where business meaning exists.

## Core Records

| Record | Purpose | Important relationships |
| --- | --- | --- |
| Collection item | Normalized source and collection outcome | Owns source evidence and platform metadata. |
| Media asset | A declared local or remote media reference | Links to an item and an ownership class. |
| Download job | Independent acquisition attempt | Links to an asset, never replaces source facts. |
| Daily and daily item | Curated daily view | References collection items and preserves ordering or edits. |
| Settings or storage root | Local operational configuration | Is validated before path resolution. |

Give records immutable identifiers. Use explicit state columns, timestamps, error summaries, and version or lease information where concurrent workers can act. Store a logical media identity and a storage-relative location rather than trusting an arbitrary absolute path supplied by a client.

## Ownership Classes

- **Original local file:** belongs to the user or an external library such as Eagle; record it by reference and never delete, move, overwrite, or classify it as disposable cache.
- **Tool-managed download copy:** belongs to the local workbench and may be retried or cleaned according to its policy.
- **Cloud playback copy:** belongs to a separate publishing lifecycle; it may be removable without changing local collection or daily facts.

## Transactions

Use transactions for coupled fact changes, such as creating a download job with its asset state or adding an item to a daily view. Queue workers and UI views should reconcile from persisted state after a restart rather than relying on in-memory progress.
