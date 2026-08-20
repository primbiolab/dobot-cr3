import { NextResponse } from "next/server";
import { getControlStore } from "@/lib/control/store";
import { requireControlUser, requireOperate } from "@/lib/control/auth";
import { getLabSlug } from "@/lib/lab";
import { mintLeaseToken } from "@/lib/control/token";

// Request the control lease. Grants immediately when free (and unqueued-for),
// otherwise joins the wait queue; waiting clients are promoted automatically
// by their heartbeats when the lease frees up.
export async function POST() {
  const auth = await requireControlUser();
  if (auth instanceof NextResponse) return auth;
  const denied = requireOperate(auth);
  if (denied) return denied;

  const store = await getControlStore();
  const result = await store.take(getLabSlug(), auth.user);
  // The token travels with the grant: the client presents it to the edge
  // gatekeeper, which has no other way to know who holds the lease.
  const leaseToken = result.granted ? await mintLeaseToken(auth.user, store) : null;
  return NextResponse.json({ ...result, leaseToken });
}
