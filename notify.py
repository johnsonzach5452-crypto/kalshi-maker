"""Discord notifications + global secret scrubbing.

Every outbound message AND every log line passes through scrub(), so an
API key can never reach Railway logs or Discord even inside an exception
string (requests exceptions embed the full URL, apiKey param included).
"""
import logging
import os
import re

import requests

log = logging.getLogger("notify")

_SECRET_ENV_KEYS = ("ODDS_API_KEY", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY",
                    "DISCORD_WEBHOOK")
_SECRETS = [v for v in (os.environ.get(k, "") for k in _SECRET_ENV_KEYS)
            if v and len(v) >= 8]
_PATTERNS = [
    re.compile(r"(apiKey=)[^&\s\"']+", re.I),
    re.compile(r"(discord\.com/api/webhooks/)\S+", re.I),
    re.compile(r"-----BEGIN[A-Z ]+PRIVATE KEY-----[\s\S]*?-----END[A-Z ]+PRIVATE KEY-----"),
]


def scrub(text: str) -> str:
    if not text:
        return text
    for s in _SECRETS:
        text = text.replace(s, "[REDACTED]")
    for pat in _PATTERNS:
        text = pat.sub(r"\1[REDACTED]" if pat.groups else "[REDACTED]", text)
    return text


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def install_log_scrubber():
    """Attach the scrubber to the root logger's handlers so every log line
    is cleaned regardless of which module emitted it."""
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(_ScrubFilter())


def notify(msg: str):
    msg = scrub(msg)
    log.info(f"NOTIFY: {msg}")
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": msg[:1900]}, timeout=10)
    except requests.RequestException as e:
        log.warning(f"discord post failed: {scrub(str(e))}")
