# Running WakeVane as a public service

What a deployment needs before strangers can sign up and pay. Seat billing is
described in [SEAT_BILLING.md](SEAT_BILLING.md), the usage ledger in
[FUNDED_TRIAL.md](FUNDED_TRIAL.md) and the Railway setup in
[../deploy/railway/README.md](../deploy/railway/README.md). This page covers
what sits around them: account safety, browser hardening and the checks that
only the operator can run.

## Account safety

These apply to the built-in `accounts` provider and need no feature flag.

### Attempt limits

The unauthenticated `/auth` routes are bounded per client address and per
account. A refused request answers `429` with a `Retry-After` header.

| Route | Limit |
| --- | --- |
| `POST /auth/login` | 5 wrong passwords per address and account in 15 minutes; 30 per address; 20 per account from any address. A successful sign-in clears that caller's count for the account. |
| `POST /auth/register` (open signup) | 10 accounts per address per hour. Invite redemption is not limited. |
| `POST /auth/password/forgot` | 5 requests per address per hour; 3 messages per account per hour |
| `POST /auth/password/reset` | 10 attempts per address per hour |

The client address is `CF-Connecting-IP`, then the first `X-Forwarded-For`
entry, then the socket peer. Counters are held in the server process, so each
replica enforces its own budget; add an edge rule when running several.
A client that reaches the origin directly can forge those headers, which is
why the per-account login limit does not depend on the address. Keep the
origin reachable only through the proxy where the platform allows it.

`OMNIGENT_ACCOUNTS_RATE_LIMIT=0` turns the limits off, for load tests.

### Password reset

With a mailer configured (`OMNIGENT_SMTP_*`), `/v1/info` reports
`password_reset: true` and the sign-in page shows **Forgot password?**.

- `POST /auth/password/forgot {"identifier": "<username or email>"}` mails a
  link to the address on the account. It answers `{"sent": true}` whether or
  not an account matched.
- `POST /auth/password/reset {"token": "...", "new_password": "..."}` sets
  the password and signs the account out everywhere.

Links expire after one hour. A link is tied to the account's current sign-in
generation, so using it (or changing the password any other way) retires it
and every other outstanding link; nothing is stored server-side. Accounts
created by invite have no address on file and still rely on the admin reset in
Settings › Members.

### Policy links

Set `OMNIGENT_TERMS_URL` and `OMNIGENT_PRIVACY_URL` to `https://` pages and
the sign-up form shows "By creating an account you agree to…". The server
does not host these pages.

## Browser hardening

Every response carries `X-Content-Type-Options: nosniff` and
`Referrer-Policy: strict-origin-when-cross-origin`; HTTPS responses add
`Strict-Transport-Security: max-age=31536000`. Routes that set their own
values keep them.

Framing is open by default because some deployments embed the web UI in
another origin. On a public service set

```text
OMNIGENT_FRAME_ANCESTORS='self'
```

to send `Content-Security-Policy: frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN`, which stops other sites from wrapping the app
in a frame to capture clicks. The value is any CSP source list, for example
`'self' https://ide.example.com`.

## Before opening signup

Settings the operator has to supply:

- [ ] `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` set before first boot, and
      `OMNIGENT_CREDENTIAL_ENC_KEY` backed up outside the platform.
- [ ] `OMNIGENT_SMTP_*` pointing at a sender with SPF and DKIM for the
      `From` domain. Verification and reset mail both depend on it.
- [ ] `OMNIGENT_FRAME_ANCESTORS`, `OMNIGENT_TERMS_URL`, `OMNIGENT_PRIVACY_URL`.
- [ ] Database backups and a tested restore. The ledger and billing mirror
      live in Postgres.
- [ ] An uptime check on `GET /health` and an alert on 5xx rate.
- [ ] A spending limit on the sandbox and model provider accounts that fund
      usage, independent of the in-app caps.

Checks to run by hand, in order:

1. Sign up with a real mailbox, open the verification link, and confirm the
   welcome credit appears in Settings › Billing.
2. Sign out, use **Forgot password?**, and confirm the old session and the
   old password stop working.
3. Enter a wrong password six times and confirm the sixth answers 429.
4. `curl -sI https://<app>/ | grep -i -E "strict-transport|frame|nosniff"`.
5. The Stripe test-mode list at the end of [SEAT_BILLING.md](SEAT_BILLING.md):
   a Checkout with a test card, the five webhooks, one run funded by period
   credit, one pushed into overage, and a portal cancellation. Switch to live
   keys only after all five pass.
