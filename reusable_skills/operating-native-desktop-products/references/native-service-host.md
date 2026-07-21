# Native Service Host

## Ownership Contract

The native application launches and retains the local service process directly. The service listens only on the intended local interface and port, and its PID has the native application as its direct PPID. The app owns startup, readiness checks, shutdown, and termination of the child it created.

Before launch or recovery, inspect the port listener, PID, PPID, executable path, command line, and start time. If the port is occupied by an unrelated process, stop and report the conflict rather than killing it. A legacy launcher or background agent must not race the native app for the same port.

## Runtime Evidence

Record the expected listener address and port, native-app PID, service PID and PPID, installed script path, and health endpoint result. Compare this evidence before and after release so a stale background service cannot be mistaken for the new runtime.

## Shutdown And Failure

On normal exit, terminate only the child process owned by the app and verify the listener is gone. On startup failure, keep unrelated processes untouched and surface a specific port, executable, permission, or readiness failure.
