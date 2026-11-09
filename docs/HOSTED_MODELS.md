# Hosted open models

Funded workspaces can select operator-priced Chat Completions models through
**Pi** in the web composer. Existing Claude bindings remain on Anthropic
Messages. Qwen and DeepSeek are the initial rollout candidates; Kimi and GLM
use the same configuration after passing the live acceptance checks below.
This does not replace existing OpenAI/Codex deployments.

## Configuration

Merge `chat_providers` and new `models` entries into the existing
`OMNIGENT_FUNDED_TRIAL_CONFIG`. Keep existing Claude prices, compute prices,
welcome-grant settings, and billing settings. Provider keys stay in server
secret variables; workspaces receive only a short-lived, account/session/model
scoped gateway token. No provider credential or upstream URL enters the catalog.

The following is a **synthetic configuration example**, not production prices:

```json
{
  "chat_providers": {
    "hosted": {
      "display_name": "OpenRouter",
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OMNIGENT_HOSTED_OPENROUTER_API_KEY"
    },
    "deepseek-direct": {
      "display_name": "DeepSeek",
      "base_url": "https://api.deepseek.com/v1",
      "api_key_env": "OMNIGENT_HOSTED_DEEPSEEK_API_KEY"
    }
  },
  "models": {
    "qwen-coder-reviewed-v1": {
      "provider": "hosted",
      "upstream_model": "REPLACE_WITH_VERIFIED_MODEL_ID",
      "upstream_provider": "REPLACE_WITH_VERIFIED_ENDPOINT_SLUG",
      "display_name": "Qwen Coder",
      "input": 2,
      "output": 4,
      "cache_read": 0.5,
      "cache_write": 2
    },
    "deepseek-reviewed-v1": {
      "provider": "deepseek-direct",
      "upstream_model": "REPLACE_WITH_VERIFIED_MODEL_ID",
      "display_name": "DeepSeek",
      "input": 2,
      "output": 4,
      "cache_read": 0.5,
      "cache_write": 2
    }
  }
}
```

All four rates are USD per million tokens and must be finite, nonnegative,
explicitly approved values. Set cache rates to the ordinary input rate if the
provider does not offer separate cache pricing. Do not infer cache discounts.
`upstream_provider` is an optional **OpenRouter endpoint slug**, not a WakeVane
provider name: it forces `only`, disables fallbacks, and requires support for
request parameters. Omit it for direct APIs. Clients cannot override routing,
activate billed server tools, request multiple completions, or add model fallbacks.

Review the actual endpoint's rates and token limits, including long-context
pricing tiers, before activation. The first implementation uses fixed per-token
prices, not tiered or per-request upstream pricing. Only enable endpoints whose
approved flat rates cover the permitted requests. Use a new public model ID for
pricing or upstream routing changes; retain the prior ID/rates while old sessions
remain active. Session snapshots preserve their selected IDs and displayed rates.

Start with hosted Qwen and DeepSeek. Add Kimi and GLM by adding further model
entries with reviewed endpoints/prices after acceptance. An omitted model is
unavailable. No catalog entry is enabled by default in code, and the feature does
not create provider accounts or install service secrets.

Advanced self-hosting uses another `chat_providers` entry whose base URL is
reachable from the server, ending at the API root (for example `/v1`). Put it
behind an authenticated endpoint and provide its secret variable. The same
explicit pricing, usage reporting, and acceptance requirements apply. A user's
browser `localhost` is not reachable as that user's machine from a hosted server.

## User workflow

1. Open a new managed chat, choose **Pi** in the agent menu, then choose a model.
2. Read the provider and input/output/cache rates shown beneath its name.
3. Send a task. The runtime uses that model through the metered gateway.
4. Reopen the session and check that the chosen model remains selected. Change
   between configured hosted models using the in-session model picker while idle.
5. The fork dialog also includes provider/rate details for its available models.

The initial hosted integration is text-only and uses Pi native. Image content
and provider-run billable tools are rejected. Client-executed coding tools are
supported. Other harnesses are offered only when they have their own compatible
inference binding; Chat Completions models are not advertised as Codex models.
Pi's native catalog retains exact IDs; the web catalog supplies display labels
and approved prices. New model settings apply to new managed workspaces; saved
session inference profiles remain immutable.

## Metering and failures

Every request reserves an output-bounded hold in the existing shared ledger.
Chat Completions prompt tokens include cached tokens; the gateway subtracts the
cached part before charging it at the cache rate. Completion counts include the
provider's reasoning tokens. Usage is requested on streaming responses.

A complete stream needs both valid usage and `[DONE]`. A missing final usage
record, an error frame, a disconnect, or an ambiguous transport failure keeps
the hold as conservative spend. Definitive connection failures and rejected
requests release it. Revoked credentials, exhausted grants, and unapproved models
fail before dispatch. There is no automatic fallback to another model or payer.

## Verification and activation

Synthetic coverage (no customer source or paid provider calls):

```sh
uv run --no-sync pytest tests/server/test_funded_trial_routes.py \
  tests/server/test_funded_trial_welcome.py tests/server/test_inference_catalog.py \
  tests/harnesses/pi_native/test_pi_native_credentials.py
pnpm --dir web test src/lib/composerModelLabel.test.ts \
  src/components/composer/ComposerConfigSections.test.tsx
pnpm --dir web run type-check
pnpm --dir web run lint
pnpm --dir web run build
```

Before enabling a model for users, use a staging account with a small funded
allowance and a synthetic repository. Ask it to read a file, change a function,
run a test, and explain the result. Check streamed text, tool arguments/results,
reasoning continuity on a second turn, cancellation, model switching, reopening,
and ledger settlement. Confirm the request uses the approved provider endpoint
and compare token counts with its usage report. Repeat for each exact model and
endpoint; mock protocol tests do not establish model quality or live compatibility.

Use the [composer feature map](../feature-map/composer.md) for both composers,
mobile, and the fork dialog. Browser/runtime verification must run in an isolated
instance following the linked verification skill. If local port binding or the
Pi CLI is unavailable, report that limitation rather than claiming live proof.

Protocol references: [OpenRouter requests](https://openrouter.ai/docs/api/reference/overview),
[OpenRouter streaming](https://openrouter.ai/docs/api/reference/streaming), and
[DeepSeek thinking/tool continuity](https://api-docs.deepseek.com/guides/thinking_mode/).
