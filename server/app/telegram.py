"""Telegram: отправка с повторами и журналом (A-08), бот только для владельца (A-09, A-10)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from . import config, db
from .timeutil import UTC, now, now_iso, parse_iso

API = "https://api.telegram.org/bot{token}/{method}"
_client: httpx.AsyncClient | None = None


def enabled() -> bool:
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=40)
    return _client


async def _call(method: str, **params):
    r = await client().post(API.format(token=config.TELEGRAM_TOKEN, method=method), json=params)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("description", f"HTTP {r.status_code}"))
    return j["result"]


def muted() -> bool:
    mu = db.get_setting("muted_until")
    return bool(mu) and parse_iso(mu) > now()


async def notify(text: str, alert_id: int | None = None, force: bool = False) -> str:
    """Отправить владельцу. 3 попытки с увеличением интервала, затем статус failed в журнале."""
    log_id = db.x("INSERT INTO notification_log(alert_id,text,status,attempts,created_at) VALUES(?,?,?,?,?)",
                  (alert_id, text, "retry", 0, now_iso()))
    if not enabled():
        db.x("UPDATE notification_log SET status='failed' WHERE id=?", (log_id,))
        db.log("error", "telegram", "Не настроены TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
        return "failed"
    if muted() and not force:
        db.x("UPDATE notification_log SET status='muted' WHERE id=?", (log_id,))
        return "muted"
    delay = 2
    for attempt in range(1, 4):
        try:
            await _call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text,
                        parse_mode="HTML", disable_web_page_preview=True)
            db.x("UPDATE notification_log SET status='sent', attempts=?, delivered_at=? WHERE id=?",
                 (attempt, now_iso(), log_id))
            return "sent"
        except Exception as e:  # noqa: BLE001
            db.x("UPDATE notification_log SET attempts=? WHERE id=?", (attempt, log_id))
            db.log("error", "telegram", f"Попытка {attempt}: {e}")
            if attempt < 3:
                await asyncio.sleep(delay)
                delay *= 4
    db.x("UPDATE notification_log SET status='failed' WHERE id=?", (log_id,))
    return "failed"


async def reply(chat_id, text: str) -> None:
    try:
        await _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        db.log("error", "telegram", f"Ответ на команду: {e}")


async def poll_commands(handler) -> None:
    """Long polling getUpdates. Сообщения не от владельца игнорируются (A-09)."""
    if not enabled():
        return
    offset = None
    try:
        await _call("deleteWebhook")
    except Exception:  # noqa: BLE001
        pass
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = await _call("getUpdates", **params)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                if chat != str(config.TELEGRAM_CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    try:
                        await reply(chat, await handler(text))
                    except Exception as e:  # noqa: BLE001
                        db.log("error", "bot", f"{text}: {e}")
                        await reply(chat, "Ошибка при выполнении команды, подробности в журнале.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            db.log("error", "telegram", f"getUpdates: {e}")
            await asyncio.sleep(15)


__all__ = ["notify", "poll_commands", "enabled", "muted", "datetime", "UTC"]
