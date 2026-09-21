"""Opt-in, allowlisted Telegram polling adapter for FRIDAY.

This is a transport boundary only.  The caller supplies a command handler;
this module does not expose a shell, filesystem, or interpreter API.
"""

import asyncio
from dataclasses import dataclass
import inspect
import io
import json
import logging
import os
import time
from pathlib import Path

import requests

from . import api_pools


LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 4096
DEFAULT_POLL_TIMEOUT = 25
MAX_POLL_TIMEOUT = 30
DEFAULT_REQUEST_TIMEOUT = 35
MAX_BACKOFF = 30.0


@dataclass
class TelegramConfig:
    token: str = ""
    allowed_chat_ids: frozenset = frozenset()
    owner_username: str = ""  # owner bound during setup mode (@handle)

    @property
    def enabled(self):
        return bool(self.token and self.allowed_chat_ids)

    @property
    def setup_mode(self):
        """Token present but nobody bound yet: the only interaction is
        telling the OWNER their numeric chat id. No commands run."""
        return bool(self.token) and not self.allowed_chat_ids


@dataclass(frozen=True)
class TelegramResponse:
    ok: bool
    text: str
    reason: str = ""


def credentials_path():
    return os.environ.get("FRIDAY_CREDENTIALS_FILE") or api_pools.CREDENTIALS_DEFAULT_PATH


def load_config(environ=None, ignore_credentials=False):
    environ = os.environ if environ is None else environ
    token = str(environ.get("FRIDAY_TELEGRAM_BOT_TOKEN", "")).strip()
    raw_ids = str(environ.get("FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS", ""))
    owner = str(environ.get("FRIDAY_TELEGRAM_OWNER_USERNAME", "")).strip()
    credential_source = credentials_path()
    if ignore_credentials:
        credential_source = ""  # hermetic tests never touch the real store
    if (not token or not raw_ids or not owner) and not ignore_credentials:
        # The sealed credentials file is the primary store; env vars override.
        try:
            with open(credential_source, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        token = token or str(data.get("telegram_bot_token", "")).strip()
        if not raw_ids:
            raw_ids = ",".join(str(one) for one in (data.get("telegram_allowed_chat_ids") or []))
        owner = owner or str(data.get("telegram_owner_username", "")).strip()
    allowed = frozenset(item.strip() for item in raw_ids.split(",") if item.strip())
    return TelegramConfig(token=token, allowed_chat_ids=allowed, owner_username=owner)


def persist_config(token=None, allowed_chat_ids=None, owner_username=None):
    """Merge Telegram settings into the sealed credentials file. Secrets
    never print; the store lives outside the repo (under ~/.friday)."""
    path = credentials_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if token:
        data["telegram_bot_token"] = str(token).strip()
    if allowed_chat_ids is not None:
        existing = {str(one) for one in (data.get("telegram_allowed_chat_ids") or [])}
        existing.update(str(one) for one in allowed_chat_ids)
        data["telegram_allowed_chat_ids"] = sorted(existing)
    if owner_username:
        data["telegram_owner_username"] = str(owner_username).strip()
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)


def redact_secret(value, secret):
    text = str(value)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text.replace("bot[REDACTED]", "bot[REDACTED]")


