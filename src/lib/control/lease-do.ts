// The control lease, in the one place all instances of this app can agree on.
//
// A Durable Object is a single globally-unique instance with its own storage,
// and requests to it are serialized. That is precisely the shape of "one
// active controller per lab": whatever else is true of the deployment — how
// many Workers isolates Cloudflare has spun up, in how many colos — there is
// exactly one of these per lab slug, and it is the only thing that decides who
// holds the lease.
//
// This matters because the alternative failed exactly as you would predict.
// The in-memory fallback store lives in `globalThis`, which on Workers means
// per-isolate: two people signing in were served by two isolates, each saw a
// free lease, each granted it, and each was minted a perfectly valid lease
// token. Both drove the arm at once. The semantics were never wrong — the
// state they ran against simply was not shared.
//
// The semantics themselves are not reimplemented here. The same pure functions
// the in-memory and Redis backends use are applied to the state this object
// holds, so there is one definition of what taking, queueing, handing over and
// expiring mean, and `npm run test:lease` covers all of them.

import {
  acceptHandover,
  declineHandover,
  forceAcquire,
  fromWire,
  heartbeat,
  publicState,
  release,
  requestHandover,
  toWire,
  touchPresence,
  tryAcquire,
  type ControlState,
  type ControlUser,
  type StoreState,
  type WireState,
} from "./store";

// Structural types for the small part of the Durable Object runtime this uses.
// Declared here rather than pulled in from the generated Cloudflare globals:
// those redefine DOM names this app also uses, and the surface needed is four
// methods wide.
interface LeaseStorage {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
}

interface LeaseObjectState {
  storage: LeaseStorage;
  blockConcurrencyWhile<T>(fn: () => Promise<T>): Promise<T>;
}

const STORAGE_KEY = "state";

/** One call from {@link DurableObjectStore}. */
export type LeaseCall =
  | { op: "state" }
  | { op: "take"; user: ControlUser }
  | { op: "force"; user: ControlUser }
  | { op: "heartbeat"; user: ControlUser }
  | { op: "release"; userId: string }
  | { op: "estop"; user: ControlUser }
  | { op: "joinPresence"; user: ControlUser }
  | { op: "leavePresence"; userId: string }
  | { op: "requestHandover"; user: ControlUser }
  | { op: "acceptHandover"; holderId: string; toUserId: string }
  | { op: "declineHandover"; holderId: string; fromUserId: string };

export interface LeaseReply<T = unknown> {
  result: T;
  state: ControlState;
}

export class ControlLeaseDO {
  // Set before any request is served: the constructor blocks on hydration.
  private state!: StoreState;
  private storage: LeaseStorage;

  constructor(ctx: LeaseObjectState) {
    this.storage = ctx.storage;
    // Without this, two concurrent first-requests would both find no state in
    // memory, both load it, and the second would overwrite the first's grant.
    ctx.blockConcurrencyWhile(async () => {
      this.state = fromWire((await ctx.storage.get<WireState>(STORAGE_KEY)) ?? null);
    });
  }

  async fetch(request: Request): Promise<Response> {
    let call: LeaseCall;
    try {
      call = (await request.json()) as LeaseCall;
    } catch {
      return new Response("malformed lease call", { status: 400 });
    }

    const s = this.state;
    const now = Date.now();
    const before = JSON.stringify(toWire(s));
    let result: unknown = null;

    switch (call.op) {
      case "state":
        break;
      case "take":
        result = tryAcquire(s, call.user, now);
        break;
      case "force":
        result = forceAcquire(s, call.user, now);
        break;
      case "heartbeat":
        result = heartbeat(s, call.user, now);
        break;
      case "release":
        result = release(s, call.userId, now);
        break;
      case "estop":
        s.estopAt = now;
        s.estopBy = call.user.name;
        break;
      case "joinPresence":
        touchPresence(s, call.user, now);
        break;
      case "leavePresence":
        s.presence.delete(call.userId);
        break;
      case "requestHandover":
        result = requestHandover(s, call.user, now);
        break;
      case "acceptHandover":
        result = acceptHandover(s, call.holderId, call.toUserId, now) !== null;
        break;
      case "declineHandover":
        result = declineHandover(s, call.holderId, call.fromUserId, now);
        break;
      default:
        return new Response("unknown lease op", { status: 400 });
    }

    // `publicState` prunes, so even a read can change the state — an expired
    // lease is dropped by looking at it. Persisting on any real change rather
    // than on any call keeps the writes to roughly one per heartbeat.
    const state = publicState(s, now);
    const after = toWire(s);
    if (JSON.stringify(after) !== before) {
      await this.storage.put(STORAGE_KEY, after);
    }

    const reply: LeaseReply = { result, state };
    return new Response(JSON.stringify(reply), {
      headers: { "Content-Type": "application/json" },
    });
  }
}
