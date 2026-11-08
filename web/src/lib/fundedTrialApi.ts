/**
 * Client for ``GET /v1/funded-trial``: the caller's service-funded allowance
 * (a welcome or operator trial grant, or a paid period's credit).
 */
import { authenticatedFetch } from "./identity";
import { withBasePath } from "./basePath";

/** Shape of ``GET /v1/funded-trial`` for the signed-in caller. */
export interface FundedTrialSummary {
  enabled: boolean;
  /** "active", or why the grant is unusable (expired, exhausted, revoked). */
  status?: string;
  /** Grant kind funding the caller: "trial", "period_credit" or "overage". */
  funding?: string;
  capabilities: string[];
  allowance_usd?: number;
  spent_usd?: number;
  reserved_usd?: number;
  remaining_usd: number;
  expires_at?: number | null;
  models?: string[];
  /** Present (with no grant) when the deployment offers a welcome credit. */
  welcome_grant?: { allowance_usd: number; expires_in_days: number };
}

/** Resolves to ``null`` when the route is absent (funded trial feature off). */
export async function getFundedTrialSummary(): Promise<FundedTrialSummary | null> {
  const res = await authenticatedFetch(withBasePath("/v1/funded-trial"));
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`funded trial summary failed: ${res.status}`);
  return (await res.json()) as FundedTrialSummary;
}

/** True when the caller holds a usable trial grant (welcome or operator-issued). */
export function hasTrialCredit(summary: FundedTrialSummary | null): boolean {
  return summary !== null && summary.enabled && summary.funding === "trial";
}
