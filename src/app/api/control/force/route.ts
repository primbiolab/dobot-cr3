import { NextResponse } from "next/server";
import { getControlStore } from "@/lib/control/store";
import { requireControlUser } from "@/lib/control/auth";
import { mintLeaseToken } from "@/lib/control/token";
import { getLabContext, getLabSlug } from "@/lib/lab";

// Seize the control lease from whoever holds it. Admins and the project owner
// only — an operator has to queue like everybody else.
//
// This exists because the lease is held by a browser, and browsers get left
// open: an arm parked mid-task by someone who went to lunch would otherwise
// block the lab for as long as their tab keeps heartbeating. Taking control is
// not silent — the change goes out on the state stream to every viewer, and it
// lands in the activity feed at the edge as soon as the new holder commands
// anything.
export async function POST() {
  const auth = await requireControlUser();
  if (auth instanceof NextResponse) return auth;

  const ctx = await getLabContext();
  // In demo mode (no Supabase) canAdmin is false but there is nobody to
  // displace either, so allow it rather than making the demo dead-ended.
  if (ctx.configured && !ctx.canAdmin) {
    return NextResponse.json(
      { error: "admin role required to force control" },
      { status: 403 },
    );
  }

  const store = await getControlStore();
  const result = await store.force(getLabSlug(), auth.user);
  const leaseToken = result.granted ? await mintLeaseToken(auth.user, store) : null;
  return NextResponse.json({ ...result, leaseToken });
}
