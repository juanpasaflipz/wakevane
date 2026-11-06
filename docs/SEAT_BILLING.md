# WakeVane seat billing

Status: implemented behind the `billing` release feature (default off);
**not yet activated** in any environment. Activation needs a Stripe account,
the objects created by `scripts/billing/bootstrap_stripe.py`, and the
secrets below. With the feature off, nothing here runs: no Stripe import, no
routes, no change to launch admission.

Related: [FUNDED_TRIAL.md](FUNDED_TRIAL.md) (the shared ledger and metered
gateway this reuses), [BROWSER_ONLY_DESIGN_REVIEW.md](BROWSER_ONLY_DESIGN_REVIEW.md),
[../designs/FEATURE_FLAGS.md](../designs/FEATURE_FLAGS.md).

## Product shape

| Tier | Price | Includes |
| --- | --- | --- |
| Self-host | free | the repository; billing feature off; BYOK |
| Pro | per seat per month (Stripe price) | included usage credit per period, then metered overage up to a user-set cap |
| Team | per seat per month, seat minimum | same wallet, more concurrent roots (plan key `team`) |

Usage is priced from the same `OMNIGENT_FUNDED_TRIAL_CONFIG` table the trial
uses (model tokens, cloud compute per hour) plus `markup_percent`. Overage is
off until the user turns it on and chooses a cap; included credit never rolls
over. Prices and credits are configuration, not code.

## How it works

1. **One ledger.** `FundedTrialStore` grants now carry a `kind`:
   `trial` (operator grant, unchanged), `period_credit` (a paid period's
   included usage) and `overage` (metered spend). Subscription grants are
   keyed by `billing_period_end`, so each paid period starts a fresh ledger
   while the previous period's rows stay for audit. Grants carry
   `markup_percent`; the store applies it to every hold and settlement.
2. **Funding order.** `FundedTrialPolicy.reserve` tries the account's grants
   in order: trial, then period credit, then overage. A hold is taken from
   exactly one grant; only an exhausted allowance moves to the next. The
   concurrency limit counts root workflows across all of the account's grants.
3. **Stripe owns money.** `omnigent/server/billing.py` mirrors the
   subscription (customer, subscription, status, seats, current period) into
   `billing_accounts`, creates or revokes grants from webhooks, and reports
   settled overage charges as Stripe meter events keyed by charge id
   (`omnigent/server/billing_reporter.py`, every 60 s). Webhook event ids are
   stored in `billing_events` before processing, so redelivery is a no-op.
4. **Denials.** A launch or gateway request the account cannot fund answers
   **402** with reason `subscription_required`, `past_due` or
   `allowance_exhausted`; the web UI points at Settings › Billing.
5. **Gateway tokens** stay bound to the grant they were minted on. A trial
   token dies with its grant's generation as before; a subscription token
   survives a period rollover and dies when the subscription's grants are
   revoked (cancellation).

Lifecycle events handled: `checkout.session.completed`, `invoice.paid`,
`invoice.payment_failed` (seat `past_due`, overage revoked, period credit kept
until period end), `customer.subscription.updated`,
`customer.subscription.deleted` (seat `canceled`, all subscription grants
revoked).

## Routes (all behind the flag, under `/v1`)

- `GET /billing` — the caller's plan, seat status, period, credit and overage
  figures, recent charges.
- `POST /billing/checkout` `{"plan"?: "pro", "seats"?: 1}` — Stripe Checkout URL.
- `POST /billing/portal` — Stripe customer portal URL (card, cancel, seats).
- `PUT /billing/overage` `{"enabled": true, "cap_usd": 50}`.
- `POST /billing/webhooks/stripe` — unauthenticated, signature-verified.
- `GET /admin/billing/accounts/{user_id}` — server admins.

Web: Settings › Billing (`web/src/pages/settings/BillingSection.tsx`) appears
for signed-in users when the server advertises `features.billing`.

## Configuration

```text
OMNIGENT_FEATURES=...,funded_trial,billing
OMNIGENT_BILLING_STRIPE_SECRET_KEY        server secret (sk_live_… / sk_test_…)
OMNIGENT_BILLING_STRIPE_WEBHOOK_SECRET    server secret (whsec_…)
OMNIGENT_BILLING_CONFIG                   JSON, see below
OMNIGENT_ACCOUNTS_BASE_URL                already set; used for return links
```

```json
{
  "plans": {
    "pro":  {"price_seat": "price_...", "included_credit_usd": 20, "max_concurrent_roots": 1},
    "team": {"price_seat": "price_...", "included_credit_usd": 20, "max_concurrent_roots": 3, "min_seats": 3}
  },
  "price_usage": "price_...",
  "meter_event_name": "wakevane_overage_cents",
  "markup_percent": 20,
  "default_overage_cap_usd": 50,
  "grace_days": 7
}
```

Startup fails when `billing` is on without `funded_trial`, a database,
multi-user auth, a priced plan, `price_usage` or the Stripe secret key. The
Docker image needs the `billing` extra (`OMNIGENT_EXTRAS=modal,billing`); the
staging image workflow already passes it.

`scripts/billing/bootstrap_stripe.py` creates the product, seat prices, the
`wakevane_overage_cents` meter, the metered price and a customer portal
configuration in the Stripe account of the `STRIPE_SECRET_KEY` it is given,
idempotently by lookup key, and prints the `OMNIGENT_BILLING_CONFIG` to set.
Register the webhook endpoint `https://<app>/v1/billing/webhooks/stripe` for
the five events above and copy its signing secret.

## Public signup and browser-only mode

- `OMNIGENT_ACCOUNTS_OPEN_SIGNUP=1` lets `POST /auth/register` create a
  non-admin account without an invite; `/v1/info` reports `open_signup` and
  the register page offers self-serve signup. Paying is the gate: a fresh
  account can sign in and browse but cannot fund a managed launch.
- `OMNIGENT_FEATURES=...,web_only` limits non-admin accounts to
  `host_type: managed` sessions (local hosts and runners answer 403) and
  makes the sandbox the default pick in New Chat.

## Verification before enabling in production

Covered by tests (`tests/server/test_billing.py`,
`tests/stores/test_funded_trial_store.py`, `tests/server/test_accounts.py`):

1. Subscribed accounts fund holds; canceled and past-due accounts are refused
   with 402 reasons, through the policy every launch path and the gateway use.
2. Period credit resets on `invoice.paid` without touching the previous
   period's ledger and never rolls over.
3. Overage stops at the cap; a cap change applies to new holds only.
4. Reporting is idempotent: one meter event per settled charge under replay.
5. Trial accounts are unchanged with the flag on; with it off no billing code
   is imported.
6. Webhook redelivery, bad signatures and processing failures.

Still to do live, in Stripe test mode against staging: a real Checkout with a
test card, the five webhooks delivered by Stripe (`stripe trigger` or the
dashboard), one managed review funded by period credit, one pushed into
overage with the meter event visible in Stripe, and a portal cancellation.
