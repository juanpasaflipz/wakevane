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
| Efficiency scorecard | Cache hit rate, output share, cost per session and model mix per harness and per model, at `GET /v1/usage/efficiency` and `omnigent usage --efficiency`. | `omnigent/server/usage_efficiency.py`, `omnigent/server/routes/usage.py`, `omnigent/cli.py` |
| Credential encryption | A local-key envelope cipher (AES-256-GCM, per-value data keys, key rotation) as the default, KMS moved to the same envelope with legacy values still readable, and `omnigent credentials rewrap`. | `omnigent/stores/credential_store/{envelope,local_cipher,rewrap}.py` |
| Web UI | Subscriptions page, Efficiency tab on the Usage page, Shims page. | `web/src/pages/`, `web/src/components/{subscriptions,usage}/` |
| Harness bench | A cost-per-solved-task probe. | `tests/harness_bench/probes/cost_per_solved_task.py` |
| Examples | A software-factory foreman with triager, fixer and watcher workers; a shim-janitor agent; an opt-in Elephant-Goldfish design-review bundle. | `examples/factory/`, `examples/shim-janitor/`, `examples/egm/` |
| Deployment | One-command Railway deploy built from source, with generated secrets and a persistent volume. | `deploy/railway/deploy.sh`, `railway.toml` |
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
| Docs | Subscriptions, shims, efficiency, credential encryption, Railway, GitHub App setup, and feature-flag inventory updated for the above. |

- Added default-off `egm_template` Design review entry, durable template ownership, immutable run snapshots, and connected-host preflight. Deployment requires migration and coordinated writer upgrade; see `examples/egm/README.md`.
