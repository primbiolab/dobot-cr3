# Edge gatekeeper

The only door into this lab's ROS 2 graph. `foxglove_bridge` has no
authentication of its own, so it stays bound to localhost and this process is
what the Cloudflare Tunnel actually reaches.

```text
browser ──wss──► dobot-cr3-control.primbiolab.org ──► gatekeeper :8766
                          (cloudflared)                    │ relays vetted frames
                                                           ▼
                                              foxglove_bridge :8765 (localhost)
                                                           ▼
                                                    ROS 2 · Dobot CR3
```

## What it does

1. **Authenticates every socket.** The first frame must be
   `{"op":"auth","token":"<supabase access token>","lease":"<lease token>"}`,
   within 5 seconds. The access token is verified against the platform's JWKS —
   no database round trip, and **no service-role key ever lives on the Pi**. The
   role comes from the `project_roles` claim the platform's access-token hook
   injects (hub migration `0004`).
2. **Authorizes every frame** against [`policy.py`](primbio_gateway/policy.py),
   which is the entire authorization surface of the edge in one file. Two rules
   carry the safety of the lab:
   - **stopping is never gated by the control lease** — any operator can stop
     the arm while somebody else drives;
   - **anything that increases motion needs the operator role *and* a live
     lease token**.
   Everything not explicitly named is denied, and the raw driver services
   (`/dobot_cr3_bringup/srv/*`) are never exposed — only the vetted `/weblab/*`
   surface.
   A valid lease token is necessary but not sufficient: two tokens minted for
   two people are each valid on their own, so `LeaseVerifier` also arbitrates
   between them. The first live token takes the lease and anybody else's is
   refused until it lapses, is cleared by its browser, or its socket closes.
   This process is the only one that sees every client of this robot, so it is
   the only place that can hold "one driver" whatever the web app believes —
   and the web app did once believe two.
3. **Fans out what the operator is doing.** Every authorized command is
   broadcast to *all* connected sessions as an `activity` frame carrying the
   name of the person who issued it, so a spectator can tell a deliberate move
   from a fault. Joins and leaves go out as `presence` frames.

The activity feed is a log of *commands accepted and dispatched*, not of
completed motions: a command that ROS later rejects still appears. Telemetry is
what says whether the arm actually moved.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `SUPABASE_URL` | yes | central platform project |
| `LAB_PROJECT_ID` | yes | `projects.id` this lab belongs to; the role is read from `project_roles[<this id>]` |
| `LAB_SLUG` | yes | must match the lease token audience |
| `LAB_CONTROL_SIGNING_SECRET` | yes in practice | shared with the lab web app. Unset ⇒ no lease can be verified ⇒ every motion command is refused and the lab is view-only |
| `SUPABASE_JWT_SECRET` | only for legacy projects | HS256 fallback when the project has no JWKS |
| `FOXGLOVE_BRIDGE_URL` | no | default `ws://127.0.0.1:8765` |
| `GATEWAY_HOST` / `GATEWAY_PORT` | no | default `127.0.0.1:8766` — bind to localhost and let `cloudflared` be the only ingress |
| `LAB_DEFAULT_ROLE` | no | role for an authenticated user holding none on this project. Default `viewer`, matching the platform rule that every account is an implicit viewer of every project (migration 0012); set it empty to close the lab to everyone without an explicit role |

## Running

```bash
python3 -m pip install -r requirements.txt
python3 -m primbio_gateway.server          # honours the variables above
curl localhost:8766/health                 # liveness; exposes no lab data
```

It never imports `rclpy`, so it runs with or without the ROS 2 workspace
sourced and keeps refusing unauthorized callers while the robot stack restarts.

## Tests

```bash
python3 -m pytest tests/ -q
```

`test_policy.py` pins the two safety invariants. `test_gateway.py` runs the
whole thing against a stand-in bridge and asserts the behaviour this lab exists
for: a viewer who does nothing still sees the operator's commands, a viewer's
own command is refused and never reaches ROS, an operator without the lease
cannot move the arm but can still stop it, and forged, expired or borrowed
lease tokens grant nothing. It also covers the two-driver case directly: two
clients arrive holding two genuine lease tokens, only one of them moves the
arm, and the one that was refused can still stop it.
