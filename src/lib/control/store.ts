// Hardware control plane: one active controller per lab, everyone else
// spectates. The lease is a 15 s TTL entry extended by client heartbeats —
// a crashed or disconnected controller releases the hardware automatically.
//
// Three backends, one set of semantics. What separates them is only *where the
// state lives*, and that turns out to be the whole safety property: a store
// that is not shared by every instance of this app does not enforce one
// controller, it enforces one controller per instance. Read `shared` below
// before adding a fourth.
//
//   durable-object  Cloudflare Durable Object — production. One instance per
//                   lab, globally, whatever Cloudflare does with isolates.
//   redis           REDIS_URL — shared across instances, with Pub/Sub fan-out.
//   memory          Neither configured. Single process only: `next dev`, the
//                   lease test, and demo mode.

import { EventEmitter } from "node:events";
import type { RedisClientType } from "redis";
import type { LeaseCall, LeaseReply } from "./lease-do";

export const LEASE_TTL_MS = 15_000;
export const QUEUE_TTL_MS = 30_000;
export const PRESENCE_TTL_MS = 60_000;

export interface ControlUser {
  id: string;
  name: string;
}

export interface ControlState {
  holder: ControlUser | null;
  queue: ControlUser[];
  presence: ControlUser[];
  /** Operators asking the current holder to hand control over. */
  handoverRequests: ControlUser[];
  /** Timestamp of the last emergency stop, if any. */
  estopAt: number | null;
  estopBy: string | null;
}

export interface TakeResult {
  granted: boolean;
  position: number; // 0 = holder, 1 = next in line…
}

export interface Entry {
  user: ControlUser;
  joined: number;
  last: number;
}

export interface StoreState {
  holder: { user: ControlUser; expires: number } | null;
  queue: Map<string, Entry>;
  presence: Map<string, Entry>;
  handovers: Map<string, Entry>;
  estopAt: number | null;
  estopBy: string | null;
}

// ── Core semantics (shared by both backends via a JSON state blob) ──────────

export function prune(s: StoreState, now: number): void {
  if (s.holder && s.holder.expires <= now) s.holder = null;
  for (const [id, e] of s.queue) if (now - e.last > QUEUE_TTL_MS) s.queue.delete(id);
  for (const [id, e] of s.presence) if (now - e.last > PRESENCE_TTL_MS) s.presence.delete(id);
  // A handover request is meaningless once the person who asked has gone, or
  // once the holder they asked has changed.
  for (const [id, e] of s.handovers) {
    if (now - e.last > QUEUE_TTL_MS || s.holder == null) s.handovers.delete(id);
  }
}

// Ask the current holder to pass control over. Unlike the queue — which waits
// for the lease to lapse — and unlike an admin force, this needs the holder to
// agree, so it is the polite path between two operators who are both entitled
// to drive.
export function requestHandover(s: StoreState, user: ControlUser, now: number): boolean {
  prune(s, now);
  if (!s.holder || s.holder.user.id === user.id) return false;
  const existing = s.handovers.get(user.id);
  s.handovers.set(user.id, {
    user,
    joined: existing?.joined ?? now,
    last: now,
  });
  return true;
}

// The holder agrees. Control moves in one step: there is no window where both
// of them hold it, and none where neither does.
export function acceptHandover(
  s: StoreState,
  holderId: string,
  toUserId: string,
  now: number,
): ControlUser | null {
  prune(s, now);
  if (s.holder?.user.id !== holderId) return null;
  const incoming = s.handovers.get(toUserId);
  if (!incoming) return null;

  s.holder = { user: incoming.user, expires: now + LEASE_TTL_MS };
  s.handovers.clear();
  s.queue.delete(incoming.user.id);
  return incoming.user;
}

export function declineHandover(s: StoreState, holderId: string, fromUserId: string, now: number): boolean {
  prune(s, now);
  if (s.holder?.user.id !== holderId) return false;
  return s.handovers.delete(fromUserId);
}

function orderedQueue(s: StoreState): Entry[] {
  return [...s.queue.values()].sort((a, b) => a.joined - b.joined);
}

export function tryAcquire(s: StoreState, user: ControlUser, now: number): TakeResult {
  prune(s, now);
  if (!s.holder || s.holder.user.id === user.id) {
    // Free (or already ours): grant only if we are first in line or the
    // queue is empty / we head it.
    const q = orderedQueue(s).filter((e) => e.user.id !== user.id);
    if (!s.holder && q.length > 0 && q[0].joined < (s.queue.get(user.id)?.joined ?? now)) {
      // Someone queued before us — keep waiting.
      s.queue.set(user.id, {
        user,
        joined: s.queue.get(user.id)?.joined ?? now,
        last: now,
      });
      return { granted: false, position: position(s, user.id) };
    }
    s.holder = { user, expires: now + LEASE_TTL_MS };
    s.queue.delete(user.id);
    return { granted: true, position: 0 };
  }
  s.queue.set(user.id, {
    user,
    joined: s.queue.get(user.id)?.joined ?? now,
    last: now,
  });
  return { granted: false, position: position(s, user.id) };
}

