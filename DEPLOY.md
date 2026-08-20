# Deployment — two halves, two hostnames

The lab is split so the interface stays up whether or not the robot does.

```text
https://dobot-cr3.primbiolab.org           UI — Next.js on a Cloudflare Worker.
        │                                  Always live: with the robot down it
        │                                  shows an offline banner and goes
        │  WebSocket + WebRTC              view-only.
        ▼
https://dobot-cr3-control.primbiolab.org   Cloudflare Tunnel → the lab computer
        ▼                                  (gatekeeper, foxglove_bridge, go2rtc)
   Dobot CR3 (ROS 2)
```

| Item | Value |
|---|---|
| Lab slug | `dobot-cr3` (registered in `remote_labs`) |
| Project | `Dobot CR3` — roles are per project, managed in the hub or at `/admin/users` |
| UI hostname | `dobot-cr3.primbiolab.org` |
| Control hostname | `dobot-cr3-control.primbiolab.org` |
| Supabase | the platform project; this lab creates none of its own |

**Why `dobot-cr3-control` and not `control.dobot-cr3`?** Cloudflare's free
Universal SSL covers one wildcard level (`*.primbiolab.org`). A second level
would fail TLS unless the zone buys Advanced Certificate Manager.

## Shared secrets

Two secrets, each shared between exactly two places. Neither belongs in git.

| Secret | Held by | Purpose |
|---|---|---|
| `LAB_CONTROL_SIGNING_SECRET` | this app + the gatekeeper | signs the short-lived lease token that lets the edge know who is driving |
| heartbeat secret | the lab computer + the database | authenticates the liveness beat; only its SHA-256 is stored |

If `LAB_CONTROL_SIGNING_SECRET` is missing or the two copies disagree, the lab
still runs and shows telemetry, and **every motion command is refused** — the
failure is view-only, never open. The gatekeeper logs it at startup.

## Releasing the UI

```bash
git submodule update --init      # the 3D model is generated from it
npm ci
npm run typecheck && npm run lint && npm run build
npx wrangler deploy
```

### The control lease lives in a Durable Object

`wrangler.jsonc` binds `CONTROL_LEASE` to the `ControlLeaseDO` class, and
`worker/index.ts` is the entry point that exports it alongside the generated
OpenNext worker. Nothing to configure: the migration in `wrangler.jsonc`
creates the namespace on the first deploy that carries it.

This is not optional dressing. The Worker runs in as many isolates as
Cloudflare decides to give it, and the fallback in-memory lease store is
per-isolate — with it, two people were each granted control of the arm and
each minted a valid lease token. If the binding is ever missing the app says
so in the logs, refuses to mint any lease token, and the lab is view-only.

A Durable Object has no Pub/Sub, so the spectator stream polls it: each open
tab costs roughly one request every two seconds plus its presence refresh. A
handful of viewers is a few requests a second — worth knowing if a whole class
leaves the lab open, since Durable Object requests are metered.

Confirm it is really there after deploying:

```bash
npx wrangler versions view <version-id> --name dobot-cr3   # expect env.CONTROL_LEASE
```

Production environment goes in the Worker, not in a file:

```bash
npx wrangler secret put LAB_CONTROL_SIGNING_SECRET
# NEXT_PUBLIC_* values are build-time; set them in .env.production.local
```

`NEXT_PUBLIC_CONTROL_URL=https://dobot-cr3-control.primbiolab.org`,
`NEXT_PUBLIC_AUTH_COOKIE_DOMAIN=.primbiolab.org` (so a sign-in on the hub
carries into this lab), `NEXT_PUBLIC_LAB_SLUG=dobot-cr3`.

Add `https://dobot-cr3.primbiolab.org` to the Supabase auth redirect allowlist
if it is not there already.

## Bringing up the control endpoint

Full walkthrough in [docs/deploy-pi.md](docs/deploy-pi.md); the short version:

```bash
cloudflared tunnel login                     # pick primbiolab.org
cloudflared tunnel create dobot-cr3-control
cloudflared tunnel route dns dobot-cr3-control dobot-cr3-control.primbiolab.org
sudo cp edge/cloudflared/config.yml /etc/cloudflared/config.yml   # fill the UUID
sudo cloudflared service install && sudo systemctl enable --now cloudflared
```

Then the four services on the lab computer — see [edge/README.md](edge/README.md).

## The heartbeat

The hub shows this lab as online only while it keeps saying so. A project admin
sets the secret once:

```sql
select set_lab_heartbeat_secret('<remote_labs.id>', '<random ≥16 chars>');
```

and the same value goes in `/etc/primbio/heartbeat.env` on the lab computer.
Silence for 90 s ⇒ the platform shows the lab offline. The beat goes straight
to Supabase, never through the hub, so a hub outage cannot black out the labs.

## Checking a deployment

```bash
curl https://dobot-cr3-control.primbiolab.org/health     # gatekeeper liveness
curl -I https://dobot-cr3.primbiolab.org                 # UI worker
```

Then, signed in as an operator: take control, jog an axis, and confirm from a
second browser signed in as a viewer that the move appears in the activity feed
with the operator's name. That single check exercises auth, the lease, the
lease token, the gatekeeper's allowlist and the fan-out in one go.
