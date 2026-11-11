# Subscriptions (event wakes)

A subscription lets a running session wake up when something happens in the world: CI going red on a pull request, an issue being opened, a deploy finishing. Today an agent can wake on *time* (timers, automations) and on *its own sub-agents* (the inbox). Subscriptions add *external events* to that list, through one primitive rather than one tool per event source.

The flow is: an agent (or a client) creates a subscription naming a `source`, an event `kind`, and an optional field filter. When a matching event reaches the server, the server posts a hidden system message into the subscribed session, exactly like a timer firing, and the model handles it on its normal turn path. If the session is mid-turn the message queues like any other user input.

A subscription can instead name an **agent** and a prompt template. Then every matching event **launches a fresh session** for the subscription's owner, the same way an automation fires, so nothing has to be running when the event arrives. See [Launch subscriptions](#launch-subscriptions).

Everything described here is behind the `subscriptions` release feature flag, which is on by default in the flywheel distribution and off upstream. Most of it can be driven [from the web UI](#from-the-web-ui) without touching the API.

## Enabling the flag

The `subscriptions` flag is on by default in the flywheel distribution; upstream
Omnigent keeps it opt-in. Disable it with `OMNIGENT_FEATURES=-subscriptions`.
While it is on, the server:

- mounts `POST /v1/events/github` and `POST /v1/events/webhook` (the ingest surface),
- mounts the `/v1/subscriptions` CRUD routes,
- builds an in-process event bus backed by a `subscriptions` table on the same database as automations.

The flag is server-only: it does not appear under `features` in `GET /v1/info`. The web page is behind its own frontend flag, `subscriptions_page` (see [From the web UI](#from-the-web-ui)). See `designs/FEATURE_FLAGS.md` for the inventory and review release.

Separately, an agent only gets the `sys_subscribe` tools when its spec opts in with a top-level `subscriptions: true`, the same way `timers: true` gates the timer tools. Both switches must be on for an agent to subscribe itself.

## GitHub webhook setup

To receive events from every repository a GitHub App is installed on, enable the App's own webhook instead of adding one per repository: see [Webhook for subscriptions](GITHUB_APP_SETUP.md#webhook-for-subscriptions) in the GitHub App guide. The per-repository steps below work without an App.

1. Pick a secret and set it on the server as `OMNIGENT_GITHUB_WEBHOOK_SECRET`. It is read on every delivery, so rotating it needs no restart. With the variable unset the endpoint answers **503** and accepts nothing.
2. In the repository (or organization) settings, add a webhook:
   - **Payload URL:** `https://<your-server>/v1/events/github`
   - **Content type:** `application/json`
   - **Secret:** the value from step 1
   - **Events:** choose *Let me select individual events* and tick **Check runs**, **Check suites**, **Pull requests**, **Issues**, **Issue comments**, and **Pushes**. Any other event is acknowledged with `202` and `matched: 0` but never published, so ticking more is harmless.
3. Send a test delivery. A `202` with `{"matched": 0, "delivered": 0, "suppressed": 0}` means the signature verified and there was simply nothing subscribed yet; a `401` means the secret does not match.

Each delivery is normalized into a `kind` and a flat `fields` dict:

| GitHub event / action | `kind` |
| --- | --- |
| `check_run` / `completed` | `check_run.completed` |
| `check_suite` / `completed` | `check_suite.completed` |
| `pull_request` / `opened`, `synchronize`, `closed` | `pull_request.opened` … |
| `issues` / `opened`, `closed` | `issues.opened`, `issues.closed` |
| `issue_comment` / `created` | `issue_comment.created` |
| `push` | `push` |

`fields` carries whichever of these the payload has: `repo` (`owner/name`), `action`, `conclusion`, `status`, `name` (the check's name), `pull_request` (number; the first when a check lists several), `issue` (number), `head_branch` (the branch a pull request or check ran on), `sender` (login), `ref`, and `url` (the subject's `html_url`). Nothing else from the payload is kept, so a filter can only name these keys.

## From the web UI

The **Subscriptions** page (`/subscriptions`, sidebar entry **Subscriptions**) is behind the frontend flag `subscriptions_page`, on by default in the flywheel distribution (`OMNIGENT_FEATURES=-subscriptions_page` hides it; the APIs below stay available). It lists your subscriptions with their event, filter, target (a link to the woken session, or the agent a launch starts), note, state, fire count and last firing, and lets you pause, resume or delete each one.

**Before you start** (once per server, by the operator):

- Set `OMNIGENT_GITHUB_WEBHOOK_SECRET` on the server.
- Make sure the server has a public URL. It is taken from `OMNIGENT_ACCOUNTS_BASE_URL`, then `OMNIGENT_DOMAIN`, then the platform's domain (`RAILWAY_PUBLIC_DOMAIN`, `RENDER_EXTERNAL_URL`, `FLY_APP_NAME`, `SPACE_HOST`), then the address the browser used. A server only reachable on `localhost` or a private IP cannot receive GitHub deliveries, and the page says so.
- Configure the GitHub App ([GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md)) and give it the Repository permission **Webhooks: Read and write**, so it can register webhooks for users. Existing installations must accept the new permission.

Each user then connects their own GitHub account once: **Settings → Sandbox Integrations → Connect GitHub**. The page links there when you have not.

### Connect a repository

**Connect a repository** opens a dialog:

1. Pick a repository (the list comes from your GitHub connection).
2. Choose what happens: **Start an agent** (pick an agent, edit the prompt template, optionally set a cost cap per session) creates [launch subscriptions](#launch-subscriptions); **Wake a session** (pick one of your sessions) creates wake subscriptions. The prompt template supports `{source}`, `{kind}`, `{subscription_id}`, `{note}` and `{fields_json}`.
3. Tick the events:

   | Checkbox | `kind` | Extra filter |
   | --- | --- | --- |
   | New issues | `issues.opened` | |
   | Failed checks | `check_run.completed` | `conclusion: failure` |
   | New PR | `pull_request.opened` | |
   | Comments | `issue_comment.created` | |

On submit the server registers its webhook on the repository with your GitHub token (`POST /v1/subscriptions/github/connect`), then the dialog creates one subscription per ticked event with `match: {"repo": "<owner>/<name>", ...}`. Errors from the server are shown as they are.

`POST /v1/subscriptions/github/connect` takes `{"owner": "acme", "repo": "api"}`. It creates a repository webhook pointing at `<public URL>/v1/events/github` with JSON content, the server's webhook secret, and the events `check_run`, `check_suite`, `issue_comment`, `issues`, `pull_request` and `push`, or reuses the webhook that already points there (adding any missing events). It answers `{"status": "created" | "existing", "hook_id": 123, "events": [...]}`; the secret is never returned. Errors:

| Status | When |
| --- | --- |
| `409` | You have not connected GitHub (the message points to Settings), the server has no GitHub App, `OMNIGENT_GITHUB_WEBHOOK_SECRET` is unset, or the server has no public URL. |
| `403` (`upstream_permission_denied`) | GitHub refused to list or create webhooks: the App lacks **Webhooks: Read and write**, is not installed on the repository's owner, or you are not an admin of the repository. |
| `400` | The owner or repository name is not a valid GitHub name. |
| `502` | Any other GitHub failure. |

`GET /v1/subscriptions/github/status?owner=acme&repo=api` reports `{"repo", "connected", "hook_exists", "hook_id", "events"}` (`connected: false` when you have not linked GitHub).

A repository webhook and the [App-level webhook](GITHUB_APP_SETUP.md#webhook-for-subscriptions) both deliver the same events; use one of them per repository.

### Start a software factory

The **Start a software factory** card at the top of the page sets up the [`examples/factory`](../examples/factory/README.md) foreman for one repository in one click. Pick the repository, a budget (default 25 USD) and whether to **Ask me before merging** (on by default), then **Start factory**. `POST /v1/templates/factory` with `{"owner", "repo", "daily_budget_usd", "ask_before_merge"}`:

1. connects the repository's webhook exactly as above (so the same errors apply, and nothing is created when one occurs),
2. installs the factory bundle as your own agent, named `factory-<owner>-<repo>`, with `daily_budget_usd` as the `max_cost_usd` of its `cost_budget` policy (it asks you to continue at 40% of it) and `ask_before_merge` as `gate_pushes` on its `blast_radius` policy,
3. creates two launch subscriptions for that agent: `issues.opened` with `match: {"repo": "<owner>/<name>"}` (the prompt asks the foreman to triage the issue in `{fields_json}`) and `check_run.completed` with `match: {"repo": "<owner>/<name>", "conclusion": "failure"}`. They run in a managed sandbox when the server can provision one, otherwise on your most recently active connected host.

The response carries `agent_id`, `agent_name`, `subscription_ids`, the `subscriptions` (each with `created: true | false`) and the `hook` status. Calling it again for the same repository returns the same agent and subscriptions instead of duplicating them; a changed budget or merge setting updates the agent in place, and a subscription you deleted is created again. Open an issue in the repository and a foreman session starts and dispatches the triager.

The budget is a per-session cap on the foreman: each launched session (and the workers it dispatches) stops at that amount; it is not pooled across a calendar day.

Workers run under the foreman's policies as well as their own. The foreman's `read_only_os` lists the fixer in `allow_sub_agents`, and its `blast_radius` lists it in `ungated_sub_agents`, so the fixer can edit files, push its feature branch and delete its scratch clone; a merge, release or deploy from any worker still asks you. On managed sandboxes the fixer reaches GitHub with your connected GitHub token through the secretless credential proxy (`gh_basic`, with an egress allowlist for GitHub and the Python and npm package indexes), and its workspace is writable and persists across shell calls.

When a session a subscription started waits on an approval for two minutes, the server emails the session owner the approval link, once per request. This needs a verified email on the owner's account and the `OMNIGENT_SMTP_*` mailer settings.

## The generic webhook

Anything that is not GitHub posts to `POST /v1/events/webhook` under normal bearer authentication:

```jsonc
{
  "kind": "deploy.finished",
  "fields": {"env": "prod", "service": "api", "ok": true}
}
```

`kind` is a lowercase identifier with at most one dot (`deploy`, `deploy.finished`); `fields` is a flat object of up to 64 scalar values. The source is fixed to `webhook`. This is the path a Slack bot, a CI script, or a cron job uses to wake sessions on things the server does not natively ingest.

### Slack messages

The Slack bot can forward the channel messages it sees to this webhook. Set `OMNIGENT_SLACK_FORWARD_EVENTS=true` on the bot (see `integrations/slack/README.md`). Every channel message and thread reply it receives (never its own, never DMs) becomes a `slack.message` event:

| Field | Value |
| --- | --- |
| `channel` | Slack channel id, e.g. `C0123ABC` |
| `thread_ts` | The thread root; a top-level message's own `ts` |
| `ts` | The message's timestamp |
| `user` | Slack user id of the author |
| `text` | Message text, truncated to 4,000 characters |
| `is_thread_reply` | `true` for a reply inside a thread |

Each message is posted with the author's own delegated token, so on an auth-enabled server only messages from users who signed in to the bot are forwarded. Forwarding failures are logged and never affect the bot's normal thread-to-session behaviour. To wake on a thread: `sys_subscribe(source="webhook", kind="slack.message", match={"thread_ts": "1712345678.000100"})`.

Both ingest routes answer `202` with `{"matched": n, "delivered": n, "suppressed": n}`: how many active subscriptions the event's filter matched, how many received a wake notice, and how many were skipped by the cooldown (below).

## Subscriptions

`POST /v1/subscriptions` takes exactly one target — `conversation_id` (wake that session) or `agent_id` (launch a new session per event, below) — plus `source` (`github` or `webhook`), `kind`, and optionally `match` (up to 16 scalar values) and `note` (up to 512 characters). Setting both targets, or neither, is a `400`. For a `conversation_id` target the caller must have **edit** access to the conversation, because a wake notice starts a turn there, and the launch-only fields below are rejected with `400`. Rows are owner-scoped like automations: `GET /v1/subscriptions` lists only your own, and another user's id answers `404`. `PATCH /v1/subscriptions/{id}` changes `state` (`active` / `paused`), `note`, or `match`; `DELETE` soft-deletes.

Matching is a subset test: every key in `match` must be present in the event's `fields` with an equal value. Numbers and strings compare as strings, so `"pull_request": 412` matches a webhook that sends `"412"`. An empty filter matches every event of that `source`/`kind`.

## Launch subscriptions

A launch subscription names an `agent_id` and a `prompt_template` instead of a conversation. When it matches, the server starts a **new** session for the subscription's owner through the same fire path automations use (`omnigent/server/scheduled/fire.py`): it resolves the execution target, creates the session, grants the owner, attaches the cost cap and launches the runner. Nothing needs to be running beforehand.

| Field | Meaning |
| --- | --- |
| `agent_id` | The agent each launched session runs. Same access rule as creating an automation: you must be able to start sessions with it (a server agent, your own, or one from a session you can read); another user's agent is copied for you at create time. |
| `prompt_template` | Required. The first user message, rendered per event. |
| `execution_target` | `connected_host` (default) or `managed_sandbox`. Anything else is a `400`. |
| `host_id`, `workspace` | Optional pins for `connected_host`, validated like an automation's. Unpinned, each launch uses your most recently active online host and its home directory. Not allowed with `managed_sandbox`, which provisions a fresh sandbox per event (the server must have a `sandbox:` config, otherwise `400`). |
| `max_cost_usd` | Optional per-session cost cap, attached as a `cost_budget` policy. |
| `model_override` | Optional model for launched sessions. |

The template is rendered with `str.format_map` and these placeholders: `{source}`, `{kind}`, `{subscription_id}`, `{note}` (empty when unset) and `{fields_json}` (the event fields as compact JSON, truncated at 4,000 characters). Any other `{placeholder}` stays literal, and a template `str.format` cannot parse is used unchanged. The standard wake notice (the `[System: event …]` line and the fields) is appended after the rendered template, so the launched agent always sees the event.

```bash
curl -X POST https://<your-server>/v1/subscriptions \
  -H "authorization: Bearer $OMNIGENT_TOKEN" -H "content-type: application/json" \
  -d '{
        "agent_id": "<triager agent id>",
        "source": "github",
        "kind": "issues.opened",
        "match": {"repo": "acme/api"},
        "note": "triage new issues",
        "prompt_template": "A new issue was opened. Triage it: reproduce, label, and comment with findings.\nEvent: {fields_json}",
        "execution_target": "managed_sandbox",
        "max_cost_usd": 2
      }'
```

Launched sessions are titled `<kind>: <note>` and labelled `omnigent.launched_by=subscription`, `omnigent.subscription=<id>` and `omnigent.event=<source>/<kind>`, so each one is traceable to the subscription that started it. The cooldown below applies per subscription, exactly as for wakes. The launch runs in the background: the ingest request counts it as `delivered` once handed off, and a launch that cannot start (owner's host offline, sandbox not configured, owner account removed) is logged on the server rather than failing the webhook. A subscription whose owner registration was deleted or rotated launches nothing.

## Agent tools

With `subscriptions: true` in the agent spec, the agent gets three tools, dispatched by the runner to the routes above:

- `sys_subscribe(source, kind, match?, note?)` — the runner fills in the calling session; the model never names one. This form is **not** approval-gated, because it can only wake the caller's own session.
- `sys_subscribe(source, kind, agent_id, prompt_template, execution_target?, host_id?, workspace?, max_cost_usd?, model_override?, match?, note?)` — creates a launch subscription. Because it creates unattended future work, the call **parks on an approval prompt** exactly like `sys_scheduled_task_create`; the prompt lists the source, kind, filter, agent, target and cost cap. The gate is the built-in `__ask_on_scheduled_task_change` policy, injected into every session, so it applies even to agents with no guardrails configured.
- `sys_unsubscribe(subscription_id)`
- `sys_subscription_list()`

A typical use is waiting for CI instead of polling:

```jsonc
// sys_subscribe
{
  "source": "github",
  "kind": "check_run.completed",
  "match": {"repo": "acme/api", "pull_request": 412, "conclusion": "failure"},
  "note": "CI on my PR"
}
```

The agent then ends its turn. When a check on PR 412 fails, the session wakes with:

```
[System: event github/check_run.completed matched subscription 3f9c… ('CI on my PR')]
{"action":"completed","conclusion":"failure","name":"unit","pull_request":412,"repo":"acme/api","status":"completed","url":"https://github.com/acme/api/runs/…"}
```

The notice is a hidden meta user message (the user sees the agent act, not the plumbing). The composed system prompt for a subscribing agent tells the model to treat it as a routine runtime status message rather than instructions typed by a person, and to act on the fields in line with its task and the note. The JSON line is truncated at 4,000 characters.

Each subscription has a 10-second cooldown: a flapping job that completes several times in a row produces one wake, and the rest count as `suppressed`. `last_fired_at` and `fire_count` on the row record deliveries.

## Limits

- **No replay.** An event that arrives while the server is down, or that matches nothing, is gone. GitHub's own redelivery can resend a webhook, which is then matched afresh.
- **One process.** The bus matches and delivers inside the server that received the event. There is no cross-replica leasing, so with several replicas each delivery is handled by whichever one GitHub reached.
- **Slack goes through the generic webhook.** The bot forwards messages as `webhook`/`slack.message` events (opt-in, above); there is no `slack` source, and only channels the bot is in are seen.
- **Delivery is best-effort.** If the session's runner is unreachable the failure is logged, the fire is still stamped (so the cooldown holds), and the ingest request still succeeds. The same holds for a launch that cannot start; unlike an automation, a launch subscription keeps no run history, so check the server log or look for the labelled session.
- **Launch fields are fixed at create.** `PATCH` changes only `state`, `note` and `match`; to change a launch subscription's agent, template or target, delete it and create a new one.