// Admin override. The reference implementation called it "force control": an
// arm left enabled by someone who walked away blocks the lab until their lease
// expires, and a project admin needs a way through that is not "wait". The
// displaced holder finds out the same way everyone else does — the state
// stream — and their next heartbeat returns no lease token, so their commands
// stop being authorized within one heartbeat interval.
export function forceAcquire(s: StoreState, user: ControlUser, now: number): TakeResult {
  prune(s, now);
  s.holder = { user, expires: now + LEASE_TTL_MS };
  s.queue.delete(user.id);
  return { granted: true, position: 0 };
}

function position(s: StoreState, userId: string): number {
  const q = orderedQueue(s);
  const idx = q.findIndex((e) => e.user.id === userId);
  return idx === -1 ? q.length + 1 : idx + 1;
}

export function heartbeat(s: StoreState, user: ControlUser, now: number): TakeResult {
  prune(s, now);
  if (s.holder?.user.id === user.id) {
    s.holder.expires = now + LEASE_TTL_MS;
    return { granted: true, position: 0 };
  }
  if (s.queue.has(user.id)) {
    // Waiting: refresh, and take over if the lease is free and we head it.
    const e = s.queue.get(user.id)!;
    e.last = now;
    return tryAcquire(s, user, now);
  }
  return { granted: false, position: position(s, user.id) };
}

export function release(s: StoreState, userId: string, now: number): boolean {
  prune(s, now);
  if (s.holder?.user.id === userId) {
    s.holder = null;
    return true;
  }
  s.queue.delete(userId);
  return false;
}

export function touchPresence(s: StoreState, user: ControlUser, now: number): void {
  const e = s.presence.get(user.id);
  s.presence.set(user.id, { user, joined: e?.joined ?? now, last: now });
}

export function publicState(s: StoreState, now: number): ControlState {
  prune(s, now);
  return {
    holder: s.holder?.user ?? null,
    queue: orderedQueue(s).map((e) => e.user),
    handoverRequests: [...s.handovers.values()]
      .sort((a, b) => a.joined - b.joined)
      .map((e) => e.user),
    presence: [...s.presence.values()]
      .sort((a, b) => a.joined - b.joined)
      .map((e) => e.user),
    estopAt: s.estopAt,
    estopBy: s.estopBy,
  };
}

// ── Serialization for the Redis backend ─────────────────────────────────────

export interface WireState {
  holder: { user: ControlUser; expires: number } | null;
  queue: Entry[];
  presence: Entry[];
  handovers: Entry[];
  estopAt: number | null;
  estopBy: string | null;
}

export function toWire(s: StoreState): WireState {
  return {
    holder: s.holder,
    queue: [...s.queue.values()],
    presence: [...s.presence.values()],
    handovers: [...s.handovers.values()],
    estopAt: s.estopAt,
    estopBy: s.estopBy,
  };
}

export function fromWire(w: WireState | null): StoreState {
  return {
    holder: w?.holder ?? null,
    queue: new Map((w?.queue ?? []).map((e) => [e.user.id, e])),
    presence: new Map((w?.presence ?? []).map((e) => [e.user.id, e])),
    handovers: new Map((w?.handovers ?? []).map((e) => [e.user.id, e])),
    estopAt: w?.estopAt ?? null,
    estopBy: w?.estopBy ?? null,
  };
}

// ── Store interface ─────────────────────────────────────────────────────────

export interface ControlStore {
  readonly backend: "durable-object" | "redis" | "memory";
  /**
   * True when every instance of this app sees the same lease.
   *
   * False means this store speaks only for the process it lives in, so "the
   * lease is free" means "free as far as I know". No lease token may be minted
   * against a store like that on a multi-instance runtime — see
   * `mintLeaseToken`, which is where that is enforced.
   */
  readonly shared: boolean;
  take(labId: string, user: ControlUser): Promise<TakeResult>;
  /** Seize the lease regardless of who holds it. Admins only — see route. */
  force(labId: string, user: ControlUser): Promise<TakeResult>;
  /** Ask the current holder to hand control over. */
  requestHandover(labId: string, user: ControlUser): Promise<boolean>;
  /** Holder accepts: control moves in one step. */
  acceptHandover(labId: string, holderId: string, toUserId: string): Promise<boolean>;
  declineHandover(labId: string, holderId: string, fromUserId: string): Promise<boolean>;
  heartbeat(labId: string, user: ControlUser): Promise<TakeResult>;
  release(labId: string, userId: string): Promise<void>;
  estop(labId: string, by: ControlUser): Promise<void>;
  joinPresence(labId: string, user: ControlUser): Promise<void>;
  leavePresence(labId: string, userId: string): Promise<void>;
  state(labId: string): Promise<ControlState>;
  subscribe(labId: string, fn: (s: ControlState) => void): Promise<() => void>;
}

