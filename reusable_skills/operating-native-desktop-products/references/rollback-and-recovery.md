# Rollback And Recovery

## Restore Point

Before replacing anything, create a rollback set containing the previous native app, service script, coupled helpers, release manifest, and exact legacy-service state. A state record must distinguish disabled, enabled but stopped, and running; recovery restores the original state rather than applying a guessed default.

## Failure Sequence

When build, installation, startup, health, identity, or ownership validation fails:

1. Stop the candidate runtime only if it is the documented process started by this release.
2. Restore all prior artifacts as one set.
3. Restore legacy-service configuration and its prior running or stopped state.
4. Restart the previous formal runtime when it had been healthy and running.
5. Recheck listener ownership, health, and version evidence; record the failed stage without credentials.

Use same-filesystem rename or an equivalent atomic replacement primitive for artifacts that must move together. Do not report recovery until the restored runtime has its own fresh health and process evidence.

## Evidence

Induce a safe staging or health-check failure in a test environment. Verify no candidate file remains installed, the prior hashes return, the prior service state returns, and the port is owned by the expected restored process.
