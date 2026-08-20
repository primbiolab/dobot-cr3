# Concurrency — one operator, many viewers

N concurrent viewers, **one active controller** (PLATFORM-GUIDE §2.3). This is
the part of the lab that makes it a shared instrument rather than a
single-user machine, so it is worth being precise about what each person can
see and do.

| Can | viewer | operator | admin / owner |
| --- | --- | --- | --- |
| Video, telemetry, 3D model | ✔ | ✔ | ✔ |
| See who is driving and what they command | ✔ | ✔ | ✔ |
| Queue for control | — | ✔ | ✔ |
| Drive the arm | — | while holding the lease | while holding the lease |
| Emergency stop | — | **always** | **always** |
| Take control from a live holder | — | — | ✔ |
| Edit programs and the camera wall | — | ✔ | ✔ |
| Lab settings, roles | — | — | ✔ |

## The lease

Control is a lease with a **15 s TTL** (`LEASE_TTL_MS`). One set of semantics,
implemented once, applied to state that lives in one of three places:

| Backend | Where the state lives | Shared? |
| --- | --- | --- |
| `durable-object` | a Cloudflare Durable Object, one per lab slug | ✔ |
| `redis` | `REDIS_URL`, with Pub/Sub fan-out | ✔ |
| `memory` | this process | ✘ |

**The semantics are not what makes one controller true — the sharing is.** The
in-memory store was described here as having "identical semantics", and it
does; what it does not have is a single copy of the state. On Cloudflare
Workers `globalThis` is per isolate, so a deployment with no shared store gave
every isolate its own lease and granted control to as many people as there were
isolates. Two of them both drove the arm.

So the app refuses to mint a lease token from an unshared store when it is
running on a multi-instance runtime (`ControlStore.shared`). The lab goes
view-only — the same failure mode as a missing signing secret — rather than
handing the hardware to two people. Production gets the Durable Object from the
`CONTROL_LEASE` binding in `wrangler.jsonc`; `next dev` and the lease test are
one process, so memory is genuinely shared there and is used as before.

- `POST /api/control/take` — grant if free and nobody queued ahead; otherwise
  join the wait queue and return the position.
- `POST /api/control/heartbeat` — every 5 s from the client. The holder extends
  the TTL; a waiting client refreshes its queue slot (30 s TTL) and
  **automatically inherits the lease** when it heads the queue and the lease is
  free.
- `POST /api/control/release` — voluntary release, or leave the queue.
- `POST /api/control/handover` — the polite path between two operators who are
  both entitled to drive: one asks, and the person currently driving accepts or
  declines. Control moves in a single step, so there is no moment where both
  hold it and none where neither does. Distinct from the queue (which waits for
  a lease to lapse) and from a force (which does not ask).
- `POST /api/control/force` — admins and the owner only. A lease is held by a
  browser and browsers get left open; an arm parked by someone who went to
  lunch would otherwise block the lab until their tab stopped beating.

Stop heartbeating — crash, tab close, network loss — and the lease expires by
itself within 15 s. Nothing has to notice, and nothing has to be revoked.

**Leases are per lab.** dobot-cr3 and tclab each have their own controller at
the same time; a lease on one never blocks another, and an emergency stop on
one does not stop the other. Everything below is scoped to a single lab.

## Reaching the hardware with it

The lease lives on Cloudflare. The hardware is on a Raspberry Pi behind an
outbound-only tunnel and cannot see the lease store at all. So "I hold the
lease" travels to the edge as a **short-lived signed token**:

```text
take/heartbeat ──► lease token (HS256, 10 s, bound to user + lab slug)
                        │
                        ▼  sent on the WebSocket, refreshed every heartbeat
              edge gatekeeper verifies it before relaying any motion command
```

The token is minted with `LAB_CONTROL_SIGNING_SECRET`, shared only between this
app and the gatekeeper. Its lifetime is the safety property: when a lease
expires, the token stops being reissued and the hardware stops accepting motion
within one token lifetime — no revocation message has to arrive for the arm to
become safe. With the secret unset the app cannot mint tokens at all, and the
lab is view-only rather than open.

### The gatekeeper arbitrates too

