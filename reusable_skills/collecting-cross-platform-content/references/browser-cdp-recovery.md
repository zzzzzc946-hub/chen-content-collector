# Browser CDP Recovery

## Diagnose In Layers

Check in this order: CDP endpoint reachable, browser connection established, context available, target page found, target page usable, then platform account evidence. Do not inspect account login state when an earlier layer is unavailable.

| Failed layer | Durable meaning | Operator direction |
| --- | --- | --- |
| Endpoint or browser absent | Browser CDP not ready | Start or restore the browser session. |
| Connection drops after attach | Browser connection lost | Reconnect, then rediscover context and page. |
| Context or page missing | Target page not found or closed | Open or restore the intended page. |
| Page cannot be queried yet | Page not ready | Wait briefly or reload within a bounded retry policy. |
| Page shows an authentication challenge | Account login required | Complete platform login, then retry collection. |
| Page evidence is inconclusive | Account login unknown | Preserve evidence and request operator confirmation. |

Messages such as `page closed`, `context closed`, `browser closed`, or an attachment error are browser-session faults, not proof that the account is logged out.

## Recovery Rules

Treat a successful CDP connection as a new discovery point: re-enumerate contexts and pages instead of reusing stale handles. Use bounded retries for transient attachment or page-read errors; stop and persist the observed layer when retries are exhausted. Never automate authentication or verification to conceal an access boundary.

## Evidence

Persist the adapter, connection phase, target identity when known, safe error summary, attempt time, and retryability. Keep diagnostic evidence distinct from the operator wording so UI language can change without corrupting recovery logic.
