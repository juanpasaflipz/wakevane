# EGM: design before implementation

An opt-in WakeVane workflow inspired by Google's
[Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model).
These are WakeVane-native agent configurations, not a copy of or installer for
Google's plugin. The existing software factory is unchanged.

The Elephant keeps the design context and writes a specification. Fresh
Goldfish sessions check comprehension, challenge the design and assess
implementation readiness. Revisions restart the reviews. The result is a
versioned implementation handoff for a human to approve.

## Try it

For the hosted web flow, start with the [beta user guide](../../docs/DESIGN_REVIEW.md).
The [browser-only delivery plan](../../docs/BROWSER_ONLY_DESIGN_REVIEW.md) describes
proposed cloud support; it is not available through this bundle today.

Use a WakeVane source checkout, a configured Claude provider (`omnigent setup`),
and the usual running server/host needed for worker sessions. Select the target
repository as the web session workspace (the folder picker above the composer),
not your home directory. Before starting, create `docs/designs/egm` inside that
repository. The coordinator has write access only to this output directory;
reviewers have no repository write grants. Both may read Git metadata.

When testing against this checkout from Terminal:

```sh
mkdir -p docs/designs/egm
omnigent run examples/egm
```

Give it an absolute repository path and a bounded feature. For a synthetic pilot:

```text
Design only, in repository /absolute/path/to/scratch-repo: add a CLI that
reads a local JSON list of tasks and prints incomplete tasks. Define behavior
for missing files, invalid JSON and duplicate IDs. Do not implement it.
```

Use an initialized scratch Git repository with a small README for the pilot.
Outputs go to `docs/designs/egm/<unique-run-id>/` in that repository:

- `r1.md`, `r2.md`, etc.: immutable specification revisions.
- `requirements.md`: independently versioned acceptance contract with requirement
  IDs, decision sources and checks; every review verifies both its hash and the
  specification hash, and assesses every requirement.
- Review reports: independent understanding, evidence and pass/revise/blocked.
- `decisions.md` and `status.md`: dispositions, counters and session handles.
- `handoff.md`: exact reviewed revision/hash, implementation steps, acceptance
  checks and approval status.

Inspect the child sessions: each stage and revision must have a distinct
session, and its first message should contain only the review packet. It must
not contain the design conversation or earlier findings. Check that all three
passing reports refer to the handoff's exact hash. A deliberate unresolved
requirement should produce a finding or blocked result, not invented behavior.

After approving the exact revision, provide the handoff to your coding agent
or factory fixer with an explicit implementation request. This example does
not wire itself into the one-click factory or automatically launch coding.

## Limits and enforcement

The workflow defaults to three specification revisions and at most nine review
dispatches, including failed launch attempts. New bundles enforce those limits
and review transitions per root session in the runner ledger described below.
The coordinator's
`cost_budget` caps the whole session tree at $15. Each reviewer's
`subagent_cost_budget` uses that child's own subtree usage and pauses at $2,
asking for approval to lift its cap. Decline to keep it blocked; review success
never authorizes a budget increase. Accounting and in-flight requests can
overshoot these thresholds. Unpriced usage is not reliably dollar-capped;
the child policy asks for approval when pricing is unavailable. Lower the YAML
budgets as needed.

Continuing an objective in a new chat or run directory does not reset revision
or dispatch counts. The coordinator must reconcile related runs first. Runtime
dollar budgets apply to session trees and do not enforce an aggregate budget
across new chats. Behavioral corrections belong in the reviewed specification,
not merely in handoff notes. Semantic requirement checking and cross-root count
reconciliation still require judgment; the runtime validates structured evidence
and enforces transitions within a root session.

### Readiness regression trial

The contract preserves the original task verbatim and quotes each user constraint.
Review packets contain paths, hashes, stage and repository evidence only. A packet
containing prior verdicts or leading review instructions must be rejected as
before launching a reviewer. Rejected preflight packets do not consume a paid
dispatch; failed launch attempts do.
Handoffs link the reviewed outlines and distinguish specified, reviewed and
executed evidence. Design approval never implies implementation tests passed.

