# Private TURN access for Pi-hosted rooms — implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task. Do not deploy until the integrated change and rollout instructions are reviewable.

**Goal:** Keep the existing shared join links working while preventing people who merely know the Worker URL from obtaining TURN credentials or creating their own relay-enabled rooms.

**Architecture:** Authenticate the Raspberry Pi when it opens a host signaling connection. Deliver separate TURN credentials to the two admitted participants over their existing signaling sockets, after the room is paired. Remove the public credential endpoint, keep signaling alive for session lifetime, and revoke credentials when the session ends.

**Tech stack:** Cloudflare Worker and SQLite Durable Objects, browser JavaScript/WebRTC, Python websockets/GStreamer, existing Python settings HTTP server on port 4001, systemd environment file.

**Status:** Approved and implemented locally on `fix/private-turn-access` in both repositories. The Pi creates rooms and its secret is replaceable through port 4001. Code review and local verification passed on 2026-11-12. Production secrets, deployment, and real Pi/LTE relay acceptance remain pending.

## Confirmed behavior and scope

- `rpanion-tweaks/main` was fetched and advanced to `338b4cd` during the review.
- The sibling `../fly-by-webrtc` checkout was inspected at `2906a29`; it contains the Worker and browser. Recheck its current branch, remote head, and instructions before implementing.
- `worker/src/turn.js` currently serves unauthenticated `GET /turn`, with a per-IP issuance limiter and a 7,200-second credential lifetime. A live request without a room code returned relay credentials.
- `worker/src/index.js` accepts unauthenticated `role=host` connections. Checking only for an existing room would let an attacker create their own room.
- `web/index.html:onPeerState` deliberately closes signaling once WebRTC connects. The Pi continues an established session after signaling closes. Both behaviors must change before close-triggered revocation is enabled.
- `worker/src/room.js` currently expires signaling rooms after ten minutes. That is not the Pi's media-session duration; do not accidentally impose a ten-minute media limit.
- The Pi settings server already reads and writes `/etc/fly-ground.env`, exposes non-secret configuration via `/api/settings`, and uses CSRF and revision checks for writes. It relies on trusted-network access rather than a login.
- Existing invite format and room hashing stay unchanged. Recipients need no additional login.
- This deployment will authorize Pi hosting only. Anonymous browser hosting must be rejected too; otherwise it becomes a bypass. Adding owner login for browser Ground mode is a separate feature.

## Approach considered

1. **Recommended: authenticated Pi hosting plus credentials over signaling.** Reuses the admitted participant sockets, removes `/turn` as a credential API, and requires no browser bearer-token store or login.
2. **Authenticated `/turn` with participant tokens.** Also secure when implemented correctly, but adds token issuance, HTTP authorization, expiry, and synchronization with the same room lifecycle.
3. **Cloudflare Access for every recipient.** Adds an identity login and changes the shared-link experience; not selected.

## Security contract

- Generate a random 32-byte secret encoded as base64url. Store matching `FLY_HOST_TOKEN` values in Worker secrets and `/etc/fly-ground.env`. Never commit the value or include it in a URL, bookmark, public asset, response, or log.
- Pi host upgrades use `Authorization: Bearer <FLY_HOST_TOKEN>` over WSS. Validate before room state is created or replaced. Missing Worker configuration fails closed; there is no development-only production bypass.
- Pilot joins require an existing, unexpired room with an authenticated live Pi host. Preserve the single-pilot lock and existing generation protections.
- TURN issuance is allowed only for a paired current generation. Mint one distinct credential set per role, not a shared room credential. Neither client supplies a credential TTL or asks the server to mint arbitrary extra sets.
- `GET /turn` and every other method at that path return `410 Gone`, `Cache-Control: no-store`, with no credentials and no upstream call.
- A link recipient can copy credentials legitimately delivered to their browser. This design restricts issuance and limits reuse through expiry and revocation; it cannot bind a TURN credential cryptographically to the page or video stream.
- Retain the existing 7,200-second TURN TTL for this change. Shortening it requires verified browser and GStreamer renewal support and is deliberately excluded from this implementation.
- Bound each paired relay session to that lifetime, with a 60-second margin before provider expiry. End and rehost cleanly, using the same room code, rather than leaving a relay session to fail silently. Default one-hour Pi sessions remain one hour. The UI/docs must explicitly describe that unlimited or extended sessions need a new connection before the relay lifetime ends; do not claim seamless renewal.
- Revoke both participants' issued usernames on session end, kick, host loss, generation replacement, or expiry. Revocation can fail during a provider outage; persist retries and rely on credential expiry as the final bound. Do not promise instant revocation of already-issued credentials.

## Protocol and lifecycle

