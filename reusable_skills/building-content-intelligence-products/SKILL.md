---
name: building-content-intelligence-products
description: Use when building or extending a product that collects cross-platform content, organizes it locally, produces daily intelligence, and publishes collaborative cloud reports.
---

# Building Content Intelligence Products

Route the task to one primary module first. Read only that module and the references it directly requires. Add an explicitly named adjacent module only when the requested acceptance, data contract, or release evidence truly crosses the boundary; do not load every module or every reference at once.

| Task area | Module |
| --- | --- |
| Cross-platform capture, browser sessions, transcription, operator states | `collecting-cross-platform-content` |
| Local SQLite facts, media downloads, daily workbench, local playback | `building-local-content-workbenches` |
| Shared cloud daily snapshots, media delivery, retention, responsive views | `publishing-collaborative-cloud-dailies` |
| Native service hosting, canonical release, rollback, runtime QA | `operating-native-desktop-products` |
| Email sessions, roles, date-based access, owner administration | `adding-email-auth-and-role-permissions` |

## Boundaries

- Local SQLite remains the collection and curation fact source.
- Cloud data is a published daily snapshot and collaboration layer; it does not overwrite local facts.
- Cloud media is an authorized, removable playback copy, never the local source-file authority.
- Use a module's declared inputs and outputs before crossing into another module.
- Keep the routing primary-module-first: name the owner module first, then name only the minimum adjacent modules needed for the explicit cross-boundary acceptance.
- Do not fan out into unrelated modules, full-system audits, or speculative future phases.

## Inputs and Outputs

Start with the user task, current system boundary, and the smallest affected module. Produce a verified change at that module boundary plus any explicitly required handoff to the next module. If acceptance spans a boundary, record the primary module and each adjacent module explicitly so scope stays controlled.

## Acceptance

Use [module map](references/module-map.md) to select the module and dependencies. Use the [acceptance matrix](references/acceptance-matrix.md) for cross-module evidence.
