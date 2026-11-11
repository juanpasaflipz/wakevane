# Sandbox provider keys

A managed sandbox runs on the inference the operator configured. On a
deployment that funds only Claude, that leaves the Codex harnesses with no
credential, so an orchestrator has one vendor to work with and cross-vendor
review cannot run. With the `sandbox_provider_keys` feature a user can save
their own OpenAI API key, and the sessions they create afterwards can run
Codex in their sandboxes on it. OpenAI bills the user directly; none of that
usage passes through the funded gateway or the user's credit.

## Turning it on

```
OMNIGENT_FEATURES=...,sandbox_provider_keys
```

The feature also needs multi-user authentication and a credential store
cipher (`OMNIGENT_CREDENTIAL_ENC_KEY`, or the KMS or Vault backend). Without
them the server logs why and does not mount the routes. The flag is read at
startup.

## What a user does

Settings → Sandbox Integrations → Model providers → paste the key → Save.
The card then shows the key's last four characters. Replace it by pasting a
new one; remove it with **Remove**.

## What changes for a session

When a user with a saved key creates a managed session, the session's saved
inference profile gains a provider named `your-openai-key`, bound to the
`codex` and `codex-native` harnesses.

- An operator binding always wins. A harness the operator bound keeps its
  provider, and nothing is added when both Codex harnesses are bound.
- The model defaults to the build's Codex launch default. The card's **Codex
  model** field names another model id to use instead, and can be changed
  without pasting the key again. OpenAI must offer that model to the key's
  organization, with a rate limit large enough for Codex's requests. A model
  change applies to sessions created afterwards.
- Child sessions inherit the profile, so an orchestrator's Codex worker gets
  the binding.
- A session created before the key was saved is unchanged. So is a session
  created after the key was removed.

## Where the key goes

- **At rest:** encrypted in the credential store, under the provider
  `openai-api-key`.
- **In the profile:** an environment reference (`env:WAKEVANE_USER_OPENAI_API_KEY`)
  only. The key is never written to the session snapshot or the sandbox
  config file.
- **At launch:** as launch-only environment for the sandbox host, the way the
  funded trial's gateway token travels. This covers a first launch, a
  relaunch, a wake and a scheduled or event-triggered launch.
- **Never:** in an API response, or in a design review sandbox.

Anything running inside the sandbox can read its environment, so the key is
exposed to the agent processes there for as long as the sandbox lives. That is
the same boundary the GitHub credential broker has. Removing the key stops
later launches from receiving it; it does not reach into a running sandbox.

## Routes

- `GET /v1/provider-keys/openai` — `{connected, key_hint, models, updated_at}`.
- `PUT /v1/provider-keys/openai` — body `{"api_key": "...", "models": []}`.
- `PATCH /v1/provider-keys/openai` — body `{"models": []}`; keeps the saved key.
- `DELETE /v1/provider-keys/openai`.

Each acts on the caller's own key.
