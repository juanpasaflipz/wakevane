# Per-user integration credential store

## Motivation

The GitHub App integration needs to persist a per-user OAuth token set and vend
it to managed sandboxes on demand (see the credential broker below). That secret
material must be encrypted at rest, per user, with the key never sitting in the
app process.

Two forces shape the design:

1. **More providers.** Connecting MCP integrations (Datadog, Slack, a hosted
   GitHub MCP, …) needs the same shape: a per-user secret + some non-secret
   metadata, connected once and re-vended to sandboxes. We do not want one
   bespoke table per provider.
2. **Key material that fits the deployment.** A self-hoster on Railway or a VM
   has no cloud KMS, while a hosted deployment wants audit and revocation. The
   secret material is therefore encrypted with a **local master key** by default
   and with **AWS KMS** (or Vault Transit) as the upgrade, behind one envelope
   format, and every ciphertext is bound to its row's identity.

Getting the schema generic now is cheap; reshaping a provider-specific table
after users have connected means migrating encrypted rows. Hence this store
lands at the base of the integration stack, with GitHub as its first (and,
today, only) provider.

## Two independent axes

The design deliberately separates two concerns that are easy to conflate:

| Axis | What varies | This design |
| --- | --- | --- |
| **Data model** | "a credential for *any* provider, per user" | one generic table + typed façades per provider |
| **Secret backend** | *where* the key lives and *who* encrypts | a `SecretCipher` port; local key, AWS KMS, and Vault Transit implement it |

Keeping them orthogonal means a new provider is a façade (no schema change) and
a different key backend (GCP/Azure KMS, Vault Transit) is a cipher adapter (no
schema change).

## Data model

One table, `connections`:

| column | type | notes |
| --- | --- | --- |
| `workspace_id` | bigint | tenant partition, part of PK (`0` = default) |
| `user_id` | str(128) | omnigent user, part of PK |
| `provider` | str(64) | e.g. `"github"`, part of PK |
| `account_id` | str(128) | provider account discriminator, part of PK; `""` = the user's single account for that provider |
| `secret_enc` | text | cipher output (see [Ciphertext format](#ciphertext-format)) of a JSON blob holding *all* secret material (access token, refresh token, …) |
| `metadata_json` | text | non-secret provider metadata (login, ids, scopes, expiries, …) as JSON |
| `created_at` / `updated_at` | integer | unix epoch seconds (repo convention; `workspace_id` is the lone `bigint`) |

Primary key: `(workspace_id, user_id, provider, account_id)`.

Why a JSON secret blob rather than typed token columns: any provider's secret
shape (single API key, OAuth access+refresh, client-cert, …) fits one encrypted
column, and the ciphertext is opaque to SQL. Non-secret fields that a provider
needs at refresh time (token expiries, granted scopes, the connected login/id)
live in `metadata_json` — queryable enough for our access patterns (we always
load the whole connection) without widening the schema per provider.

The `account_id` column is `""` today (one account per provider per user) but is
in the PK so a future "connect two GitHub orgs" needs no migration.

## `SecretCipher` port

```python
class SecretCipher(Protocol):
    def encrypt(self, plaintext: str, *, context: Mapping[str, str]) -> str: ...
    def decrypt(
        self, ciphertext: str, *, context: Mapping[str, str]
    ) -> str | None: ...  # None on mismatch
```

`context` is the row's identity (`workspace`/`user`/`provider`/`account`) and is
**required** — every secret is bound to a user; there is no unbound/global-key
path. The store depends on this port, not on a concrete backend, so the KMS
implementation can be swapped for GCP/Azure KMS or Vault Transit without a
schema change.

Three backends implement it:

| Backend | Selected by | Write path |
| --- | --- | --- |
| `local` (`LocalSecretCipher`) | `OMNIGENT_CREDENTIAL_ENC_KEY` | envelope; data key wrapped by the local master key with AES-256-GCM |
| `kms` (`KmsSecretCipher`) | `OMNIGENT_CREDENTIAL_KMS_KEY_ID` | envelope; data key from `kms:GenerateDataKey` with `context` as the encryption context |
| `vault` (`VaultSecretCipher`) | `OMNIGENT_CREDENTIAL_VAULT_KEY` | Vault Transit encrypt on a derived key (`osvault:` format, unchanged) |

`build_secret_cipher()` is the single seam where a deployment selects the
backend. `OMNIGENT_CREDENTIAL_CIPHER` (`local` | `kms` | `vault`) picks one
explicitly and then requires its key; unset, the single configured backend is
auto-detected, more than one is an error, and none returns `None` — the store,
and every integration on it, is then disabled. **There is no plaintext
fallback.** A configured integration without a backend logs
`… is configured but disabled` with the three options at startup.

### Ciphertext format

The local and KMS backends share one versioned, self-describing envelope:

```
env1:<backend>:<key_id>:<wrapped_key>:<nonce>:<ciphertext>
```

- `backend` is `local` or `kms`.
- `key_id` is base64url (unpadded) of the wrapping key's id: for `local`, the
  first 8 hex chars of SHA-256 of the master key (a fingerprint, never the key);
  for `kms`, the configured key id/ARN/alias.
- `wrapped_key` is the per-value 256-bit data key, wrapped by the backend. For
  `local` it is `wrap_nonce (12 bytes) ‖ AES-256-GCM(master, data_key)`; for
  `kms` it is the `CiphertextBlob` from `GenerateDataKey`.
- `nonce` is the 96-bit AES-GCM nonce, and `ciphertext` the AES-256-GCM output
  (16-byte tag appended) of the UTF-8 secret under the data key.

All binary fields are unpadded base64url, so `:` only separates fields. Every
`encrypt` draws a fresh random data key and nonce. The associated data on both
AES-GCM layers is `env1:<backend>:<key_id>` plus a newline and the row identity
as sorted-key JSON (empty values dropped, as KMS and Vault do), so a value moved
to another row, backend, or key id fails authentication; for KMS the identity is
also the KMS encryption context on the data key. The `env1:` prefix cannot be
confused with Vault's `osvault:` envelope, and a legacy KMS value (written by
the earlier direct `kms:Encrypt` code) is plain standard base64, whose alphabet
has no `:`. Values without a recognized prefix are read as legacy KMS values,
which still decrypt; envelopes also remove the old 4096-byte `kms:Encrypt`
limit.

### Failure semantics

Per-row problems — a tampered value, a mismatched identity, a malformed
envelope — make `decrypt` return `None` (⇒ reconnect), matching the KMS and
Vault soft-fail. Store-wide problems raise: a value naming a local key id that
is not configured (the message names the id), a value from a backend this
server cannot read, a malformed `OMNIGENT_CREDENTIAL_ENC_KEY` (rejected at
startup with the `openssl rand -base64 32` hint), and operational KMS/Vault
errors. Nothing logs key material or plaintext.

### Threat model

A local key protects stored secrets against a leaked database or backup, but
not against someone who can read the server's environment, since the key sits
there. KMS keeps the master key out of the process and adds an audit trail
(CloudTrail on every `Decrypt`) and revocation (disable the key), at the cost of
an AWS dependency.

### Keys, rotation, and migration

`OMNIGENT_CREDENTIAL_ENC_KEY` is the current local master key: 32 random bytes,
base64 (standard or urlsafe, padding optional). `OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS`
is an optional comma-separated list of retired keys used for decrypt only;
encrypt always uses the current key, and decrypt picks the key by id.

Decrypt dispatches on each value's backend prefix. When the selected backend is
not the only one with key material configured, `build_secret_cipher()` returns a
`MultiBackendSecretCipher` that writes with the selected backend and reads the
others; local keys listed only in `…_PREVIOUS` count as read-only material. So:

- **Rotate a local key:** set the new key as `OMNIGENT_CREDENTIAL_ENC_KEY`, move
  the old one into `OMNIGENT_CREDENTIAL_ENC_KEYS_PREVIOUS`, redeploy, run
  `omnigent credentials rewrap`, then remove the old key.
- **Local → KMS:** set `OMNIGENT_CREDENTIAL_KMS_KEY_ID`, move the local key into
  `…_PREVIOUS` (or keep it current and set `OMNIGENT_CREDENTIAL_CIPHER=kms`),
  redeploy, rewrap, then drop the local key.
- **New KMS key:** point `OMNIGENT_CREDENTIAL_KMS_KEY_ID` at it while the server
  can still decrypt with the old key, then rewrap.

`omnigent credentials rewrap [--dry-run] [--database-uri URI]` walks
`connections.secret_enc` — the only column holding cipher output, shared by the
GitHub, Databricks, MCP and pending MCP OAuth rows — across all workspaces and
re-encrypts any value whose backend/key differs from the current write key
(legacy KMS values always qualify). Each row is a separate transaction that only
updates if the value is unchanged since the scan, so a crash or a concurrent
token refresh is safe and a rerun is a no-op. It reports counts per provider and
per source `backend:key_id`; a row it cannot read is reported and skipped, and
the command exits non-zero.

The key belongs to the **credential store**, not to any one provider: every
provider façade shares the one cipher, so connecting an MCP server or Datadog
account needs no provider-specific key and does not depend on GitHub being
configured. Provider config gates that provider's routes, not the store's
ability to encrypt.

### What the encryption context buys (and what it doesn't)

The *tenancy boundary* is the primary key and the `workspace_id`-scoped queries
— that is what stops user A reading user B's row, and it holds regardless of the
cipher. The KMS encryption context is not an access-control boundary; it is
cryptographic binding. What it buys:

- **The master key never enters the process.** KMS does the crypto inside its
  HSM; the server holds only an IAM permission (via IRSA). A stolen DB dump *and*
  a compromised server yield no exportable key — an attacker can only call
  `Decrypt` while the pod's credentials are valid, which is logged in CloudTrail
  and revocable.
- **Per-row binding.** A ciphertext can only be decrypted by presenting the same
  `{workspace, user, provider, account}` context, so a blob can't be replayed
  under a different identity, and a leak is scoped to one row.
- **Rotation / revocation / audit** are KMS key-policy operations, not app
  changes: rotate the key (versions, no re-encrypt), disable it to stop all
  decrypts, read every call in CloudTrail. An encryption-context *condition* in
  the key policy can further constrain what the server may decrypt.

For KMS: empty-valued context fields (`account_id=""`, the default single account) are
dropped before the KMS call — KMS rejects empty encryption-context values — done
consistently on encrypt and decrypt, so the binding is unchanged and a named
account still yields a distinct context.

`decrypt` returning `None` (not raising) on a ciphertext/context mismatch
(`InvalidCiphertextException` / `IncorrectKeyException`) is load-bearing: a
rotated-away key degrades to "reconnect this integration," never a 500 on the
vend path. Operational failures — access denied, disabled key, throttling —
propagate instead, since telling the user to reconnect wouldn't help and would
hide a misconfiguration.

### Pod → KMS auth (IRSA)

On the demo/prod EKS cluster the server runs as the `omnigent-server`
ServiceAccount, annotated with an IAM role (`eks.amazonaws.com/role-arn`) whose
policy allows `kms:GenerateDataKey`/`kms:Decrypt`/`kms:DescribeKey` on the one
credential key (`kms:Encrypt` is no longer used; keep `kms:Decrypt` for legacy
values until they are rewrapped). No AWS credentials sit in the pod; the KMS key ARN is passed as
plain config (`OMNIGENT_CREDENTIAL_KMS_KEY_ID`), not a secret. The key + role +
policy are provisioned in the infra repo (Pulumi, `cloud-infra/core`).

## Layers

The stack has four layers, each with one job:

```
route layer            provider-specific writes, uniform credential reads
   │
   ▼
ConnectionStore(ABC)   per-provider façade: maps the provider surface ⇄ a row
   │                   (GithubConnectionStore, DatabricksConnectionStore, …)
   ▼
CredentialStore        the one concrete DB backend (SQLAlchemy) — uniform writes
   │
   ▼
SecretCipher(Protocol) encrypt/decrypt the secret blob (local / KMS / Vault)
```

- **`CredentialStore`** is the generic, provider-agnostic persistence: rows keyed
  by `(workspace_id, user_id, provider, account_id)`, secret blob + metadata,
  returning plain `ProviderConnection` entities.
- **`ConnectionStore(ABC)`** is the per-provider façade base. It owns a
  `CredentialStore`, carries the provider key (`_PROVIDER`), and factors the
  uniform reads (`get`, `delete`, `list_all`) so a concrete façade only sets
  `_PROVIDER`, implements `_to_entity`, and adds its provider-specific writes.
  Callers (routes, broker, launch path) talk to the façade, never the generic
  store, so nothing changes when a second provider is added.

**This base PR ships `CredentialStore` + `ConnectionStore` + `SecretCipher`; the
first concrete façade lands in the connect PR (#4235):**

- `GithubConnectionStore(ConnectionStore[GithubConnection])` — `provider="github"`;
  secret = `{access_token, refresh_token}`; metadata = `{github_login,
  github_user_id, token_expires_at, refresh_token_expires_at, scopes}`; adds
  `upsert` / `update_tokens` and implements `_to_entity`.

A future MCP-credential façade is the same pattern with `provider="mcp:<name>"`.

## Migration

This PR adds a single new migration (`add_connections`) that
`CREATE`s `connections` with the generic schema. There is no
provider-specific table in the tree to reshape — the generic store lands at the
base of the stack, before any such table existed, which is the whole point of
sequencing it first. The migration chains off the current `upstream/main` head
(the branch is kept rebased on main) so the branch and the PR merge both resolve
to a single Alembic head; a test guards that
(`tests/db/test_migration_connections.py`).

## Broker endpoint (provider-generic)

The credential broker is generic over providers:
`GET /v1/hosts/{host_id}/credentials/{provider}` resolves the launch token to
the session owner and returns that provider's secret + attribution metadata
(the git credential helper and the GitHub MCP proxy pass `provider=github`).
One route (`routes/host_credentials.py`) serves every provider that registers a
`credential_resolver` on the `ConnectionProvider` registry; a provider with no
resolver — connect-only, or on-demand delivery not built yet — returns `404`. A
new provider is one registry entry, not another hand-copied route + client. The
resolver is best-effort: a fault degrades to `{"connected": false}` rather than
a 500, and the token is never persisted in the sandbox.

## Revocation & audit note

A vended GitHub token's blast radius is bounded by the launch token: the broker
resolves `(host_id, launch_token)` server-side and stops vending the moment the
launch token expires or the host row is deleted, and the raw token never touches
sandbox disk. KMS adds its own layer — CloudTrail logs every `Decrypt`, and
disabling the key stops all decrypts globally — but KMS does not know about
launch tokens. So "stops vending when the session ends" must remain a
server-side check on the broker path, independent of KMS; KMS is defence in
depth on the storage, not the session boundary.

## Dependencies

The credential store is backend-agnostic. AES-GCM for the envelope comes from
`cryptography`, a core dependency; the cloud backends declare their own extra,
imported lazily:

- **AWS KMS** (`KmsSecretCipher`) needs boto3 — `omnigent[kms]`.
- **HashiCorp Vault** (`VaultSecretCipher`, Transit) needs hvac — `omnigent[vault]`.

boto3 / hvac load only when the matching backend is selected
(`OMNIGENT_CREDENTIAL_KMS_KEY_ID` / `OMNIGENT_CREDENTIAL_VAULT_KEY`), so a deployment
that doesn't enable the credential store — or uses the other backend — needs neither.

`OMNIGENT_CREDENTIAL_CIPHER` (`local` | `kms` | `vault`) selects the backend
explicitly per server; the chosen backend's key env var is then required. Leave it
unset to auto-detect the single configured backend — configuring more than one
without the selector is an error (no silent precedence), and configuring none
disables the store.

## MCP auth & roadmap

The store is the base for two credential needs; the encryption above is shared,
the delivery differs.

**MCP auth → Databricks Unity Gateway (AIGW), primary.** Routing MCP through AIGW
makes per-MCP OAuth *Databricks's* problem, not Omnigent's. The store then holds
only a small `provider="databricks"` exchange grant (per-user, KMS-encrypted);
the server performs the omni→databricks token exchange + refresh, and the
sandbox's MCP proxy fetches a short-lived AIGW token from the broker per
connection, never persisted. Leave an "MCP connection backend" seam (mirroring
the `SecretCipher` port) so a self-hosted OSS layer (e.g. Nango) can slot in
later.

**GitHub clone stays on the broker.** MCP can't `git clone`, so the GitHub App
token remains in the store and the broker vends it into the sandbox for git
(the credential-helper path); a wrapper `gh` CLI can do the Open-in-Omnigent PR
stamp instead of the MCP proxy.

### Phased plan

1. Generic per-user store + KMS cipher behind `SecretCipher`. *(this PR)*
2. KMS key + IRSA role provisioned in infra (Pulumi); wired into the demo
   deployment. *(connect PR / infra)*
3. AIGW MCP track: `provider="databricks"` + omni→databricks exchange/refresh +
   AIGW MCP proxy + the sandbox Databricks plugin behind the connection seam.

### Open questions

- **Data-key caching:** KMS envelopes still make one `Decrypt` call per vend.
  Caching unwrapped data keys per `(context, process)` would cut that, at the
  cost of plaintext keys in memory.
- **Encryption-context IAM condition:** whether to constrain the server's key
  policy with a `kms:EncryptionContext:*` condition (tighter, but couples the
  policy to the identity shape) or keep it key-scoped (simpler).
- **Databricks token exchange:** what the server exchanges the omni identity for
  (AIGW OAuth token / RFC 8693 token exchange / Databricks U2M/M2M grant) —
  drives the `provider=databricks` row shape and refresh cadence.
