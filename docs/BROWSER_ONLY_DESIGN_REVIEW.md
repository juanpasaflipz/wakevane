# Browser-only Design review: delivery plan

Status: proposed implementation, not a shipped capability. The current
[beta guide](DESIGN_REVIEW.md) requires a connected host.

## Intended experience

A user signs in, selects a GitHub repository and revision, describes a design
request, and starts a review. WakeVane provisions an isolated cloud workspace.
The user can close the browser or laptop and return to progress, reviewer reports,
and a downloadable handoff. No Terminal, local installation, or manually created
output folder is required.

The normal beta path uses users' own model API keys (BYOK). A separate, private
trial will offer service-funded models and the complete design-to-PR workflow to
explicitly allowed accounts. See the [funded trial gate](FUNDED_TRIAL.md).
Design approval precedes coding; merge and deployment remain explicit approval
steps. The coordinator and reviewers retain their existing write restrictions;
implementation runs in a separate coding session.

Repository contents are untrusted inputs. Private repository access must be
explicitly granted by its owner. Public repositories can provide the first
synthetic acceptance fixtures. The owner approved a $100 USD total funded trial
allowance. Cloud provider selection remains open. Neither trial funding nor cloud
access is enabled by this document.

## Reuse the existing runtime

| Existing component | Role and gap |
| --- | --- |
| `omnigent/server/managed_hosts.py` | Managed provisioning, repository cloning, and host launch already exist. Add EGM preparation through this lifecycle; do not build another executor. |
| `omnigent/server/routes/sandbox_inference.py` | Validates managed model selection and snapshots inference settings. Reuse inheritance for reviewers. This does not establish an end-user API-key vault. |
| `omnigent/server/egm.py` | Packages immutable EGM snapshots and validates continuation. Its target checks explicitly reject managed sandboxes today. Replace that restriction only for a validated cloud path. |
| `omnigent/server/routes/_sessions/orchestration.py` | Owns session launch and snapshot compensation. Keep ownership and rollback in this flow. |
| `omnigent/server/managed_sandbox_reaper.py` | Cleans up cloud sandboxes. Persist review outputs before cleanup; retained session records alone do not guarantee retained files. |

The new capability should have a separate default-off rollout flag, proposed as
`egm_cloud`. Keep existing connected-host reviews operational. Enabling generic
managed sandboxes or removing `require_egm_target` checks alone is insufficient.

## Delivery sequence

### 1. Cloud workspace and durable results

- Add a cloud review launch choice using the existing managed-host lifecycle.
  Resolve the requested branch to an exact commit, record it, and check out that
  commit. Existing clone branch parsing must not be assumed to accept a commit
  SHA. Validate repository URLs and never accept embedded credentials.
- Prepare `docs/designs/egm` before starting the coordinator, validate its canonical
  location, and verify sandbox confinement. Fail closed if confinement is absent.
  Keep repository writes limited to review outputs; do not run project install
  scripts merely to inspect its source.
- Publish versioned review outputs to durable, owner-authorized storage with a
  manifest of relative paths, sizes, content hashes, run ID, and source commit.
  Reject path traversal, symlink escapes, excessive sizes, and unexpected files.
  Do not upload the whole checkout or credential files.
- Checkpoint outputs during execution and on completion/cancellation. Surface
  persistence failures explicitly; do not report a durable handoff before its
  manifest is committed. Make publication idempotent and define recovery after
  unexpected sandbox loss.
- Provide authenticated result viewing and download. Preserve exact reviewed
  bytes and hashes; revoking a user's access must also revoke artifact access.

### 2. Identity, credentials, and resource controls

- Scope every launch, child session, checkout, artifact, and model credential to
  the authenticated owner. Test cross-user access rather than relying on hidden
  UI controls. Never reuse another user's workspace or credentials.
- For private GitHub access, prefer repository-scoped installation authorization
  and short-lived read-only checkout credentials. Keep credentials out of clone
  URLs, prompts, logs, and exported results. Credential revocation must prevent
  new access.
- Keep BYOK as the normal path with a separately authorized funded trial.
  BYOK requires encrypted secret storage, redaction, revocation, and deletion.
  Service-funded inference requires server-enforced allowance reservation and
  usage accounting. Neither can rely solely on the agent's prompt budgets.
- Prefer scoped model-gateway access over exposing reusable provider secrets to
  repository-facing tools. Review network access and credential exposure for the
  chosen sandbox provider before enabling private repositories.
- Enforce owner concurrency, runtime, storage, and cost admission limits outside
  prompts. Reserve allowance atomically, reconcile actual usage, and reject
  unknown-priced models where dollar enforcement is required. Model spending and
  cloud compute are separate costs. Cancellation must stop descendants and clean
  up resources without deleting already published results.

### 3. Browser onboarding and release

- Show repository access, selected revision, model/payment setup, allowance, and
  estimated limits before launch. Explain who pays without exposing runtime
  implementation details.
- Show preparation, reviewing, needs-input, blocked, completed, and failed states.
  Closing a tab must not cancel a run. Returning after sandbox expiry must still
  show saved results and a clear recovery action.
- Add explicit result retention/deletion behavior and operator diagnostics for
  provision failures, abandoned resources, failed exports, and accounting gaps.
- Enable first for an internal cohort, then invited users. Expand only after the
  acceptance checks below pass against the selected cloud provider.

## Acceptance evidence required

Use synthetic repositories and credentials for automated tests. Reuse existing
managed-host, authorization, and session primitives; see the
[test guide](../tests/README.md) before adding setup.

1. A fresh account starts a review entirely in the browser. No connected laptop
   host exists. Close the browser and verify execution continues.
2. The coordinator and all reviewers use the recorded commit and immutable agent
   snapshot. A moving branch cannot silently change the reviewed source.
3. Coordinator writes outside the output directory and reviewer repository writes
   are denied by the actual cloud runtime. Missing confinement blocks launch.
4. Another user cannot read, continue, cancel, download, or infer private results
   through session, artifact, host, or shared-link routes.
5. Kill the sandbox after a published checkpoint. Saved outputs remain available;
   any incomplete run is clearly labeled. A retry does not create duplicate
   billable launches or falsely claim review completion.
6. Exercise quota exhaustion, unknown model pricing, cancellation, credential
   revocation, provisioning failure, and failed result upload. Confirm bounded
   resource cleanup and accurate user-visible status.
7. Complete a real-provider review with all three final stages passing, download
   its handoff, and verify hashes against durable storage after sandbox deletion.
8. Repeat connected-host regression checks and disable the cloud rollout flag.
   Existing saved results remain readable; new cloud launches are rejected.

Record real elapsed time, compute and model usage, cleanup outcomes, and remaining
limitations. Synthetic reviewer tests alone cannot establish live service readiness.
