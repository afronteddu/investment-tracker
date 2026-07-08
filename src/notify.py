"""
Notification dispatcher. Reads NOTIFY_CHANNEL from env and routes accordingly.
Supports: mac | pushover | telegram | none

Cooldown per alert key prevents notification spam.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime

import requests

_sent: dict[str, float] = {}  # key → last sent timestamp


def _cooldown_ok(key: str) -> bool:
    cooldown = int(os.getenv("ALERT_COOLDOWN_MINUTES", "60")) * 60
    last = _sent.get(key, 0)
    if time.time() - last >= cooldown:
        _sent[key] = time.time()
        return True
    return False


def send(title: str, message: str, alert_key: str = ""):
    """Send a notification. alert_key deduplicates within the cooldown window."""
    key = alert_key or f"{title}:{message}"
    if not _cooldown_ok(key):
        return

    channel = os.getenv("NOTIFY_CHANNEL", "mac").lower()

    if channel == "pushover":
        _send_pushover(title, message)
    elif channel == "telegram":
        _send_telegram(title, message)
    elif channel == "none":
        pass
    else:
        _send_mac(title, message)


def _send_mac(title: str, message: str):
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
        capture_output=True,
    )


def _send_pushover(title: str, message: str):
    user_key = os.getenv("PUSHOVER_USER_KEY", "")
    api_token = os.getenv("PUSHOVER_API_TOKEN", "")
    if not user_key or not api_token:
        _send_mac(title, message)  # fallback
        return
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": api_token, "user": user_key, "title": title, "message": message},
            timeout=5,
        )
    except Exception:
        pass


def _send_telegram(title: str, message: str):
    # No parse_mode: Markdown 400s silently when the title/body contains
    # unbalanced parens, underscores, or asterisks — which happens routinely
    # (e.g. "🟢 LDO.MI (Leonardo SpA): +3.1%", "S&P 500 ETF", RSI lines with
    # parens). Plain text is delivered reliably; boldness of the title is
    # nice-to-have, deliverability is not.
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        _send_mac(title, message)  # fallback
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": f"{title}\n\n{message}"},
            timeout=5,
        )
    except Exception:
        pass
