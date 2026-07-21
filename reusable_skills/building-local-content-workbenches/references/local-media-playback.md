# Local Media Playback

## Safe Resolution

Accept a logical asset identifier, not a client-provided filesystem path. Resolve it through SQLite, verify its ownership and allowed storage root, normalize the resulting path, and reject traversal, symlink escapes, unmapped assets, and non-files before opening bytes.

Do not use a browser path parameter as a direct file path. Original files may be readable only when the user explicitly authorized that root; tool-managed downloads use a separate managed root.

## Range Responses

Support one valid byte range at a time. Return:

| Request condition | Response |
| --- | --- |
| No `Range` header | `200` with the full readable file. |
| Valid satisfiable range | `206` with exact `Content-Range`, length, and seekable bytes. |
| Unsatisfiable range, including past end | `416` with the total length in `Content-Range`. |
| Multiple or malformed ranges | Reject clearly or use a documented supported policy; never return incorrect bytes. |

Calculate response lengths from the resolved file at request time. Set appropriate media type where known and avoid loading large files fully into memory.

## Missing And Changed Files

When a record points to a file that no longer exists, is unreadable, or is outside its allowed root, persist a distinct missing or inaccessible asset state and return a controlled not-found or unavailable response. Preserve the collection item, daily selection, and source link. Recovery may relink an operator-approved file or reacquire a tool-managed download; it must not create or delete an original local file.
