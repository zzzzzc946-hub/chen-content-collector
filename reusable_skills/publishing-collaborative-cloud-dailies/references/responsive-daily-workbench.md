# Responsive Daily Workbench

## Layout Contract

Build a working surface for scanning, filtering, reading, and permitted collaboration rather than a marketing page. Use a stable report header, compact controls, and a primary list or table that can change presentation without changing snapshot identity.

Wide layouts may show filters, item metadata, source links, and collaboration controls together. Narrow layouts should stack metadata beneath the item title, move secondary controls into menus, and preserve a clear source-link action. Keep touch targets reachable and do not rely on hover for required actions.

## States

Render explicit states for loading, empty daily, publishing progress, upload failure, current-day playable media, historical source-only items, access denial, and revoked collaboration access. A historical item must not show a disabled-looking player; omit the player and present the durable source link instead.

## Collaboration Boundaries

Show edit controls only after the server authorizes the session and only for fields the policy permits. Optimistic edits must have a conflict or retry path and must never imply that they changed upstream local facts. Do not bind component names, colors, copy, routes, or breakpoints to one product.

## Evidence

Capture real browser checks at a desktop and narrow mobile viewport. Exercise keyboard navigation, source-link visibility, access changes, long titles, no-media items, and current-to-historical day transition without layout overlap or hidden required actions.
