"""Tests for AgentCore Identity integration.

Requirements: REQ-021.1, REQ-021.2, REQ-021.3
"""

from __future__ import annotations

import os

import pytest

from magic_content_engine.identity import (
    SUPPORTED_CREDENTIALS,
    AgentCoreIdentityProvider,
    IdentityProviderProtocol,
    LocalIdentityProvider,
    get_identity_provider,
)


# ---------------------------------------------------------------------------
# SUPPORTED_CREDENTIALS
# ---------------------------------------------------------------------------


class TestSupportedCredentials:
    def test_contains_github_token(self):
        assert "github_token" in SUPPORTED_CREDENTIALS

    def test_contains_devto_api_key(self):
        assert "devto_api_key" in SUPPORTED_CREDENTIALS

    def test_contains_devto_username(self):
        assert "devto_username" in SUPPORTED_CREDENTIALS


# ---------------------------------------------------------------------------
# LocalIdentityProvider
# ---------------------------------------------------------------------------


class TestLocalIdentityProvider:
    def test_reads_github_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        provider = LocalIdentityProvider()
        assert provider.get_credential("github_token") == "ghp_test123"

    def test_reads_devto_api_key(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DEVTO_API_KEY", "devto_key_abc")
        provider = LocalIdentityProvider()
        assert provider.get_credential("devto_api_key") == "devto_key_abc"

    def test_reads_devto_username(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DEVTO_USERNAME", "mikeartee")
        provider = LocalIdentityProvider()
        assert provider.get_credential("devto_username") == "mikeartee"

    def test_raises_for_missing_credential(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        provider = LocalIdentityProvider()
        with pytest.raises(ValueError, match="not found in environment variable"):
            provider.get_credential("github_token")

    def test_raises_for_unknown_credential_name(self):
        provider = LocalIdentityProvider()
        with pytest.raises(ValueError, match="Unknown credential"):
            provider.get_credential("some_random_key")

    def test_satisfies_protocol(self):
        assert isinstance(LocalIdentityProvider(), IdentityProviderProtocol)


# ---------------------------------------------------------------------------
# AgentCoreIdentityProvider
# ---------------------------------------------------------------------------


class TestAgentCoreIdentityProvider:
    def test_raises_not_implemented(self):
        provider = AgentCoreIdentityProvider()
        with pytest.raises(NotImplementedError, match="not yet wired"):
            provider.get_credential("github_token")

    def test_satisfies_protocol(self):
        assert isinstance(AgentCoreIdentityProvider(), IdentityProviderProtocol)


# ---------------------------------------------------------------------------
# get_identity_provider factory
# ---------------------------------------------------------------------------


class TestGetIdentityProvider:
    def test_returns_local_by_default(self):
        provider = get_identity_provider()
        assert isinstance(provider, LocalIdentityProvider)

    def test_returns_agentcore_when_requested(self):
        provider = get_identity_provider(use_agentcore=True)
        assert isinstance(provider, AgentCoreIdentityProvider)
