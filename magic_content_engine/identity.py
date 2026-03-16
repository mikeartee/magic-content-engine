"""AgentCore Identity integration for credential management.

Provides a protocol-based identity provider abstraction:
- LocalIdentityProvider reads credentials from environment variables
  (loaded from .env by python-dotenv in config.py).
- AgentCoreIdentityProvider is a production stub that will call
  AgentCore Identity API once wired.

Requirements: REQ-021.1, REQ-021.2, REQ-021.3
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

SUPPORTED_CREDENTIALS: frozenset[str] = frozenset(
    {"github_token", "devto_api_key", "devto_username"}
)

_CREDENTIAL_ENV_MAP: dict[str, str] = {
    "github_token": "GITHUB_TOKEN",
    "devto_api_key": "DEVTO_API_KEY",
    "devto_username": "DEVTO_USERNAME",
}


@runtime_checkable
class IdentityProviderProtocol(Protocol):
    """Protocol for credential retrieval."""

    def get_credential(self, name: str) -> str: ...


class LocalIdentityProvider:
    """Reads credentials from environment variables.

    Environment variables are expected to be loaded from a .env file
    via python-dotenv (handled in config.py).
    """

    def get_credential(self, name: str) -> str:
        if name not in SUPPORTED_CREDENTIALS:
            raise ValueError(f"Unknown credential: {name!r}")
        env_var = _CREDENTIAL_ENV_MAP[name]
        value = os.environ.get(env_var)
        if not value:
            raise ValueError(
                f"Credential {name!r} not found in environment variable {env_var!r}"
            )
        return value


class AgentCoreIdentityProvider:
    """Production provider that retrieves credentials via AgentCore Identity.

    This is a stub — production wiring will replace the implementation
    once AgentCore Identity SDK integration is complete.
    """

    def get_credential(self, name: str) -> str:
        raise NotImplementedError(
            "AgentCoreIdentityProvider is not yet wired to AgentCore Identity. "
            "Production credential retrieval will be implemented during deployment."
        )


def get_identity_provider(
    use_agentcore: bool = False,
) -> IdentityProviderProtocol:
    """Factory that returns the appropriate identity provider.

    Args:
        use_agentcore: When True, returns AgentCoreIdentityProvider
            for production use. Defaults to False (local development).
    """
    if use_agentcore:
        return AgentCoreIdentityProvider()
    return LocalIdentityProvider()
