# Canonical Release

## Preconditions

The release procedure accepts exactly one canonical source root and formal branch. Require the checked-out revision to equal the formal branch revision and require a clean worktree before invoking the complete test command. Refuse feature branches, auxiliary worktrees, detached revisions, stale copies, and dirty formal branches.

Declare one release command and one installed application entrypoint in product-owned deployment code. A person must not substitute a manual copy, an alternate build command, or a helper script as a formal release path.

## Transaction

1. Capture installed artifact versions, file hashes, running-service evidence, and legacy-service state.
2. Build every coupled component in a staging directory on the target filesystem.
3. Run complete tests from canonical source before replacement.
4. Stop only the documented formal runtime and preserve the prior artifacts as a rollback set.
5. Atomically replace the native app, service script, and all coupled helper or publisher components.
6. Start from the formal entrypoint and run health, identity, ownership, and page checks.

The release manifest should bind source revision, canonical source root, installed artifact hashes, release-script hash, application metadata, and timestamp. It is an evidence record, not a source of credentials.

## Evidence

Verify the version endpoint exposes the expected revision, canonical source root, and service-script SHA-256. Independently calculate source and installed hashes, inspect bundle metadata, and confirm the native app owns the service process.
