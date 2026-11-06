/**
 * Client for the seat billing routes (``/v1/billing``), mounted only when the
 * server's ``billing`` release feature is on.
 */

import { authenticatedFetch } from "./identity";
import { withBasePath } from "./basePath";
import { apiErrorFromResponse } from "./sessionsApi";

export type SeatStatus = "none" | "active" | "past_due" | "canceled";

export interface BillingPlan {
  key: string;
  included_credit_usd: number;
  max_concurrent_roots: number;
  min_seats: number;
}

export interface BillingFigures {
  allowance_usd: number;
  spent_usd: number;
  reserved_usd: number;
  remaining_usd: number;
  active: boolean;
}

export interface BillingCharge {
  id: string;
  kind: string;
  status: string;
  reserved_usd: number;
  settled_usd: number | null;
  created_at: number;
}

/** Shape of ``GET /v1/billing`` for the signed-in caller. */
export interface BillingSummary {
  enabled: boolean;
  plans: BillingPlan[];
  markup_percent: number;
  default_overage_cap_usd: number;
  grace_days: number;
  plan: string | null;
  seat_status: SeatStatus;
  seats?: number;
  period_start?: number | null;
  period_end?: number | null;
  in_period?: boolean;
  credit?: BillingFigures | null;
  overage?: Partial<BillingFigures> & { enabled: boolean; cap_usd: number };
  portal_available?: boolean;
  recent_charges?: BillingCharge[];
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

export async function getBillingSummary(): Promise<BillingSummary> {
  const res = await authenticatedFetch(withBasePath("/v1/billing"));
  return readJson<BillingSummary>(res);
}

/** Start Stripe Checkout; resolves with the hosted page URL to navigate to. */
export async function startCheckout(plan: string, seats?: number): Promise<string> {
  const res = await authenticatedFetch(withBasePath("/v1/billing/checkout"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(seats === undefined ? { plan } : { plan, seats }),
  });
  return (await readJson<{ url: string }>(res)).url;
}

/** Open the Stripe customer portal (cards, cancel, seat count). */
export async function openPortal(): Promise<string> {
  const res = await authenticatedFetch(withBasePath("/v1/billing/portal"), { method: "POST" });
  return (await readJson<{ url: string }>(res)).url;
}

export async function setOverage(enabled: boolean, capUsd: number | null): Promise<BillingSummary> {
  const res = await authenticatedFetch(withBasePath("/v1/billing/overage"), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ enabled, cap_usd: capUsd }),
  });
  return readJson<BillingSummary>(res);
}

/** Human label for a seat status. */
export function seatStatusLabel(status: SeatStatus): string {
  switch (status) {
    case "active":
      return "Active";
    case "past_due":
      return "Payment past due";
    case "canceled":
      return "Canceled";
    default:
      return "No seat";
  }
}

/** True when the server refused work because the account must pay first. */
export function isPaymentRequired(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status?: number }).status === 402
  );
}
