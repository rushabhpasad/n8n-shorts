"""Tests for the analytics backend: OAuth scopes, db helper, and service rollup.

All Google API calls are mocked — no network, no credentials needed.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest


def test_oauth_scopes_include_analytics_read():
    from services import youtube
    assert "https://www.googleapis.com/auth/youtube.upload" in youtube.SCOPES
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in youtube.SCOPES
    assert "https://www.googleapis.com/auth/youtube.readonly" in youtube.SCOPES
    assert callable(youtube.credentials)
