"""Regression guard for the audit's P0 fix:
- _jwt_secret() MUST fail closed when RAILWAY_ENVIRONMENT is set and no
  secret is configured (otherwise anyone can forge sessions with the
  hard-coded dev fallback).
"""

import importlib
import os

import pytest

from backend import auth


def _reload_auth_with_env(**overrides):
    """Re-import auth so the env reads in _jwt_secret pick up the new values.
    Returns the reloaded module."""
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(auth)
    return auth


def test_uses_env_var_when_set(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "real-secret")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    assert auth._jwt_secret() == "real-secret"


def test_dev_fallback_outside_hosted_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    # Should not raise — dev convenience path.
    assert auth._jwt_secret() == "dev-insecure-secret-do-not-use-in-prod"


def test_fail_closed_in_hosted_env(monkeypatch):
    """The actual P0 regression: prod set, secret missing → blow up loud."""
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="JWT_SECRET is unset"):
        auth._jwt_secret()
