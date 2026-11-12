"""Accounts auth routes: login, logout, me, invite, register, magic-link, members admin.

Mounted at ``/auth`` when ``OMNIGENT_AUTH_PROVIDER=accounts``.
Mutually exclusive with the OIDC auth router (which mounts at the
same prefix for OIDC mode) — only one is wired by ``create_app``.

The shape mirrors what the field has converged on for self-hosted
projects with built-in accounts (Immich / Gitea / n8n / Coolify /
Plausible — see ``designs/oss-cuj/01-research-summary.md`` §2.2.1):

- Login / logout via username + password (one cookie set on success).
- Invite-only signup — an admin mints a single-use copyable URL.
- No SMTP required — invite + admin-initiated reset cover the
  "I forgot my password" path.
- A short-TTL magic-link endpoint so the CLI can hand a signed-in
  session to the browser without the user typing their password
  on the laptop a second time.

Cookie machinery is shared verbatim with the OIDC router (same
HS256 ``__Host-ap_session`` JWT). The only difference is what
mints the token — here it's ``/auth/login`` validating a stored
password hash, not an IdP authorization code exchange.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, RedirectResponse, Response

from omnigent.db.account_authority import bind_account_authority, target_account_scope
from omnigent.errors import OmnigentError
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList, promote_if_listed
from omnigent.server.auth import _RESERVED_USERS, RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.auth_rate_limit import (
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_FAILURES_PER_ADDRESS,
    LOGIN_FAILURES_PER_ADDRESS_AND_ACCOUNT,
    REGISTRATIONS_PER_ADDRESS,
    RESET_REDEMPTIONS_PER_ADDRESS,
    RESET_REQUESTS_PER_ACCOUNT,
    RESET_REQUESTS_PER_ADDRESS,
    AttemptLimiter,
    client_address,
    rate_limiting_enabled,
)
from omnigent.server.funded_trial import FundedTrialPolicy
from omnigent.server.funded_trial_store import MICROS_PER_USD
from omnigent.server.mailer import SmtpMailer
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.password_reset import (
    RESET_TOKEN_TTL_SECONDS,
    mint_reset_token,
    read_reset_token,
)
from omnigent.server.passwords import (
    InvalidPasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from omnigent.server.signup_email import blocked_domain, normalize_email
from omnigent.stores.permission_store import PermissionStore

if TYPE_CHECKING:
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# Minimum password length enforced at the route layer. Matches
# NIST SP 800-63B §5.1.1.2 ("memorized secrets shall be at least
# 8 characters"). We don't enforce composition rules (mix of
# uppercase / digits / etc.) — current NIST guidance is to drop
# composition rules in favor of length + denylist.
_MIN_PASSWORD_LENGTH = 8

# Username constraints. Lowercase letters, digits, hyphens, dots,
# underscores; 1-64 chars. Restrictive on purpose — narrow the
# attack surface for log injection, terminal escape sequences,
# email-impersonation tricks. Real emails are also accepted (the
# @ + dot pattern matches the regex).
_USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$"


class LoginRequest(BaseModel):
    """Body of ``POST /auth/login``.

    :param username: The user's chosen username, lowercased
        before lookup.
    :param password: Plaintext password, length-validated against
        :data:`_MIN_PASSWORD_LENGTH` but otherwise opaque.
    :param issue_refresh: When ``True`` and a grant store is wired, also
        issue a login-scoped refresh grant and include ``refresh_token``
        in the response. Intended for CLI / unattended-host callers only
        (``omnigent login``); the web form never sends this field, so
        browser sessions never receive long-lived refresh material. Pydantic
        coerces non-bool values to bool, so only the literal JSON booleans
        ``true``/``false`` are accepted.
    """

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    issue_refresh: bool = False


class RegisterRequest(BaseModel):
    """Body of ``POST /auth/register``.

    :param invite: The invite token from the copyable URL. Omitted only on
        deployments with open signup enabled.
    :param username: The user's chosen username.
    :param password: The user's chosen password.
    :param email: Optional contact address, accepted on open signup only.
        Stored for follow-up; it is never a login identity.
    """

    invite: str | None = Field(default=None, min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64, pattern=_USERNAME_RE)
    password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)
    email: str | None = Field(default=None, max_length=254)


class SetupRequest(BaseModel):
    """Body of ``POST /auth/setup`` (first-run web admin claim).

    :param username: The chosen admin username, e.g. ``"alice"``.
        Same charset rules as registration.
    :param password: The chosen admin password.
    """

    username: str = Field(min_length=1, max_length=64, pattern=_USERNAME_RE)
    password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)


class InviteRequest(BaseModel):
    """Body of ``POST /auth/invite``.

    :param is_admin: Whether the resulting user should be created
        with admin rights. Defaults False; only admins can mint
        invites at all (route-level guard), and admin invites are
        a deliberate sub-case.
    """

    is_admin: bool = False


class ForgotPasswordRequest(BaseModel):
    """Body of ``POST /auth/password/forgot``.

    :param identifier: The account's username or its contact address,
        e.g. ``"ana"`` or ``"ana@example.com"``.
    """

    identifier: str = Field(min_length=1, max_length=254)


class ResetPasswordRequest(BaseModel):
    """Body of ``POST /auth/password/reset``.

    :param token: The token from the emailed reset link.
    :param new_password: Replacement password.
    """

    token: str = Field(min_length=1, max_length=2048)
    new_password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)


class ChangePasswordRequest(BaseModel):
    """Body of ``POST /auth/users/me/password``.

    :param old_password: Current password, verified before the
        change takes effect.
    :param new_password: Replacement password.
    """

    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)


def _set_session_cookie(
    response: Response,
    token: str,
    *,
    cookie_name: str,
    secure: bool,
    max_age_seconds: int,
) -> None:
    """Attach the session JWT to a response.

    Centralized so every cookie-setting site (login, register,
    magic redeem) uses the same attributes — divergence here is
    a recipe for "works in one route but not another" auth bugs.
    """
    # samesite="lax" is the right CSRF-safe default for a standalone deploy
    # (top-level navigation to the app's own domain). It does NOT work when the
    # app is embedded in a cross-origin iframe — e.g. Hugging Face Spaces' preview
    # pane — because browsers won't send a Lax cookie in a third-party frame, so
    # login appears to loop. The validated workaround is to open the app at its
    # direct URL (top-level tab). To support the embedded case, this would need a
    # "samesite=none; Secure" option gated behind an opt-in env var — deferred, as
    # it widens CSRF exposure and the direct-URL path already works.
    response.set_cookie(
        key=cookie_name,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response, *, cookie_name: str, secure: bool) -> None:
    """Delete the session cookie. Same attrs as the setter, by design."""
    response.delete_cookie(
        key=cookie_name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _validate_username(username: str) -> str | None:
    """Normalize + check a username; return error message or None.

    Lowercases, strips, then rejects reserved names + non-matching
    pattern. Reserved-name rejection here is belt-and-suspenders
    — the auth provider also rejects them at cookie validation
    time so the constraint can't be bypassed even if a row sneaks
    in via direct SQL.
    """
    norm = username.strip().lower()
    if norm in _RESERVED_USERS:
        return f"username {norm!r} is reserved"
    if not re.fullmatch(_USERNAME_RE, norm):
        return (
            "username must be lowercase letters / digits / "
            "dots / hyphens / underscores (or a lowercase email)"
        )
    return None


def _registration_source(request: Request, secret: bytes) -> str | None:
    """Opaque tag for the registering client, used to cap welcome grants.

    Prefers the address Cloudflare forwards (the proxy sits in front of the
    public deployment) and falls back to the socket peer. Only a keyed hash
    is stored, never the address itself.
    """
    address = client_address(request)
    if not address:
        return None
    return hashlib.sha256(secret + b":" + address.encode()).hexdigest()[:32]


def _redact_for_log(user_id: str) -> str:
    """Truncate a user id for log lines.

    Logs are aggregated centrally and we don't want full emails
    landing in DataDog / journald. Show prefix + length so a
    real incident can correlate without grepping the full PII.
    """
    if len(user_id) <= 4:
        return f"{user_id[:1]}***"
    return f"{user_id[:3]}***(len={len(user_id)})"


def create_accounts_auth_router(
    auth_provider: UnifiedAuthProvider,
    account_store: SqlAlchemyAccountStore,
    admin_list: AdminList,
    permission_store: PermissionStore | None = None,
    device_grant_store: DeviceGrantStore | None = None,
    scheduled_task_store: ScheduledTaskStore | None = None,
    funded_trial: FundedTrialPolicy | None = None,
    mailer: SmtpMailer | None = None,
) -> APIRouter:
    """Build the ``/auth/*`` router for the accounts provider.

    :param auth_provider: Must have ``_source == 'accounts'`` and
        ``_accounts_config`` populated.
    :param account_store: Accounts-specific persistence (user
        rows + invite/magic tokens). Sibling to PermissionStore;
        accounts routes never touch the latter so the
        PermissionStore interface stays stable for the internal
        hosted product. Required — there's no header fallback in
        accounts mode.
    :param admin_list: File-backed admin roster, consulted on each
        successful login/register/magic redeem to promote a listed
        username to admin (additive — see
        :mod:`omnigent.server.admin_list`).
    :param permission_store: Optional — used ONLY on a loopback
        single-user server's first-run ``/auth/setup`` to migrate the
        reserved ``local`` user's session grants to the new admin (so
        pre-accounts chats stay visible). ``None`` (and on non-loopback
        deploys) leaves session permissions untouched, preserving the
        accounts-routes-don't-touch-PermissionStore boundary for the
        hosted product.
    :param device_grant_store: When set, ``POST /auth/login`` issues a
        login-scoped refresh grant alongside the session JWT when the
        request body includes ``"issue_refresh": true``. The CLI sets
        this flag; the web browser form never does, so long-lived
        unattended credentials never reach a browser session. See
        :func:`omnigent.server.routes.device_auth.issue_login_grant`.
    :param scheduled_task_store: When set, ``DELETE /auth/users/{id}``
        disarms the scheduler timers of the deleted user's tasks (the
        rows themselves are disabled by ``AccountStore.delete_user``).
    :param funded_trial: When set and its config names a ``welcome_grant``,
        an open-signup account receives that trial grant once its email
        is verified (or at registration when no mailer is configured).
    :param mailer: Sends verification links; ``None`` disables email
        verification and the open-signup email stays optional.
    :returns: APIRouter to mount at ``/auth``.
    """
    if auth_provider._source != "accounts":
        raise RuntimeError("create_accounts_auth_router called with non-accounts provider")
    config = auth_provider._accounts_config
    if config is None:
        raise RuntimeError("accounts auth provider has no accounts configuration")
    router = APIRouter()

    _secure = config.secure_cookies
    _session_cookie = config.session_cookie_name
    _session_max_age = config.session_ttl_hours * 3600
    limiter = AttemptLimiter(enabled=rate_limiting_enabled())

    def _too_many_attempts(retry_after_seconds: int) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"error": "too many attempts, try again later"},
            headers={"Retry-After": str(retry_after_seconds)},
        )

    def _reissue_session_cookie(response: Response, user_id: str, generation: str) -> None:
        """Keep the caller signed in after rotating their own account generation."""
        bind_account_authority(user_id, generation)
        session_jwt = mint_session_cookie(
            user_id=user_id,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
            account_generation=generation,
        )
        _set_session_cookie(
            response,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )

    # ── Login / logout / me ───────────────────────────────────────

    @router.post("/login")
    async def login(request: Request, body: LoginRequest) -> Response:
        """Verify password and mint a session cookie.

        Returns 401 on any failure (unknown user, wrong password,
        malformed hash). The route does NOT distinguish between
        "no such user" and "wrong password" — same status code,
        same response body, same timing path (the password
        verify runs against a sentinel hash when the user is
        missing so timing comparison is constant). This matches
        OWASP authentication cheat-sheet guidance.
        """
        username = body.username.strip().lower()
        address = client_address(request)
        failure_keys = (
            (f"login:pair:{address}:{username}", LOGIN_FAILURES_PER_ADDRESS_AND_ACCOUNT),
            (f"login:address:{address}", LOGIN_FAILURES_PER_ADDRESS),
            (f"login:account:{username}", LOGIN_FAILURES_PER_ACCOUNT),
        )
        # Checked before any hashing so a blocked caller costs no argon2 work.
        retry_after = max(limiter.retry_after(key, limit) for key, limit in failure_keys)
        if retry_after:
            _logger.warning("auth/login: rate limited %s", _redact_for_log(username))
            return _too_many_attempts(retry_after)

        def _record_failure() -> None:
            for key, limit in failure_keys:
                limiter.record(key, limit)

        password_hash, generation = account_store.login_snapshot(username)
        if generation is not None:
            bind_account_authority(username, generation)
        # Always run a verify even on missing user — keeps the
        # response time roughly constant regardless of whether
        # the username exists. The dummy hash is the argon2
        # encoded form of "" which will never match a real
        # password, so the verify always raises here.
        if password_hash is None:
            with contextlib.suppress(InvalidPasswordError):
                verify_password(body.password, _DUMMY_HASH)
            _logger.info("auth/login: unknown user %s", _redact_for_log(username))
            _record_failure()
            return JSONResponse(status_code=401, content={"error": "invalid username or password"})

        try:
            verify_password(body.password, password_hash)
        except InvalidPasswordError:
            _logger.info("auth/login: bad password for %s", _redact_for_log(username))
            _record_failure()
            return JSONResponse(status_code=401, content={"error": "invalid username or password"})

        # Opportunistic rehash if argon2 parameters were upgraded.
        # Cheap and self-healing; failure is non-fatal (the user
        # still gets logged in with the old hash). Same password, so
        # the generation and the user's other sessions are kept.
        if needs_rehash(password_hash):
            try:
                account_store.update_password(username, hash_password(body.password))
            except Exception as exc:  # noqa: BLE001 — never fail login on rehash
                _logger.warning(
                    "auth/login: rehash failed for %s: %s",
                    _redact_for_log(username),
                    exc,
                )

        now = int(time.time())
        account_store.mark_logged_in(username, now)
        # Apply the file-backed admin list (additive — never demotes).
        # Runs after the row is known to exist so the set_admin UPDATE
        # matches; before get_user so the response reflects the flag.
        promote_if_listed(admin_list, account_store, username)

        session_jwt = mint_session_cookie(
            user_id=username,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
            account_generation=generation,
        )

        user = account_store.get_user(username)
        if user is None or user.account_generation != generation:
            return JSONResponse(status_code=401, content={"error": "invalid username or password"})
        body_payload: dict[str, object] = {
            "token": session_jwt,
            "expires_in": _session_max_age,
            "user": {"id": user.id, "is_admin": user.is_admin},
        }
        # Browser login must never receive refresh material — only CLI/device
        # flows do (via issue_refresh=True or the device-grant callback).
        # Gated on body.issue_refresh so the web form, which never sends
        # the field, cannot obtain long-lived unattended credentials even
        # under XSS or form-hijack (the browser has no way to set it True).
        if body.issue_refresh is True and device_grant_store is not None:
            from omnigent.server.routes.device_auth import issue_login_grant

            try:
                body_payload["refresh_token"] = issue_login_grant(
                    device_grant_store,
                    user_id=username,
                    cookie_secret=config.cookie_secret,
                )
            except Exception:  # grant failure must never break login
                _logger.exception(
                    "auth/login: refresh grant issuance failed for %s",
                    _redact_for_log(username),
                )
        if generation is None or not account_store.accepts_generation(username, generation):
            return JSONResponse(status_code=401, content={"error": "invalid username or password"})
        resp = JSONResponse(status_code=200, content=body_payload)
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        # A correct password clears this caller's strikes against the account;
        # the cross-account and cross-address counters keep running.
        limiter.reset(failure_keys[0][0])
        _logger.info("auth/login: success for %s", _redact_for_log(username))
        return resp

    @router.post("/logout")
    async def logout() -> Response:
        """Clear the session cookie. Always 204 (no body)."""
        resp = Response(status_code=204)
        _clear_session_cookie(resp, cookie_name=_session_cookie, secure=_secure)
        return resp

    @router.get("/me")
    async def me(request: Request) -> Response:
        """Return the current user's identity, or 401."""
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})
        user = account_store.get_user(user_id)
        if user is None:
            # Cookie validated but user row gone — admin deleted them
            # mid-session. Surface as 401 so the frontend re-logs in.
            return JSONResponse(status_code=401, content={"error": "user no longer exists"})
        return JSONResponse(
            status_code=200,
            content={
                "id": user.id,
                "is_admin": user.is_admin,
                "created_at": user.created_at,
                "last_login_at": user.last_login_at,
                "email": user.email,
                "email_verified_at": user.email_verified_at,
            },
        )

    # ── Invite + register (admin-issued, self-serve) ──────────────

    @router.post("/invite")
    async def invite(request: Request, body: InviteRequest) -> Response:
        """Mint a single-use invite URL (admin only)."""
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})

        token_id = secrets.token_urlsafe(32)
        now = int(time.time())
        account_store.create_token(
            token_id,
            kind="invite",
            user_id=None,
            created_by=admin_id,
            created_at=now,
            expires_at=now + config.invite_ttl_seconds,
            invited_is_admin=body.is_admin,
        )
        # The full register URL is built off the configured base
        # URL so admins can share it directly. Including the token
        # in a query param (not a fragment) means the URL works in
        # the simplest case: paste into any client.
        register_url = f"{config.base_url}/register?invite={token_id}"
        _logger.info(
            "auth/invite: %s minted invite (admin=%s)",
            _redact_for_log(admin_id),
            body.is_admin,
        )
        return JSONResponse(
            status_code=200,
            content={
                "token": token_id,
                "register_url": register_url,
                "expires_at": now + config.invite_ttl_seconds,
                "is_admin": body.is_admin,
            },
        )

    @router.post("/register")
    async def register(request: Request, body: RegisterRequest) -> Response:
        """Consume an invite, create the user, sign them in.

        Ordering: validate format → check name availability → THEN
        redeem the invite. Reversing this would burn the (single-use)
        invite on every name collision — and an attacker holding a
        valid invite URL could trivially DoS it with one POST using
        ``username: "admin"`` (always taken after bootstrap).
        Validation + cheap-read precede the token consumption.

        A narrow TOCTOU race remains (name becomes taken between
        the check and the INSERT). We catch the resulting ValueError
        from ``create_user_with_password`` and accept the invite
        is consumed in that case — the user was genuinely racing
        another registrant for the same token, which is fine.
        """
        err = _validate_username(body.username)
        if err is not None:
            return JSONResponse(status_code=400, content={"error": err})

        username = body.username.strip().lower()
        email, email_key, email_err = normalize_email(body.email)
        if email_err is not None:
            return JSONResponse(status_code=400, content={"error": email_err})
        open_signup = body.invite is None
        signup_key = f"register:address:{client_address(request)}"
        if open_signup:
            retry_after = limiter.retry_after(signup_key, REGISTRATIONS_PER_ADDRESS)
            if retry_after:
                return _too_many_attempts(retry_after)
        if open_signup and mailer is not None and email is None:
            return JSONResponse(status_code=400, content={"error": "email is required"})
        if open_signup and email_key is not None and blocked_domain(email_key):
            return JSONResponse(
                status_code=400,
                content={"error": "disposable email domains are not accepted"},
            )
        if account_store.get_user(username) is not None:
            # Don't burn the invite — the user can retry with a
            # different name on the same URL.
            return JSONResponse(status_code=409, content={"error": "username already taken"})

        now = int(time.time())
        invited_is_admin = False
        if body.invite is not None:
            token = account_store.redeem_token(body.invite, kind="invite", now_epoch_seconds=now)
            if token is None:
                # Don't distinguish missing / expired / already-redeemed
                # — keeps the route opaque to brute-force token guessing.
                return JSONResponse(
                    status_code=400, content={"error": "invite invalid or expired"}
                )
            invited_is_admin = token.invited_is_admin
        elif not config.open_signup:
            return JSONResponse(status_code=400, content={"error": "invite required"})

        try:
            user = account_store.create_user_with_password(
                username,
                hash_password(body.password),
                is_admin=invited_is_admin,
                # Invited accounts are reached through the inviter; only
                # self-serve signups leave a contact address.
                email=email if open_signup else None,
                email_key=email_key if open_signup else None,
            )
        except ValueError:
            # Genuine race against another registrant using the same
            # invite — invite is now consumed, no recovery here.
            return JSONResponse(status_code=409, content={"error": "username already taken"})
        if open_signup:
            limiter.record(signup_key, REGISTRATIONS_PER_ADDRESS)

        welcome = None
        verification_sent: bool | None = None
        if open_signup and mailer is not None and email is not None:
            # Credit waits for the verification link; the account exists
            # either way and the user can resend from Settings › Billing.
            verification_sent = await asyncio.to_thread(_send_verification, username, email)
        elif open_signup and funded_trial is not None:
            # No mailer: fall back to granting at registration. A failed
            # grant never fails the registration; an operator can grant by hand.
            try:
                welcome = await asyncio.to_thread(
                    funded_trial.grant_welcome,
                    username,
                    source=_registration_source(request, config.cookie_secret),
                )
            except Exception:
                _logger.exception(
                    "auth/register: welcome grant failed for %s", _redact_for_log(username)
                )

        if user.account_generation is not None:
            bind_account_authority(username, user.account_generation)
        account_store.mark_logged_in(username, now)
        # Admin list applies to invite-registered users too (additive).
        # Re-fetch so the response reflects a promotion; the invite's
        # own ``invited_is_admin`` still applies independently.
        if promote_if_listed(admin_list, account_store, username):
            user = account_store.get_user(username) or user
        session_jwt = mint_session_cookie(
            user_id=username,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
            account_generation=user.account_generation,
        )
        content: dict[str, Any] = {
            "token": session_jwt,
            "expires_in": _session_max_age,
            "user": {"id": user.id, "is_admin": user.is_admin},
        }
        if welcome is not None:
            content["welcome_grant"] = {
                "allowance_usd": welcome.allowance_micros / MICROS_PER_USD,
                "expires_at": welcome.expires_at,
                "capabilities": sorted(welcome.capabilities),
            }
        if verification_sent is not None:
            content["email_verification"] = {"email": email, "sent": verification_sent}
        resp = JSONResponse(status_code=200, content=content)
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info(
            "auth/register: %s created via %s (admin=%s, email=%s, welcome=%s, verify=%s)",
            _redact_for_log(username),
            "invite" if body.invite is not None else "open signup",
            invited_is_admin,
            email is not None,
            welcome is not None,
            verification_sent,
        )
        return resp

    _verify_ttl_seconds = 24 * 3600
    _resend_min_interval_s = 60
    _last_resend: dict[str, int] = {}

    def _send_verification(user_id: str, email: str) -> bool:
        """Mint a single-use verify token and mail its link; False if sending failed."""
        assert mailer is not None
        now = int(time.time())
        token_id = secrets.token_urlsafe(32)
        account_store.create_token(
            token_id,
            kind="verify",
            user_id=user_id,
            created_by=None,
            created_at=now,
            expires_at=now + _verify_ttl_seconds,
        )
        link = f"{config.base_url}/auth/verify-email?t={token_id}"
        text = (
            f"Hi {user_id},\n\n"
            "Confirm this address to finish setting up your WakeVane account"
            " and unlock your trial credit:\n\n"
            f"{link}\n\n"
            "The link works once and expires in 24 hours. If you did not create"
            " this account, ignore this message.\n"
        )
        try:
            mailer.send(to=email, subject="Confirm your WakeVane email", text=text)
        except Exception:
            _logger.exception("auth/verify: could not send to %s", _redact_for_log(user_id))
            return False
        return True

    @router.get("/verify-email")
    async def verify_email(request: Request) -> Response:
        """Consume a verification link, record it and issue the welcome credit.

        Success redirects to the home page with ``?verified=1`` (plus
        ``&credit=0`` when no credit was issued). A missing, used or stale
        link redirects to Settings › Billing with ``?verified=expired``, where
        a new link can be sent.
        """
        target = f"{config.base_url}/settings/billing"
        token_id = request.query_params.get("t", "").strip()
        if not token_id:
            return RedirectResponse(url=f"{target}?verified=expired", status_code=302)
        now = int(time.time())
        token = account_store.redeem_token(token_id, kind="verify", now_epoch_seconds=now)
        if token is None or token.user_id is None:
            return RedirectResponse(url=f"{target}?verified=expired", status_code=302)
        user = account_store.get_user(token.user_id)
        if user is None or user.account_generation != token.account_generation:
            return RedirectResponse(url=f"{target}?verified=expired", status_code=302)
        first_time = await asyncio.to_thread(account_store.mark_email_verified, user.id, now)
        granted = False
        if first_time and funded_trial is not None:
            # One mailbox, one credit: aliases of an already-verified address
            # confirm fine but earn nothing.
            duplicates = (
                account_store.count_verified_with_email_key(
                    user.email_key, exclude_user_id=user.id
                )
                if user.email_key
                else 0
            )
            if duplicates == 0:
                try:
                    granted = (
                        await asyncio.to_thread(
                            funded_trial.grant_welcome,
                            user.id,
                            source=_registration_source(request, config.cookie_secret),
                        )
                        is not None
                    )
                except Exception:
                    _logger.exception(
                        "auth/verify: welcome grant failed for %s", _redact_for_log(user.id)
                    )
            else:
                _logger.warning(
                    "auth/verify: %s verified a mailbox already used by %d account(s); no credit",
                    _redact_for_log(user.id),
                    duplicates,
                )
        _logger.info(
            "auth/verify: %s verified (first=%s, credit=%s)",
            _redact_for_log(user.id),
            first_time,
            granted,
        )
        suffix = "" if granted or not first_time else "&credit=0"
        # Home, not billing: a mailed link that lands on a page asking for
        # payment is the pattern browsers flag as phishing.
        return RedirectResponse(url=f"{config.base_url}/?verified=1{suffix}", status_code=302)

    @router.post("/verify-email/resend")
    async def resend_verification(request: Request) -> Response:
        """Send a fresh verification link to the signed-in user's address."""
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})
        user = account_store.get_user(user_id)
        if user is None or not user.email:
            return JSONResponse(status_code=400, content={"error": "no email on this account"})
        if user.email_verified_at is not None:
            return JSONResponse(status_code=409, content={"error": "email already verified"})
        if mailer is None:
            return JSONResponse(status_code=503, content={"error": "email is not configured"})
        now = int(time.time())
        if now - _last_resend.get(user_id, 0) < _resend_min_interval_s:
            return JSONResponse(status_code=429, content={"error": "try again in a minute"})
        _last_resend[user_id] = now
        sent = await asyncio.to_thread(_send_verification, user_id, user.email)
        if not sent:
            return JSONResponse(status_code=502, content={"error": "could not send the email"})
        return JSONResponse(status_code=200, content={"sent": True, "email": user.email})

    # ── Self-serve password reset (needs a mailer) ────────────────

    def _send_reset(user_id: str, email: str, generation: str) -> None:
        """Mail a reset link; failures are logged, never surfaced to the caller."""
        assert mailer is not None
        token = mint_reset_token(
            user_id=user_id, account_generation=generation, cookie_secret=config.cookie_secret
        )
        link = f"{config.base_url}/reset-password?t={token}"
        text = (
            f"Hi {user_id},\n\n"
            "Someone asked to reset the password for this account. To choose a"
            " new one, open:\n\n"
            f"{link}\n\n"
            f"The link works once and expires in {RESET_TOKEN_TTL_SECONDS // 60} minutes."
            " If you did not ask for this, ignore this message; your password"
            " stays as it is.\n"
        )
        try:
            mailer.send(to=email, subject="Reset your password", text=text)
        except Exception:
            _logger.exception(
                "auth/password/forgot: could not send to %s", _redact_for_log(user_id)
            )

    @router.post("/password/forgot")
    async def forgot_password(request: Request, body: ForgotPasswordRequest) -> Response:
        """Mail a reset link to the account behind a username or address.

        Answers the same 200 whether or not an account matched, so the route
        cannot be used to learn which usernames or addresses are registered.
        """
        if mailer is None:
            return JSONResponse(status_code=503, content={"error": "email is not configured"})
        address_key = f"reset:address:{client_address(request)}"
        retry_after = limiter.retry_after(address_key, RESET_REQUESTS_PER_ADDRESS)
        if retry_after:
            return _too_many_attempts(retry_after)
        limiter.record(address_key, RESET_REQUESTS_PER_ADDRESS)

        identifier = body.identifier.strip().lower()
        accounts = []
        by_name = account_store.get_user(identifier) if identifier else None
        if by_name is not None:
            accounts.append(by_name)
        email, _, email_error = normalize_email(identifier)
        if email is not None and email_error is None:
            known = {account.id for account in accounts}
            accounts.extend(
                account
                for account in account_store.find_users_by_email(email)
                if account.id not in known
            )
        for account in accounts:
            if not account.email or account.account_generation is None:
                continue
            if account_store.get_password_hash(account.id) is None:
                continue
            account_key = f"reset:account:{account.id}"
            if limiter.retry_after(account_key, RESET_REQUESTS_PER_ACCOUNT):
                continue
            limiter.record(account_key, RESET_REQUESTS_PER_ACCOUNT)
            await asyncio.to_thread(
                _send_reset, account.id, account.email, account.account_generation
            )
        _logger.info("auth/password/forgot: %d message(s) queued", len(accounts))
        return JSONResponse(status_code=200, content={"sent": True})

    @router.post("/password/reset")
    async def reset_password(request: Request, body: ResetPasswordRequest) -> Response:
        """Set a new password from an emailed reset link.

        Signs the account out everywhere, which also retires the link and any
        other reset link issued before it. The user signs in afterwards.
        """
        address_key = f"reset-redeem:address:{client_address(request)}"
        retry_after = limiter.retry_after(address_key, RESET_REDEMPTIONS_PER_ADDRESS)
        if retry_after:
            return _too_many_attempts(retry_after)
        limiter.record(address_key, RESET_REDEMPTIONS_PER_ADDRESS)

        invalid = JSONResponse(status_code=400, content={"error": "reset link invalid or expired"})
        claims = read_reset_token(body.token, cookie_secret=config.cookie_secret)
        if claims is None:
            return invalid
        user_id, token_generation = claims
        user = account_store.get_user(user_id)
        if user is None or user.account_generation != token_generation:
            return invalid
        if account_store.get_password_hash(user_id) is None:
            return invalid
        try:
            with target_account_scope(user_id, user.account_generation):
                account_store.update_password(
                    user_id, hash_password(body.new_password), revoke_sessions=True
                )
        except OmnigentError:
            # The generation moved between the check and the write: another
            # request already used this link.
            return invalid
        auth_provider.revoke_user_sessions(user_id)
        limiter.reset(f"login:account:{user_id}")
        _logger.info("auth/password/reset: password reset for %s", _redact_for_log(user_id))
        return JSONResponse(status_code=200, content={"reset": True, "username": user_id})

    @router.post("/setup")
    async def setup(body: SetupRequest) -> Response:
        """Create the FIRST admin account from the web UI (first-run claim).

        Unauthenticated by necessity — there's no one to authenticate
        AS before the first account exists. Hard-gated instead: it
        succeeds only while the instance has **zero** password-having
        accounts, and 409s forever after. So it can't escalate or add
        a second admin once setup is done. This is the Immich/Jellyfin
        first-run model: on a fresh remote deploy (Docker / Render /
        Railway), the first visitor claims the admin account by
        choosing a username + password in the browser — no container
        access, no log-digging.

        Local (loopback) deploys never reach this: bootstrap already
        auto-provisions the admin + signs the operator in. A deploy
        that pre-seeds ``OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`` also
        won't (the admin exists at boot, so this 409s).
        """
        # Gate: only while no account has been claimed yet. Checked
        # against the same "any password-having user" predicate the
        # bootstrap uses, so the two agree on what "set up" means.
        if any(u.has_password for u in account_store.list_users()):
            return JSONResponse(status_code=409, content={"error": "setup already completed"})

        username = body.username.strip().lower()
        now = int(time.time())
        try:
            user = account_store.create_user_with_password(
                username,
                hash_password(body.password),
                is_admin=True,
            )
        except ValueError:
            # Lost a race: another setup request claimed the instance
            # between our gate check and this insert. Either way, setup
            # is now done — surface the same terminal 409.
            return JSONResponse(status_code=409, content={"error": "setup already completed"})

        if user.account_generation is not None:
            bind_account_authority(username, user.account_generation)
        account_store.mark_logged_in(username, now)
        # Admin list applies (additive); the setup admin is already admin.
        if promote_if_listed(admin_list, account_store, username):
            user = account_store.get_user(username) or user
        # Loopback CLI handoff: the operator who spawned this local server via
        # `omnigent run` needs a CLI token too, not just the browser cookie.
        # bootstrap_admin only mints the token when an admin already exists at
        # boot; on a fresh first run the admin is claimed HERE, so mint it now
        # (loopback only) — otherwise the in-flight `run` 401s until the next
        # server boot. Gated to loopback, mirroring bootstrap, so a remote
        # deploy never writes a CLI token for the server's own host.
        from omnigent.server.accounts_bootstrap import (
            _is_loopback_base_url,
            _mint_loopback_cli_token,
        )

        if _is_loopback_base_url(config.base_url):
            _mint_loopback_cli_token(
                username,
                base_url=config.base_url,
                cookie_secret=config.cookie_secret,
                session_ttl_hours=config.session_ttl_hours,
                account_generation=user.account_generation,
            )
            # Single-user continuity: a loopback server flipping into accounts
            # mode is the same human's laptop. The pre-accounts chats are owned
            # by the reserved ``local`` user; hand them to the new admin so they
            # stay visible (sessions are listed by owner, with no admin-sees-all
            # bypass). Loopback-only, so a multi-user deploy never reassigns one
            # user's sessions to another.
            if permission_store is not None:
                permission_store.ensure_user(username, is_admin=True)
                permission_store.reassign_user_grants(RESERVED_USER_LOCAL, username)
        session_jwt = mint_session_cookie(
            user_id=username,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
            account_generation=user.account_generation,
        )
        resp = JSONResponse(
            status_code=200,
            content={
                "token": session_jwt,
                "expires_in": _session_max_age,
                "user": {"id": user.id, "is_admin": user.is_admin},
            },
        )
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info("auth/setup: first admin %s claimed the instance", _redact_for_log(username))
        return resp

    # ── Magic link (CLI → web UI handoff) ─────────────────────────

    @router.post("/magic")
    async def magic_mint(request: Request) -> Response:
        """Mint a short-TTL magic-login token for the current user.

        Called by the CLI after it has authenticated via password
        login or a stored cookie. The returned URL is opened in
        the user's browser; ``/auth/magic/redeem`` then signs the
        browser session in as the same user — zero password
        re-entry. TTL is intentionally short (default 10 min).
        """
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})

        token_id = secrets.token_urlsafe(32)
        now = int(time.time())
        account_store.create_token(
            token_id,
            kind="magic",
            user_id=user_id,
            created_by=None,
            created_at=now,
            expires_at=now + config.magic_ttl_seconds,
        )
        redeem_url = f"{config.base_url}/auth/magic/redeem?t={token_id}"
        return JSONResponse(
            status_code=200,
            content={
                "redeem_url": redeem_url,
                "expires_at": now + config.magic_ttl_seconds,
            },
        )

    @router.get("/magic/redeem")
    async def magic_redeem(request: Request) -> Response:
        """Consume a magic token and redirect to ``/``.

        Sets the session cookie as if the user had just typed
        their password. Always returns a redirect — on failure,
        redirects to ``/login?magic=expired`` so the login page
        can show a graceful error.
        """
        token_id = request.query_params.get("t", "").strip()
        if not token_id:
            return RedirectResponse(url=f"{config.base_url}/login?magic=missing", status_code=302)

        now = int(time.time())
        token = account_store.redeem_token(token_id, kind="magic", now_epoch_seconds=now)
        if token is None or token.user_id is None:
            return RedirectResponse(url=f"{config.base_url}/login?magic=expired", status_code=302)

        # Confirm the underlying user still exists (admin may have
        # deleted them after the token was minted).
        user = account_store.get_user(token.user_id)
        if user is None or user.account_generation != token.account_generation:
            return RedirectResponse(url=f"{config.base_url}/login?magic=expired", status_code=302)
        if token.account_generation is not None:
            bind_account_authority(token.user_id, token.account_generation)

        account_store.mark_logged_in(token.user_id, now)
        # Admin list applies on magic-link sign-in too (additive).
        promote_if_listed(admin_list, account_store, token.user_id)
        session_jwt = mint_session_cookie(
            user_id=token.user_id,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
            account_generation=user.account_generation,
        )
        resp = RedirectResponse(url=f"{config.base_url}/", status_code=302)
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info("auth/magic/redeem: signed in %s", _redact_for_log(token.user_id))
        return resp

    # ── Members admin (list, delete, reset password) ──────────────

    @router.get("/users")
    async def list_users(request: Request) -> Response:
        """List all users (admin only)."""
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})

        users = account_store.list_users()
        return JSONResponse(
            status_code=200,
            content={
                "users": [
                    {
                        "id": u.id,
                        "is_admin": u.is_admin,
                        "created_at": u.created_at,
                        "last_login_at": u.last_login_at,
                        "has_password": u.has_password,
                        "email": u.email,
                        "email_verified_at": u.email_verified_at,
                    }
                    for u in users
                ]
            },
        )

    @router.delete("/users/{user_id}")
    async def delete_user(request: Request, user_id: str) -> Response:
        """Remove a user (admin only).

        Refuses two cases:

        - **Self-delete** — the calling admin would lock themselves
          out of their own session (cookie still valid but row
          gone; every subsequent /v1/me 401s).
        - **Last admin** — the deploy must always have at least one
          admin or there's no recovery path. Previously this was a
          hardcoded "can't delete the user named 'admin'", but the
          bootstrap admin's name now defaults to the OS user
          (e.g. ``dhruv.gupta``), so the check generalizes to
          "would this leave zero admins". Same invariant, name-agnostic.
          Checked and applied atomically (see
          ``AccountStore.delete_user``) so two concurrent deletes of
          two different admins can't both slip past the check.
        """
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})
        if user_id == admin_id:
            return JSONResponse(status_code=400, content={"error": "cannot delete self"})

        owned_task_ids: list[str] = []
        if scheduled_task_store is not None:
            owned_task_ids = [t.id for t in scheduled_task_store.list(owner_user_id=user_id)]

        result = account_store.delete_user(user_id)
        if result is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if result is False:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "cannot delete the last admin — promote another "
                    "user first or the deploy would have no recovery path"
                },
            )
        # The store already disabled the rows; drop the in-memory timers so
        # the scheduler stops re-arming them.
        scheduler = getattr(request.app.state, "scheduled_task_scheduler", None)
        if scheduler is not None:
            for task_id in owned_task_ids:
                scheduler.remove(task_id)
        auth_provider.revoke_user_sessions(user_id)
        return Response(status_code=204)

    @router.post("/users/{user_id}/reset")
    async def reset_user_password(request: Request, user_id: str) -> Response:
        """Admin-initiated password reset.

        Mints a fresh random password, stores its hash, returns
        the plaintext exactly once to the calling admin who
        then DMs it to the user out-of-band. No SMTP needed.
        Also revokes the user's existing sessions and CLI logins.
        """
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})

        user = account_store.get_user(user_id)
        if user is None:
            return JSONResponse(status_code=404, content={"error": "not found"})

        new_password = secrets.token_urlsafe(16)
        with target_account_scope(user_id, user.account_generation):
            generation = account_store.update_password(
                user_id, hash_password(new_password), revoke_sessions=True
            )
        auth_provider.revoke_user_sessions(user_id)
        _logger.info(
            "auth/users/reset: %s reset by %s",
            _redact_for_log(user_id),
            _redact_for_log(admin_id),
        )
        resp = JSONResponse(
            status_code=200,
            content={"id": user_id, "new_password": new_password},
        )
        # An admin resetting their own account keeps this session, so the
        # next request cannot sign them out before they save the password.
        if user_id == admin_id and generation is not None:
            _reissue_session_cookie(resp, admin_id, generation)
        return resp

    @router.post("/users/me/password")
    async def change_own_password(request: Request, body: ChangePasswordRequest) -> Response:
        """Self-serve password change (requires old password).

        Revokes every other session and CLI login of the account. The
        caller gets a fresh session cookie; a bearer-token caller must
        log in again.
        """
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})

        current_hash = account_store.get_password_hash(user_id)
        if current_hash is None:
            # User exists (cookie valid) but has no password — they
            # came in via header/oidc. Self-serve change is N/A.
            return JSONResponse(
                status_code=400,
                content={"error": "no password set for this account"},
            )

        try:
            verify_password(body.old_password, current_hash)
        except InvalidPasswordError:
            return JSONResponse(status_code=401, content={"error": "old password incorrect"})

        generation = account_store.update_password(
            user_id, hash_password(body.new_password), revoke_sessions=True
        )
        auth_provider.revoke_user_sessions(user_id)
        _logger.info("auth/users/me/password: changed for %s", _redact_for_log(user_id))
        resp = Response(status_code=204)
        if generation is not None:
            _reissue_session_cookie(resp, user_id, generation)
        return resp

    return router


# Pre-computed dummy hash for the timing-equalization path on
# /auth/login when the username is unknown. argon2 verify against
# this will always raise InvalidPasswordError — we only run it
# for the side effect of taking roughly the same wall time as a
# verify against a real stored hash, so an attacker can't use
# response latency to enumerate valid usernames.
_DUMMY_HASH = hash_password("__dummy_for_timing_equalization__")
