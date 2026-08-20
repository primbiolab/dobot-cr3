#!/usr/bin/env node --experimental-strip-types
// Concurrency check for the control lease: one active controller, N viewers.
//
// This exercises the store directly rather than through the HTTP API, because
// the interesting cases are about *time* — a lease expiring because a browser
// stopped heartbeating — and driving them through two real browser sessions
// would mean waiting on wall-clock for every assertion. The API routes on top
// are a thin shell over exactly these calls.
//
// Run: node --experimental-strip-types scripts/test-control-lease.mjs

import assert from "node:assert/strict";
import { setTimeout as sleep } from "node:timers/promises";
import {
  LEASE_TTL_MS,
  getControlStore,
} from "../src/lib/control/store.ts";

const LAB = "test-lab";
const ana = { id: "u-ana", name: "Ana" };
const beto = { id: "u-beto", name: "Beto" };
const caro = { id: "u-caro", name: "Caro" };

let failures = 0;

async function check(name, fn) {
  try {
    await fn();
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`  FAIL ${name}\n       ${error.message}`);
  }
}

const store = await getControlStore();
console.log(`control store backend: ${store.backend}\n`);

// Each scenario uses its own lab id so state never leaks between them.
const lab = (suffix) => `${LAB}-${suffix}`;

console.log("one active controller");

await check("the first to ask gets control", async () => {
  const id = lab("first");
  const result = await store.take(id, ana);
  assert.equal(result.granted, true);
  const state = await store.state(id);
  assert.equal(state.holder?.id, ana.id);
});

await check("a second operator is queued, not granted", async () => {
  const id = lab("queue");
  await store.take(id, ana);
  const result = await store.take(id, beto);
  assert.equal(result.granted, false);
  assert.ok(result.position >= 1);

  const state = await store.state(id);
  assert.equal(state.holder?.id, ana.id, "holder changed under a second taker");
  assert.deepEqual(
    state.queue.map((u) => u.id),
    [beto.id],
  );
});

await check("everyone can watch while one drives", async () => {
  const id = lab("viewers");
  await store.take(id, ana);
  for (const user of [ana, beto, caro]) await store.joinPresence(id, user);

  const state = await store.state(id);
  assert.equal(state.presence.length, 3, "spectators were not all registered");
  assert.equal(state.holder?.id, ana.id);
});

console.log("\nthe lease outlives nothing");

await check("a holder that stops heartbeating loses control", async () => {
  const id = lab("crash");
  await store.take(id, ana);
  assert.equal((await store.state(id)).holder?.id, ana.id);

  // Simulates the browser dying: no release, no heartbeat, just silence.
  await sleep(LEASE_TTL_MS + 400);

  const state = await store.state(id);
  assert.equal(state.holder, null, "a crashed controller kept the hardware");
});

await check("the next in line inherits it on their own heartbeat", async () => {
  const id = lab("inherit");
  await store.take(id, ana);
  await store.take(id, beto); // queued behind Ana

  await sleep(LEASE_TTL_MS + 400); // Ana's browser dies

  const promoted = await store.heartbeat(id, beto);
  assert.equal(promoted.granted, true, "the queued operator was not promoted");
  assert.equal((await store.state(id)).holder?.id, beto.id);
});

await check("heartbeats hold the lease indefinitely", async () => {
  const id = lab("heartbeat");
  await store.take(id, ana);
  for (let i = 0; i < 3; i++) {
    await sleep(LEASE_TTL_MS / 2);
    const beat = await store.heartbeat(id, ana);
    assert.equal(beat.granted, true, `lost the lease on beat ${i + 1}`);
  }
  assert.equal((await store.state(id)).holder?.id, ana.id);
});

await check("releasing hands over immediately", async () => {
  const id = lab("release");
  await store.take(id, ana);
  await store.take(id, beto);
  await store.release(id, ana.id);

  assert.equal((await store.state(id)).holder, null);
  const beat = await store.heartbeat(id, beto);
  assert.equal(beat.granted, true);
});

console.log("\nadmin override and emergency stop");

await check("force takes the lease from a live holder", async () => {
  const id = lab("force");
  await store.take(id, ana);
  const forced = await store.force(id, beto);

  assert.equal(forced.granted, true);
  assert.equal((await store.state(id)).holder?.id, beto.id);

  // The displaced holder must not silently get it back on their next beat.
  const anaBeat = await store.heartbeat(id, ana);
  assert.equal(anaBeat.granted, false, "the displaced holder reclaimed control");
});

await check("e-stop is recorded for everyone, from a non-holder", async () => {
  const id = lab("estop");
  await store.take(id, ana);

  // Beto holds nothing at all — that is the point.
  await store.estop(id, beto);

  const state = await store.state(id);
  assert.ok(state.estopAt, "the stop was not recorded");
  assert.equal(state.estopBy, beto.name);
  // Stopping does not steal control; it stops the hardware.
  assert.equal(state.holder?.id, ana.id);
});

