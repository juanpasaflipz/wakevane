#!/usr/bin/env bash
# Stand up WakeVane on Railway with one command.
#
# Creates a project with a Postgres database and an `omnigent` service built
# from this repository's `dist` branch (railway.toml builds the source
# Dockerfile, web UI included), wires DATABASE_URL, attaches a volume for
# artifacts and server state, generates an admin password, a GitHub webhook
# secret, and the key that encrypts stored credentials, and gives the service a
# public *.up.railway.app domain.
#
# Requirements: the Railway CLI, logged in (`railway login`), and Railway's
# GitHub app allowed to read the repository.
#
#   deploy/railway/deploy.sh [project-name]
#
# Environment overrides: OMNIGENT_REPO (owner/name), OMNIGENT_BRANCH.
set -euo pipefail

project_name="${1:-wakevane}"
repo="${OMNIGENT_REPO:-juanpasaflipz/wakevane}"
branch="${OMNIGENT_BRANCH:-dist}"
service="omnigent"

command -v railway >/dev/null || { echo "Install the Railway CLI first: https://docs.railway.com/cli" >&2; exit 1; }
railway whoami >/dev/null 2>&1 || { echo "Run 'railway login' first." >&2; exit 1; }

random_secret() { openssl rand -hex "$1"; }
admin_password="$(random_secret 16)"
webhook_secret="$(random_secret 32)"
# 32 random bytes, base64: the local key that encrypts stored GitHub tokens.
credential_key="$(openssl rand -base64 32)"

echo "==> Creating project ${project_name}"
railway init --name "$project_name" >/dev/null

echo "==> Adding Postgres"
railway add --database postgres >/dev/null

# Order matters: every variable, secrets included, is in place before the repo
# is connected, so the first boot already has an admin password. Otherwise the
# instance could be claimed by its first visitor through POST /auth/setup.
echo "==> Adding an empty ${service} service"
# The ${{Postgres.DATABASE_URL}} reference is resolved by Railway, not the shell.
# shellcheck disable=SC2016
railway add --service "$service" \
  --variables 'DATABASE_URL=${{Postgres.DATABASE_URL}}' \
  --variables "OMNIGENT_AUTH_PROVIDER=accounts" \
  --variables "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME=admin" \
  --variables "OMNIGENT_ACCOUNTS_AUTO_OPEN=0" \
  --variables "OMNIGENT_DATA_DIR=/data/omnigent" \
  --variables "OMNIGENT_ADMIN_CREDENTIALS_PATH=/data/admin-credentials" \
  --variables "ARTIFACT_DIR=/data/artifacts" \
  --variables "OMNIGENT_FRAME_ANCESTORS='self'" >/dev/null

railway service link "$service" >/dev/null

# Secrets go in through stdin: `railway add --variables` echoes every value.
set_secret() {
  printf '%s' "$2" | railway variables set "$1" --stdin --service "$service" --skip-deploys >/dev/null
}
set_secret OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD "$admin_password"
set_secret OMNIGENT_GITHUB_WEBHOOK_SECRET "$webhook_secret"
set_secret OMNIGENT_CREDENTIAL_ENC_KEY "$credential_key"
unset credential_key

echo "==> Attaching a volume at /data"
railway volume add --mount-path /data >/dev/null

echo "==> Connecting ${repo}@${branch}; this starts the first build"
railway service source connect --repo "$repo" --branch "$branch" --service "$service" >/dev/null

echo "==> Generating a public domain"
railway domain --service "$service"

cat <<EOF

Done. The first build compiles the web UI and the server, so it takes several
minutes. Follow it with:  railway logs --build

Sign in as user "admin" with this password (shown once; it is also stored in
the service's Variables as OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD):

  ${admin_password}

GitHub webhook secret for subscriptions (Variables: OMNIGENT_GITHUB_WEBHOOK_SECRET):

  ${webhook_secret}

Back up OMNIGENT_CREDENTIAL_ENC_KEY from the service's Variables now. It
encrypts stored GitHub tokens; if it is lost they cannot be recovered and every
user has to connect GitHub again.
EOF