Before rolling out a changed bundle, use a disposable repository with synthetic
money helpers and a pinned test runner. Give the coordinator a design-only task
to split an integer amount among guests, requiring reuse of an existing helper,
one named test per rule, two allowed implementation files and forbidden paths.
Record the original task, source hashes and dirty baseline. Independently review
these deliberately flawed specifications, one defect per specimen:

| Injected defect | Required review result |
| --- | --- |
| Replace separate named tests with assertions in one test | revise; test granularity violated |
| Duplicate helper validation using built-ins and an error class | revise; helper reuse violated |
| Use a predicate callback with a matcher that does not support it | revise; test outline invalid |
| Validate scope using only filtered, unstaged diff output | revise; scope verification incomplete |
| Claim tests passed while only listing future commands | revise; execution claim unsupported |
| Accept arbitrary guest counts while allocating a bounded array | revise; input/allocation contract incomplete |
| Omit a user constraint from the derived requirements | revise; original task and contract disagree |
| Include earlier PASS verdicts in the review packet | blocked; fresh clean review required |

Also review a corrected control specimen; it should pass design readiness while
keeping implementation execution pending. Include unrelated staged, unstaged and
untracked baseline files to exercise the scope-verification plan. Do not mutate
the evidence during a review round. Reviewers inspect only; any executable
experiments belong in a separate disposable implementation test workflow.

For a model comparison, hold specimens, source evidence and prompts constant.
Record actual coordinator/reviewer model IDs, missed injected defects, false
rejections of the corrected control, dispatch count, wall time and billed cost
(unknown if unavailable). Repeat trials before drawing reliability conclusions.
Do not infer a quality gain from agreement, and do not silently switch providers.
These behavioral trials supplement the deterministic protocol tests. The runtime
validates evidence structure, identity and transitions; semantic design quality
still requires reviewers and these behavioral trials.

Reviewers use fresh named worker sessions. Reusing an `(agent, title)` pair
normally continues its history; the runtime gate rejects repeated titles in a
review root. Include run, revision, stage and attempt in each new title.
Conversation isolation is a workflow convention, not an access-control boundary:
the shared caller filesystem and session tools may expose other artifacts.
The read-only policy blocks write/edit tools and the reviewer sandbox has no
repository write grants. Framework scratch directories remain writable.
Use a disposable checkout for the pilot. Strong history access isolation
would need a separate runtime change.

Only Markdown authoring is requested of the coordinator; file extensions are a
prompt constraint, while the output-directory write boundary is sandboxed.
There are no changes to default factory behavior. Review agreement does not
prove correctness. Model-backed execution,
context discipline and quality must be checked in a live pilot; structural
bundle validation alone cannot establish them.

Record actual elapsed time, review rounds, token/cost usage and later rework.
Compare equivalent tasks with the existing workflow before claiming savings.
No TypeSafe calls are required for this initial workflow.

## Web Design review (opt-in)

Operators can enable `egm_template` through `OMNIGENT_FEATURES` and restart the
server. Run database migrations and upgrade every API, worker and CLI writer
before enabling; mixed writer versions are unsupported. Keep the management
column on downgrade while protected EGM rows exist. The feature is off by default.

New Chat then offers **Design review** after Debby. Select an external connected
host with a signed-in provider and a Git repository containing `docs/designs/egm`.
Create that folder on the host first. Managed sandboxes are not supported in this
release. A sleeping laptop cannot run reviews. Host confinement must be available.

The packaged entry is `egm-review`; the importable bundle remains `egm`.
Custom name collisions are preserved and the packaged entry is skipped.
`--agent` and extra operator registrations take ownership of an existing packaged
entry, even with identical content. Removing the option does not restore packaged
ownership. The operator can explicitly remove their custom registration after
handling its sessions; there is no automatic destructive cleanup.

Each new packaged run stores an immutable bundle snapshot. Existing review trees
can continue after feature disablement or template replacement; new roots, forks
and scheduled starts require current availability. Snapshot configurations cannot
be edited, even by their owner. Start a new run or import an editable custom copy.
The $15 session-tree and $2 reviewer-subtree thresholds remain unchanged.

See [the verification recipe](../../feature-map/egm.md) before enabling a deployment.

