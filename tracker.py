"""Poll validated public RSS sources and mirror new posts to Telegram."""
from __future__ import annotations

import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import feedparser

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
USER_AGENT = "TiboRSSGitHubAction/1.0 (+https://github.com/)"
ID_RE = re.compile(r"/status/(\d+)")
IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.I)


@dataclass
class Post:
    post_id: str
    text: str
    link: str
    images: list[str]
    published: str = ""
    raw: Any = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def default_state() -> dict[str, Any]:
    return {"initialized": False, "recent_post_ids": [], "last_good_instance": None,
            "consecutive_feed_failures": 0, "feed_unhealthy": False,
            "last_failure_notice_at": None}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    base = default_state(); base.update(value)
    return base


def save_state(state: dict[str, Any]) -> bool:
    state = dict(state)
    state["recent_post_ids"] = list(dict.fromkeys(state.get("recent_post_ids", [])))[-200:]
    new = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    old = STATE_PATH.read_text(encoding="utf-8") if STATE_PATH.exists() else ""
    if old == new:
        return False
    STATE_PATH.write_text(new, encoding="utf-8")
    return True


def canonical_link(username: str, post_id: str) -> str:
    return f"https://x.com/{username}/status/{post_id}"


def _value(entry: Any, *names: str) -> str:
    for name in names:
        value = entry.get(name, "")
        if isinstance(value, list):
            value = value[0] if value else ""
        if isinstance(value, dict):
            value = value.get("value", "")
        if value:
            return str(value)
    return ""


def clean_text(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:p|div|li|blockquote)\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_images(entry: Any) -> list[str]:
    fields = [_value(entry, "content", "content:encoded"), _value(entry, "description")]
    result: list[str] = []
    for field in fields:
        for url in IMG_RE.findall(field):
            if url.startswith(("https://", "http://")) and url not in result:
                result.append(url)
    return result


def classify(entry: Any, text: str) -> str:
    haystack = " ".join([_value(entry, "title"), _value(entry, "description"), text]).lower()
    if re.search(r"\b(retweeted|reposted)\b|^rt\s+@", haystack):
        return "repost"
    if re.search(r"\b(replying to|in reply to|replied to)\b", haystack):
        return "reply"
    return "ambiguous"


def parse_feed(data: bytes | str, username: str = "thsottiaux") -> list[Post]:
    parsed = feedparser.parse(data)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError("feed XML could not be parsed")
    if not parsed.entries:
        raise ValueError("feed has no entries")
    posts: list[Post] = []
    for entry in parsed.entries:
        candidates = [_value(entry, "link", "id", "guid")]
        links = entry.get("links", []) or []
        candidates.extend(str(x.get("href", "")) for x in links if isinstance(x, dict))
        post_id = next((m.group(1) for c in candidates if (m := ID_RE.search(c))), None)
        if not post_id:
            continue
        original = _value(entry, "content", "content:encoded", "description", "title")
        posts.append(Post(post_id, clean_text(original), canonical_link(username, post_id),
                          extract_images(entry), _value(entry, "published", "updated", "pubDate"), entry))
    if not posts:
        raise ValueError("feed has no /status/<id> entries")
    return posts


def valid_xml_response(body: bytes, content_type: str) -> bool:
    sample = body.lstrip()[:200].lower()
    return ("xml" in content_type.lower() or sample.startswith((b"<?xml", b"<rss", b"<feed"))) and not sample.startswith((b"<html", b"<!doctype"))


def fetch_feed(instances: list[str], last_good: str | None, timeout: int = 9,
               fetcher: Callable[[str, int], tuple[bytes, str]] | None = None) -> tuple[str, list[Post]]:
    ordered = ([last_good] if last_good else []) + [x for x in instances if x != last_good]
    errors: list[str] = []
    for instance in ordered:
        try:
            body, content_type = fetcher(instance, timeout) if fetcher else http_fetch(instance, timeout)
            if not valid_xml_response(body, content_type):
                raise ValueError("response was not RSS or Atom XML")
            return instance, parse_feed(body)
        except Exception as exc:
            errors.append(f"{instance}: {exc}")
    raise RuntimeError("; ".join(errors))


