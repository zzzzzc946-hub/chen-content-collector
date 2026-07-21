# Download Queue

## Task State

Model downloads independently from collection and daily curation. A useful lifecycle is queued, leased or active, completed, failed, cancelled, and interrupted or recoverable. Record attempts, progress, byte counts when known, error class, local asset reference, and lease expiry.

## Execution And Recovery

Claim work with a persisted lease or equivalent atomic transition. Write into a tool-owned temporary file, validate completion, then atomically promote it to the tool-managed final location and update SQLite. On startup, reclaim expired leases, inspect partial artifacts only within managed roots, and resume or fail tasks based on recorded evidence.

Do not delete a partial file that may be an original local file. Never infer cleanup ownership from a filename alone; use the asset ownership class and managed-root validation.

## Failure Handling

Classify authentication, session data, network, disk space, platform restriction, cancellation, and integrity failures separately. Permit bounded retry for transient failures and retain blocked or operator-required tasks until their prerequisites change. A completed download does not alter a collection record's source link or automatically add it to a daily.

## Cleanup

Only clean tool-managed downloads and their temporary files, subject to an explicit retention policy and a database reference check. Cloud cleanup is owned by the publishing lifecycle. Original user or Eagle files are never queue-cleaner candidates.