1. Pi loads its host token and opens the host socket with the authorization header. Invalid/missing host authorization gets a defined authentication failure without allocating a room.
2. Authenticated host opens the waiting room. Do not mint TURN credentials while waiting. Retain a ten-minute waiting window, then let the Pi rehost normally.
3. Pilot joins using the room ID derived from the existing invite. The room locks before any asynchronous credential request.
4. Room records an issuance-in-progress state for this generation. Mint the two role-specific credential sets, saving issued usernames and expiry before delivery. After each upstream await, recheck generation and session state. Revoke credentials returned after their generation was closed or replaced.
5. Server sends each admitted participant only its own bootstrap message:

   ```json
   {"type":"session-ready","generation":"opaque-generation","iceServers":[],"expiresAt":0,"turnStatus":"available"}
   ```

   `iceServers` holds that participant's actual ICE configuration; `expiresAt` is the server-selected session deadline in epoch milliseconds. `turnStatus` is `available` or `unavailable`. Reject malformed or stale bootstrap messages in clients.

6. Both clients construct their peer and acknowledge `{ "type": "rtc-ready", "generation": "..." }`. Only after both acknowledgments does the server send the existing `peer-joined` event to the Pi to trigger its offer. Queueing/order tests must cover a fast host and a slow pilot.
7. Keep signaling open after WebRTC connects. Pi sends an application-level host heartbeat every 20 seconds; the room acknowledges it and extends a 60-second host lease. Pilot traffic cannot extend the lease. Bound setup to 60 seconds from pairing; close on missing bootstrap acknowledgments or negotiation timeout.
8. Session end closes the current generation, stops further issuance, closes both sockets, and schedules credential revocation. Pi releases RC overrides using existing cleanup, then may rehost. Socket loss now ends the active session; this intentional dependency allows the Worker to revoke credentials reliably. Existing faster control/heartbeat watchdogs stay in force.
9. Durable Object alarms handle the earliest waiting deadline, host lease expiry, paired-session deadline, or pending revocation retry. Do not delete pending revocation records when clearing a room generation.

If the provider is unavailable, revoke any partially issued pair and send STUN-only bootstrap to the already-authorized pair, with `turnStatus: unavailable`. Show the degraded connection status. Authentication and room admission never fall back. No client resumes calls to public `/turn`.

## Task 1: Configure the Pi secret through port 4001

**Files in rpanion-tweaks:**
- Modify `python/fly_ground_settings.py`, `python/fly_ground_settings.html`, `python/fly_ground.py`, `README.md`.
- Modify `tests/test_fly_ground_settings.py`; create `tests/test_fly_ground_auth.py`.

- [x] Add a dedicated host-token reader/validator for service startup. Require a base64url token decoding to 32 random bytes; fail clearly before connecting when absent or invalid. Do not add the secret to the publicly normalized settings dictionary or dataclass representations.
- [x] Add a separate `POST /api/host-token` mutation accepting exactly `{token, revision}` and protected by the existing JSON, origin/CSRF, size, and revision checks. Use the existing atomic write and mode-0600 behavior, removing duplicate assignments for this key. Do not return or echo `token` in success or error responses.
- [x] Expose only `host_token_configured: true/false` alongside the normal state. Keep `FLY_HOST_TOKEN` outside `FIELDS`, which drives public settings serialization.
- [x] Add a password input and explicit Save host secret action. Empty input means no change. Clear the input after a successful save, never persist it in localStorage, and tell the owner that a bridge restart applies the update. There is no read-back or reveal-saved-secret action.
- [x] Verify ordinary settings saves and room-code recreation preserve an existing token. No systemd unit change is required: `EnvironmentFile=/etc/fly-ground.env` already loads it on restart.
- [x] Test missing/invalid token, set/replace, duplicate assignment cleanup, stale revision, CSRF rejection, unchanged normal saves, response redaction, and file permissions.
- [x] Document that port 4001 remains an owner administration surface on the trusted network. Masking the token is not a replacement for restricting access to that port.

Run: `python3 -m unittest discover -s tests -p 'test_fly_ground_settings.py'` and `python3 -m unittest discover -s tests -p 'test_fly_ground_auth.py'`.

## Task 2: Authenticate hosting and retire public credential issuance

**Files in fly-by-webrtc:**
- Modify `worker/src/index.js`, `worker/src/turn.js`, `worker/src/limiter.js`, `wrangler.toml` comments/documentation.
- Create `worker/src/host-auth.js`, `test/unit/host-auth.test.js`.
- Modify `test/worker/signaling.test.js`, `test/worker/turn.test.js`, `test/unit/turn.test.js`.

