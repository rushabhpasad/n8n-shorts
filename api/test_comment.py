"""Tests for the conversion seed-comment path.

Covers services.youtube.post_top_comment (best-effort, never raises), the
POST /{channel}/comment endpoint (failure-tolerant, channel-default text), and
that channel configs expose the new cta / seed_comment fields. The real Google
API client is monkeypatched — no network, no OAuth.
"""

import channels
import services.youtube as yt


# ─── post_top_comment (service) ──────────────────────────────────────────────

def test_post_top_comment_success(monkeypatch):
    captured = {}

    class FakeInsert:
        def execute(self):
            return {"snippet": {"topLevelComment": {"id": "CID123"}}}

    class FakeThreads:
        def insert(self, **kw):
            captured["body"] = kw["body"]
            return FakeInsert()

    class FakeYT:
        def commentThreads(self):
            return FakeThreads()

    monkeypatch.setattr(yt, "credentials", lambda ch: object())
    monkeypatch.setattr(yt, "build", lambda *a, **k: FakeYT())

    result = yt.post_top_comment("open-verdicts", "vid1", "What's your verdict?")

    assert result == {"posted": True, "comment_id": "CID123", "error": None}
    snip = captured["body"]["snippet"]
    assert snip["videoId"] == "vid1"
    assert snip["topLevelComment"]["snippet"]["textOriginal"] == "What's your verdict?"


def test_post_top_comment_empty_text_is_noop():
    result = yt.post_top_comment("x", "v", "   ")
    assert result["posted"] is False
    assert "empty" in result["error"]


def test_post_top_comment_swallows_api_error(monkeypatch):
    # Token predating the force-ssl scope → 403. Must NOT raise; pipeline continues.
    def boom(ch):
        raise RuntimeError("insufficientPermissions")

    monkeypatch.setattr(yt, "credentials", boom)
    result = yt.post_top_comment("x", "v", "hello")
    assert result["posted"] is False
    assert "insufficientPermissions" in result["error"]


# ─── /{channel}/comment endpoint ─────────────────────────────────────────────

def _client():
    import main
    from fastapi.testclient import TestClient
    return main, TestClient(main.app)


def test_comment_endpoint_defaults_to_channel_seed(monkeypatch):
    main, client = _client()
    seen = {}

    def fake_post(channel, video_id, text):
        seen.update(channel=channel, video_id=video_id, text=text)
        return {"posted": True, "comment_id": "C1", "error": None}

    monkeypatch.setattr(main.youtube_svc, "post_top_comment", fake_post)

    resp = client.post("/open-verdicts/comment", json={"video_id": "v9"})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "channel": "open-verdicts", "video_id": "v9",
        "posted": True, "comment_id": "C1", "error": None,
    }
    # endpoint pulled the channel's configured seed_comment
    assert seen["video_id"] == "v9"
    assert "verdict" in seen["text"].lower()


def test_comment_endpoint_is_failure_tolerant(monkeypatch):
    main, client = _client()

    def fake_post(channel, video_id, text):
        return {"posted": False, "comment_id": None, "error": "403 forbidden"}

    monkeypatch.setattr(main.youtube_svc, "post_top_comment", fake_post)

    resp = client.post("/the-mythscape/comment", json={"video_id": "v1"})

    assert resp.status_code == 200          # never errors the pipeline
    body = resp.json()
    assert body["posted"] is False
    assert body["error"] == "403 forbidden"


def test_comment_endpoint_unknown_channel_404():
    _, client = _client()
    resp = client.post("/no-such-channel/comment", json={"video_id": "v1"})
    assert resp.status_code == 404


# ─── channel config fields ───────────────────────────────────────────────────

def test_channel_config_exposes_cta_and_seed_comment():
    channels.load.cache_clear()
    for slug in ("open-verdicts", "wordstrata", "the-mythscape", "bright-beasts"):
        cfg = channels.load(slug)
        assert cfg.cta and "subscribe" in cfg.cta.lower(), slug
        assert cfg.seed_comment, slug
