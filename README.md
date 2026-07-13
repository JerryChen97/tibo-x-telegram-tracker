# Tibo X → Telegram tracker

Independent Python 3.12 implementation of a small RSS-to-Telegram bot. It monitors `@thsottiaux` without the official X API, logged-in scraping, cookies, paid services, databases, VPS hosting, or Cloudflare.

## Why GitHub Actions

The tracker runs on public GitHub-hosted runners every five minutes (`3/5 * * * *`), with manual `workflow_dispatch` support. A public repository is required for the intended free Actions usage. GitHub may delay scheduled jobs, and scheduled workflows can be disabled after long repository inactivity; `heartbeat.yml` runs monthly and creates a heartbeat file only after 45 days without any repository commit.

## Architecture and state

`track.yml` installs Python 3.12 and `feedparser`, then `tracker.py` tries the last successful Nitter-compatible instance first and the remaining configured instances sequentially. It requires an RSS/Atom response, rejects HTML challenges, uses a nine-second timeout, and stores the successful instance in `state.json`. Current validated candidates are `nitter.net` and `xcancel.com`; replace dead instances in `config.json`.

The committed JSON state retains the latest 200 delivered IDs. The first successful run baselines all visible items and sends one startup message without replaying history. A post ID is added only after Telegram confirms delivery. Posts are sorted oldest-first. If a later Telegram send fails, previous successful IDs remain saved and the failed/later posts are retried next run. The workflow uses `concurrency` and rebases before pushing so newer remote state is not silently overwritten; a true conflict fails for inspection.

Replies are suppressed only when the item clearly contains `replying to`, `in reply to`, or `replied to`. Reposts are suppressed only for `retweeted`, `reposted`, or an `RT @` prefix. Ambiguous items and quote posts are forwarded. RSS image URLs are extracted from HTML; one image uses `sendPhoto`, multiple use `sendMediaGroup` (up to Telegram's ten-photo group limit).

## Setup (PowerShell)

```powershell
gh auth login
gh repo create tibo-x-telegram-tracker --public --source . --remote origin --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh workflow run "Tibo RSS tracker"
gh run list --workflow track.yml --limit 5
gh run watch RUN_ID
```

Enter secret values only at the interactive prompts. Never put them in PowerShell history, `.env`, source, or logs. The workflow needs only `contents: write` and uses the built-in `GITHUB_TOKEN` to commit `state.json`.

To test locally:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest
python -m compileall .
```

## Troubleshooting

Check Actions logs for `Initialized from ...` or `Checked ...`; the instance name is logged but no secret is. If all feeds fail, a warning is sent after three consecutive failures and at most once per six hours. When a feed recovers, one recovery message is sent. Telegram failures are not counted as RSS failures. Ensure the bot can send to the configured chat and that the chat ID is correct. If an RSS instance dies or returns a challenge page, edit `config.json`, run tests, commit, and push.

Public Nitter-compatible feeds are an availability risk: hosts may disappear, rate-limit GitHub IP ranges, change XML shapes, or lose access to X. GitHub scheduler delays mean “every five minutes” is approximate. The monthly heartbeat is intentionally infrequent and does not create a commit on tracker runs unless state changes.

## Security

No official X API calls, browser cookies, logged-in X access, or personal-account automation are used. Telegram tokens are repository secrets and are never printed. Actions are pinned to stable release tags; the only third-party runtime dependency is pinned `feedparser`.

## Upstream reference

`HelenOne/tg-x-mirror` was inspected for architecture, but it has no license file. Its implementation was therefore not copied; this repository is independently reimplemented.