- [x] Validate the Pi's bearer token using an appropriate constant-time secret comparison before calling the room DO. Reject invalid host requests without mutating or replacing existing room state. Apply the existing trusted-edge IP attempt limiter to failures; use a fixed auth failure code such as `4401` for WebSocket clients.
- [x] Require the same authorization for all `role=host` requests, regardless of room prefix or client type. Public browsers have no owner secret and cannot host on this deployment.
- [x] Replace the public `/turn` handler with the fixed 410 response for all methods. Keep `/turn` in `run_worker_first` so static assets cannot accidentally handle it.
- [x] Refactor provider mint/revoke logic into internal helpers called only by the room. Preserve response validation and no-secret error reporting. Apply the existing per-IP issuance limit using the trusted edge IP recorded for each socket.
- [x] Test valid/invalid/missing host token, missing Worker secret, existing-room non-mutation on failed auth, public-browser host rejection, non-owner room creation attempts, and zero upstream requests through `/turn`.

Run in fly-by-webrtc: `npm run test:unit` and `npm run test:worker`.

## Task 3: Authorize bootstrap and implement revocation lifecycle

**Files in fly-by-webrtc:**
- Modify `worker/src/room.js`, `worker/src/room-state.js`, `worker/src/turn.js`.
- Create `worker/src/room-turn.js`, `test/unit/room-turn.test.js`.
- Modify `test/unit/room-state.test.js`, `test/worker/signaling.test.js`, `test/worker/turn.test.js`.

- [x] Implement the protocol above, including per-generation role bindings and the two-client readiness barrier. Server control messages must be handled locally and excluded from the general peer-to-peer relay whitelist; clients cannot spoof `session-ready` or `peer-joined`.
- [x] Serialize issuance state before awaiting upstream requests. Limit to one issuance attempt per role per generation; duplicate requests/events return existing state rather than minting again. Bound paired-session starts to three per minute per room and retain the ten-per-minute issuance cap per IP. Return rate-limit close code `4029` without leaking other room state.
- [x] Persist issued usernames/expiry before sending credentials; keep actual passwords only as long as delivery/setup needs them. Use generation/role tags rather than raw room codes for any optional analytics identifier.
- [x] Implement heartbeat lease, waiting/setup/active deadlines, and deterministic cleanup. Late generation results must never reach new sockets.
- [x] Persist revocation jobs before ending the generation. Treat successful revoke and already-absent credentials as completed. Retry transient errors after 5, 15, 30, then 60 seconds, repeating at 60 seconds until success or credential expiry. Validate provider status handling against official docs during implementation. Never erase pending old-generation jobs when a new host joins.
- [x] Test one role's mint failing after the other's succeeds, close during mint, duplicate readiness, two concurrent joins, cross-role control-message injection, old-socket events, replacement, host disappearance, alarm execution, provider timeout, failed revocation retry, and expiry cleanup.
- [x] Test that pilot messages cannot keep a room alive after the authenticated host disappears, and that repeated stale/wrong-code joins never trigger minting.

Run in fly-by-webrtc: `npm test`.

## Task 4: Migrate the Pi and browser clients together

**Files in rpanion-tweaks:**
- Modify `python/fly_ground.py`, `python/fly_ground_peer.py` if bootstrap ownership requires it.
- Replace obsolete public-HTTP expectations in `tests/test_fly_ground_ice.py`; extend `tests/test_fly_ground_peer.py`, `tests/test_fly_ground_retry.py`; create `tests/test_fly_ground_bootstrap.py`.

**Files in fly-by-webrtc:**
- Modify `web/index.html`, `web/lib/signal.js`, `web/lib/turn.js` and existing browser bootstrap/refresh tests.
- Create `test/unit/session-bootstrap.test.js`.

- [x] Pi supplies the secret through `websockets.connect(..., additional_headers=...)`, never the URL. Load it at startup without including it in routine settings serialization or exception output. Authentication failure uses a slow retry with a configuration-specific message rather than rapid reconnects.
- [x] Pi waits for `session-ready` before constructing `GstPeer`, acknowledges `rtc-ready`, and offers only after `peer-joined`. Remove the pre-join HTTP `ice_servers(site_url)` credential fetch.
- [x] Browser joins signaling before creating its peer, validates its bootstrap, constructs the peer, and acknowledges readiness. Protect asynchronous handlers with current-generation checks and cancel setup when the socket/session ends.
- [x] Remove the `onPeerState('connected')` signaling close. Make a real signaling close end the session; Pi follows the same rule. Add heartbeat and deadline handling without weakening RC cleanup, neutral/resume requirements, or takeover behavior.
- [x] Remove all browser `/turn` calls. Reuse only pure ICE configuration validation if helpful; do not leave an unauthenticated fallback fetch.
- [x] Disable browser Host session controls on this Pi-only deployment with a clear explanation. Join controls and the existing invite format remain available without a login.
- [x] Test delayed bootstrap, early/late offers, provider STUN-only status, rejected auth, signaling loss during media, heartbeat timeout, cancellation, refresh/rehost with the same link, and absence of tokens in captured logs or request URLs.

