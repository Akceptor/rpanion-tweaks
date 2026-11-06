# Pi bridge settings page

Network HTTP service on port 4001, relying on the operator's ZeroTier/private network boundary. Default bind 0.0.0.0, configurable address. No application login requested. Serve a local, dependency-free UI and JSON API. Do not log credentials or configuration contents. Reject cross-site mutation requests and require a process-issued CSRF token.

All settings supported by fly_ground_config.Settings are visible and initially read-only. Edit unlocks fields; Save validates and atomically writes the file; Cancel restores saved state. Recreate code generates a new CRAWLER code and saves immediately without entering edit mode. Recreate is disabled while editing to avoid losing pending changes. Service restart is an explicit separate operation and the UI explains it disconnects sessions. A saved code does not become active in the bridge until restart.

Preserve unrecognized settings/comments, permissions and ownership. Reject unsupported input syntax rather than reinterpret it. Use revision checks to prevent stale pages overwriting newer values. Keep /etc/fly-ground.env as the bridge-facing path, backed by a symlink into /var/lib/fly-ground-settings so atomic writes can be confined to one directory.

Settings server runs separately from fly-ground.service so restarting the bridge does not stop the UI. Provide a systemd unit and installation instructions. Do not deploy to the real Pi without connectivity; deliver a PR and manual installation commands.
