---
name: operating-native-desktop-products
description: Use when releasing or recovering a native desktop product that directly hosts a local service and requires canonical source, rollback, or runtime evidence.
---

# Operating Native Desktop Products

Treat a desktop release as a verified transaction from canonical source to a native-hosted runtime. A feature worktree is for development and testing, never for the formal release.

## Entry And Decision Order

1. Identify the canonical source branch, its exact revision, and the single formal release command and installed entrypoint. Refuse a feature worktree, stale checkout, detached candidate, or dirty canonical branch.
2. Run the complete release test suite from the canonical source before building or replacing any installed component.
3. Confirm the native app directly owns the local service. Capture port listener, PID, PPID, executable path, and legacy-service state before changing them.
4. Build all coupled artifacts in a staging location, then atomically replace the native app, service script, and any publisher or helper components as one release.
5. Start through the formal entrypoint and prove runtime identity with the version endpoint, source and installed SHA-256 values, bundle metadata, process ancestry, and real-page checks.
6. On any failure, restore the prior artifacts and service state before reporting failure; do not leave a partially installed runtime.

## Inputs And Outputs

Inputs: canonical source policy, clean formal branch and revision, release candidate, complete test command, installed-runtime locations, and rollback-capable deployment procedure.

Outputs: a native app that directly hosts its service, a release evidence record that ties runtime to source, or a documented restoration of the previous healthy runtime.

## Boundaries

- One formal command and one formal application entrypoint own release. Manual copies, ad hoc scripts, and feature worktrees are not release sources.
- Do not kill an unrelated port owner. Diagnose and report ownership before taking action.
- Preserve and restore legacy service configuration exactly as found; do not assume it was disabled.
- A successful build is not a successful release. Runtime identity, process ownership, and actual page behavior are separate checks.
- Keep credentials, private paths, bundle identifiers, hostnames, and production URLs out of reusable instructions and evidence samples.

## Validation

- Verify release refusal for a feature worktree and a dirty canonical branch before installation starts.
- Exercise a failing build or health check and verify that every replaced component and previous service state is restored.
- Confirm the version endpoint revision and source root, source-to-installed SHA-256 equality, expected bundle metadata, and native-app-to-service PPID chain.
- Inspect the live page in a real browser and retain screenshots for desktop and narrow viewport behavior when the release changes a page.

## Read References When Needed

- For native service ownership and port diagnosis, read [native service host](references/native-service-host.md).
- For canonical-source gating and transactional installation, read [canonical release](references/canonical-release.md).
- For restoration ordering and failure evidence, read [rollback and recovery](references/rollback-and-recovery.md).
- For real-runtime browser and screenshot checks, read [visual runtime QA](references/visual-runtime-qa.md).
