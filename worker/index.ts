// The Worker entry point.
//
// OpenNext generates `.open-next/worker.js`, which serves the Next.js app and
// exports the Durable Objects it uses for its own caching. Cloudflare needs
// every Durable Object class exported from the *entry* module, so this file
// re-exports that worker unchanged and adds ours to it. `wrangler.jsonc`
// points `main` here rather than at the generated file.
//
// It is not typechecked (see tsconfig `exclude`): `.open-next/worker.js` is a
// build artefact and does not exist until `opennextjs-cloudflare build` has
// run. Everything it pulls in from `src/` is.

// @ts-expect-error — generated at build time by opennextjs-cloudflare.
export { default } from "../.open-next/worker.js";
// @ts-expect-error — same; re-exports OpenNext's own Durable Objects.
export * from "../.open-next/worker.js";

export { ControlLeaseDO } from "../src/lib/control/lease-do";