console.log("\nlabs are independent of each other");

await check("two labs each have their own controller at the same time", async () => {
  // The whole platform runs many labs at once. A lease is per lab, so an
  // operator driving the arm must not block anyone driving a different rig.
  const dobot = lab("dobot-cr3");
  const tclab = lab("tclab");

  assert.equal((await store.take(dobot, ana)).granted, true);
  assert.equal(
    (await store.take(tclab, beto)).granted,
    true,
    "a lease on one lab blocked a lease on another",
  );

  assert.equal((await store.state(dobot)).holder?.id, ana.id);
  assert.equal((await store.state(tclab)).holder?.id, beto.id);

  // And releasing one leaves the other untouched.
  await store.release(dobot, ana.id);
  assert.equal((await store.state(dobot)).holder, null);
  assert.equal((await store.state(tclab)).holder?.id, beto.id);
});

await check("an e-stop on one lab does not stop another", async () => {
  const one = lab("estop-a");
  const two = lab("estop-b");
  await store.take(one, ana);
  await store.take(two, beto);
  await store.estop(one, ana);

  assert.ok((await store.state(one)).estopAt);
  assert.equal((await store.state(two)).estopAt, null, "e-stop crossed labs");
});

console.log("\npassing control between operators");

await check("the holder can hand control over, and then cannot drive", async () => {
  const id = lab("handover");
  await store.take(id, ana);

  assert.equal(await store.requestHandover(id, beto), true);
  assert.deepEqual(
    (await store.state(id)).handoverRequests.map((u) => u.id),
    [beto.id],
    "the holder was not told who is asking",
  );

  assert.equal(await store.acceptHandover(id, ana.id, beto.id), true);

  const after = await store.state(id);
  assert.equal(after.holder?.id, beto.id, "control did not move");
  assert.deepEqual(after.handoverRequests, [], "the request was left pending");

  // The point of the whole exercise: the previous holder is no longer able to
  // drive, and does not silently get it back on their next heartbeat.
  const anaBeat = await store.heartbeat(id, ana);
  assert.equal(anaBeat.granted, false, "the previous holder kept control");
  assert.equal((await store.state(id)).holder?.id, beto.id);
});

await check("declining leaves the holder in place", async () => {
  const id = lab("decline");
  await store.take(id, ana);
  await store.requestHandover(id, beto);

  assert.equal(await store.declineHandover(id, ana.id, beto.id), true);
  const after = await store.state(id);
  assert.equal(after.holder?.id, ana.id);
  assert.deepEqual(after.handoverRequests, []);
});

await check("only the holder can accept, and only a real request", async () => {
  const id = lab("handover-auth");
  await store.take(id, ana);
  await store.requestHandover(id, beto);

  // Caro is not the holder: she cannot give Ana's control away.
  assert.equal(await store.acceptHandover(id, caro.id, beto.id), false);
  assert.equal((await store.state(id)).holder?.id, ana.id);

  // And the holder cannot hand control to somebody who never asked.
  assert.equal(await store.acceptHandover(id, ana.id, caro.id), false);
  assert.equal((await store.state(id)).holder?.id, ana.id);
});

await check("nobody can request a handover from themselves", async () => {
  const id = lab("self-handover");
  await store.take(id, ana);
  assert.equal(await store.requestHandover(id, ana), false);
  assert.deepEqual((await store.state(id)).handoverRequests, []);
});

console.log("\nspectator fan-out");

await check("every mutation reaches a subscriber", async () => {
  const id = lab("subscribe");
  const seen = [];
  const unsubscribe = await store.subscribe(id, (state) =>
    seen.push(state.holder?.id ?? null),
  );

  await store.take(id, ana);
  await store.release(id, ana.id);
  await sleep(150);
  unsubscribe();

  assert.ok(seen.includes(ana.id), "a spectator never saw the takeover");
  assert.ok(
    seen.lastIndexOf(null) > seen.indexOf(ana.id),
    "a spectator never saw the release",
  );
});

// ── The store has to be able to speak for the whole lab ────────────────────
//
// Every check above runs inside one process, which is exactly the assumption
// that broke in production: the in-memory store was serving a deployment made
// of many isolates, so each of them granted the lease to a different person
// and both were minted a valid token. No single-process test can catch that.
// What it can do is pin the flag the mint keys off, so a store that only
// speaks for itself can never quietly start handing out hardware credentials.

console.log("\nspeaking for the whole lab");

await check("a store declares whether it is shared", async () => {
  assert.equal(
    typeof store.shared,
    "boolean",
    "every backend must say whether all instances see the same lease",
  );
  const expected = { memory: false, redis: true, "durable-object": true };
  assert.equal(
    store.shared,
    expected[store.backend],
    `the ${store.backend} backend reports the wrong sharing`,
  );
});

console.log(
  failures === 0
    ? "\nall control-lease checks passed"
    : `\n${failures} control-lease check(s) FAILED`,
);
process.exit(failures === 0 ? 0 : 1);
