"""Per-user model-provider API key store.

A :class:`~omnigent.connections.ConnectionStore` façade for a key the user
supplies themselves so their managed sandboxes can run a second vendor's
harness. The key is encrypted at rest by the underlying store's cipher and is
decrypted only on the launch path; status reads carry the last four characters
and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from omnigent.connections import ConnectionStore
from omnigent.entities import ProviderConnection

# Characters of the key a status read may show, so the user can tell keys apart.
_KEY_HINT_CHARS = 4


@dataclass(frozen=True)
class UserProviderKey:
    """A user's own API key for one model provider.

    :param user_id: Owner of the key, e.g. ``"alice@example.com"``.
    :param api_key: The key itself, or ``None`` on a metadata-only read or
        when the stored value could not be decrypted.
    :param key_hint: Last characters of the key, e.g. ``"9f2c"``.
    :param models: Model ids the user restricted the key to; empty for the
        deployment's default list.
    :param updated_at: Unix epoch seconds the key was last saved.
    """

    user_id: str
    api_key: str | None = field(repr=False)
    key_hint: str
    models: tuple[str, ...]
    updated_at: int


class OpenAIKeyStore(ConnectionStore[UserProviderKey]):
    """A user's own OpenAI API key, over the shared credential store."""

    _PROVIDER: ClassVar[str] = "openai-api-key"

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> UserProviderKey:
        """Rebuild a :class:`UserProviderKey` from a generic connection."""
        secret = conn.secret or {}
        raw_models = conn.metadata.get("models")
        models = raw_models if isinstance(raw_models, list) else []
        api_key = secret.get("api_key")
        return UserProviderKey(
            user_id=conn.user_id,
            api_key=api_key if isinstance(api_key, str) and api_key else None,
            key_hint=str(conn.metadata.get("key_hint") or ""),
            models=tuple(model for model in models if isinstance(model, str) and model),
            updated_at=conn.updated_at,
        )

    def upsert(
        self, user_id: str, *, api_key: str, models: tuple[str, ...] = ()
    ) -> UserProviderKey:
        """Save or replace the user's key and its optional model restriction."""
        conn = self._store.upsert(
            user_id,
            self._PROVIDER,
            secret={"api_key": api_key},
            metadata={"key_hint": api_key[-_KEY_HINT_CHARS:], "models": list(models)},
        )
        return self._to_entity(conn)

    def set_models(self, user_id: str, models: tuple[str, ...]) -> UserProviderKey | None:
        """Change the saved key's model restriction, keeping the key itself.

        :returns: The updated key, or ``None`` when the user has no readable key.
        """
        current = self.get(user_id, with_tokens=True)
        if current is None or current.api_key is None:
            return None
        return self.upsert(user_id, api_key=current.api_key, models=models)
