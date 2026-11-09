# Repository model planning

New Chat offers **Plan model usage** for a managed Pi session with one repository
and a ready, configured model catalog. The user enters a task, requests a plan,
reviews or changes its model choices, and clicks **Use implementation model**.
The normal Send action launches the selected model with the existing catalog
revision validation. Planning itself creates no session or sandbox.

This is the recommendation-first release. Applying a plan selects the primary
model only. Reviewer suggestions and escalation guidance are manual. The
planning budget is advisory, not a per-task spend cap; account credit, reservation,
and overage controls continue to govern actual spend. Plans are not saved as
session records. The inspected commit is displayed but does not pin the later
session checkout. Editing the prompt, repository, branch, agent, provider, or
catalog revision discards the displayed recommendation.

## Availability

- Enable a managed Pi inference binding with priced models, as described in
  [Hosted models](HOSTED_MODELS.md). Only candidates in the caller's current
  authorized catalog are considered; unpriced models are excluded.
- Configure the existing GitHub App integration and connect the user's account.
  Both public and private repository inspection use that user's GitHub token.
- No additional inference key is required for rules-based planning.
- For optional semantic task analysis, set
  `WAKEVANE_MODEL_PLANNER_TYPESAFE_KEY` on the server. The user must separately
  check the consent box for each planning context. The call sends their prompt
  and aggregate language/file-count/test-presence statistics to TypeSafe;
  source contents, file names, repository name, commit, credentials, and model
  prices are not sent. Prompts can themselves contain sensitive information.
  Tests use only synthetic evidence and mocked HTTP transport.

The TypeSafe request batches scope and review-risk questions using the current
[System One API](https://docs.typesafe.ai/api/system-one). Judgments can add a
review recommendation, but cannot remove review triggered by conservative rules,
select an unauthorized model, change prices, launch work, or approve spending.
Timeouts, invalid replies, missing configuration, and capacity limits fall back
to rules with an explicit message. No confidence threshold is presented as a
correctness guarantee. Model quality and savings have not been measured.

## How recommendations work

The server resolves the GitHub branch to a commit and requests its recursive
file tree. It samples at most 3,000 entries, reports truncation, detects language
extensions and test-path signals, and never clones or executes the repository.
It does not read source files or assess code correctness. GitHub itself bounds
the recursive tree response; the inspection has a 30-second deadline.

Economy selects the lowest estimated cost among priced candidates. Balanced
uses the configured default when it is priced, otherwise the cheapest priced
candidate. Quality uses that same primary rule and adds a suggested review pass;
it does not claim the most expensive model is best. Sensitive or broad tasks
also receive a reviewer suggestion in other modes. The reviewer is the cheapest
available alternate, or the same model if only one is priced. Users can override
both choices before applying the primary model.

Estimates are transparent token scenarios, not forecasts: standard tasks use
7,500–30,000 input and 1,250–5,000 output tokens; broad/sensitive work uses
30,000–120,000 input and 4,500–18,000 output tokens. A review adds 35% of the
implementation token scenario at the reviewer's rates. Configured subscription
markup is included when billing is enabled; trials may have a different actual
markup. Compute, cache writes, retries, and AI task analysis are excluded. Actual
usage can exceed the displayed range. Changing a model recalculates the estimate.

## API and operating limits

`POST /v1/sandbox-providers/{provider}/harnesses/pi-native/model-plan` requires
normal user authentication, the current `configuration_revision`, repository
URL, and prompt. Optional fields are `branch`, `agent_id`, `mode`, `budget_usd`,
and `analyze_with_ai` (false by default). The route reuses model preview's agent
access checks, then resolves GitHub credentials for the authenticated user.
Client-supplied model catalogs, prices, endpoints, and credentials are rejected.
Responses use `Cache-Control: private, no-store`; there is no application-level
plan persistence. Do not configure infrastructure request-body logging for prompts.

Planning is limited to ten requests per user per hour per server process. Optional
TypeSafe analysis is additionally limited to 100 calls per hour per process,
with a twelve-second deadline and no retries. These are local abuse controls,
not a distributed quota or billing ledger. TypeSafe charges are currently an
operator expense and are not debited from the user's credits. Keep the optional
key unset until those operating costs are acceptable; multi-replica deployments
need shared rate enforcement before increasing availability.

## Verify

1. Configure two priced Pi models, connect GitHub, and open New Chat with the
   managed Pi agent and one repository.
2. Enter `Fix a typo in the README`, open **Plan model usage**, select Economy,
   and click **Recommend models**. Confirm the commit and sample are shown and
   the cheapest model is selected. No session should start.
3. Change the prompt to `Change payment authorization across the API` and request
   another plan. Confirm a manual reviewer and a larger token scenario appear.
4. Change either model and verify the estimate changes. Set a small planning
   budget and confirm the over-budget warning. Click **Use implementation model**,
   then Send, and confirm the session uses that model.
5. Edit the prompt or branch after a recommendation: the old plan must disappear.
   Try a repository the connected user cannot read: show an access error without
   starting a session. Disconnect GitHub and confirm the connection guidance.
6. With a synthetic prompt, opt into AI analysis. With no key configured, confirm
   the explicit rules fallback. A live TypeSafe check requires a configured key;
   it is not covered by mocked integration tests.

Automated checks:

```sh
.venv/bin/python -m pytest -p no:rerunfailures tests/server/test_model_plans.py tests/server/test_sandbox_inference_routes.py
cd web
node_modules/.bin/vitest run src/components/composer/ModelPlanPanel.test.tsx src/shell/NewChatDialog.test.tsx
node_modules/.bin/tsc -b
node_modules/.bin/vite build
```
