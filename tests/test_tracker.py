import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

import tracker

FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"


def posts():
    return tracker.parse_feed(FIXTURE.read_bytes())


def test_parser_html_cdata_unicode_images_and_ids():
    p = posts()[0]
    assert p.post_id == "1002" and p.link.endswith("/1002")
    assert "Hello 😀 & world\nline two" in p.text
    assert p.images == ["https://cdn.example/a.jpg"]


def test_quote_multiple_images_and_classification():
    p = posts()[1]
    assert len(p.images) == 2 and tracker.classify(p.raw, p.text) == "ambiguous"


def test_reply_repost_filtered_but_ambiguous_forwarded():
    parsed = posts()
    assert tracker.classify(parsed[2].raw, parsed[2].text) == "repost"
    assert tracker.classify(parsed[3].raw, parsed[3].text) == "reply"
    assert tracker.classify(parsed[4].raw, parsed[4].text) == "ambiguous"


def test_duplicate_ids_and_oldest_first():
    parsed = posts() + posts()
    unique = {p.post_id for p in parsed}
    assert len(unique) == 5
    ordered = sorted(posts(), key=lambda p: (p.published, int(p.post_id)))
    assert [p.post_id for p in ordered] == ["1000", "1001", "1002", "1003", "1004"]


def test_feed_fallback_and_reject_html():
    calls = []
    def fake(instance, timeout):
        calls.append(instance)
        if instance == "bad": return b"<html>challenge</html>", "text/html"
        return FIXTURE.read_bytes(), "application/rss+xml"
    good, parsed = tracker.fetch_feed(["bad", "good"], None, fetcher=fake)
    assert good == "good" and parsed and calls == ["bad", "good"]


def test_all_instances_fail():
    def fail(instance, timeout): raise TimeoutError("timeout")
    with pytest.raises(RuntimeError): tracker.fetch_feed(["a", "b"], None, fetcher=fail)


def test_failure_notice_rate_limit_and_recovery_state():
    now = datetime.now(timezone.utc)
    state = {"last_failure_notice_at": now.isoformat()}
    assert not tracker.should_notify_failure(state, now + timedelta(hours=1), 6)
    assert tracker.should_notify_failure(state, now + timedelta(hours=6), 6)


def test_telegram_markup_and_safe_unicode_truncation():
    p = posts()[0]; p.text = "😀" * 4000
    msg = tracker.format_message(p)
    assert "&lt;" not in msg and "x.com/thsottiaux/status/1002" in msg
    assert len(msg) < 4100


def test_photo_caption_respects_telegram_limit(monkeypatch):
    post = posts()[0]
    post.text = "<long> 😀" * 1000
    sent = []
    monkeypatch.setattr(tracker, "telegram_call", lambda token, method, payload, timeout=15: sent.append(payload))

    tracker.deliver(post, "TOKEN", "CHAT", "thsottiaux")

    assert len(sent[0]["caption"]) <= 1024
    assert sent[0]["caption"].endswith('在 X 查看原帖</a>')


def test_telegram_http_error_includes_api_description(monkeypatch):
    error = HTTPError("https://api.telegram.org/botTOKEN/sendMessage", 400, "Bad Request", {},
                      BytesIO(b'{"ok":false,"description":"Bad Request: chat not found"}'))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(tracker, "urlopen", fail)
    with pytest.raises(RuntimeError, match="HTTP 400: Bad Request: chat not found"):
        tracker.telegram_call("TOKEN", "sendMessage", {"chat_id": "CHAT"})


def test_daily_summary_uses_toronto_date_and_lists_matching_posts():
    summary = tracker.daily_summary_message(posts(), "2024-01-02")
    assert "共 5 条帖子" in summary
    assert "x.com/thsottiaux/status/1004" in summary


def test_initial_baseline_does_not_replay(monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "state.json"; monkeypatch.setattr(tracker, "STATE_PATH", state_path)
    monkeypatch.setattr(tracker, "load_config", lambda: {"username":"thsottiaux","rss_instances":["good"],"failure_threshold":3,"failure_notice_cooldown_hours":6,"request_timeout_seconds":9})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(tracker, "fetch_feed", lambda *args, **kwargs: ("good", posts()))
    sent = []; monkeypatch.setattr(tracker, "telegram_call", lambda token, method, payload, timeout=15: sent.append((method, payload)))
    assert tracker.run() == 0
    saved = json.loads(state_path.read_text())
    assert saved["initialized"] and set(saved["recent_post_ids"]) == {"1000","1001","1002","1003","1004"}
    assert len(sent) == 1 and "x.com" not in sent[0][1].get("text", "")


def test_partial_delivery_marks_only_successful(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"; state_path.write_text(json.dumps({**tracker.default_state(), "initialized": True}))
    monkeypatch.setattr(tracker, "STATE_PATH", state_path)
    monkeypatch.setattr(tracker, "load_config", lambda: {"username":"thsottiaux","rss_instances":["good"],"failure_threshold":3,"failure_notice_cooldown_hours":6,"request_timeout_seconds":9})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(tracker, "fetch_feed", lambda *args, **kwargs: ("good", [posts()[4], posts()[0]]))
    delivered = []
    def deliver(p, *args):
        delivered.append(p.post_id)
        if len(delivered) == 2: raise RuntimeError("Telegram failed")
    monkeypatch.setattr(tracker, "deliver", deliver)
    with pytest.raises(RuntimeError): tracker.run()
    assert json.loads(state_path.read_text())["recent_post_ids"] == ["1002"]
