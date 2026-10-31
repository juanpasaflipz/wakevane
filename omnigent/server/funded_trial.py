"""Service-funded trial policy: eligibility, pricing and scoped gateway tokens.

The server is the only authority. Eligibility needs the deployment kill switch
(``funded_trial`` feature), an active per-account grant in
:class:`~omnigent.server.funded_trial_store.FundedTrialStore`, the requested
capability, explicit operator pricing, and an atomic ledger reservation.
Anything unknown or unconfigured denies instead of falling back to another payer.

Operator settings come from ``OMNIGENT_FUNDED_TRIAL_CONFIG`` (JSON). The
gateway signing key and provider API key are read from the environment
variables it names and never leave the server.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from omnigent.llms.context_window import ModelPricing, compute_llm_cost
from omnigent.server.funded_trial_store import (
    MICROS_PER_USD,
    FundedTrialDenied,
    FundedTrialStore,
    TrialCharge,
    TrialGrant,
)

CONFIG_ENV_VAR = "OMNIGENT_FUNDED_TRIAL_CONFIG"
TRIAL_PROVIDER = "wakevane-trial"
TRIAL_HARNESSES = ("claude-sdk", "claude-native")
GATEWAY_PATH = "/v1/funded-trial/gateway/anthropic"
# Placeholder reference in catalogs and snapshots; launches replace it with a minted token.
_TOKEN_REF = "env:WAKEVANE_TRIAL_GATEWAY_TOKEN"
_TOKEN_PREFIX = "wvt1."
_MAX_TOKEN_LIFETIME_S = 12 * 3600


def _usd_to_micros(value: float) -> int:
    """Round a dollar amount up to whole micro-USD so holds never undershoot."""
    micros = value * MICROS_PER_USD
    whole = int(micros)
    return whole if whole == micros else whole + 1


@dataclass(frozen=True)
class FundedTrialConfig:
    """Operator-approved prices and limits; absent entries are unpriced.

    :param model_pricing: Model id → per-token prices for the gateway upstream.
    :param compute_usd_per_hour: Sandbox provider → hourly price.
    :param max_output_tokens: Upper bound enforced on every gateway request.
    :param max_request_bytes: Upper bound on a gateway request body.
    :param max_compute_hours: Longest cloud lifetime one reservation covers.
    :param upstream_base_url: Anthropic-compatible upstream for the gateway.
    :param upstream_api_key_env: Server env var holding the service API key.
    :param signing_key_env: Server env var holding the token signing key.
    """

    model_pricing: Mapping[str, ModelPricing] = field(default_factory=dict)
    compute_usd_per_hour: Mapping[str, float] = field(default_factory=dict)
    max_output_tokens: int = 16_000
    max_request_bytes: int = 2_000_000
    max_compute_hours: float = 4.0
    upstream_base_url: str = "https://api.anthropic.com"
    upstream_api_key_env: str = "OMNIGENT_FUNDED_TRIAL_ANTHROPIC_API_KEY"
    signing_key_env: str = "OMNIGENT_FUNDED_TRIAL_SIGNING_KEY"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> FundedTrialConfig:
        """Parse operator JSON; prices are USD per million tokens."""
        models: dict[str, ModelPricing] = {}
        for model, prices in (raw.get("models") or {}).items():
            input_price = float(prices["input"])
            models[str(model)] = ModelPricing(
                input_per_token=input_price / 1_000_000,
                output_per_token=float(prices["output"]) / 1_000_000,
                cache_read_per_token=float(prices["cache_read"]) / 1_000_000,
                cache_write_per_token=float(prices["cache_write"]) / 1_000_000,
            )
        compute = {
            str(provider): float(rate)
            for provider, rate in (raw.get("compute_usd_per_hour") or {}).items()
        }
        if any(rate <= 0 for rate in compute.values()):
            raise ValueError("Compute prices must be positive")
        defaults = cls()
        return cls(
            model_pricing=models,
            compute_usd_per_hour=compute,
            max_output_tokens=int(raw.get("max_output_tokens", defaults.max_output_tokens)),
            max_request_bytes=int(raw.get("max_request_bytes", defaults.max_request_bytes)),
            max_compute_hours=float(raw.get("max_compute_hours", defaults.max_compute_hours)),
            upstream_base_url=str(raw.get("upstream_base_url", defaults.upstream_base_url)).rstrip(
                "/"
            ),
            upstream_api_key_env=str(
                raw.get("upstream_api_key_env", defaults.upstream_api_key_env)
            ),
            signing_key_env=str(raw.get("signing_key_env", defaults.signing_key_env)),
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> FundedTrialConfig:
        """Read ``OMNIGENT_FUNDED_TRIAL_CONFIG``; unset means nothing is priced."""
        text = (environ if environ is not None else os.environ).get(CONFIG_ENV_VAR, "").strip()
        return cls.from_mapping(json.loads(text)) if text else cls()

    def inference_hold_micros(self, model: str, request_bytes: int, max_tokens: int) -> int:
        """Conservative maximum cost of one Messages request.

        Every request byte is priced as a token at the dearest input rate,
        which bounds text, cache writes and images from above.
        """
        pricing = self.model_pricing[model]
        dearest_input = max(
            pricing.input_per_token,
            pricing.cache_write_per_token or 0.0,
            pricing.cache_read_per_token or 0.0,
        )
        return _usd_to_micros(
            request_bytes * dearest_input + max_tokens * pricing.output_per_token
        )

    def inference_cost_micros(self, model: str, usage: Mapping[str, Any]) -> int:
        """Measured cost from provider-reported Anthropic usage."""
        return _usd_to_micros(compute_llm_cost(dict(usage), self.model_pricing[model]))

    def compute_hold_micros(self, provider: str, hours: float) -> int:
        """Reservation for a bounded cloud lifetime."""
        return _usd_to_micros(self.compute_usd_per_hour[provider] * hours)


@dataclass(frozen=True)
class GatewayClaims:
    """Authority carried by a scoped gateway token."""

    user_id: str
    grant_id: str
    root_session_id: str
    session_id: str
    models: tuple[str, ...]
    expires_at: int
    generation: int


class FundedTrialPolicy:
    """Single decision point shared by launch paths and the inference gateway."""

    def __init__(
        self,
        *,
        enabled: bool,
        store: FundedTrialStore | None,
        config: FundedTrialConfig,
        environ: Mapping[str, str] | None = None,
        clock: Any = time.time,
    ) -> None:
        self.enabled = enabled
        self.store = store
        self.config = config
        self._environ = environ if environ is not None else os.environ
        self._clock = clock

    def _signing_key(self) -> bytes:
        key = self._environ.get(self.config.signing_key_env, "")
        if len(key) < 32:
            raise FundedTrialDenied(
                "unconfigured", "The funded trial gateway signing key is not configured."
            )
        return key.encode("utf-8")

    def upstream_api_key(self) -> str:
        """Service provider key for the gateway; never returned to clients."""
        key = self._environ.get(self.config.upstream_api_key_env, "")
        if not key:
            raise FundedTrialDenied(
                "unconfigured", "The funded trial provider credential is not configured."
            )
        return key

    def _require_store(self) -> FundedTrialStore:
        if not self.enabled or self.store is None:
            raise FundedTrialDenied("disabled", "The funded trial is not enabled.")
        return self.store

    def active_grant(self, user_id: str | None, capability: str) -> TrialGrant:
        """Return the caller's usable grant or raise a denial (audited)."""
        store = self._require_store()
        if not user_id:
            raise FundedTrialDenied("no_grant", "Funded access requires a signed-in account.")
        grant = store.get_grant(user_id)
        reason: str | None
        if grant is None:
            reason = "no_grant"
        else:
            reason = grant.inactive_reason(int(self._clock()))
            if reason is None and capability not in grant.capabilities:
                reason = "capability"
        if reason is not None:
            store.record_event(
                "denied",
                user_id=user_id,
                actor_id=user_id,
                detail={"reason": reason, "capability": capability},
            )
            raise FundedTrialDenied(reason, f"Funded access denied: {reason}.")
        assert grant is not None
        return grant

    def summary(self, user_id: str | None) -> dict[str, Any]:
        """The caller's own effective capabilities and remaining allowance."""
        if not self.enabled or self.store is None or not user_id:
            return {"enabled": False, "capabilities": [], "remaining_usd": 0.0}
        grant = self.store.get_grant(user_id)
        if grant is None:
            return {"enabled": False, "capabilities": [], "remaining_usd": 0.0}
        inactive = grant.inactive_reason(int(self._clock()))
        return {
            "enabled": inactive is None,
            "status": inactive or "active",
            "capabilities": sorted(grant.capabilities) if inactive is None else [],
            "allowance_usd": grant.allowance_micros / MICROS_PER_USD,
            "spent_usd": grant.spent_micros / MICROS_PER_USD,
            "reserved_usd": grant.reserved_micros / MICROS_PER_USD,
            "remaining_usd": grant.remaining_micros / MICROS_PER_USD,
            "expires_at": grant.expires_at,
            "models": sorted(self.config.model_pricing),
        }

    def reserve_compute(
        self,
        user_id: str | None,
        *,
        provider: str,
        idempotency_key: str,
        root_session_id: str,
        session_id: str,
        hours: float | None = None,
    ) -> TrialCharge:
        """Hold the cost of a bounded cloud lifetime before provisioning."""
        store = self._require_store()
        self.active_grant(user_id, "compute")
        assert user_id is not None
        if provider not in self.config.compute_usd_per_hour:
            store.record_event(
                "denied",
                user_id=user_id,
                actor_id=user_id,
                detail={"reason": "unpriced_compute", "provider": provider},
            )
            raise FundedTrialDenied(
                "unpriced_compute", f"No approved compute price for {provider!r}."
            )
        lifetime = min(hours or self.config.max_compute_hours, self.config.max_compute_hours)
        return store.reserve(
            user_id,
            capability="compute",
            kind="compute",
            amount_micros=self.config.compute_hold_micros(provider, lifetime),
            idempotency_key=idempotency_key,
            root_session_id=root_session_id,
            session_id=session_id,
            detail={"provider": provider, "hours": lifetime},
        )

    def settle_compute(self, charge_id: str, *, provider: str, seconds: int) -> TrialCharge:
        """Settle a compute hold from the generation's measured lifetime."""
        if self.store is None:
            raise FundedTrialDenied("disabled", "The funded trial is not enabled.")
        rate = self.config.compute_usd_per_hour.get(provider)
        if rate is None:
            # Unpriced now (config changed): charge the full hold rather than undercharge.
            charge = self.store.get_charge(charge_id)
            if charge is None:
                raise KeyError(charge_id)
            return self.store.settle(
                charge_id, charge.reserved_micros, detail={"unpriced": provider}
            )
        return self.store.settle(
            charge_id,
            _usd_to_micros(rate * seconds / 3600),
            detail={"provider": provider, "seconds": seconds},
        )

    def mint_gateway_token(
        self,
        user_id: str | None,
        *,
        root_session_id: str,
        session_id: str,
        models: tuple[str, ...] | None = None,
        lifetime_s: int = 3600,
    ) -> str:
        """Issue a short-lived token bound to account, grant, session tree and models."""
        grant = self.active_grant(user_id, "inference")
        allowed = tuple(sorted(models if models is not None else self.config.model_pricing))
        unpriced = [model for model in allowed if model not in self.config.model_pricing]
        if not allowed or unpriced:
            raise FundedTrialDenied("unpriced_model", f"Unpriced models requested: {unpriced}")
        expires_at = min(
            int(self._clock()) + min(lifetime_s, _MAX_TOKEN_LIFETIME_S), grant.expires_at
        )
        payload = json.dumps(
            {
                "sub": grant.user_id,
                "grant": grant.id,
                "gen": grant.generation,
                "root": root_session_id,
                "session": session_id,
                "models": list(allowed),
                "exp": expires_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self._signing_key(), body, hashlib.sha256).digest()
        return (
            _TOKEN_PREFIX
            + body.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    def verify_gateway_token(self, token: str) -> GatewayClaims:
        """Validate signature, expiry and the grant still being active."""
        self._require_store()
        invalid = FundedTrialDenied("invalid_token", "The gateway credential is invalid.")
        if not token.startswith(_TOKEN_PREFIX):
            raise invalid
        try:
            body, signature = token[len(_TOKEN_PREFIX) :].split(".", 1)
            expected = hmac.new(self._signing_key(), body.encode("ascii"), hashlib.sha256).digest()
            presented = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            if not hmac.compare_digest(expected, presented):
                raise invalid
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            result = GatewayClaims(
                user_id=str(claims["sub"]),
                grant_id=str(claims["grant"]),
                root_session_id=str(claims["root"]),
                session_id=str(claims["session"]),
                models=tuple(str(model) for model in claims["models"]),
                expires_at=int(claims["exp"]),
                generation=int(claims["gen"]),
            )
        except FundedTrialDenied:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise invalid from None
        if result.expires_at <= int(self._clock()):
            raise FundedTrialDenied("token_expired", "The gateway credential has expired.")
        grant = self.active_grant(result.user_id, "inference")
        # Revocation or new terms bump the generation, retiring older tokens.
        if grant.id != result.grant_id or grant.generation != result.generation:
            raise invalid
        return result


def has_trial_targets(deployment: Any) -> bool:
    """Whether any sandbox target in the deployment is trial-funded."""
    return any(
        getattr(target, "funding", None) == "trial"
        for target in getattr(deployment, "configs", ())
    )


def funded_target(deployment: Any, provider: str | None) -> Any | None:
    """The sandbox target if launches on it are trial-funded, else ``None``."""
    if not has_trial_targets(deployment):
        return None
    target = deployment.recorded(provider)
    return target if target is not None and getattr(target, "funding", None) == "trial" else None


def require_launch_eligibility(
    deployment: Any,
    provider: str | None,
    owner: str | None,
    capabilities: tuple[str, ...] = (),
) -> None:
    """Synchronous precheck so an ineligible create fails before anything persists.

    The authoritative check is :func:`admit_managed_launch`, which reserves.

    :param capabilities: Grant capabilities the launch needs beyond compute,
        e.g. ``("coding",)`` for an approved coding session.
    """
    target = funded_target(deployment, provider)
    if target is None:
        return
    policy: FundedTrialPolicy | None = getattr(deployment, "funded_trial", None)
    if policy is None:
        raise FundedTrialDenied("disabled", "The funded trial is not enabled.")
    for capability in capabilities:
        policy.active_grant(owner, capability)
    policy.active_grant(owner, "compute")
    if target.provider not in policy.config.compute_usd_per_hour:
        raise FundedTrialDenied(
            "unpriced_compute", f"No approved compute price for {target.provider!r}."
        )


def admit_managed_launch(
    deployment: Any,
    *,
    provider: str | None,
    owner: str | None,
    session_id: str,
    root_session_id: str,
    phase: str,
    launch_id: str,
    capabilities: tuple[str, ...] = (),
) -> TrialCharge | None:
    """Reserve compute before any trial-funded sandbox is provisioned or resumed.

    Returns ``None`` for operator-funded targets. Raises
    :class:`FundedTrialDenied` when the account cannot fund the launch.

    :param phase: ``launch``, ``relaunch`` or ``wake`` (audit detail and key).
    :param launch_id: Stable id of this launch attempt; replays reuse its hold.
    :param capabilities: Grant capabilities required besides compute.
    """
    target = funded_target(deployment, provider)
    if target is None:
        return None
    policy: FundedTrialPolicy | None = getattr(deployment, "funded_trial", None)
    if policy is None:
        raise FundedTrialDenied("disabled", "The funded trial is not enabled.")
    for capability in capabilities:
        policy.active_grant(owner, capability)
    return policy.reserve_compute(
        owner,
        provider=str(target.provider),
        idempotency_key=f"compute:{phase}:{session_id}:{launch_id}",
        root_session_id=root_session_id,
        session_id=session_id,
    )


def trial_inference_config(server_url: str, models: tuple[str, ...]) -> dict[str, Any]:
    """Host config binding Claude harnesses to the metered gateway for priced models.

    Carries only a placeholder credential reference, so it is safe to show in
    model catalogs and to persist in session inference snapshots.
    """
    allowlist = sorted(models)
    return {
        "providers": {
            TRIAL_PROVIDER: {
                "kind": "gateway",
                "anthropic": {
                    "base_url": server_url.rstrip("/") + GATEWAY_PATH,
                    "api_key_ref": _TOKEN_REF,
                },
            }
        },
        "inference": {
            "harnesses": {
                harness: {
                    "provider": TRIAL_PROVIDER,
                    "default_model": allowlist[0],
                    "model_allowlist": allowlist,
                }
                for harness in TRIAL_HARNESSES
            }
        },
    }


def bind_trial_inference(deployment: Any, policy: FundedTrialPolicy) -> None:
    """Give each trial target the gateway inference profile, when models are priced.

    Without priced models the targets keep no inference binding and their
    launches fail closed at the model picker.
    """
    models = tuple(policy.config.model_pricing)
    if not policy.enabled or not models:
        return
    deployment.configs = tuple(
        replace(
            target,
            host_config={
                **(target.host_config or {}),
                **trial_inference_config(target.server_url, models),
            },
        )
        if getattr(target, "funding", None) == "trial"
        else target
        for target in deployment.configs
    )


def with_gateway_token(deployment: Any, token: str) -> Any:
    """Copy of *deployment* whose trial targets carry *token* as the gateway key.

    The token is written into the sandbox's own host config only; it never
    enters the persisted snapshot, which keeps the placeholder reference.
    """
    configs = []
    for target in deployment.configs:
        provider = ((target.host_config or {}).get("providers") or {}).get(TRIAL_PROVIDER)
        if getattr(target, "funding", None) != "trial" or not isinstance(provider, dict):
            configs.append(target)
            continue
        host_config = copy.deepcopy(target.host_config)
        family = dict(host_config["providers"][TRIAL_PROVIDER]["anthropic"])
        family.pop("api_key_ref", None)
        family["api_key"] = token
        host_config["providers"][TRIAL_PROVIDER]["anthropic"] = family
        configs.append(replace(target, host_config=host_config))
    return replace(deployment, configs=tuple(configs))


@dataclass(frozen=True)
class TrialLaunch:
    """The deployment to launch with and the compute hold it reserved, if any."""

    deployment: Any
    charge: TrialCharge | None = None


def prepare_trial_launch(
    deployment: Any,
    *,
    provider: str | None,
    owner: str | None,
    session_id: str,
    root_session_id: str,
    phase: str,
    launch_id: str,
    capabilities: tuple[str, ...] = (),
) -> TrialLaunch:
    """Reserve compute and arm the workspace's gateway credential for one launch.

    Returns *deployment* unchanged for operator-funded targets. The token
    expires with the reserved compute lifetime, so an over-running sandbox
    loses model access instead of spending past its hold.
    """
    charge = admit_managed_launch(
        deployment,
        provider=provider,
        owner=owner,
        session_id=session_id,
        root_session_id=root_session_id,
        phase=phase,
        launch_id=launch_id,
        capabilities=capabilities,
    )
    if charge is None:
        return TrialLaunch(deployment)
    policy: FundedTrialPolicy = deployment.funded_trial
    try:
        token = policy.mint_gateway_token(
            owner,
            root_session_id=root_session_id,
            session_id=session_id,
            lifetime_s=int(policy.config.max_compute_hours * 3600),
        )
    except FundedTrialDenied:
        # No sandbox will start, so the compute hold must not stay reserved.
        assert policy.store is not None
        policy.store.release(charge.id)
        raise
    return TrialLaunch(with_gateway_token(deployment, token), charge)