def http_fetch(instance: str, timeout: int) -> tuple[bytes, str]:
    url = instance.rstrip("/") + "/thsottiaux/rss"
    request = Request(url, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"})
    with urlopen(request, timeout=timeout) as response:
        body = response.read(2_000_000)
        return body, response.headers.get("Content-Type", "")


def escape_telegram(value: str) -> str:
    return html.escape(value, quote=True)


def truncate_unicode(value: str, limit: int = 3800) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def format_message(post: Post, username: str = "thsottiaux", now: datetime | None = None) -> str:
    local = (now or utc_now()).astimezone().strftime("%Y-%m-%d %H:%M")
    # The runner timezone is UTC; convert explicitly to Toronto without a third-party dependency.
    from zoneinfo import ZoneInfo
    local = (now or utc_now()).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %H:%M")
    return (f"🔔 Tibo 发布了新帖\n@{escape_telegram(username)} · {local}\n\n"
            f"{escape_telegram(truncate_unicode(post.text))}\n\n"
            f'<a href="{post.link}">在 X 查看原帖</a>')


def post_toronto_date(post: Post) -> str | None:
    if not post.published:
        return None
    try:
        value = parsedate_to_datetime(post.published)
    except (TypeError, ValueError, IndexError):
        try:
            value = datetime.fromisoformat(post.published.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    return value.astimezone(ZoneInfo("America/Toronto")).date().isoformat()


def daily_summary_message(posts: list[Post], date: str, username: str = "thsottiaux") -> str:
    matching = [p for p in posts if post_toronto_date(p) == date]
    lines = [f"📊 Tibo 每日发帖统计", f"@{escape_telegram(username)} · {date} (Toronto)", "", f"共 {len(matching)} 条帖子"]
    for post in matching:
        title = truncate_unicode(post.text.replace("\n", " "), 180) or "(无文字内容)"
        lines.append(f'• <a href="{post.link}">{escape_telegram(title)}</a>')
    return "\n".join(lines)


def telegram_call(token: str, method: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        # Telegram returns useful JSON (for example, malformed HTML or an
        # invalid image URL) together with HTTP 400.  urllib raises before
        # the normal response path, so preserve that diagnostic without
        # logging the bot URL, token, or request payload.
        try:
            error_result = json.loads(exc.read())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            error_result = {}
        description = error_result.get("description") if isinstance(error_result, dict) else None
        detail = f": {description}" if description else ""
        raise RuntimeError(f"Telegram request {method} failed with HTTP {exc.code}{detail}") from exc
    if not result.get("ok"):
        description = result.get("description")
        detail = f": {description}" if description else ""
        raise RuntimeError(f"Telegram rejected {method}{detail}")
    return result


def deliver(post: Post, token: str, chat_id: str, username: str) -> None:
    caption = format_message(post, username)
    if len(post.images) == 1:
        telegram_call(token, "sendPhoto", {"chat_id": chat_id, "photo": post.images[0], "caption": caption, "parse_mode": "HTML"})
    elif len(post.images) > 1:
        media = [{"type": "photo", "media": url, "caption": caption if i == 0 else None,
                  "parse_mode": "HTML"} for i, url in enumerate(post.images[:10])]
        media = [{k: v for k, v in x.items() if v is not None} for x in media]
        telegram_call(token, "sendMediaGroup", {"chat_id": chat_id, "media": media})
    else:
        telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"})


def should_notify_failure(state: dict[str, Any], now: datetime, cooldown_hours: int) -> bool:
    previous = state.get("last_failure_notice_at")
    if not previous:
        return True
    try:
        return now - datetime.fromisoformat(previous) >= timedelta(hours=cooldown_hours)
    except ValueError:
        return True


def run() -> int:
    config = load_config(); state = load_state(); now = utc_now()
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    daily_date = os.environ.get("DAILY_SUM_DATE", "").strip()
    if daily_date:
        try:
            datetime.strptime(daily_date, "%Y-%m-%d")
        except ValueError as exc:
            raise RuntimeError("DAILY_SUM_DATE must be YYYY-MM-DD") from exc
        _, daily_posts = fetch_feed(config["rss_instances"], None, config.get("request_timeout_seconds", 9))
        telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": daily_summary_message(daily_posts, daily_date, config["username"]), "parse_mode": "HTML"})
        print(f"Sent daily summary for {daily_date}")
        return 0
    if os.environ.get("SEND_TEST_MESSAGE", "false").lower() == "true":
        telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "🧪 Tibo Tracker 测试消息\nTelegram delivery is working."})
        print("Sent Telegram test message")
        return 0
    try:
        instance, posts = fetch_feed(config["rss_instances"], state.get("last_good_instance"), config.get("request_timeout_seconds", 9))
    except Exception as exc:
        state["consecutive_feed_failures"] += 1; state["feed_unhealthy"] = True
        if state["consecutive_feed_failures"] >= config["failure_threshold"] and should_notify_failure(state, now, config["failure_notice_cooldown_hours"]):
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "⚠️ Tibo Tracker 暂时无法读取 RSS\n所有配置的数据源当前均不可用，系统会继续自动重试。"})
            state["last_failure_notice_at"] = now.isoformat()
        save_state(state); print("RSS unavailable; state preserved")
        return 0
    was_unhealthy = state["feed_unhealthy"]; state.update(last_good_instance=instance, consecutive_feed_failures=0, feed_unhealthy=False)
    posts = sorted(posts, key=lambda p: (p.published, int(p.post_id)))
    delivered = set(state["recent_post_ids"])
    if not state["initialized"]:
        # Baseline every visible item; no history replay.
        state["initialized"] = True; state["recent_post_ids"] = list(dict.fromkeys(p.post_id for p in posts))[-200:]
        telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Tibo Tracker 已启动\n正在监控 @{config['username']} 的新帖。"})
        if was_unhealthy:
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Tibo Tracker 已恢复\nRSS 数据源重新可用。"})
        save_state(state); print(f"Initialized from {instance}"); return 0
    for post in posts:
        if post.post_id in delivered or classify(post.raw, post.text) in {"reply", "repost"}:
            continue
        deliver(post, token, chat_id, config["username"])
        state["recent_post_ids"] = (state["recent_post_ids"] + [post.post_id])[-200:]
        save_state(state)
    if was_unhealthy:
        telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Tibo Tracker 已恢复\nRSS 数据源重新可用。"})
    save_state(state); print(f"Checked {instance}; delivered new posts")
    return 0


if __name__ == "__main__":
    try: sys.exit(run())
    except (HTTPError, URLError, RuntimeError) as exc:
        print(f"Tracker error: {exc}", file=sys.stderr); sys.exit(1)