// ── In-memory backend (single instance; dev and degraded mode) ──────────────

class MemoryStore implements ControlStore {
  readonly backend = "memory" as const;
  // One process's opinion. Correct when there is only one process.
  readonly shared = false;
  private labs = new Map<string, StoreState>();
  private bus = new EventEmitter();

  private lab(id: string): StoreState {
    let s = this.labs.get(id);
    if (!s) {
      s = fromWire(null);
      this.labs.set(id, s);
    }
    return s;
  }

  private emit(id: string): void {
    this.bus.emit(id, publicState(this.lab(id), Date.now()));
  }

  async take(id: string, user: ControlUser): Promise<TakeResult> {
    const r = tryAcquire(this.lab(id), user, Date.now());
    this.emit(id);
    return r;
  }
  async force(id: string, user: ControlUser): Promise<TakeResult> {
    const r = forceAcquire(this.lab(id), user, Date.now());
    this.emit(id);
    return r;
  }
  async requestHandover(id: string, user: ControlUser): Promise<boolean> {
    const ok = requestHandover(this.lab(id), user, Date.now());
    this.emit(id);
    return ok;
  }
  async acceptHandover(id: string, holderId: string, toUserId: string): Promise<boolean> {
    const moved = acceptHandover(this.lab(id), holderId, toUserId, Date.now());
    this.emit(id);
    return moved !== null;
  }
  async declineHandover(id: string, holderId: string, fromUserId: string): Promise<boolean> {
    const ok = declineHandover(this.lab(id), holderId, fromUserId, Date.now());
    this.emit(id);
    return ok;
  }
  async heartbeat(id: string, user: ControlUser): Promise<TakeResult> {
    const before = publicState(this.lab(id), Date.now()).holder?.id ?? null;
    const r = heartbeat(this.lab(id), user, Date.now());
    if ((this.lab(id).holder?.user.id ?? null) !== before) this.emit(id);
    return r;
  }
  async release(id: string, userId: string): Promise<void> {
    release(this.lab(id), userId, Date.now());
    this.emit(id);
  }
  async estop(id: string, by: ControlUser): Promise<void> {
    const s = this.lab(id);
    s.estopAt = Date.now();
    s.estopBy = by.name;
    this.emit(id);
  }
  async joinPresence(id: string, user: ControlUser): Promise<void> {
    touchPresence(this.lab(id), user, Date.now());
    this.emit(id);
  }
  async leavePresence(id: string, userId: string): Promise<void> {
    this.lab(id).presence.delete(userId);
    this.emit(id);
  }
  async state(id: string): Promise<ControlState> {
    return publicState(this.lab(id), Date.now());
  }
  async subscribe(id: string, fn: (s: ControlState) => void): Promise<() => void> {
    this.bus.on(id, fn);
    return () => this.bus.off(id, fn);
  }
}

// ── Redis backend ───────────────────────────────────────────────────────────
// State lives in one key per lab, mutated under a short WATCH-free lock via
// Lua-less optimistic writes: mutations are serialized through a per-lab
// Redis lock key (SET NX PX 2000) to keep read-modify-write atomic enough
// for this small state, and every mutation publishes the new public state.

class RedisStore implements ControlStore {
  readonly backend = "redis" as const;
  readonly shared = true;

  private client: RedisClientType;
  private subClient: RedisClientType;

  // Written out rather than as constructor parameter properties so this module
  // can be loaded directly by Node's type-stripping loader, which the
  // control-lease test script relies on.
  constructor(client: RedisClientType, subClient: RedisClientType) {
    this.client = client;
    this.subClient = subClient;
  }

  private key(id: string): string {
    return `lab:${id}:control`;
  }
  private chan(id: string): string {
    return `lab:${id}:events`;
  }

