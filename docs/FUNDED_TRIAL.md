# Private funded trial

Status: grants, ledger, metered gateway and launch admission are implemented
and tested locally but **not deployed or enabled**. Cloud token injection,
compute settlement, durable outputs and the design-to-PR flow remain. See
[Implementation status](#implementation-status) and the
[cloud delivery plan](BROWSER_ONLY_DESIGN_REVIEW.md).

## Product decision

BYOK remains the normal beta offering. The owner wants to test service-funded
models and the complete workflow privately before deciding on commercial pricing.
Start with the owner's explicitly identified account; do not grant all admins,
email-domain members, or newly registered users access automatically.

The funded workflow includes repository selection, design and review, approved
implementation, tests, and a pull request. Merge and deployment require separate
explicit approval. Closing the browser must not stop cloud execution.

## Gate contract

All of these conditions must hold before a funded operation can run:

1. The operator has enabled the trial's global kill switch (default off).
2. The authenticated account has an unexpired, unrevoked trial grant, keyed by
   stable server user ID. Client-supplied labels, email strings, session owners,
   model settings, and agent instructions cannot grant eligibility.
3. The grant allows the requested capability: funded inference, cloud execution,
   coding, or PR creation. Repository authorization is checked independently.
4. The model and compute configuration are approved and priced. A usable funding
   ledger has atomically reserved the operation's allowance.

Evaluate the same policy on direct API calls, initial launches, forks, retries,
continuations, scheduled work, child sessions, and model changes. Recheck before
billable gateway requests and cloud renewals. Revocation blocks new spending,
cancels remaining work, and preserves authorized access to saved results.
Already dispatched provider requests may still incur charges.

The UI receives only the current user's effective capabilities and remaining
allowance. It hides unavailable actions and explains denial, but the server is
the authority. Exposing a generic managed host must not provide a route around
funded inference admission. A rejected funded operation must not silently switch
to the user's API key or another payer.

## Funding and credentials

The owner approved a **$100 USD total trial allowance**, covering models, cloud
execution, and storage together. No automatic top-up is authorized. Activation
also requires an explicit expiry; propose 30 days from activation.
Start with one concurrent root workflow per trial account. All descendants,
retries, and new chats debit the same trial ledger, including compute and storage.

Reserve conservative maximum request cost before inference dispatch, using input
bounds and an enforced output-token limit. Bound cloud lifetime and reserve its
cost separately. Settle measured usage idempotently; release unused reservations
only when safe. Unknown pricing or unavailable accounting blocks new spending.
Record any provider billing discrepancy rather than promising an exact invoice
cap. The existing $15/$2 agent thresholds do not implement this ledger.

Service model credentials remain in server-controlled secret storage. Give
workspaces short-lived, scoped gateway credentials; never put reusable provider
keys in agent prompts, browser responses, repository environment files, or logs.
Bind gateway authority to owner, trial, session tree, allowed models, and expiry.

## Design-to-PR separation

Keep the existing read-only reviewers and output-confined coordinator. After the
user approves a specific specification hash and source commit, create a separate
coding session on an isolated branch. Bind that session to the approved handoff.
Permit tests there within resource and network limits. Do not broaden the design
agent's sandbox to implement code.

Authorize repository-scoped GitHub writes only for the coding/PR stage. Store
test evidence and resulting diff with the handoff. Never treat reviewer agreement
as authorization to merge or deploy. A later merge/deploy feature needs its own
server-enforced action authorization and reviewable target revision.

## Implementation and verification order

1. Implement account grants and the common authorization policy, including audit
   events for grants, revocations, denials, and funding changes.
2. Implement the atomic trial ledger and scoped inference gateway, then integrate
   every managed launch and continuation path. A standalone allowlist is not a
   sufficient funding gate.
3. Complete cloud checkout, confinement, durable results, and cancellation from
   the cloud delivery plan. Then add the approved coding-session transition and
   PR flow without changing EGM's design-only contract.
4. Test with an allowed account and a second denied account. Verify direct API,
   child/fork/schedule, replay, concurrent allowance exhaustion, expired grant,
   revoked access, model-switch, and kill-switch behavior. Confirm no rejected
   request creates a billable resource or exposes a credential.
5. Run a synthetic repository from browser launch through reviewed design,
   explicitly approved implementation, tests, and PR creation. Delete its sandbox
   and verify durable outputs. Confirm no merge or deployment occurred.
6. Activate only after selecting the cloud provider, funding allowance, expiry,
   and owner account ID, and recording live verification. Document the actual
   activation settings; names in this plan are not implemented configuration keys.

## Implementation status

### Implemented (local, uncommitted)

| Piece | Location |
| --- | --- |
| Grants, charges, audit events (micro-USD, migration `e4f5a6b7c8d9`) | `omnigent/server/funded_trial_store.py`, `omnigent/db/db_models.py` |
| Atomic reservation, idempotent settle/release, overrun audit, one concurrent root | `FundedTrialStore.reserve` / `settle` / `release` |
| Kill switch: `funded_trial` feature, default off, server-only | `omnigent/server/feature_flags.py` |
| Policy, operator pricing, scoped gateway tokens (HMAC, generation-bound) | `omnigent/server/funded_trial.py` |
| Metered Anthropic Messages gateway (stream and non-stream) | `omnigent/server/routes/funded_trial.py` |
| Operator grant/revoke/read and caller summary routes | same |
| `sandbox.funding: trial` targets; admission on create, launch, relaunch, wake | `managed_hosts.py`, `_sessions/orchestration.py` |

Gateway behavior: holds `request_bytes × dearest input rate + max_tokens × output
rate` before dispatch; rejects unapproved models, `max_tokens` above the limit,
server-executed tools (`web_search`, `web_fetch`, `code_execution`, MCP) and
`mcp_servers`/`container`; releases holds on provider errors; settles reported
usage; keeps the whole hold when a stream ends without `message_stop`. The
service key is attached server-side only.

### Routes

- `GET /v1/funded-trial` — the caller's capabilities and remaining allowance.
- `PUT|GET|DELETE /v1/admin/funded-trial/grants/{user_id}` — server admins only;
  refused in single-user mode. `PUT` body:
  `{"allowance_usd": 100, "expires_in_days": 30, "capabilities": [...], "max_concurrent_roots": 1}`.
- `POST /v1/funded-trial/gateway/anthropic/v1/messages[/count_tokens]` — gateway
  token in `x-api-key` (or `Authorization: Bearer`).

### Activation settings (actual keys)

- `OMNIGENT_FEATURES` includes `funded_trial`.
- `OMNIGENT_FUNDED_TRIAL_CONFIG` JSON: `models` (USD per million:
  `input`, `output`, `cache_read`, `cache_write`), `compute_usd_per_hour`
  (by sandbox provider), optional `max_output_tokens`, `max_request_bytes`,
  `max_compute_hours`, `upstream_base_url`. Unlisted models/providers are denied.
- `OMNIGENT_FUNDED_TRIAL_SIGNING_KEY` (≥32 chars) and
  `OMNIGENT_FUNDED_TRIAL_ANTHROPIC_API_KEY` as server secrets.
- A sandbox target with `funding: trial`; its `host_config` may not define
  inference providers (startup fails if it does).

The feature flag is read at startup, so flipping it requires a redeploy.
Revoking a grant takes effect immediately and retires issued gateway tokens.

### Workspace inference and compute settlement (implemented, local)

- `bind_trial_inference` gives each trial target a `wakevane-trial` gateway
  provider and Claude harness bindings restricted to the priced models. Catalogs
  and session snapshots hold only a placeholder reference.
- `prepare_trial_launch` reserves compute, then mints a gateway token whose
  lifetime equals the reserved compute hours, and writes it only into that
  launch's in-sandbox host config. A mint denial releases the compute hold.
- The compute meter (`omnigent/server/compute_meter.py`, table
  `managed_compute_usage`, migration `f5a6b7c8d9e0`) records every generation,
  stops trial generations after `sandbox.metering.idle_shutdown_s` (default
  900) without runner activity or at `max_lifetime_s` (default: the reserved
  hours), and settles the compute hold from measured seconds. Holds that no
  metered generation will settle (failed launches, duplicate wakes) are settled
  for elapsed provisioning time or released.

### Cloud Design review checkout and confinement (implemented, local)

Behind the `egm_cloud` feature (default off). A `host_type: managed` Design
review create resolves the branch to an exact commit with `git ls-remote`
(public `https://` repositories only) and records it in `omnigent.egm.*` session
labels; continuations inherit them. Every new sandbox generation, including a
relaunch, then runs `prepare_cloud_review` after the host is online and before
any runner starts: detached checkout of that commit, symlink-safe creation of
`docs/designs/egm`, and an in-sandbox probe through the image's own bwrap
backend proving writes land only in that directory. Any failure tears the
sandbox down and the launch fails. Code: `omnigent/server/egm_cloud.py`, the
`prepare_workspace` hook in `managed_hosts.py`. Tests: `tests/server/test_egm_cloud.py`,
`tests/server/test_managed_hosts.py`.

### Not yet implemented

1. Storage charges, durable output publication, private-repository commit
   resolution (GitHub App), the approved coding/PR stage, and browser UI.
2. Real-provider (Modal) pilot and the live verification list above.
