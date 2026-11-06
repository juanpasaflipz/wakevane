import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getBillingSummary,
  isPaymentRequired,
  seatStatusLabel,
  setOverage,
  startCheckout,
} from "./billingApi";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("billingApi", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reads the caller's summary and posts checkout and overage changes", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(200, { enabled: true, plans: [], seat_status: "none" }))
      .mockResolvedValueOnce(jsonResponse(200, { url: "https://checkout.stripe.test/cs" }))
      .mockResolvedValueOnce(
        jsonResponse(200, { enabled: true, plans: [], seat_status: "active" }),
      );

    const summary = await getBillingSummary();
    expect(summary.seat_status).toBe("none");
    expect(await startCheckout("pro", 2)).toBe("https://checkout.stripe.test/cs");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      plan: "pro",
      seats: 2,
    });
    const updated = await setOverage(true, 25);
    expect(updated.seat_status).toBe("active");
    expect(fetchMock.mock.calls[2][1]?.method).toBe("PUT");
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({
      enabled: true,
      cap_usd: 25,
    });
  });

  it("surfaces server denials as errors that keep the HTTP status", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse(402, { error: { code: "payment_required", message: "Subscribe first" } }),
    );
    const failure = await startCheckout("pro").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toBe("Subscribe first");
    expect(isPaymentRequired(failure)).toBe(true);
    expect(isPaymentRequired(new Error("x"))).toBe(false);
  });

  it("labels seat statuses for display", () => {
    expect(seatStatusLabel("active")).toBe("Active");
    expect(seatStatusLabel("past_due")).toBe("Payment past due");
    expect(seatStatusLabel("canceled")).toBe("Canceled");
    expect(seatStatusLabel("none")).toBe("No seat");
  });
});
