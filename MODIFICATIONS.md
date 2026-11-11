# Modifications in WakeVane

WakeVane is a distribution of [Omnigent](https://github.com/omnigent-ai/omnigent),
licensed under the Apache License 2.0. Section 4(b) of that license asks that
modified files carry a notice that they were changed. This file is that notice
for the whole distribution: it lists what WakeVane adds to and changes in the
original work. The exact line-level history is in this repository's git log,
comparing the `dist` branch with upstream `main`.

Internal names are deliberately unchanged. The Python package is `omnigent`,
the `omnigent` and `omni` commands still work, and settings keep their
`OMNIGENT_*` names, so WakeVane can merge upstream changes every week. The
`wakevane` command is an alias for the same entry point.

## Added

| Area | What | Where |
| --- | --- | --- |
| Event subscriptions | Agents wake on GitHub, Slack and generic webhook events through one `sys_subscribe` tool; a subscription can wake an existing session or launch a new one. Verified GitHub webhook ingest, an event bus with per-subscription cooldown, REST CRUD, a database table and migrations. | `omnigent/server/events/`, `omnigent/server/routes/{events,subscriptions,subscriptions_github}.py`, `omnigent/stores/subscription_store/`, `omnigent/entities/subscription.py`, `omnigent/tools/builtins/subscriptions.py` |
| Repository connect and factory template | Register a GitHub webhook for a repository from the web UI, and create a software-factory agent with its subscriptions in one step. | `omnigent/server/github_webhooks.py`, `omnigent/server/factory_template.py`, `omnigent/server/routes/templates.py` |
| Shim registry | Deterministic patches over model gaps are registered with a reason and a review-by release, counted per model, reported at `GET /v1/shims` (admin-only in multi-user deployments), retirable only after a full observation window, and gated at build time. | `omnigent/runtime/shims.py`, `omnigent/server/routes/shims.py` |
| Ladders | Work a repository event with agents cheapest first: each attempt is judged by the repository's checks on its own branch and a failure escalates to the next agent. A per-repository scoreboard reports pass rate per step and cost per solved task. Behind the `ladders` flag, off by default. | `omnigent/server/ladders/`, `omnigent/server/routes/ladders.py`, `docs/LADDERS.md` |
| Efficiency scorecard | Cache hit rate, output share, cost per session and model mix per harness and per model, at `GET /v1/usage/efficiency` and `omnigent usage --efficiency`. | `omnigent/server/usage_efficiency.py`, `omnigent/server/routes/usage.py`, `omnigent/cli.py` |
| Credential encryption | A local-key envelope cipher (AES-256-GCM, per-value data keys, key rotation) as the default, KMS moved to the same envelope with legacy values still readable, and `omnigent credentials rewrap`. | `omnigent/stores/credential_store/{envelope,local_cipher,rewrap}.py` |
| Web UI | Subscriptions page, Efficiency tab on the Usage page, Shims page. | `web/src/pages/`, `web/src/components/{subscriptions,usage}/` |
| Harness bench | A cost-per-solved-task probe. | `tests/harness_bench/probes/cost_per_solved_task.py` |
| Examples | A software-factory foreman with triager, fixer and watcher workers; a shim-janitor agent; an opt-in Elephant-Goldfish design-review bundle. | `examples/factory/`, `examples/shim-janitor/`, `examples/egm/` |
| Deployment | One-command Railway deploy built from source, with generated secrets and a persistent volume. | `deploy/railway/deploy.sh`, `railway.toml` |
| Public signup safety | Attempt limits on sign-in, open signup and password reset; self-serve password reset by emailed single-use link; baseline security response headers with an optional `frame-ancestors` setting; operator terms and privacy links on the sign-up page. | `omnigent/server/{auth_rate_limit,password_reset,security_headers}.py`, `omnigent/server/routes/accounts_auth.py`, `web/src/pages/{ForgotPasswordPage,ResetPasswordPage}.tsx`, `docs/PUBLIC_LAUNCH.md` |
| Sandbox provider keys | A user's own OpenAI API key, encrypted at rest, binds the Codex harnesses in the sessions they create and reaches the sandbox only as launch environment. Behind the `sandbox_provider_keys` flag, off by default. | `omnigent/connections/provider_keys.py`, `omnigent/server/user_provider_keys.py`, `omnigent/server/routes/provider_keys.py`, `web/src/pages/settings/OpenAIKeyControl.tsx`, `docs/SANDBOX_PROVIDER_KEYS.md` |
| Branding | The WakeVane name, tagline and mark as default branding, with the "Powered by Omnigent" credit shown. | `omnigent/resources/branding/wakevane/` |

## Changed

| Area | Change |
| --- | --- |
| Feature defaults | `subscriptions`, `subscriptions_page`, `usage_page` and `shims_page` are on by default; upstream keeps them opt-in. Each can be switched off with `OMNIGENT_FEATURES=-name`. |
| Existing patches | Three existing model-gap patches report their firings to the shim registry: the compaction provider prefix, the context-overflow text sniff, and the inbox empty-completion phrasing. |
| Scheduled tasks | The session launch path is shared with launch subscriptions; scheduled-task behaviour is unchanged. |
| Usage report | The session walk behind `GET /v1/usage` is shared with the efficiency report; its output is unchanged. |
| Policies | The approval gate for unattended work also covers agent-created launch subscriptions. |
| Slack integration | Optionally forwards channel messages to the event bus. |
| Auth helpers | A generic `require_admin` helper. |
| Railway config | Builds from source instead of pulling upstream's prebuilt image. |
| Funded launches | An unattended launch (subscription or scheduled task) refused only because the account's funded workflow slot is busy stays queued and is retried for up to 30 minutes instead of failing at once. Interactive creates are unchanged. |
| Polly example | The roster preflight treats `needs-auth` as launchable (a session can supply the credential); workers check the repository's pinned runtime before trusting its checks and report a mismatch as such; the orchestrator is told which models let `claude_code` run without approval prompts. |
| Efficiency scorecard | A dated Claude snapshot id is counted under its alias. |
| Web UI | The settings Admin group appears on a direct load of a settings URL; settings copy uses the configured app name; the Subscriptions repository picker has a filter for long lists; a failed launch has its own headline. |
| Funded gateway pricing | A priced model may carry a `long_prompt` tier; a request whose prompt passes the tier's token threshold is billed at that tier, and holds use the dearer tier. |
| Docs | Subscriptions, shims, efficiency, credential encryption, Railway, GitHub App setup, and feature-flag inventory updated for the above. |

- Added default-off `egm_template` Design review entry, durable template ownership, immutable run snapshots, and connected-host preflight. Deployment requires migration and coordinated writer upgrade; see `examples/egm/README.md`.
