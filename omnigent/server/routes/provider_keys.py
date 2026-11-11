"""Routes for the model-provider API key a user supplies for their own sandboxes.

Mounted only with the ``sandbox_provider_keys`` feature and a credential
store cipher. Every route acts on the caller's own key; none returns the key.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent.connections.provider_keys import OpenAIKeyStore, UserProviderKey
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

# Printable, unbroken, and long enough to be a real key rather than a typo.
_API_KEY_PATTERN = re.compile(r"[\x21-\x7e]{20,512}")
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_MAX_MODELS = 8


class UpdateProviderKeyModelsRequest(BaseModel):
    """Body of ``PATCH /v1/provider-keys/openai``."""

    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list, max_length=_MAX_MODELS)

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        models = list(dict.fromkeys(model.strip() for model in value))
        if any(not _MODEL_ID_PATTERN.fullmatch(model) for model in models):
            raise ValueError("each model must be a plain model id")
        return models


class SaveProviderKeyRequest(UpdateProviderKeyModelsRequest):
    """Body of ``PUT /v1/provider-keys/openai``."""

    # Unconstrained on purpose: a model-level rejection would echo the value back.
    api_key: str


def _status(key: UserProviderKey | None) -> dict[str, Any]:
    """The caller-visible state of their key; the key itself is never included."""
    if key is None:
        return {"connected": False, "key_hint": None, "models": [], "updated_at": None}
    return {
        "connected": True,
        "key_hint": key.key_hint,
        "models": list(key.models),
        "updated_at": key.updated_at,
    }


def create_provider_keys_router(
    store: OpenAIKeyStore, *, auth_provider: AuthProvider | None
) -> APIRouter:
    """Build the caller's provider key routes.

    :param store: The provider key store.
    :param auth_provider: The deployment's auth provider. The feature needs a
        stable account to own a key, so the routes refuse single-user mode.
    :returns: The router, to be mounted under ``/v1``.
    """
    router = APIRouter()

    def _owner(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        if user_id is None:
            raise OmnigentError(
                "Provider keys need a signed-in account", code=ErrorCode.INVALID_INPUT
            )
        return user_id

    @router.get("/provider-keys/openai")
    async def get_openai_key(request: Request) -> dict[str, Any]:
        """Whether the caller has saved an OpenAI key, and its last characters."""
        return _status(await asyncio.to_thread(store.get, _owner(request)))

    @router.put("/provider-keys/openai")
    async def save_openai_key(request: Request, body: SaveProviderKeyRequest) -> dict[str, Any]:
        """Save or replace the caller's OpenAI key; new sessions pick it up."""
        owner = _owner(request)
        api_key = body.api_key.strip()
        # Checked here, not by the model, so a rejected key is never echoed back.
        if not _API_KEY_PATTERN.fullmatch(api_key):
            raise OmnigentError(
                "The key must be 20 to 512 printable characters with no spaces",
                code=ErrorCode.INVALID_INPUT,
            )
        saved = await asyncio.to_thread(
            lambda: store.upsert(owner, api_key=api_key, models=tuple(body.models))
        )
        return _status(saved)

    @router.patch("/provider-keys/openai")
    async def update_openai_key_models(
        request: Request, body: UpdateProviderKeyModelsRequest
    ) -> dict[str, Any]:
        """Change which models the saved key runs, without sending the key again."""
        owner = _owner(request)
        saved = await asyncio.to_thread(lambda: store.set_models(owner, tuple(body.models)))
        if saved is None:
            raise OmnigentError("Save an OpenAI key first", code=ErrorCode.NOT_FOUND)
        return _status(saved)

    @router.delete("/provider-keys/openai")
    async def delete_openai_key(request: Request) -> dict[str, Any]:
        """Remove the caller's OpenAI key; sandboxes launched later do not receive it."""
        await asyncio.to_thread(store.delete, _owner(request))
        return _status(None)

    return router
