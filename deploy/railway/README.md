# WakeVane on Railway

Deploy WakeVane, a distribution of Omnigent, to Railway: the server and web UI built from
this repository, next to a managed Postgres, served over HTTPS on
`*.up.railway.app`. A hosted server is also what makes subscriptions useful,
because GitHub and Slack can only deliver events to a public URL.

## One command (Railway CLI)

```bash
railway login
deploy/railway/deploy.sh my-wakevane
```

The script creates the project, adds Postgres, adds an `omnigent` service built
from `juanpasaflipz/wakevane@dist`, wires `DATABASE_URL`, attaches a volume at
`/data`, generates an admin password, a GitHub webhook secret, and the credential
encryption key, and prints a public domain. The first build compiles the web UI and the server and takes
several minutes; follow it with `railway logs --build`. Sign in as `admin` with
the printed password.

Railway's GitHub app must be allowed to read the repository. If `railway add`
reports it cannot access the repo, grant access at
`github.com/settings/installations` and rerun.

## What gets provisioned

- **omnigent**: a web service built from source by `railway.toml`, which
  points at `deploy/docker/Dockerfile`. Its `web-builder` stage compiles the
  web UI and the final `runtime` stage is the server, so no prebuilt image is
  needed.
- **Postgres**: a Railway-managed database, referenced from the service as
  `DATABASE_URL=${{Postgres.DATABASE_URL}}`.
- **A volume at `/data`** holding artifacts (`ARTIFACT_DIR=/data/artifacts`),
  admin credentials, and server state (`OMNIGENT_DATA_DIR=/data/omnigent`),
  which includes the shim registry's firing log and first-seen record. Without
  it every redeploy forgets which shims have fired.

The flywheel features (`subscriptions`, `usage_page`, `shims_page`) are on by
default; nothing needs to be set to enable them.

## From the dashboard instead

1. **New Project, Deploy from GitHub repo**, pick your fork of this repository
   and set the branch to `dist`. Add a **Postgres** database to the project.
2. On the **omnigent** service, open **Variables** and add:

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | `OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME` | `admin` |
   | `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` | output of `openssl rand -hex 16` |
   | `OMNIGENT_GITHUB_WEBHOOK_SECRET` | output of `openssl rand -hex 32` |
   | `OMNIGENT_CREDENTIAL_ENC_KEY` | output of `openssl rand -base64 32` |
   | `OMNIGENT_DATA_DIR` | `/data/omnigent` |
   | `OMNIGENT_ADMIN_CREDENTIALS_PATH` | `/data/admin-credentials` |
   | `OMNIGENT_FRAME_ANCESTORS` | `'self'` |

   Opening the server to public signup needs more than these; see
   [Running WakeVane as a public service](../../docs/PUBLIC_LAUNCH.md).

3. Add a **Volume** to the service mounted at `/data`.
4. Under **Settings, Networking**, generate a domain. Sign in as `admin`.

