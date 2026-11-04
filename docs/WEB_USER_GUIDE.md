# WakeVane browser user guide

Read the published guide at [docs.wakevane.com](https://docs.wakevane.com).

Use WakeVane to give an AI agent a task, follow its progress, review its work,
and collaborate with teammates from your browser. A **session** is a conversation
with an agent and the workspace where it works.

This guide is for users of an existing WakeVane server. Ask your administrator
for its web address and access instructions. The available models, workspaces,
and sharing options depend on how your server is configured.

## How the harness works

An AI harness connects a model to tools, project context, an execution environment,
and feedback. In WakeVane, you give an agent a task in a session, follow its tool
activity, and review the result. A coding workflow can move through planning,
implementation, testing, correction, and human review in the same environment.

| Term | What it means |
| --- | --- |
| Agent | The software carrying out your task, with its instructions and tools. |
| Model | The AI the agent uses for reasoning and generation. |
| Session | The conversation and record of the work. |
| Host | The computer or cloud environment executing the work. |
| Workspace | The directory on that host where files and code live. |
| Policy | A rule that allows, denies, or asks for approval before an action. |

WakeVane is alpha. Available features depend on the server configuration and
installed version. Its command-line tool keeps the upstream name `omnigent`.

## Sign in

1. Open the web address supplied by your administrator.
2. Follow the sign-in screen. If it asks for a username and password, use the
   account provided to you. If your organization uses its own sign-in service,
   follow that service's prompts.
3. If you received an invitation link, open it and complete account setup first.

An expired or invalid sign-in link does not mean your work has been deleted.
Use your password if the screen offers that option, or ask your administrator
for help accessing your account.

You do not need to install the command-line tool to use an already configured
server through the browser.

## Connect a workspace

A browser controls the session; the selected host executes the task. Signing in
to the website does not automatically connect a computer, grant repository access,
or authenticate an agent to its model provider.

### Use a configured cloud environment

If your server offers a managed sandbox, select it when starting the session.
The server provisions the execution environment. Make sure the intended repository
and model credentials are available there. If no sandbox option is offered, ask
the administrator to configure one or connect a computer.

### Connect your computer

Install WakeVane using the [project installation instructions](https://github.com/juanpasaflipz/wakevane/blob/dist/README.md#install-wakevane).
Then run these commands in Terminal, substituting your own server address when
using a self-hosted deployment:

```sh
omnigent login https://app.wakevane.com
omnigent setup
omnigent host --server https://app.wakevane.com
```

Complete sign-in and model-provider setup when prompted. Leave the host process
running and keep the computer awake. In the browser, select that connected host
and the directory containing your project. A remote session can continue after
you close the browser only while its execution environment remains available.

### Give the workflow repository access

For an existing checkout, select its directory on the connected host. For GitHub
subscriptions and repository setup, connect your account through **Settings →
Sandbox Integrations → Connect GitHub**, when offered. Your GitHub App installation
must include the repository you want to use. Access to GitHub and access to the
model provider are separate connections.

## Complete your first task

1. Choose **New session** in the sidebar.
2. Review the available agent and model choices. An agent is the software that
   carries out your task; a model is the AI it uses. Keep the configured defaults
   if you are unsure which to choose.
3. If asked to select a host or workspace, choose the environment intended for
   your task. A host is the computer or cloud environment where the agent runs.
   It is not necessarily the device running your browser.
4. Describe what you want, provide the relevant context, and specify what a
   finished result should include. For example:

   > Review the attached draft for unclear instructions. Return a revised draft
   > and a short list of changes. Keep the tone friendly and under 500 words.

5. Add supporting files using the attachment control when it is available, then
   send your message.
6. Follow the response in the conversation. Answer any questions or approval
   requests that appear.
7. Review the result and send a follow-up if something needs changing.

For a coding task, name the repository, explain the expected behavior, and ask
the agent to report how it verified its changes. For writing or analysis, specify
the audience, source material, and output format.

## Example: build an inventory feature

Start with a small development repository and a bounded request. This fictional
store example gives the agent both the desired behavior and a way to check it:

> Add inventory tracking to this store app. First inspect the order flow and
> propose a plan. Reduce stock when an order is confirmed, restore it on
> cancellation, and show a low-stock alert below a configurable threshold.
> Processing the same order event twice must not deduct stock twice. Add tests
> for these cases and run the relevant existing tests. Summarize the changes,
> results, and remaining questions. Prepare the work for review; ask before
> pushing, merging, or deploying.

1. **Review the plan.** Confirm when an order changes stock, how cancellations
   behave, and what should happen if stock is insufficient.
2. **Follow implementation.** The agent inspects the repository, updates the data
   model and interface, and uses the configured tools to run the code.
3. **Check the evidence.** Ask for the test commands and results, including any
   failed or skipped checks. The agent can correct failures and rerun checks.
4. **Review the change.** Inspect the diff and try the inventory flow yourself.
   Use a pull request if your repository workflow supports it.
5. **Approve delivery separately.** Decide whether to merge or release based on
   the behavior and evidence. Passing tests does not establish that every edge
   case is covered.

Instructions express your intent; configured policies provide enforcement.
Use a policy approval gate when an action must require permission.

## Follow and direct ongoing work

The conversation shows the agent's responses and tool activity. An agent may
pause because it needs an answer, permission, or a working connection.

- **Add a follow-up:** send another message in the same session to retain the
  conversation's context.
- **Check queued messages:** a message sent while the agent is busy may appear
  in the queue. You can edit or remove it. If **Send now** is offered, use it to
  deliver that message without waiting for its normal place in the queue.
- **Stop work:** use the stop or interrupt control in the composer. Stopping a
  turn does not undo changes or external actions already completed.
- **Return later:** reopen the session from the sidebar. Use the sidebar search
  to find an older conversation.

Delivery and interruption behavior varies by agent. Check the visible queue
and status before sending the same request again.

## Respond to approval requests

When an action requires permission, an approval card appears in the conversation.
Read the proposed action and its target before choosing **Approve** or **Reject**.
If the agent needs a different approach, explain it in a follow-up message.

Some agents also offer permission for the whole session, remembered permissions,
or an automatic approval mode. These choices affect later actions as described
on the card. Choose the ordinary **Approve** action when you only intend to
approve the displayed request.

Server policies can limit actions even when you want to allow them. Ask your
administrator about a blocked action that you need for your work.

## Work with files and results

Attachments supply context to the agent. The session's **Files** panel, when
available, lets you inspect files in its workspace. These files belong to the
session's execution environment; they are not automatically files on your laptop.

For coding work, use **Changes** when available to review edits. Ask the agent
to identify the files it changed, summarize the result, and explain any checks
it ran. Review the output before treating the task as complete.

If you cannot find a deliverable, ask the agent for its exact filename and
workspace location. If you need a local copy, use a download control where one
is offered; a path printed in chat alone does not download the file.

## Share a session

1. Open the session you want to share and choose **Share**.
2. Review the access settings and choose the intended audience and permission.
3. Copy the session link and send it to your teammate.

Read access lets a teammate view the session. Edit access allows collaboration,
including sending messages to the agent. Available permissions depend on server
settings. A link does not bypass sign-in or access restrictions.

Before sharing, review the conversation and its work for information the intended
audience should not receive. Collaboration happens in the shared session and its
execution environment.

To remove someone else's shared session from your sidebar, use **Leave session**
in its row menu. This does not delete the owner's session.

## Start work from repository events

**Subscriptions** can wake an existing session or launch a new one when an event
arrives, such as an opened issue or a failed check. The page and event endpoints
must be enabled on your server.

1. Open **Subscriptions** and choose **Connect a repository**.
2. Select a repository available through your GitHub connection.
3. Choose **Start an agent** for a fresh session per matching event, or **Wake a
   session** to notify an existing session.
4. Select the events and review the prompt, target, and cost cap where offered.
5. Create the subscription, then trigger one test event in a development repository.
6. Open the resulting session and confirm that it received the expected context.

The **Start a software factory** option sets up a foreman for issues and failed
checks. Review the repository, budget, and **Ask me before merging** setting
before starting it. The factory budget applies per launched session and its
workers; it is not a shared daily allowance.

Pause a subscription to stop future matching deliveries. Pausing it does not
stop work already launched. Check for a resulting session before retrying an
event: event delivery is best-effort and a successful webhook response alone
does not prove that an agent finished its task.

If connection fails, an administrator may need to configure the public server
URL, webhook secret, and GitHub App webhook permission. See the
[subscriptions reference](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/SUBSCRIPTIONS.md) for setup and delivery limits.

## Schedule recurring work

Use **Automations** to run a saved prompt on a schedule rather than in response
to a repository event. Each firing creates a session.

1. Open **Automations** and create a task.
2. Set the agent, prompt, schedule, and timezone. Choose the model and execution
   target when those controls are offered.
3. Choose a connected host that will remain available, or a managed sandbox if
   the server supports one.
4. Review the next run and inspect the first resulting session before relying
   on the automation.

A useful first prompt is: “Inspect this repository's documentation for broken
internal links. Report findings and proposed corrections for review.”

Unattended work can still wait for an approval. An offline connected host can
prevent a run from starting. Pause or delete the automation when future work is
no longer needed; separately inspect any session already in progress.

See the [automations reference](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/AUTOMATIONS.md) for supported schedules,
per-run budgets, execution targets, and run history.

## Understand usage and budgets

Open **Usage** to inspect recorded usage when enabled on your server. The
**Efficiency** tab compares cost, cache reuse, and model mix, and links to
expensive sessions. Missing measurements are different from zero usage.

From an authenticated command-line installation, you can also run:

```sh
omnigent usage
omnigent usage --efficiency --days 7 --limit 20
```

Review cost alongside completed work and verification results. A cheaper session
that does not solve the task is not necessarily a better result. These reports
reflect the usage recorded by the harness, not a replacement for provider invoices.

Budget policies check recorded spend at enforcement points. They can block later
requests or tool calls, but do not guarantee an exact billing ceiling or interrupt
an already running model turn. See the [usage reference](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/USAGE_EFFICIENCY.md) and
[policy reference](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/POLICIES.md) for details.

## Use WakeVane on your phone

Open the same server address in your phone's browser and sign in with the same
account. Use the sidebar navigation to open a session. On smaller screens,
panels and navigation may be hidden until you open them.

Your phone must be able to reach the server. If the address works only on your
organization's network, use the network access your administrator provides.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| I cannot open the site. | Confirm the server address and your network connection. Ask the administrator whether the server is available and whether you need access to a private network. |
| I cannot sign in. | Check that you are using the correct server and account. If an invitation or sign-in link is invalid, ask the administrator for help. |
| No usable agent, model, or host appears. | Ask the administrator to confirm that the required agent, model credentials, and execution environment are configured. |
| The agent seems stuck. | Check for an approval card, an unanswered question, a queued message, or a connection error. If a host is still starting, wait for its status to change. |
| I lost the connection. | Restore your connection and reopen the same session. Check its latest messages before resending a task; the agent may already have acted. |
| I cannot find a session. | Confirm that you are on the same server and account. Search the sidebar, check archived sessions, or ask the owner to confirm your access. |
| My teammate cannot open a shared link. | Confirm that they can reach the server, sign in, and have permission to access the session. |
| An event did not start work. | Check that the subscription is active, the repository and event filters match, and the host or sandbox can run. Ask the operator to inspect webhook delivery and launch logs. |
| A scheduled task did not run. | Check its timezone, next run, enabled state, host availability, and run history. Look for an approval request in any session it created. |
| Usage is blank or incomplete. | Confirm the reporting window and that the chosen agent reports usage. Missing metrics do not establish that a run was free. |
| A file is missing from my computer. | Check the session workspace. Files created remotely do not automatically appear on your device. |

When requesting help, include what you were trying to do, the visible error,
the approximate time, and the session link if your support contact has access.
Do not include passwords or API keys.

## Administrator setup

Browser users need a reachable server, an account, an available agent and model
provider, and an execution host or managed sandbox. To enable shared workflows,
configure authentication, repository integration, policies, and cost controls
for your deployment.

- Start with the [deployment guide](https://github.com/juanpasaflipz/wakevane/blob/dist/deploy/README.md) or
  [Railway deployment instructions](https://github.com/juanpasaflipz/wakevane/blob/dist/deploy/railway/README.md).
- Configure repository access using [GitHub App setup](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/GITHUB_APP_SETUP.md).
- Configure action gates and budgets in the [policies reference](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/POLICIES.md).
- Enable event workflows with the [subscriptions guide](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/SUBSCRIPTIONS.md).
- For the design-review beta, follow its [dedicated guide](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/DESIGN_REVIEW.md).
  That workflow currently requires a connected computer; browser-only execution
  is not part of that beta.

## Related documentation

- [Project overview and installation](https://github.com/juanpasaflipz/wakevane/blob/dist/README.md)
- [Deployment guide for administrators](https://github.com/juanpasaflipz/wakevane/blob/dist/deploy/README.md)
- [Policies reference](https://github.com/juanpasaflipz/wakevane/blob/dist/docs/POLICIES.md)
- [Web development guide](https://github.com/juanpasaflipz/wakevane/blob/dist/web/README.md)
