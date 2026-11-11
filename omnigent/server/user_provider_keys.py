"""Run a second vendor's harness in a managed sandbox on the owner's own API key.

A managed sandbox's inference is otherwise fixed by the operator: every bound
harness reaches the operator's provider. With the ``sandbox_provider_keys``
feature, an owner who saved their own OpenAI key gets one more provider in
their sessions' saved inference profile, bound to the Codex harnesses the
operator left unbound. The profile carries only an environment reference; the
key itself travels as launch-only environment, the way the funded trial's
gateway token does, and is never written to the sandbox config file or the
persisted session snapshot.

Anything running in the sandbox can read that environment, so the trust
boundary is the sandbox itself. Design review sandboxes never receive it.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from omnigent.connections.provider_keys import OpenAIKeyStore, UserProviderKey
from omnigent.models.model_fallbacks import CODEX_DEFAULT_MODEL
from omnigent.server.routes.host_credentials import DESIGN_REVIEW_LABEL_PREFIX

# Provider name the owner's key appears under in a session's inference profile.
USER_OPENAI_PROVIDER = "your-openai-key"
USER_OPENAI_KEY_ENV = "WAKEVANE_USER_OPENAI_API_KEY"
USER_OPENAI_BASE_URL = "https://api.openai.com/v1"
# Harnesses the owner's key serves when the operator has not bound them.
USER_OPENAI_HARNESSES = ("codex", "codex-native")


def provider_key_store(app_state: Any) -> OpenAIKeyStore | None:
    """The deployment's provider key store, or ``None`` when the feature is off."""
    return getattr(app_state, "provider_key_store", None)


def bind_user_openai_key(host_config: dict[str, Any], key: UserProviderKey) -> list[str]:
    """Add the owner's key as a provider for the Codex harnesses still unbound.

    Mutates *host_config* (a per-request copy of a target's inference
    profile). An operator binding always wins: a harness the operator bound
    keeps its provider, and nothing is added when both are bound.

    :param host_config: The profile being prepared for one session.
    :param key: The owner's saved key; only its model restriction is read.
    :returns: The harnesses now bound to the owner's key, e.g.
        ``["codex", "codex-native"]``; empty when none were free.
    """
    harnesses = host_config.setdefault("inference", {}).setdefault("harnesses", {})
    free = [harness for harness in USER_OPENAI_HARNESSES if harness not in harnesses]
    if not free or USER_OPENAI_PROVIDER in host_config.get("providers", {}):
        return []
    models = list(key.models) or [CODEX_DEFAULT_MODEL]
    host_config.setdefault("providers", {})[USER_OPENAI_PROVIDER] = {
        "kind": "key",
        "display_name": "Your OpenAI key",
        "openai": {
            "base_url": USER_OPENAI_BASE_URL,
            "api_key_ref": f"env:{USER_OPENAI_KEY_ENV}",
        },
    }
    for harness in free:
        harnesses[harness] = {
            "provider": USER_OPENAI_PROVIDER,
            "default_model": models[0],
            "model_allowlist": list(models),
        }
    return free


def with_user_provider_keys(
    deployment: Any,
    *,
    owner: str,
    labels: dict[str, str] | None,
    store: OpenAIKeyStore | None,
) -> Any:
    """Copy of *deployment* whose targets launch with the owner's key in the host env.

    Only a target whose host config already names the owner's provider gets
    the key: that is a session created after the key was saved. A session
    created before it, a design review, and an owner whose key is gone or
    unreadable all launch unchanged.

    :param deployment: The deployment about to launch, with the session's
        saved inference profile already applied.
    :param owner: The session owner, e.g. ``"alice@example.com"``.
    :param labels: The session's labels, used to recognise a design review.
    :param store: The provider key store, or ``None`` when the feature is off.
    :returns: *deployment*, or a copy carrying the launch-only environment.
    """
    from omnigent.onboarding.sandboxes.base import HOST_LAUNCH_ENV_KEY

    if store is None:
        return deployment
    if any(name.startswith(DESIGN_REVIEW_LABEL_PREFIX) for name in labels or {}):
        return deployment
    wanting = [
        target
        for target in deployment.configs
        if USER_OPENAI_PROVIDER in ((target.host_config or {}).get("providers") or {})
    ]
    if not wanting:
        return deployment
    key = store.get(owner, with_tokens=True)
    if key is None or key.api_key is None:
        return deployment
    configs = []
    for target in deployment.configs:
        if target not in wanting:
            configs.append(target)
            continue
        host_config = copy.deepcopy(target.host_config)
        host_config[HOST_LAUNCH_ENV_KEY] = {
            **(host_config.get(HOST_LAUNCH_ENV_KEY) or {}),
            USER_OPENAI_KEY_ENV: key.api_key,
        }
        configs.append(replace(target, host_config=host_config))
    return replace(deployment, configs=tuple(configs))
