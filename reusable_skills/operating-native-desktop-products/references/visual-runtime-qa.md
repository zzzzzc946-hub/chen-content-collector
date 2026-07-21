# Visual Runtime QA

## Runtime-First Checks

Open the installed native application through its formal entrypoint, then verify its service health and version identity before inspecting the page. A source-tree development server, static build output, or HTTP success from an unknown PID is not release evidence.

Use browser automation such as Playwright against the live local endpoint. Capture screenshots at a desktop viewport and a narrow mobile viewport whenever a page changed. Assert a real workflow: the page loads from the installed service, primary controls work, errors are visible, and no required text or controls overlap or fall outside the viewport.

## Evidence Set

Keep the version response, source and installed SHA-256 values, bundle metadata, listener and PID/PPID data, browser assertions, and screenshots together under release evidence. Redact credentials and private operational identifiers before sharing evidence.

## Failure Signals

Fail runtime QA when the listener belongs to a stale process, the version identity differs from the canonical revision, the page is blank or blocked by startup errors, browser interactions fail, or desktop and narrow layouts hide required actions. Repair the runtime or restore the prior release before proceeding.
