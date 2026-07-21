# Operator Statuses

## Two Status Layers

Persist a machine-readable state for workflow and retries, plus a user-visible status for the operator. The visible wording is a translation of the durable state and evidence, not the only stored diagnosis.

Each writeback should include: state code, visible status, safe evidence summary, failed boundary, retryability, next action, and updated time. Preserve an earlier failure history when a later retry changes the current state.

## Mapping Principles

| Condition | Durable state family | Visible status intent |
| --- | --- | --- |
| Metadata and usable text saved | Completed | Ready for review. |
| Captions absent, ASR not started | Transcription pending | Needs transcription. |
| Audio or ASR active | Transcription active | Transcription in progress. |
| Browser attach or page recovery failed | Browser unavailable | Restore browser session. |
| Page requires authentication | Authentication required | Sign in to the platform. |
| Cookie-specific access is required | Cookie required | Refresh allowed session data. |
| ASR is unavailable or failed | Transcription blocked or failed | Configure, retry, or handle manually. |
| Input or outcome cannot be safely classified | Manual review | Operator decision required. |

Use precise status values for platform restriction, verification, network failure, missing captions, download failure, and manual review. Do not write only a generic failure when evidence identifies the layer. A transient network condition and a hold condition must remain distinct so background retry cannot erase an operator decision.

## Writeback Contract

External collaboration tools may display only the visible status and summary, but the local collection record retains the durable code and evidence. A downstream queue may consume retryability; it must not infer success merely from the absence of a visible error.
