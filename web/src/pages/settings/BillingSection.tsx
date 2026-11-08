/**
 * Settings › Billing: the caller's seat, this period's included credit, the
 * metered overage opt-in and the Stripe-hosted Checkout / portal hand-offs.
 * Mounted only when the server advertises the ``billing`` feature.
 */

import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "@/lib/routing";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SettingsLabel } from "@/components/SettingsLabel";
import {
  getBillingSummary,
  openPortal,
  seatStatusLabel,
  setOverage,
  startCheckout,
  type BillingSummary,
} from "@/lib/billingApi";
import {
  getFundedTrialSummary,
  hasTrialCredit,
  type FundedTrialSummary,
} from "@/lib/fundedTrialApi";
import { getMe, resendVerification, type CurrentAccount } from "@/lib/accountsApi";

function usd(value: number | undefined | null): string {
  return `$${(value ?? 0).toFixed(2)}`;
}

function formatDay(epochSeconds: number | null | undefined): string | null {
  if (!epochSeconds) return null;
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function periodEnd(summary: BillingSummary): string | null {
  return formatDay(summary.period_end);
}

export function BillingSection() {
  const [params] = useSearchParams();
  const [summary, setSummary] = useState<BillingSummary | "loading" | "error">("loading");
  // Welcome / operator trial credit lives in the funded-trial ledger, not the
  // seat; a failed lookup only hides the panel and never blocks billing.
  const [trial, setTrial] = useState<FundedTrialSummary | null>(null);
  const [account, setAccount] = useState<CurrentAccount | null>(null);
  const [resending, setResending] = useState(false);
  const [busy, setBusy] = useState(false);
  const [capDraft, setCapDraft] = useState<string>("");

  const reload = useCallback(async () => {
    const [billing, funded, me] = await Promise.allSettled([
      getBillingSummary(),
      getFundedTrialSummary(),
      getMe(),
    ]);
    setTrial(funded.status === "fulfilled" ? funded.value : null);
    setAccount(me.status === "fulfilled" ? me.value : null);
    if (billing.status === "fulfilled") {
      setSummary(billing.value);
      setCapDraft(String(billing.value.overage?.cap_usd ?? billing.value.default_overage_cap_usd));
    } else {
      setSummary("error");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const checkout = params.get("checkout");
    if (checkout === "success") toast.success("Thanks! Your seat is being activated.");
    if (checkout === "canceled") toast.message("Checkout canceled. Nothing was charged.");
    const verified = params.get("verified");
    if (verified === "1" && params.get("credit") === "0") {
      toast.message("Email confirmed. This mailbox already received a trial credit.");
    } else if (verified === "1") {
      toast.success("Email confirmed. Your trial credit is ready.");
    } else if (verified === "expired") {
      toast.error("That confirmation link has expired or was already used. Send a new one below.");
    }
    if (params.get("verify") === "sent") {
      toast.message("Check your inbox for the confirmation link.");
    }
  }, [params]);

  const onResend = useCallback(async () => {
    setResending(true);
    const result = await resendVerification();
    setResending(false);
    if (result.ok) toast.success("Sent. Check your inbox.");
    else toast.error(result.error);
  }, []);

  const onCheckout = useCallback(async (plan: string) => {
    setBusy(true);
    try {
      window.location.href = await startCheckout(plan);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not start checkout.");
      setBusy(false);
    }
  }, []);

  const onPortal = useCallback(async () => {
    setBusy(true);
    try {
      window.location.href = await openPortal();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not open the billing portal.");
      setBusy(false);
    }
  }, []);

  const onOverage = useCallback(
    async (enabled: boolean) => {
      const cap = Number(capDraft);
      if (enabled && (!Number.isFinite(cap) || cap < 0)) {
        toast.error("Enter a non-negative overage cap.");
        return;
      }
      setBusy(true);
      try {
        const next = await setOverage(enabled, enabled ? cap : null);
        setSummary(next);
        setCapDraft(String(next.overage?.cap_usd ?? cap));
        toast.success(enabled ? "Overage enabled." : "Overage disabled.");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Could not update overage.");
      } finally {
        setBusy(false);
      }
    },
    [capDraft],
  );

  if (summary === "loading") {
    return <p className="text-ui text-muted-foreground">Loading billing…</p>;
  }
  if (summary === "error") {
    return (
      <div role="alert" className="text-ui text-destructive">
        Billing is unavailable right now.{" "}
        <Button variant="link" className="px-1" onClick={() => void reload()}>
          Retry
        </Button>
      </div>
    );
  }

  const subscribed = summary.seat_status === "active" || summary.seat_status === "past_due";
  const credit = summary.credit ?? null;
  const overage = summary.overage;
  const ends = periodEnd(summary);
  const trialCredit = hasTrialCredit(trial) ? trial : null;
  const trialEnds = formatDay(trialCredit?.expires_at);
  const pendingCredit =
    !trialCredit && !!account?.email && !account.email_verified_at ? trial?.welcome_grant : null;

  return (
    <div className="space-y-8" data-testid="billing-section">
      {pendingCredit && (
        <section className="space-y-3" data-testid="billing-verify-email">
          <SettingsLabel
            label="Confirm your email"
            description={`A link went to ${account?.email}. Open it to unlock ${usd(pendingCredit.allowance_usd)} of trial usage, good for ${pendingCredit.expires_in_days} days.`}
          />
          <div className="rounded-lg border p-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              Didn't get it? Check spam, then send a new link.
            </p>
            <Button
              variant="outline"
              disabled={resending}
              onClick={() => void onResend()}
              componentId="billing.verify.resend"
            >
              {resending ? "Sending…" : "Resend link"}
            </Button>
          </div>
        </section>
      )}
      {trialCredit && (
        <section className="space-y-3" data-testid="billing-trial-credit">
          <SettingsLabel
            label="Trial credit"
            description="Usage on us. Start a session from the home page; no card needed until it runs out."
          />
          <div className="rounded-lg border p-4 space-y-2">
            <div className="h-2 w-full overflow-hidden rounded bg-muted">
              <div
                className="h-2 bg-primary"
                style={{
                  width: `${Math.min(
                    100,
                    (100 * ((trialCredit.spent_usd ?? 0) + (trialCredit.reserved_usd ?? 0))) /
                      Math.max(trialCredit.allowance_usd ?? 0, 0.01),
                  )}%`,
                }}
              />
            </div>
            <p className="text-sm text-muted-foreground">
              {usd(trialCredit.remaining_usd)} of {usd(trialCredit.allowance_usd)} remaining
              {trialEnds ? ` · expires ${trialEnds}` : ""}
            </p>
          </div>
        </section>
      )}
      <section className="space-y-3">
        <SettingsLabel label="Seat" description="Your subscription and its billing period." />
        <div className="rounded-lg border p-4 space-y-2">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-ui font-medium">
                {summary.plan ? `${summary.plan} · ` : ""}
                {seatStatusLabel(summary.seat_status)}
                {summary.seats && summary.seats > 1 ? ` · ${summary.seats} seats` : ""}
              </p>
              {ends && (
                <p className="text-sm text-muted-foreground">
                  {summary.in_period ? "Current period ends" : "Period ended"} {ends}.
                </p>
              )}
              {summary.seat_status === "past_due" && (
                <p className="text-sm text-destructive">
                  Your last payment failed. Update your card within {summary.grace_days} days to
                  keep your seat; included credit stays usable until the period ends.
                </p>
              )}
            </div>
            {summary.portal_available ? (
              <Button variant="outline" disabled={busy} onClick={() => void onPortal()}>
                Manage billing
              </Button>
            ) : null}
          </div>
          {!subscribed && (
            <div className="flex flex-wrap gap-2 pt-2">
              {summary.plans.map((plan) => (
                <Button
                  key={plan.key}
                  disabled={busy}
                  onClick={() => void onCheckout(plan.key)}
                  componentId={`billing.subscribe.${plan.key}`}
                >
                  Subscribe to {plan.key}
                  {plan.min_seats > 1 ? ` (${plan.min_seats}+ seats)` : ""}
                </Button>
              ))}
            </div>
          )}
          {!subscribed && (
            <p className="text-sm text-muted-foreground">
              Each seat includes {usd(summary.plans[0]?.included_credit_usd)} of model and compute
              usage per month; usage beyond that is metered at list price plus{" "}
              {summary.markup_percent}% only when you enable overage below.
            </p>
          )}
        </div>
      </section>

      {subscribed && (
        <section className="space-y-3">
          <SettingsLabel
            label="Included credit this period"
            description="Model and compute usage covered by your seat; resets each paid period."
          />
          <div className="rounded-lg border p-4 space-y-2">
            {credit ? (
              <>
                <div className="h-2 w-full overflow-hidden rounded bg-muted">
                  <div
                    className="h-2 bg-primary"
                    style={{
                      width: `${Math.min(
                        100,
                        (100 * (credit.spent_usd + credit.reserved_usd)) /
                          Math.max(credit.allowance_usd, 0.01),
                      )}%`,
                    }}
                  />
                </div>
                <p className="text-sm text-muted-foreground">
                  {usd(credit.remaining_usd)} of {usd(credit.allowance_usd)} remaining
                  {credit.reserved_usd > 0 ? ` (${usd(credit.reserved_usd)} on hold)` : ""}.
                </p>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                Credit appears once the first invoice is paid.
              </p>
            )}
          </div>
        </section>
      )}

      {subscribed && overage && (
        <section className="space-y-3">
          <SettingsLabel
            label="Overage"
            description="Optional metered usage once the included credit is spent."
          />
          <div className="rounded-lg border p-4 space-y-3">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-ui font-medium">Keep working past the included credit</p>
                <p className="text-sm text-muted-foreground">
                  Metered usage is billed to your card at the end of the period, never beyond your
                  cap.
                  {overage.spent_usd !== undefined
                    ? ` Spent this period: ${usd(overage.spent_usd)}.`
                    : ""}
                </p>
              </div>
              <Switch
                checked={overage.enabled}
                disabled={busy || summary.seat_status !== "active"}
                onCheckedChange={(checked) => void onOverage(checked)}
                aria-label="Enable overage"
              />
            </div>
            <div className="flex items-center gap-2">
              <label htmlFor="billing-overage-cap" className="text-sm text-muted-foreground">
                Monthly cap (USD)
              </label>
              <Input
                id="billing-overage-cap"
                type="number"
                min={0}
                step={1}
                className="w-28"
                value={capDraft}
                disabled={busy}
                onChange={(event) => setCapDraft(event.target.value)}
              />
              <Button
                variant="outline"
                size="sm"
                disabled={busy || !overage.enabled}
                onClick={() => void onOverage(true)}
              >
                Save cap
              </Button>
            </div>
          </div>
        </section>
      )}

      {summary.recent_charges && summary.recent_charges.length > 0 && (
        <section className="space-y-3">
          <SettingsLabel
            label="Recent usage"
            description="Holds and settled charges on your account."
          />
          <ul className="divide-y rounded-lg border text-sm">
            {summary.recent_charges.map((charge) => (
              <li key={charge.id} className="flex items-center justify-between px-4 py-2">
                <span className="text-muted-foreground">
                  {new Date(charge.created_at * 1000).toLocaleString()} · {charge.kind}
                </span>
                <span>
                  {charge.status === "settled"
                    ? usd(charge.settled_usd)
                    : charge.status === "released"
                      ? "released"
                      : `${usd(charge.reserved_usd)} on hold`}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