class TelegramRemoteAdapter:
    """One-message-at-a-time Telegram long-poll worker.

    ``command_handler`` receives ``(text, chat_id)`` and may return a string
    or an awaitable string.  It must enforce any FRIDAY-specific confirmation
    policy itself; this adapter never executes commands directly.
    """

    def __init__(self, command_handler, *, config=None, poll_timeout=DEFAULT_POLL_TIMEOUT,
                 request_timeout=DEFAULT_REQUEST_TIMEOUT, min_message_interval=0.5,
                 sleep=asyncio.sleep):
        if not callable(command_handler):
            raise TypeError("command_handler must be callable")
        self.command_handler = command_handler
        self.config = config or load_config()
        self.poll_timeout = max(1, min(int(poll_timeout), MAX_POLL_TIMEOUT))
        self.request_timeout = max(self.poll_timeout + 1, min(float(request_timeout), 120.0))
        self.min_message_interval = max(0.0, min(float(min_message_interval), 3600.0))
        self._sleep = sleep
        self._task = None
        self._stop_event = asyncio.Event()
        self._offset = None
        self._last_message_at = {}
        self._last_error = ""
        self._processed = 0

    @property
    def enabled(self):
        return self.config.enabled

    def status(self):
        task = self._task
        return {
            "enabled": self.enabled,
            "running": bool(task and not task.done()),
            "allowed_chat_count": len(self.config.allowed_chat_ids),
            "offset": self._offset,
            "processed": self._processed,
            "last_error": self._last_error,
        }

    @property
    def enabled(self):
        """Fully armed (token + bound chats). Setup mode polls too — it just
        refuses to run any command until the owner chat binds."""
        return self.config.enabled

    @property
    def can_poll(self):
        return bool(self.config.token)

    async def start(self, validate=True):
        if not self.can_poll:
            self._last_error = "Telegram adapter disabled: bot token is required"
            return False
        if self._task and not self._task.done():
            return True
        if validate:
            self.validate_token()  # fail fast on 401 instead of polling blind
        self._stop_event.clear()
        self._last_error = ""
        self._task = asyncio.create_task(self._run(), name="friday-telegram")
        return True

    def validate_token(self):
        """getMe sanity check at startup: good tokens show the bot identity,
        bad ones raise immediately (fail fast instead of polling blind)."""
        try:
            payload = requests.post(self._url("getMe"), json={}, timeout=self.request_timeout).json()
            bot_name = str(payload.get("result", {}).get("username") or "")
            if payload.get("ok"):
                print(f"[telegram] bot authenticated as @{bot_name}", flush=True)
                return True
            self._last_error = "Telegram getMe rejected the token: " + str(payload.get("description", ""))
        except Exception as error:
            self._last_error = redact_secret(error, self.config.token)
        print(f"[telegram] token check failed: {self._last_error}", flush=True)
        return False

    async def stop(self, timeout=3.0):
        self._stop_event.set()
        task = self._task
        if task and not task.done():
            try:
                await asyncio.wait_for(task, max(0.0, float(timeout)))
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return not self._task or self._task.done()

    def _url(self, method):
        return f"https://api.telegram.org/bot{self.config.token}/{method}"

    def _request(self, method, params):
        response = requests.post(self._url(method), json=params, timeout=self.request_timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError("Telegram API returned an unsuccessful response")
        return payload.get("result", [])

    async def _api(self, method, params):
        return await asyncio.to_thread(self._request, method, params)

    async def _run(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            params = {"timeout": self.poll_timeout, "allowed_updates": '["message"]'}
            if self._offset is not None:
                params["offset"] = self._offset
            try:
                await self._poll_once(params)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (requests.Timeout, TimeoutError):
                self._last_error = "Telegram polling timed out; retrying"
                await self._wait(backoff)
                backoff = min(MAX_BACKOFF, backoff * 2)
            except Exception as error:
                self._last_error = redact_secret(error, self.config.token)
                LOGGER.warning("Telegram polling failed: %s", self._last_error)
                await self._wait(backoff)
                backoff = min(MAX_BACKOFF, backoff * 2)

    async def _poll_once(self, params=None):
        """Fetch and process one bounded batch; useful to lifecycle callers/tests."""
        if params is None:
            params = {"timeout": self.poll_timeout, "allowed_updates": '["message"]'}
            if self._offset is not None:
                params["offset"] = self._offset
        updates = await self._api("getUpdates", params)
        if not isinstance(updates, list):
            raise RuntimeError("Telegram updates response was not a list")
        for update in updates:
            if self._stop_event.is_set():
                break
            await self.process_update(update)
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(update_id, int):
                self._offset = update_id + 1
        return len(updates)

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def process_update(self, update):
        """Process one update and return a truthful, non-credential response."""
        if not self.config.token:
            return TelegramResponse(False, "Telegram adapter disabled (no token).", "disabled")
        message = update.get("message", {}) if isinstance(update, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        sender = message.get("from", {}) if isinstance(message, dict) else {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return TelegramResponse(False, "Unsupported Telegram message.", "unsupported")
        # Setup mode: bind the configured owner account ONLY, by reading the
        # numeric chat id from their very first message. Nobody else ever
        # gets a reply or any action.
        if self.config.setup_mode:
            return await self._setup_mode(message, sender, chat_id)
        if chat_id not in self.config.allowed_chat_ids:
            # Strangers: completely ignored (no reply, no PC actions).
            return TelegramResponse(False, "", "unauthorized")
        now = time.monotonic()
        if now - self._last_message_at.get(chat_id, 0.0) < self.min_message_interval:
            response = TelegramResponse(False, "Rate limit exceeded; try again shortly.", "rate_limited")
            await self._send(chat_id, response.text)
            return response
        self._last_message_at[chat_id] = now
        photo_jpeg = await self._download_photo(message)
        text = str(message.get("text", "") or message.get("caption", "") or "").strip()
        photo_sent = False
        if photo_jpeg is None and not text:
            response = TelegramResponse(False, "Empty command.", "empty")
        else:
            try:
                result = self.command_handler(text[:MAX_MESSAGE_CHARS], chat_id, message=message)
                if inspect.isawaitable(result):
                    result = await result
                if result is None:
                    # The handler already delivered its own media (e.g. the
                    # screenshot photo) — do not add a "no response" bubble.
                    return TelegramResponse(True, "", "media_only")
                result = str(result)[:MAX_MESSAGE_CHARS]
                response = TelegramResponse(True, result)
            except Exception as error:
                safe_error = redact_secret(error, self.config.token)
                self._last_error = safe_error
                # Log the real culprit (never leaking the token) so a generic
                # "Command handler failed" is never a silent black box again.
                LOGGER.warning("Telegram command handler raised: %s", str(safe_error)[:600])
                response = TelegramResponse(False, "Command handler failed; no action was confirmed.", "handler_error")
            # Incoming photo: run the SAME vision stack FRIDAY uses for screens.
            if photo_jpeg is not None and callable(getattr(self, "photo_handler", None)):
                try:
                    question = text or "What is in this image? Answer in one warm sentence."
                    seen = self.photo_handler(photo_jpeg, question, chat_id)
                    if inspect.isawaitable(seen):
                        seen = await seen
                    if seen:
                        response = TelegramResponse(True, str(seen)[:MAX_MESSAGE_CHARS])
                except Exception as error:
                    self._last_error = redact_secret(error, self.config.token)
                    response = TelegramResponse(False, "Vision did not confirm that image.", "vision_error")
        await self._send(chat_id, response.text)
        self._processed += 1
        return response

    async def _setup_mode(self, message, sender, chat_id):
        """First-contact binding for the owner account; everyone else: only
        their numeric chat id is echoed back (identity help), never control.

        The owner is identified by their Telegram username (persisted in the
        credentials file). Their numeric chat id is stored durably on match —
        the username is just the setup bootstrap.
        """
        owner = str(self.config.owner_username or "").lstrip("@").lower()
        username = str(sender.get("username") or "").lstrip("@").lower()
        LOGGER.info(
            "telegram setup contact: chat_id=%s username=%s owner=%s",
            chat_id, username or "<none>", owner or "<unset>",
        )
        if owner and username == owner:
            persist_config(allowed_chat_ids=[chat_id])
            self.config.allowed_chat_ids = frozenset({chat_id})
            await self._send(chat_id, f"Its on.\nLinked: chat id {chat_id} (@{username}) can now control FRIDAY. Everyone else is ignored.")
            self._processed += 1
            return TelegramResponse(True, "Owner chat bound.", "setup_bound")
        # Not the owner: help them find THEIR numeric id for the user, but
        # no binding happens and nothing can be controlled.
        await self._send(
            chat_id,
            "Its on.\nSetup: your numeric chat id is "
            f"{chat_id}. Tell FRIDAY to add this id to the allowlist if this is your account.",
        )
        return TelegramResponse(False, "Setup contact.", "setup_mode_unbound")

    async def _download_photo(self, message):
        """Largest photo variant → JPEG bytes (or None). Telegram 'photos'
        and image documents both flow through the vision system."""
        photos = message.get("photo") or []
        file_id = None
        if isinstance(photos, list) and photos:
            matrix = [entry for entry in photos if isinstance(entry, dict) and entry.get("file_id")]
            if matrix:
                biggest = max(matrix, key=lambda entry: entry.get("file_size") or 0)
                file_id = biggest.get("file_id")
        if file_id is None:
            document = message.get("document") or {}
            if (document.get("mime_type") or "").startswith("image/"):
                file_id = document.get("file_id")
        if not file_id:
            return None
        try:
            payload = requests.post(self._url("getFile"), json={"file_id": file_id},
                                    timeout=self.request_timeout).json()
            file_path = str(payload.get("result", {}).get("file_path", ""))
            if not file_path:
                return None
            file_url = f"https://api.telegram.org/file/bot{self.config.token}/{file_path}"
            response = requests.get(file_url, timeout=self.request_timeout)
            response.raise_for_status()
            return response.content
        except Exception as error:
            self._last_error = redact_secret(error, self.config.token)
            LOGGER.warning("Telegram photo download failed: %s", self._last_error)
            return None

    async def send_text(self, chat_id, text):
        """Public send used by the startup greeting and response capture."""
        await self._send(chat_id, text)

    async def send_photo(self, chat_id, jpeg_bytes, caption=None):
        """Send a JPEG (e.g. an on-demand PC screenshot) back to the chat."""
        try:
            buffer = io.BytesIO(jpeg_bytes)
            buffer.name = "friday_screenshot.jpg"
            response = await asyncio.to_thread(
                requests.post,
                self._url("sendPhoto"),
                files={"photo": ("screenshot.jpg", buffer.getvalue(), "image/jpeg")},
                data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            return True
        except Exception as error:
            self._last_error = redact_secret(error, self.config.token)
            LOGGER.warning("Telegram sendPhoto failed: %s", self._last_error)
            return False

    async def announce(self, text="its on"):
        """Startup greeting to every allowlisted chat (and owner in setup)."""
        chats = self.config.allowed_chat_ids
        if not chats and self.config.setup_mode:
            return False  # announced only after the owner binds their chat
        for chat_id in sorted(chats):
            await self.send_text(chat_id, text)
        return bool(chats)

    async def _send(self, chat_id, text):
        try:
            await self._api("sendMessage", {"chat_id": chat_id, "text": str(text)[:MAX_MESSAGE_CHARS]})
        except Exception as error:
            self._last_error = redact_secret(error, self.config.token)
            LOGGER.warning("Telegram response failed: %s", self._last_error)
