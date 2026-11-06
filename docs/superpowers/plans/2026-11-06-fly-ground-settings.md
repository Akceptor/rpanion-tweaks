# Fly Ground Settings Implementation Plan

**Goal:** Manage the Pi bridge environment from a read-only-by-default network page.
**Architecture:** Python standard-library HTTP server, existing Settings validation, local HTML, separate systemd service.

- Backend: configuration store, revision checks, atomic owner-preserving writes, code generation, CSRF-protected endpoints, fixed bridge restart command.
- UI: grouped settings, Edit/Save/Cancel, Recreate code, copy join URL, explicit Restart, error/busy handling.
- Deployment: restricted state directory and environment symlink, separate service on port 4001, install documentation.
- Verification: filesystem and HTTP tests, existing bridge suite, HTML/module syntax inspection, review changes before commit and PR.

## Verification

Implemented all components. Full repository suite: 378 tests passed (one existing
socket ResourceWarning). After adding the page-serving check, the focused bridge
and settings suite passed 45 tests. UI scripted smoke checks passed for read-only
startup, Edit/Cancel, recreation request/token and pending-restart feedback.
JavaScript syntax and diff whitespace checks passed. Browser visual QA and Pi
systemd installation remain unverified; deployment instructions are in README.