> **Back up `OMNIGENT_CREDENTIAL_ENC_KEY`.** It encrypts stored GitHub tokens
> and other connected-account credentials. If it is lost they cannot be
> decrypted and every user has to reconnect. To rotate it, see
> [Encrypting stored tokens](../../docs/GITHUB_APP_SETUP.md#encrypting-stored-tokens).

> **Set the admin username too.** Without `OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME`
> the first admin is named after the container's OS user, which is `root`.

> **Set the admin password before the first boot.** While no account exists,
> `POST /auth/setup` is unauthenticated, so an instance exposed without
> `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` can be claimed by its first visitor.

> **`HOST` is handled automatically.** Railway injects `HOST=[::]`; the
> entrypoint detects Railway and binds `0.0.0.0`. If the domain returns
> "Application failed to respond", set the domain's target port under
> **Settings, Networking** to the `PORT` shown in the boot log.

The cookie secret is auto-minted and `OMNIGENT_ACCOUNTS_BASE_URL` is detected
from `RAILWAY_PUBLIC_DOMAIN`.

## Release features

In the Omnigent service's **Variables** tab, set `OMNIGENT_FEATURES` to a
comma-separated overrides such as `usage_page`: names enable, `-name` disables,
and empty uses defaults. Railway redeploys the service automatically. Add
`-feature_name` to roll back, then reload the web app. Runner overrides such as
`-claude_osc_status` also need to be set on separately running hosts; restart
their host/runners and launch fresh terminals. See [`designs/FEATURE_FLAGS.md`](../../designs/FEATURE_FLAGS.md) for known
keys.

## Use your own IdP instead (OIDC)

Prefer GitHub / Google / Okta login over built-in accounts? Switch the provider
in the service Variables. OIDC requires HTTPS — Railway provides it
automatically on `*.up.railway.app`. If you set a custom domain, point it at
your project before completing these steps.

### GitHub OAuth (simplest to register)

1. Go to `github.com/settings/developers` → **New OAuth App**.
   - Homepage URL: `https://<project>.up.railway.app`
   - Authorization callback URL: `https://<project>.up.railway.app/auth/callback`
   - Click **Register application**, then **Generate a new client secret**.

2. In your Railway project, open the **omnigent** service → **Variables**
   and add:

   | Variable | Value |
   |---|---|
   | `OMNIGENT_AUTH_PROVIDER` | `oidc` |
   | `OMNIGENT_OIDC_ISSUER` | `https://github.com` |
   | `OMNIGENT_OIDC_CLIENT_ID` | your GitHub OAuth client ID |
   | `OMNIGENT_OIDC_CLIENT_SECRET` | your GitHub OAuth client secret |
   | `OMNIGENT_OIDC_REDIRECT_URI` | `https://<project>.up.railway.app/auth/callback` |
   | `OMNIGENT_OIDC_COOKIE_SECRET` | output of `openssl rand -hex 32` |

3. Railway redeploys automatically. Visit the URL — you'll be redirected to
   GitHub to log in.

### Google Workspace

| Variable | Value |
|---|---|
| `OMNIGENT_AUTH_PROVIDER` | `oidc` |
| `OMNIGENT_OIDC_ISSUER` | `https://accounts.google.com` |
| `OMNIGENT_OIDC_CLIENT_ID` | `…apps.googleusercontent.com` |
| `OMNIGENT_OIDC_CLIENT_SECRET` | your client secret |
| `OMNIGENT_OIDC_REDIRECT_URI` | `https://<project>.up.railway.app/auth/callback` |
| `OMNIGENT_OIDC_COOKIE_SECRET` | output of `openssl rand -hex 32` |
| `OMNIGENT_OIDC_ALLOWED_DOMAINS` | `example.com` (critical — see note below) |

> **Important:** Without `OMNIGENT_OIDC_ALLOWED_DOMAINS`, any Google account
> can log in when the OAuth consent screen is "External." Always restrict to
> your domain.

### Generic OIDC (Okta, Auth0, Keycloak, Entra ID)

Set `OMNIGENT_OIDC_ISSUER` to your IdP's base URL (the one that publishes
`/.well-known/openid-configuration`). The rest of the variables are the same
as above.

## Custom domain

In your Railway project, open **Settings** → **Domains** → **Add domain**.
Point your DNS A/AAAA record at the Railway-assigned address. Railway
provisions a Let's Encrypt cert automatically.

Update `OMNIGENT_OIDC_REDIRECT_URI` to use the custom domain after DNS
propagates.

## Upgrading

The service deploys from the `dist` branch, so Railway rebuilds on every push
to it. To redeploy by hand, open the **omnigent** service and choose
**Deploy, Redeploy**, or run `railway redeploy --service omnigent`.

## Cost

Railway Hobby plan: ~$5/month base + per-minute CPU/memory usage. A lightly
loaded Omnigent instance (few concurrent users) typically stays under
$10–15/month total including the Postgres plugin.

## Publishing the template

A published Railway template turns this into a single "Deploy on Railway"
button. It is created from a working project:

1. Deploy once with `deploy.sh` or the dashboard steps above.
2. Create a draft from the project, either with the dashboard's **Generate
   Template** action or `railway templates create` while linked to it.
3. In the draft, replace the generated secrets with template functions so
   every deploy gets its own:

   | Variable | Template value |
   | --- | --- |
   | `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` | `${{secret(32, "abcdef0123456789")}}` |
   | `OMNIGENT_GITHUB_WEBHOOK_SECRET` | `${{secret(64, "abcdef0123456789")}}` |
   | `OMNIGENT_CREDENTIAL_ENC_KEY` | `${{secret(43, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")}}` |

   43 base64url characters decode to the 32-byte key the server requires.

4. Publish it and put its `railway.com/new/template/<code>` link in the README
   badge.