Run the affected Python tests, then `python3 -m unittest discover -s tests`. Run `npm test` in fly-by-webrtc. Integration acceptance requires the real Pi/GStreamer runtime; mocks cannot verify relay transport behavior.

## Task 5: Review, deploy, and verify the access boundary

**Files:** Update both READMEs and fly-by-webrtc `DESIGN.md` with the new protocol, configuration, Pi-only hosting policy, expiration behavior, and recovery procedure.

- [x] Recheck the latest repository states and review the combined changes before deployment. Confirm ordinary tests do not call real Cloudflare credential APIs.
- [ ] Generate one random token privately. Set Worker `FLY_HOST_TOKEN` using the Cloudflare secret mechanism; enter the same value through the Pi's port 4001 secret field. Keep the existing `TURN_KEY_ID` and `TURN_API_TOKEN` server-side. Do not print the secret or place it in shell arguments/history or the plan.
- [ ] Test the paired releases in an isolated preview/local environment first. Test with simulated clock advancement rather than waiting two hours for expiry cases.
- [ ] Deploy during a brief session outage: stop the Pi bridge; install the new Pi code; deploy the Worker and browser together; restart the Pi. Cached old browser clients must reload. Do not preserve the anonymous `/turn` route for compatibility.
- [ ] With no credentials, verify `/turn` returns 410, host creation is denied, and wrong/nonexistent/expired room joins never mint credentials. Inspect only status and redacted summaries during live checks.
- [ ] With the legitimate Pi, verify its existing invite joins successfully on the actual LTE/NAT path. Confirm a relay candidate in WebRTC stats; a direct-only success is insufficient evidence.
- [ ] Leave/kick/restart the session and verify the issued usernames are revoked; simulate a provider outage to verify retry persistence. A controlled TURN allocation/refresh test must confirm old credentials stop working; an HTTP revoke response alone is insufficient.
- [ ] Check port 4001 GET responses never contain the token; normal edits preserve it; replacement plus restart works. Confirm control releases on signaling loss and room expiry with wheels off the ground.
- [ ] Check Cloudflare usage for unexpected traffic. Previously issued public credentials can remain valid for their original lifetime; closing `/turn` does not revoke them retroactively. If immediate cutoff is needed, schedule a separate TURN-key retirement/replacement and verify its behavior with Cloudflare before acting.
- [ ] Roll back only to a build with public `/turn` disabled and host creation blocked, even if that temporarily leaves service unavailable. Never restore anonymous credential issuance as a compatibility rollback.

## Acceptance criteria

- Knowing the Worker URL grants neither room creation nor TURN credentials.
- Only the Pi with the matching secret can create or replace a host room.
- People with a valid link can join its active room without another login.
- Each admitted peer receives its own credential set; third parties and stale generations receive none.
- Session closure stops issuance immediately and starts durable revocation; failures remain bounded by credential expiry.
- The Pi secret can be replaced through port 4001 but is never returned by its APIs or saved into browser preferences.
- Existing control watchdogs and neutral/resume requirements still pass verification.
- The documented limitation remains explicit: invited participants can reuse delivered credentials until revocation takes effect or credentials expire.

## Source references

- Cloudflare credential generation, expiry and revocation: https://developers.cloudflare.com/realtime/turn/generate-credentials/
- Cloudflare guidance on end-user credential abuse and usage attribution: https://developers.cloudflare.com/realtime/turn/replacing-existing/
- Cloudflare TURN credential expiration behavior: https://developers.cloudflare.com/realtime/turn/faq/

## Implementation verification — 2026-11-12

- Pi worktree: `/Users/vostapiv/Drones/rpanion-tweaks/.worktrees/private-turn`.
- Worker/browser worktree: `/private/tmp/fly-private-turn`.
- Pi full suite: **408 tests passed**. Use `/private/tmp/private-turn-pi-venv/bin/python` with real `gi, gi.repository` imported before unittest discovery; the existing battery test otherwise pollutes GI imports with global mocks. Original baseline passed 387 tests under the same setup.
- Worker/browser: **263 unit tests and 23 local Workers integration tests passed** with `npm test`. Provider calls are mocked; no production credentials are issued.
- Worker bundle: `wrangler deploy --dry-run` passed. Browser and Pi-settings inline JavaScript syntax checks and both `git diff --check` checks passed.
- Spec and code-quality review found no remaining important issues after correcting alarm persistence, retry serialization, conservative credential cleanup expiry, plugin diagnostic redaction, and pre-bootstrap browser rejection cleanup.
- No production token was generated or configured, no release was deployed, and no real Pi/LTE relay allocation or revocation acceptance result is claimed.
