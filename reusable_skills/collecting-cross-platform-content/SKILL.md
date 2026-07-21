---
name: collecting-cross-platform-content
description: Use when collecting content from platform links or logged-in browser sessions, handling browser access failures, obtaining transcripts, or reporting collection progress to an operator.
---

# Collecting Cross-Platform Content

Use this module for the state transition from a source link or discovered page to a normalized collection record. Keep platform-specific behavior behind adapters and preserve the source link and evidence needed to retry.

## Entry And Decision Order

1. Normalize the link and select a platform adapter; use the generic path only when no adapter applies.
2. Prefer public metadata or platform-provided captions. Request a browser session only when the selected path needs page state.
3. Diagnose CDP, browser, context, and target page before judging account state. A logged-in browser does not prove CDP is ready; a closed browser, context, or page is not a login failure.
4. Store normalized metadata, collected evidence, machine state, and an operator-facing status. Start transcription fallback only after caption availability is known.
5. Retry only the failed boundary; preserve records needing human action.

## Inputs And Outputs

Inputs: a source link or page candidate, operator intent, an optional browser session, and optional transcription capability.

Outputs: one normalized collection record with source evidence, platform identity, metadata, transcript state, a durable machine state, and a separate user-visible operator status.

## Boundaries

- Do not bypass platform authentication, cookies, verification, rate limits, or access controls.
- Do not reduce an uncertain failure to a vague failure label. Persist the failed layer, error summary, retryability, and next action.
- This module owns collection evidence and status handoff, not download queues, local daily views, or cloud publication.

## Validation

- Exercise an adapter with a supported platform link and verify the normalized record retains its source evidence.
- Simulate unavailable CDP and a closed target page; neither may be written as an account-login requirement.
- Exercise captions unavailable, transcription unavailable, and transcription failed paths; each must retain a distinct retry or operator action.
- Confirm durable machine state and user-visible wording are mapped separately.

## Read References When Needed

- For adapter choice, normalized fields, and supported-platform variations, read [platform adapters](references/platform-adapters.md).
- For CDP attachment, page recovery, and evidence-led diagnosis, read [browser CDP recovery](references/browser-cdp-recovery.md).
- For captions, audio acquisition, ASR, and fallback termination, read [transcription fallback](references/transcription-fallback.md).
- For persistence fields, status mapping, and non-vague writeback, read [operator statuses](references/operator-statuses.md).