  private async mutate<T>(
    id: string,
    fn: (s: StoreState, now: number) => T,
  ): Promise<T> {
    const lockKey = `lab:${id}:mutex`;
    // Spin briefly for the mutation mutex; contention here is tiny.
    for (let i = 0; i < 50; i++) {
      const ok = await this.client.set(lockKey, "1", { NX: true, PX: 2000 });
      if (ok) break;
      await new Promise((r) => setTimeout(r, 40));
    }
    try {
      const now = Date.now();
      const raw = await this.client.get(this.key(id));
      const s = fromWire(raw ? (JSON.parse(raw) as WireState) : null);
      const result = fn(s, now);
      await this.client.set(this.key(id), JSON.stringify(toWire(s)), {
        PX: 24 * 3600 * 1000,
      });
      await this.client.publish(
        this.chan(id),
        JSON.stringify(publicState(s, now)),
      );
      return result;
    } finally {
      await this.client.del(lockKey);
    }
  }

  take(id: string, user: ControlUser): Promise<TakeResult> {
    return this.mutate(id, (s, now) => tryAcquire(s, user, now));
  }
  force(id: string, user: ControlUser): Promise<TakeResult> {
    return this.mutate(id, (s, now) => forceAcquire(s, user, now));
  }
  requestHandover(id: string, user: ControlUser): Promise<boolean> {
    return this.mutate(id, (s, now) => requestHandover(s, user, now));
  }
  acceptHandover(id: string, holderId: string, toUserId: string): Promise<boolean> {
    return this.mutate(
      id, (s, now) => acceptHandover(s, holderId, toUserId, now) !== null);
  }
  declineHandover(id: string, holderId: string, fromUserId: string): Promise<boolean> {
    return this.mutate(id, (s, now) => declineHandover(s, holderId, fromUserId, now));
  }
  heartbeat(id: string, user: ControlUser): Promise<TakeResult> {
    return this.mutate(id, (s, now) => heartbeat(s, user, now));
  }
  async release(id: string, userId: string): Promise<void> {
    await this.mutate(id, (s, now) => release(s, userId, now));
  }
  async estop(id: string, by: ControlUser): Promise<void> {
    await this.mutate(id, (s) => {
      s.estopAt = Date.now();
      s.estopBy = by.name;
    });
  }
  async joinPresence(id: string, user: ControlUser): Promise<void> {
    await this.mutate(id, (s, now) => touchPresence(s, user, now));
  }
  async leavePresence(id: string, userId: string): Promise<void> {
    await this.mutate(id, (s) => s.presence.delete(userId));
  }
  async state(id: string): Promise<ControlState> {
    const raw = await this.client.get(this.key(id));
    return publicState(
      fromWire(raw ? (JSON.parse(raw) as WireState) : null),
      Date.now(),
    );
  }
  async subscribe(
    id: string,
    fn: (s: ControlState) => void,
  ): Promise<() => void> {
    const handler = (message: string) => {
      try {
        fn(JSON.parse(message) as ControlState);
      } catch {
        // Malformed event: skip.
      }
    };
    await this.subClient.subscribe(this.chan(id), handler);
    return () => {
      void this.subClient.unsubscribe(this.chan(id), handler);
    };
  }
}

// ── Durable Object backend (production on Cloudflare Workers) ───────────────
// The state and the semantics live inside the object itself (lease-do.ts);
// this is the client. Every call is one request to the object named after the
// lab, and every reply carries the new public state, so a mutation and the
// broadcast that follows it are always consistent with each other.

// How often a spectator's stream re-reads the object. There is no Pub/Sub to
// subscribe to, so this is both the fan-out latency and a per-viewer request
// cost: a viewer sees control change hands within this long, and each open tab
// costs one request to the lease object per interval for as long as it is
// open. Two seconds is well inside the 15 s lease TTL and keeps a classroom of
// spectators to a few requests a second.
const DO_POLL_MS = 2000;

interface LeaseStub {
  fetch(input: string, init?: RequestInit): Promise<Response>;
}

interface LeaseNamespace {
  idFromName(name: string): unknown;
  get(id: unknown): LeaseStub;
}

class DurableObjectStore implements ControlStore {
  readonly backend = "durable-object" as const;
  readonly shared = true;

  private async call<T>(labId: string, body: LeaseCall): Promise<LeaseReply<T>> {
    const ns = await getLeaseNamespace();
    if (!ns) throw new Error("control store: CONTROL_LEASE binding unavailable");
    // Resolved per call rather than held: a binding belongs to the request
    // context it was taken from, and this store outlives any one request.
    const stub = ns.get(ns.idFromName(labId));
    const response = await stub.fetch("https://control.lease/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`control store: lease object returned ${response.status}`);
    }
    return (await response.json()) as LeaseReply<T>;
  }

