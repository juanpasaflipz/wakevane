# Ladders

A ladder works one kind of event in one repository with an ordered list of
agents, cheapest first. Each attempt is judged by the repository's own checks.
A passing check ends the task; a failing check hands it to the next agent. The
scoreboard then shows, for that repository, how often each step solves a task
and what a solved task costs.

Ladders are off by default. Turn them on with:

```text
OMNIGENT_FEATURES=ladders
```

They also need the `subscriptions` flag (on by default in this distribution)
and a repository whose webhook reaches the server; see
[Subscriptions](SUBSCRIPTIONS.md#connect-a-repository). The webhook must
deliver **Issues**, **Pull requests** and **Check suites** (or **Check runs**).
The flag is server-only: there is no web page yet, so ladders are driven
through the API below.

## How a task runs

```
issue opened ──► rung 1 (cheap agent) ──► pushes wakevane/att-<id>, opens a PR
                                              │
                          checks on that branch conclude
                         ┌────────────────────┴───────────────────┐
                      success                                  failure
                         │                                        │
                    task solved              rung 2 (stronger agent) starts,
                                             told what failed and where
```

1. An event of the ladder's `trigger_kind` arrives for its `repo` and matches
   `trigger_match`. The ladder starts a session with the first rung's agent.
2. The session is told to push a branch named `wakevane/att-<attempt id>` and
   open a pull request from it. That name is how a check result is tied back
   to the attempt.
3. The verdict is the conclusion of the ladder's `verify_kind` event on that
   branch: `success` passes, `failure` or `timed_out` fails. Other conclusions
   (`neutral`, `cancelled`, `skipped`) are ignored.
4. A failed attempt starts the next rung in a fresh session. Its prompt names
   the failed check, the earlier branch and the earlier pull request.
5. An attempt with no verdict after `attempt_timeout_minutes` counts as timed
   out and escalates the same way. So does an attempt whose session went idle
   more than five minutes ago without pushing its branch: the agent gave up.
6. An attempt whose session cannot run ends as `launch_failed` and does not
   escalate, because it says nothing about the agent: the session failed, was
   deleted, has been waiting for an approval for five minutes, or has been
   quiet for five minutes without its model ever answering (usually an agent
   that is not signed in).
7. When the last rung fails, the task stays unsolved. Nothing is merged at any
   point; a person reviews and merges the passing pull request.

A ladder runs one task at a time per subject (`issue:42`, `pr:7`), so a second
event about the same issue while an attempt is running starts nothing.

## Start a ladder in one call

```bash
curl -X POST https://<your-server>/v1/templates/ladder \
  -H "authorization: Bearer $OMNIGENT_TOKEN" -H "content-type: application/json" \
  -d '{
        "owner": "acme", "repo": "api",
        "rungs": [
          {"model": "<small model>", "max_cost_usd": 1},
          {"model": "<large model>", "max_cost_usd": 8}
        ],
        "check_name": "unit"
      }'
```

This connects the repository's webhook, installs a fixer agent for it as your
own agent (`ladder-fixer-<owner>-<repo>`) and creates a ladder that runs that
fixer once per model, in the order given. It needs GitHub connected in
Settings and the GitHub App installed on the repository, as for
[Start a software factory](SUBSCRIPTIONS.md#start-a-software-factory).

The fixer is the software factory's fixer run on its own. It never asks before
acting, pushes only its own branch, and on managed sandboxes reaches GitHub
through the credential proxy, so it can clone, push and open a pull request.

| Field | Meaning |
| --- | --- |
| `rungs` | One to five steps: `model`, and `max_cost_usd` (default 5) as the spend cap for one attempt. |
| `trigger_kind` | Event that starts a task. Default `issues.opened`. |
| `check_name` | The one check whose conclusion is the verdict. Omitted, any check suite on the branch decides. |
| `attempt_timeout_minutes` | 5 to 1440, default 120. |

Calling it again for the same repository and trigger reinstalls the fixer and
returns the existing ladder unchanged. To change the steps, delete the ladder
and call it again.

## Create a ladder by hand

```bash
curl -X POST https://<your-server>/v1/ladders \
  -H "authorization: Bearer $OMNIGENT_TOKEN" -H "content-type: application/json" \
  -d '{
        "name": "issue fixer",
        "repo": "acme/api",
        "trigger_kind": "issues.opened",
        "prompt_template": "Fix {task} in {repo}. Reproduce it first, then make the smallest change that passes the tests.",
        "rungs": [
          {"agent_id": "<agent id>", "model_override": "<small model>", "label": "cheap",  "max_cost_usd": 1},
          {"agent_id": "<agent id>", "model_override": "<large model>", "label": "strong", "max_cost_usd": 8}
        ]
      }'
```

| Field | Meaning |
| --- | --- |
| `repo` | `owner/name`. Events from other repositories are ignored. |
| `trigger_kind` | Event that starts a task, e.g. `issues.opened` or `check_run.completed`. |
| `trigger_match` | Optional extra filter on the event's fields, as for a subscription. |
| `prompt_template` | First message of every attempt. `{repo}`, `{task}`, `{kind}`, `{branch}` and `{fields_json}` are filled in. The branch and verdict instructions are appended for you. |
| `rungs` | One to five steps, tried in order. Each has an `agent_id` and optionally `model_override`, `max_cost_usd` (spend cap for one attempt), a `label` for the scoreboard and a `permission_mode`. Rungs may use different agents, so a ladder can mix harnesses. |
| `verify_kind` | `check_suite.completed` (default) or `check_run.completed`. |
| `verify_name` | With `check_run.completed`: the one check whose conclusion is the verdict, e.g. `unit`. |
| `execution_target` | `managed_sandbox` (default) or `connected_host`, with `host_id` / `workspace` as for a launch subscription. |
| `attempt_timeout_minutes` | 5 to 1440, default 120. |

Nobody is watching an attempt, so its agent must be able to work without
being asked. Two things decide that:

- **It must not wait for approvals.** A native-harness agent (Claude Code,
  Codex and the like) starts in its default mode, which asks before acting.
  Give the rung a `permission_mode` such as `bypassPermissions`, as for a
  scheduled task. Agents with policies that ask a person have the same
  problem.
- **It must have a model login and a way to push.** In a managed sandbox a
  native-harness agent is not signed in to its provider, and no agent has
  GitHub credentials unless its spec sets up the credential proxy. The fixer
  the template installs has both.

Each rung is authorized like a launch subscription: you need to be able to
start sessions with its agent. Attempts are billed like any other session you
start, against your credit and caps.

Other routes: `GET /v1/ladders`, `GET /v1/ladders/{id}`,
`PATCH /v1/ladders/{id}` with `{"state": "paused"}` or `"active"`,
`DELETE /v1/ladders/{id}` and `GET /v1/ladders/{id}/attempts`. A paused ladder
starts no tasks and stops escalating; sessions already running continue.

## The scoreboard

```bash
curl "https://<your-server>/v1/ladders/scoreboard?days=30&repo=acme/api" \
  -H "authorization: Bearer $OMNIGENT_TOKEN"
```

`tasks` reports how many tasks were started, solved, left unsolved or are
still in progress, their total cost, `cost_per_solved_task` (all spend,
including failed attempts, divided by solved tasks) and `solved_by_rung`.

`rungs` has one row per step of each ladder: attempts, passed, failed, timed
out, `pass_rate`, total cost, `cost_per_attempt` and `cost_per_pass`.
`low_yield` is true once a step has at least ten verdicts and passes fewer
than one in five of them; that step is mostly adding cost and delay.

Read the rows as positions in the ladder. The first step sees every task. A
later step only sees tasks the earlier steps failed, so its pass rate is on
harder work and is not a general ranking of the agent.

## Limits

- **Tests decide.** A repository with weak checks gets weak verdicts. With
  several check suites on a branch, the first conclusion wins; set
  `verify_kind` to `check_run.completed` and name the required check.
- **Timeouts need traffic.** Stale attempts and stuck sessions are swept when
  a GitHub event arrives or when attempts or the scoreboard are read, not on a
  timer.
- **A hung session that already used its model** looks like slow work, so it
  is only caught by the timeout.
- **Ending an attempt does not stop its session.** The sandbox is reclaimed
  by the server's own idle handling.
- **Cost is read from the session.** It is recorded when the attempt ends and
  refreshed on the scoreboard while the session still exists. Unpriced models
  count as zero.
- **A launch that fails does not escalate.** It says nothing about the agent,
  so the task stops and the attempt is marked `launch_failed`.
- **One server process.** Like subscriptions, there is no multi-replica
  leasing; two replicas could both act on one event.
- **Pricing is unchanged.** Ladders do not charge per solved task. The
  scoreboard is what such a price would be set from.
