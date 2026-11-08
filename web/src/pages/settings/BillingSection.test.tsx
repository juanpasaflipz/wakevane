import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/routing", () => ({ useSearchParams: () => [new URLSearchParams()] }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), message: vi.fn(), error: vi.fn() } }));

import { authenticatedFetch } from "@/lib/identity";
import { BillingSection } from "./BillingSection";

const BILLING_UNSUBSCRIBED = {
  enabled: true,
  plans: [{ key: "pro", included_credit_usd: 20, max_concurrent_roots: 1, min_seats: 1 }],
  markup_percent: 20,
  default_overage_cap_usd: 50,
  grace_days: 7,
  plan: null,
  seat_status: "none",
  credit: null,
};

function mockRoutes(trial: unknown, trialStatus = 200) {
  vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
    const path = String(url);
    if (path.endsWith("/v1/billing")) {
      return new Response(JSON.stringify(BILLING_UNSUBSCRIBED), { status: 200 });
    }
    if (path.endsWith("/v1/funded-trial")) {
      return new Response(trial === null ? "" : JSON.stringify(trial), { status: trialStatus });
    }
    throw new Error(`unexpected fetch ${path}`);
  });
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("BillingSection trial credit", () => {
  it("shows a welcome trial grant to an unsubscribed member", async () => {
    mockRoutes({
      enabled: true,
      status: "active",
      funding: "trial",
      capabilities: ["coding", "compute", "inference"],
      allowance_usd: 3,
      spent_usd: 0.5,
      reserved_usd: 0,
      remaining_usd: 2.5,
      expires_at: Math.floor(new Date("2026-11-15T00:00:00Z").getTime() / 1000),
    });
    render(<BillingSection />);
    const panel = await screen.findByTestId("billing-trial-credit");
    expect(panel).toHaveTextContent("$2.50 of $3.00 remaining");
    expect(panel).toHaveTextContent("expires");
    // Subscribing is still offered alongside the credit.
    expect(screen.getByText(/Subscribe to pro/)).toBeInTheDocument();
  });

  it("hides the panel when no trial grant is active", async () => {
    mockRoutes({ enabled: false, capabilities: [], remaining_usd: 0 });
    render(<BillingSection />);
    await screen.findByTestId("billing-section");
    expect(screen.queryByTestId("billing-trial-credit")).toBeNull();
  });

  it("hides the panel and keeps billing usable when the trial route is absent", async () => {
    mockRoutes(null, 404);
    render(<BillingSection />);
    await screen.findByTestId("billing-section");
    expect(screen.queryByTestId("billing-trial-credit")).toBeNull();
    expect(screen.getByText(/Subscribe to pro/)).toBeInTheDocument();
  });
});
