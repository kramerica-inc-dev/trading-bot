#!/usr/bin/env python3
"""Best-effort Telegram notifier for operator alerts (watchdog, breaker trips).

send() never raises and is a silent no-op without TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_ID in the env — alerting must never be able to take the money
path down with it. Stdlib-only (urllib), one short POST, hard timeout.
"""
from __future__ import annotations

import json
import os
import urllib.request


def send(text: str, *, timeout: float = 5.0) -> bool:
    """POST one message to the configured Telegram chat. True only on a 2xx —
    False on missing env, network failure, or a non-2xx (never raises)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text[:4000]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
