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
dispatches, including failed attempts. Those limits and state transitions are
prompt instructions, not a deterministic state machine. The coordinator's
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
not merely in handoff notes. Requirement checking and count reconciliation are
prompt-level conventions; offline configuration tests do not prove compliance.

Reviewers use fresh named worker sessions. Reusing an `(agent, title)` pair
continues its history, so titles include run, revision, stage and attempt.
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
