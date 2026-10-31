# Design review: beta user guide

Design review turns a bounded request into a specification and implementation
handoff. A coordinator writes the design; separate reviewers check understanding,
challenge it, and assess readiness. Passing reviews do not approve implementation.

The current beta uses the web interface **and a connected computer**. The computer
holds your repository and runs the agents. Keep it awake and connected until the
review finishes. Browser-only cloud execution is [planned](BROWSER_ONLY_DESIGN_REVIEW.md),
not part of this workflow yet.

## First setup

Start with a disposable repository for your first review. You need access to
[WakeVane](https://app.wakevane.com), Git, and a configured Claude provider on the
computer that will run the review. A successful website sign-in does not sign
the agent into its model provider.

1. Install WakeVane using the [installation instructions](../README.md#install-wakevane).
2. In your computer's Terminal, authenticate to WakeVane:

   ```sh
   omnigent login https://app.wakevane.com
   ```

3. Run `omnigent setup` in Terminal to configure the model provider. Model access
   and charges depend on your provider configuration. Do not paste credentials
   into the review conversation.
4. Connect the computer:

   ```sh
   omnigent host --server https://app.wakevane.com
   ```

   Leave this Terminal window running. The website's host indicator should turn
   green. Closing the process or sleeping the computer interrupts execution.

## Start a review

1. In another Terminal window, enter the target Git repository and create the
   review output folder:

   ```sh
   cd /absolute/path/to/your-repository
   mkdir -p docs/designs/egm
   ```

   Replace the example path with your repository's actual path.
2. Open a new session on WakeVane. Select **Design review** in the agent picker.
3. Select your connected computer and use the folder picker above the message
   box to select the repository itself. Confirm that folder, not your home folder
   or the `docs` subfolder.
4. Send a bounded request. For example, in a scratch repository:

   > Design only: specify a CLI that reads a local JSON task list and prints
   > incomplete tasks. Define behavior for missing files, invalid JSON, and
   > duplicate IDs. Inspect this workspace, write the specification, run the
   > reviews, and produce a handoff. Do not implement, commit, push, or deploy.

The coordinator writes under `docs/designs/egm/<run-id>/`. Review stages appear as
separate child sessions. A review may request a revision or stop with a blocker;
neither outcome counts as a pass.

## Read the result

Open `handoff.md` in the run folder. It identifies the reviewed specification
revision and hash, the review results, remaining limitations, and acceptance
checks. Read that specification and the decisions before approving it.

- `r1.md`, `r2.md`, and later revisions contain the specifications.
- Requirements files contain the acceptance contract.
- Review reports contain findings and pass, revise, or blocked verdicts.
- `status.md` and `decisions.md` record progress and decisions.

Approval should identify the exact revision. Give the handoff and an explicit
implementation request to a coding agent when you are ready. Design review does
not automatically implement or deploy the result.

## Costs and boundaries

The packaged workflow requests at most three revisions and nine reviewer
dispatches. These counts are agent instructions, not a deterministic execution
limit. Its configured cost thresholds are $15 for the session tree and $2 per
reviewer subtree; accounting delays can overshoot them, and unpriced usage is
not reliably dollar-capped. A new chat does not enforce an account-wide budget.
See [limits and enforcement](../examples/egm/README.md#limits-and-enforcement).

The coordinator's repository writes are confined to the output directory;
reviewers have no repository write grants. Reviewers share the caller's filesystem:
fresh review conversations are not a strong confidentiality boundary between
review stages. Use only repositories you are authorized to send to your model
provider. Review agreement is useful evidence, not proof of correctness.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| Host disconnected | Keep the host command running and the computer awake. Reconnect, then inspect the existing session before starting another run. |
| `command not found: omnigent` | Complete installation and open a new Terminal so its executable is on your PATH. |
| Host tunnel rejected with HTTP 403 | Check server-account access and run `omnigent login https://app.wakevane.com` in Terminal. Repeated rejection needs operator investigation. |
| Claude not logged in | Authenticate/configure Claude on the host, not by sending `/login` to the web conversation. If Terminal authentication succeeds but the agent fails, report a sandbox/provider configuration problem. |
| Output folder missing | Create `docs/designs/egm` inside the selected repository, then start again. |
| `Operation not permitted` | Keep confinement enabled. Report the denied path and runtime version to the operator; changing Unix permissions alone may not fix a sandbox denial. |
| Design review absent | Ask the operator whether `egm_template` is enabled and the packaged agent is available. |
| Review stopped at a budget | Inspect the partial result and usage before deciding whether to increase a limit. It has not passed. |

The release has completed a real-provider macOS pilot. Fresh third-party
onboarding, other host platforms, and cross-user operation still require broader
beta validation. For operators, see the [bundle documentation](../examples/egm/README.md#web-design-review-opt-in).
