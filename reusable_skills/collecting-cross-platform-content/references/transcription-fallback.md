# Transcription Fallback

## Decision Tree

1. Save platform or page captions when they are available and attributable to the source.
2. When captions are absent, record that absence before attempting audio acquisition.
3. Acquire audio only through an allowed media path. Record audio acquisition separately from ASR execution.
4. Submit audio to the configured transcription capability, then persist transcript provenance, language when known, and outcome.
5. Stop at a clear manual-action or configuration-needed state when no permitted audio path or ASR capability exists.

## Independent Outcomes

Metadata collection can succeed while captions are absent. Audio acquisition can fail while the source record remains valid. ASR can fail after audio is available. Do not collapse these into one generic collection failure or fabricate transcript text.

## Retry Boundaries

Retry a transient fetch or provider error with a bounded policy. Do not retry a missing configuration, authentication requirement, unsupported media access, or operator decision until its prerequisite changes. Preserve the source record and exact failed stage for a later retry or manual transcript.

## Handoff

Persist the durable transcription state, evidence summary, retry eligibility, and an operator-facing status. The caller maps these values using the status reference; it does not expose provider internals as the user-facing explanation.