  async take(id: string, user: ControlUser): Promise<TakeResult> {
    return (await this.call<TakeResult>(id, { op: "take", user })).result;
  }
  async force(id: string, user: ControlUser): Promise<TakeResult> {
    return (await this.call<TakeResult>(id, { op: "force", user })).result;
  }
  async heartbeat(id: string, user: ControlUser): Promise<TakeResult> {
    return (await this.call<TakeResult>(id, { op: "heartbeat", user })).result;
  }
  async requestHandover(id: string, user: ControlUser): Promise<boolean> {
    return (await this.call<boolean>(id, { op: "requestHandover", user })).result;
  }
  async acceptHandover(id: string, holderId: string, toUserId: string): Promise<boolean> {
    return (
      await this.call<boolean>(id, { op: "acceptHandover", holderId, toUserId })
    ).result;
  }
  async declineHandover(id: string, holderId: string, fromUserId: string): Promise<boolean> {
    return (
      await this.call<boolean>(id, { op: "declineHandover", holderId, fromUserId })
    ).result;
  }
  async release(id: string, userId: string): Promise<void> {
    await this.call(id, { op: "release", userId });
  }
  async estop(id: string, by: ControlUser): Promise<void> {
    await this.call(id, { op: "estop", user: by });
  }
  async joinPresence(id: string, user: ControlUser): Promise<void> {
    await this.call(id, { op: "joinPresence", user });
  }
  async leavePresence(id: string, userId: string): Promise<void> {
    await this.call(id, { op: "leavePresence", userId });
  }
  async state(id: string): Promise<ControlState> {
    return (await this.call(id, { op: "state" })).state;
  }

  async subscribe(id: string, fn: (s: ControlState) => void): Promise<() => void> {
    // Polled rather than pushed. A Durable Object could hold the sockets and
    // push, but that would move the SSE endpoint into it for a second of
    // latency; the route already only writes a frame when the state differs.
    let last: string | null = null;
    const tick = async () => {
      try {
        const state = await this.state(id);
        const frame = JSON.stringify(state);
        if (frame === last) return;
        last = frame;
        fn(state);
      } catch {
        // Transient: the next tick tries again. A stream that stops updating
        // is recoverable; one that throws here would close on every blip.
      }
    };
    const timer = setInterval(tick, DO_POLL_MS);
    return () => clearInterval(timer);
  }
}

/**
 * The lease object's namespace binding, or null when this is not running on
 * Workers — `next dev`, the lease test, `next build`.
 */
async function getLeaseNamespace(): Promise<LeaseNamespace | null> {
  try {
    const { getCloudflareContext } = await import("@opennextjs/cloudflare");
    const { env } = await getCloudflareContext({ async: true });
    const ns = (env as unknown as { CONTROL_LEASE?: LeaseNamespace }).CONTROL_LEASE;
    return ns ?? null;
  } catch {
    return null;
  }
}

/**
 * True when this process is one of many serving the same app, so a store that
 * is not shared cannot be trusted to say who holds the lease.
 */
export function isMultiInstanceRuntime(): boolean {
  return globalThis.navigator?.userAgent === "Cloudflare-Workers";
}

// ── Singleton ───────────────────────────────────────────────────────────────

declare global {
  var __controlStore: Promise<ControlStore> | undefined;
}

async function build(): Promise<ControlStore> {
  // The lease object first: on Workers it is the only backend that can hold
  // the invariant, and it needs no configuration beyond its binding.
  if (await getLeaseNamespace()) return new DurableObjectStore();

  const url = process.env.REDIS_URL;
  if (url) {
    try {
      const { createClient } = await import("redis");
      const client = createClient({ url }) as RedisClientType;
      const subClient = client.duplicate() as RedisClientType;
      await client.connect();
      await subClient.connect();
      return new RedisStore(client, subClient);
    } catch (e) {
      console.warn(
        `control store: Redis unavailable (${(e as Error).message}); using in-memory store`,
      );
    }
  }

  if (isMultiInstanceRuntime()) {
    // Loud, because the lab is about to be view-only and the reason is a
    // missing binding rather than anything an operator did. Granting control
    // from a per-isolate store is the failure this refuses to repeat.
    console.error(
      "control store: no CONTROL_LEASE binding and no REDIS_URL on a " +
        "multi-instance runtime — the lease cannot be shared, so no lease " +
        "token will be minted and the lab stays view-only.",
    );
  }
  return new MemoryStore();
}

export function getControlStore(): Promise<ControlStore> {
  // Survives dev hot reloads; one store per server process.
  globalThis.__controlStore ??= build();
  return globalThis.__controlStore;
}
