# Design review

Design review runs an Elephant-Goldfish coordinator with independent reviewers
from New Chat, without importing a bundle. It is initially disabled.

## Sub-features

- `picker`: Design review follows Debby in the Agents group when enabled and healthy.
- `launch`: connected local or remote host, authenticated provider, Git repository,
  and an existing docs/designs/egm directory are required.
- `continuation`: existing reviews keep their saved bundle after the entry is disabled
  or an operator changes the packaged registration.
- `compatibility`: imported egm agents and operator-owned registrations remain custom.
- `failure`: missing content hides the optional entry; missing output, disconnected
  hosts, managed sandboxes and unavailable confinement cannot start a review.

## How to get to it (user POV)

- Open New Chat, choose Design review under Agents, select a connected host and
  repository, and describe the design task.
- Open an existing review from the sidebar and continue it.
- Fork a review to start new work; it requires the currently available template
  and receives a fresh saved bundle.
- Scheduled tasks and subscription launches also count as new work and use the
  current template at each firing. They require an external host and repository.
- Custom agent import continues to accept the ordinary egm bundle.

## Driving it with the repro environment

Follow [Verify Omnigent](skills/verify-omnigent/SKILL.md). Use an isolated test
repository and configured mock provider. Enable egm_template on the server
before startup. Create docs/designs/egm in the scratch repository beforehand.

1. With the flag disabled, confirm the entry is absent. Enable it and restart;
   confirm Design review appears after Debby and ordinary egm imports remain listed.
2. Select the entry and the scratch repository. Request a design, without code
   implementation. Confirm a new run folder, a specification, and separate reviewer
   sessions. Confirm the coordinator and reviewers use that exact repository.
3. Ask the coordinator to write the repository root README and a reviewer to write
   any file. Both must be denied. A coordinator write inside its run folder succeeds.
4. Disable the feature and restart. A stale picker, fresh start, fork and scheduled
   firing must be refused. An authorized existing coordinator can dispatch a fresh
   Goldfish child using its saved bundle. A read-only collaborator cannot dispatch.
5. Repeat startup with a missing or invalid packaged bundle. Other agents remain
   available, new Design review starts fail, and existing valid snapshots continue.
6. Try a repository without the output directory, a disconnected host, an
   unauthenticated provider, a managed target and a host without confinement.
   Each must give an actionable failure before review execution.
7. Import egm and resume it before and after enabling the feature. Pre-register a
   custom egm-review, including an operator registration, and confirm no overwrite.
   An operator takeover stays custom after removing that startup option.
8. Verify scheduled and forked reviews receive independent saved bundles. Editing
   a run snapshot is refused. Deleting/replacing the template does not remove the
   saved artifacts of existing runs.

## Proof and coverage

Store tests cover collision races, immutable snapshots, takeover, revision checks
and filtering. Backend tests cover disabled continuation, read-only rejection,
invalid ancestry, workspace preflight, optional seeding and failed persistence.
Web component tests cover capability-based naming, ordering and hidden snapshots.
These tests do not establish a live provider, browser or OS-sandbox pass. Record
those drives separately before enabling a deployment.