Failed creates compensate their snapshot writes. For a process crash between
snapshot and root persistence, `cleanup_abandoned_snapshots` in
`omnigent.server.egm` provides offline maintenance: stop all writers first,
pass the deployment's agent/artifact stores and a conservative epoch cutoff.
It only removes snapshots with no surviving root or child reference; it is not
an online collector and must never run concurrently with session creation.

## Runtime review protocol

New bundles opt into `egm_review_protocol`. The runner exposes `sys_egm_review`
and keeps an SQLite ledger under its runtime data directory, outside the agent's
writable design directory. The ledger survives runner restarts on the same host;
it does not survive deletion of that host's runtime storage. Existing immutable
session bundles retain their old workflow; start a new session to use this protocol.

Before freezing, use this YAML front matter in the specification:

```yaml
---
decisions: []
acceptance_corrections: []
---
```

Each nonempty entry has `id`, `status: open | resolved`, `material: true | false`,
and nonempty `evidence`. An open material decision or any open acceptance
correction prevents the readiness dispatch. Resolve routine delegated design
choices in the specification; do not turn every implementation choice into a
request for user approval.

The contract front matter contains `original_task` (verbatim user request) and
`requirements`, each with unique `id` and `source_quote` copied from that task.
Keep the detailed acceptance contract in the Markdown body. The quote check
validates literal correspondence, not semantic completeness.

Call `sys_egm_review` with `action: freeze`, absolute `spec_path`, `contract_path`,
and `evidence_paths` (existing repository files outside `docs/designs/egm`).
For a new-file design, use existing source, configuration, or a README as the
baseline; future implementation paths need not exist. Never create placeholder
code/tests or use review artifacts as repository evidence. The tool requires a
nonempty evidence list. It snapshots the artifact bytes, hashes,
HEAD, index diff and dirty/untracked contents, excluding design outputs. Send its
returned packet as JSON text in `sys_session_send.args.input`, with
`purpose: review`, `agent: goldfish`, and a fresh `egm-...` title. Change only
`packet.stage` for critic/readiness. Additional fields, custom prompts, reused
titles, source drift and out-of-order stages are rejected before launch.

Actual child IDs from dispatch are bound to the reservation. The inbox adapter
records their full reports before truncating the displayed text. Only completed,
well-formed reports matching the frozen revision, contract, HEAD and every
requirement quote can pass. Missing checks, duplicate checks, unknown/violated
requirements, evidence limitations, blocking findings and unresolved behavior or
acceptance findings prevent advancement. Failed/cancelled dispatches consume the
nine-attempt budget. Invalid preflight packets do not launch or consume it.

Call `sys_egm_review` with `action: status` to compute readiness. All three stages
must pass the current unchanged revision. Never substitute hand-edited status
Markdown or a narrative PASS for this result. Frozen bytes cannot be replaced
under the same artifact path: corrections require a new versioned path and fresh
reviews, including packaging corrections. Three revisions and nine dispatches
are enforced per root session across turns; cross-root objective accounting is
still manual. A crash after reserving a launch fails closed as pending rather
than risking a duplicate paid reviewer.

The gate validates structured evidence and workflow transitions. It cannot prove
that an author disclosed every unresolved decision, that a reviewer classified a
finding honestly, or that draft artifacts are true. The coordinator's runner
stream suppresses model-authored chat prose and emits a completion status derived
from a fresh ledger/hash check. Tool activity and Goldfish reports remain visible.
This prevents an unreviewed draft's prose from becoming a ready chat verdict;
Markdown files and session titles remain explanatory, not machine-authoritative
readiness. Implementation
approval, deployment authorization and execution results remain separate.

Run regression coverage with:

```sh
python -m pytest tests/runner/test_egm_review.py tests/spec/test_egm_bundle.py
```

A manual trial should reject a packet with `prior_verdict`, refuse critic before
comprehension passes, reject reuse of a frozen path after even a wording edit,
and refuse readiness while an open material decision remains. A clean three-stage
trial should return `ready: true` with `execution_status: not_run` and approval
pending. This is a protocol check, not proof of the proposed implementation.
