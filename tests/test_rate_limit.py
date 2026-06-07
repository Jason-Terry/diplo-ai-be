"""Regression guard for the rate-limit pass.

Toggles the limiter on for this test only (conftest disables it by default
so the bulk of the suite doesn't trip into 429s as it iterates).
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.rate_limit import limiter


@pytest.fixture
def rate_limited_client():
    limiter.enabled = True
    # slowapi keeps an in-memory bucket across tests; clear it so the
    # quota for this test starts fresh.
    limiter.reset()
    with TestClient(app) as c:
        yield c
    limiter.enabled = False


def test_forgot_password_throttles_after_three(rate_limited_client):
    body = {"email": "x@example.com"}
    statuses = [
        rate_limited_client.post("/api/auth/forgot-password", json=body).status_code
        for _ in range(5)
    ]
    # First three succeed (200), the next two trip the 3/minute limit.
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    assert statuses[4] == 429