A lease token is verifiable on its own — signed, unexpired, bound to the person
presenting it — which means two of them, minted for two people, both pass. That
is exactly what happened, and every check on the edge passed for both. So the
gatekeeper no longer takes a valid token as the end of the question: it admits
the **first** valid token it sees and refuses anybody else's while that one is
still live (`LeaseVerifier`, `edge/gateway/primbio_gateway/auth.py`).

It is the only process that sees every client of this one robot, so it is the
only place the invariant can be held independently of what the control plane
believes. Two consequences worth knowing:

- A displaced operator's browser clears its token as soon as the state stream
  says somebody else is driving, and the gatekeeper releases the claim on that,
  on disconnect, or when the token lapses. A **force therefore takes effect at
  once in the normal case**, and within one token lifetime (10 s) if the
  outgoing browser does not cooperate.
- Refusing the lease never refuses a **stop**. Stops require the operator role
  and nothing else, and that is tested from a client the arbiter has just
  turned down.

## Emergency stop

`POST /api/control/estop` and the `/weblab/estop` service both require the
**operator role only, never the lease**. Any operator can stop the hardware
while somebody else drives. There are deliberately two independent paths — the
operator's own WebSocket straight to the edge, and a server-to-server call from
this app — because a wedged socket must not be able to leave an arm moving with
no way to stop it. Do not "fix" either by adding a lease check.

The lab computer stops on its own too: the weblab node halts a jog after 1 s of
silence, well before the lease expires.

## What spectators see

A spectator who can only see joint angles cannot tell a deliberate move from a
fault, nor who is responsible. Three streams answer that, and every connected
person receives all three regardless of role:

1. **Telemetry** — joints, TCP pose, enabled state, speed, and the running
   program's step counter. Published once by the weblab node and fanned out by
   `foxglove_bridge`.
2. **Activity** — every command the gatekeeper accepts, broadcast to all
   sessions with the name of whoever issued it. It is a log of commands
   *dispatched*, not of completed motions: a command ROS later rejects still
   appears. Telemetry is what says the arm actually moved.
3. **Presence and lease state** — `GET /api/control/state`, an SSE stream.
   Connecting registers the client in the presence list; closing removes it.
   Every mutation publishes through the store's Pub/Sub, and a 5 s keepalive
   re-read catches silent lease expiries.

## Authorization

Two independent gates, and they do not trust each other:

- **This app** resolves the session from cookies and the role from the lab's
  project (`owner_id` / `project_members`), then decides whether to grant a
  lease and mint a token.
- **The edge gatekeeper** verifies the Supabase token itself against the
  platform's public keys and reads the role from the `project_roles` JWT claim,
  then applies its own allowlist. It would refuse a forged lease token from a
  compromised web tier, and it holds no database credential of its own.

The UI's disabled buttons are cosmetic. With Supabase unconfigured the app runs
in demo mode: a local pseudo-user with operator affordances, mock telemetry, no
hardware.

## Verifying it

```bash
npm run test:lease                                   # memory backend
REDIS_URL=redis://localhost:6379 npm run test:lease  # redis backend
npm run test:gateway                                 # the edge, including the arbiter
```

Covers: two labs each holding their own controller at once; a second operator
is queued rather than granted; a holder that stops
heartbeating loses control within the TTL; the next in line inherits it on
their own heartbeat; force displaces a live holder and the displaced holder
does not reclaim it on their next beat; a handover moves control in one step
and leaves the previous holder unable to drive or to reclaim it; only the real
holder can accept, and only a request that was actually made; e-stop is
recorded from someone holding nothing and does not cross labs; and every
mutation reaches a subscriber. Both backends must pass identically.

What no single-process test can cover is the failure that actually happened —
one store per instance — because there is only ever one instance under test.
That gap is closed from the other end: `npm run test:gateway` puts two clients
holding two valid lease tokens on one gatekeeper and asserts that only one of
them moves the arm, and that the refused one can still stop it.

The edge half is covered by `edge/gateway/tests/` — in particular that a viewer
who does nothing still receives the operator's commands, and that a viewer's
own command is refused and never reaches ROS.
