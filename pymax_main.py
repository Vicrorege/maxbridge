import asyncio
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from pymax import MaxClient, Message
from pymax.files import File, Photo, Video
from pymax.formatter import ColoredFormatter
from pymax.payloads import UserAgentPayload
from pymax.types import (
    AudioAttach,
    ContactAttach,
    ControlAttach,
    FileAttach,
    PhotoAttach,
    StickerAttach,
    VideoAttach,
)

load_dotenv(override=True)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_handler = logging.StreamHandler()


class BridgeFormatter(ColoredFormatter):
    """ColoredFormatter из pymax отбрасывает traceback — возвращаем его."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.exc_info:
            return f"{message}\n{self.formatException(record.exc_info)}"
        return message


root_handler.setFormatter(BridgeFormatter())
root_logger.addHandler(root_handler)
logger = logging.getLogger("maxbridge")

MAX_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DOWNLOAD_HEADERS = {
    "User-Agent": MAX_USER_AGENT,
    "Accept": "*/*",
}

PHONE = os.getenv("PHONE")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY") or None
FORWARD_TG_TO_MAX = os.getenv("FORWARD_TG_TO_MAX", "false").lower() in ("true", "1", "yes")

# Пути к файлам персистентности в кэше
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
COMMON_CHAT_FILE = CACHE_DIR / "common_chat_id.txt"
EXCLUDED_CHATS_FILE = CACHE_DIR / "excluded_chats.json"

# Общий чат Telegram
COMMON_TG_CHAT_ENV_RAW = (
    os.getenv("COMMON_TG_CHAT")
    or os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TG_CHAT_ID")
    or ""
).strip()
COMMON_TG_CHAT_ENV: int | None = int(COMMON_TG_CHAT_ENV_RAW) if COMMON_TG_CHAT_ENV_RAW else None


def load_common_chat_id() -> int | None:
    if COMMON_TG_CHAT_ENV is not None:
        return COMMON_TG_CHAT_ENV
    if COMMON_CHAT_FILE.exists():
        try:
            val = COMMON_CHAT_FILE.read_text(encoding="utf-8").strip()
            if val:
                return int(val)
        except Exception as error:
            logger.warning("Не удалось прочитать %s: %s", COMMON_CHAT_FILE, error)
    return None


def save_common_chat_id(chat_id: int) -> None:
    try:
        COMMON_CHAT_FILE.write_text(str(chat_id), encoding="utf-8")
        logger.info("Сохранен общий чат Telegram: %s", chat_id)
    except Exception as error:
        logger.warning("Не удалось сохранить %s: %s", COMMON_CHAT_FILE, error)


CURRENT_COMMON_TG_CHAT: int | None = load_common_chat_id()

# Показывать ли префикс с названием чата [Название чата]
SHOW_CHAT_TITLE = os.getenv("SHOW_CHAT_TITLE", "true").lower() in ("true", "1", "yes")

# Исключения чатов MAX
EXCLUDE_CHATS_RAW = os.getenv("EXCLUDE_CHATS") or os.getenv("EXCLUDE_MAX_CHATS") or os.getenv("IGNORED_CHATS")


def parse_exclude_chats(raw: str | None) -> set[int]:
    if not raw:
        return set()
    raw = raw.strip()
    if not raw:
        return set()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return {int(x) for x in data}
        except Exception as error:
            logger.warning("Не удалось распарсить EXCLUDE_CHATS как JSON: %s", error)
    result = set()
    for item in raw.replace(",", " ").split():
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                logger.warning("Некорректный ID чата в EXCLUDE_CHATS: %s", item)
    return result


def load_persisted_exclusions() -> set[int]:
    if EXCLUDED_CHATS_FILE.exists():
        try:
            data = json.loads(EXCLUDED_CHATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {int(x) for x in data}
        except Exception as error:
            logger.warning("Не удалось прочитать %s: %s", EXCLUDED_CHATS_FILE, error)
    return set()


def save_persisted_exclusions(exclusions: set[int]) -> None:
    try:
        EXCLUDED_CHATS_FILE.write_text(
            json.dumps(list(exclusions), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as error:
        logger.warning("Не удалось сохранить %s: %s", EXCLUDED_CHATS_FILE, error)


EXCLUDED_CHATS: set[int] = parse_exclude_chats(EXCLUDE_CHATS_RAW) | load_persisted_exclusions()


def telegram_proxy_for_log(proxy: str) -> str:
    """Скрывает user:password в URL прокси для логов."""
    if "@" not in proxy:
        return proxy
    scheme, rest = proxy.split("://", 1) if "://" in proxy else ("", proxy)
    credentials, host = rest.rsplit("@", 1)
    if ":" in credentials:
        user, _password = credentials.split(":", 1)
        redacted = f"{user}:***"
    else:
        redacted = "***"
    return f"{scheme}://{redacted}@{host}" if scheme else f"{redacted}@{host}"


if not PHONE:
    raise RuntimeError("В .env не задан PHONE")
if not BOT_TOKEN:
    raise RuntimeError("В .env не задан BOT_TOKEN")


def parse_chats(value: str | None) -> dict[int, int]:
    if not value:
        return {}
    try:
        raw_chats = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError("В .env CHATS должен быть JSON-объектом") from error
    if not isinstance(raw_chats, dict):
        raise RuntimeError("В .env CHATS должен быть JSON-объектом")
    try:
        return {int(max_id): int(telegram_id) for max_id, telegram_id in raw_chats.items()}
    except (TypeError, ValueError) as error:
        raise RuntimeError("В .env CHATS должен быть формата {\"max_chat_id\": telegram_chat_id}") from error


CHATS_JSON = os.getenv("CHATS")
CHATS = parse_chats(CHATS_JSON)
CHATS_TELEGRAM = {telegram_id: max_id for max_id, telegram_id in CHATS.items()}


class BoundedDict(OrderedDict):
    def __init__(self, maxsize: int = 10000, *args: Any, **kwargs: Any) -> None:
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)


# Кэши сопоставления сообщений для реплаев
MAX_TO_TG_MSG: BoundedDict = BoundedDict(maxsize=10000)
TG_TO_MAX_MSG: BoundedDict = BoundedDict(maxsize=10000)

# Кэш названий чатов MAX и известных чатов
CHAT_TITLE_CACHE: dict[int, str] = {}
KNOWN_CHATS: dict[int, dict[str, Any]] = {}


class BridgeMaxClient(MaxClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_session_alert: float = 0.0

    async def _send_notification_response(self, chat_id: int, message_id: str) -> None:
        logger.debug("Skip MAX message ACK: chat_id=%s message_id=%s", chat_id, message_id)

    async def _handle_message_notifications(self, data: dict[str, Any]) -> None:
        try:
            await super()._handle_message_notifications(data)
        except Exception:
            logger.exception("PyMax не смог обработать входящее MAX-сообщение: %r", data)

    async def alert_session_dropped(self, reason: str = "") -> None:
        now = time.time()
        # Защита от спама алертами (не чаще 1 раза в 5 минут)
        if now - self.last_session_alert < 300:
            return
        self.last_session_alert = now
        logger.error("Сессия MAX потеряна или требует повторной авторизации: %s", reason)

        target_chat = CURRENT_COMMON_TG_CHAT or (list(CHATS.values())[0] if CHATS else None)
        if target_chat:
            alert_msg = (
                "⚠️ <b>Внимание: Сессия MAX слетела или требует входа!</b>\n\n"
                f"<i>Причина: {reason or 'Потеря соединения / недействительный токен'}</i>\n\n"
                "🛠 <b>Как быстро переавторизоваться:</b>\n"
                "1. Подключитесь к контейнеру на сервере:\n"
                "   <code>docker attach maxbridge</code>\n"
                "   <i>(введите код из SMS/приложения или подтвердите вход)</i>\n\n"
                "2. Или перезапустите мост:\n"
                "   <code>cd /root/maxbridge && docker compose restart maxbridge && docker attach maxbridge</code>"
            )
            try:
                await telegram_bot.send_message(
                    chat_id=target_chat,
                    text=alert_msg,
                    parse_mode="HTML",
                )
            except Exception as error:
                logger.error("Не удалось отправить алерт о сессии в Telegram: %s", error)

    async def _sync(self, user_agent: UserAgentPayload | None = None) -> None:
        try:
            await super()._sync(user_agent)
        except Exception as error:
            await self.alert_session_dropped(f"Ошибка синхронизации: {error}")
            raise

    async def _login(self) -> None:
        await self.alert_session_dropped("Сессия не найдена или устарела. Требуется ввод кода подтверждения.")
        await super()._login()


MAX_HEADERS = UserAgentPayload(
    device_type="WEB",
    device_name="Chrome",
    os_version="macOS 10.15.7",
    header_user_agent=MAX_USER_AGENT,
    screen="1440x900 1.0x",
    timezone="Europe/Moscow",
)

client = BridgeMaxClient(
    phone=PHONE,
    work_dir="cache",
    headers=MAX_HEADERS,
    reconnect=True,
    proxy=None,
)
client.logger.propagate = False
telegram_session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else AiohttpSession()
telegram_bot = Bot(token=BOT_TOKEN, session=telegram_session)
dp = Dispatcher()

if TELEGRAM_PROXY:
    logger.info("Telegram traffic via proxy: %s", telegram_proxy_for_log(TELEGRAM_PROXY))
else:
    logger.info("Telegram traffic: direct (TELEGRAM_PROXY not set)")

logger.info("Forwarding TG -> MAX is strictly %s", "ENABLED" if FORWARD_TG_TO_MAX else "DISABLED")
if CURRENT_COMMON_TG_CHAT:
    logger.info("Current common Telegram chat: %s", CURRENT_COMMON_TG_CHAT)
if CHATS:
    logger.info("1-to-1 mapped chats: %s", CHATS)
if EXCLUDED_CHATS:
    logger.info("Excluded MAX chats count: %d (%s)", len(EXCLUDED_CHATS), list(EXCLUDED_CHATS))


def build_author_name(user: object | None, fallback: str = "MAX") -> str:
    names = getattr(user, "names", None) or []
    if names:
        entry = names[0]
        name = (getattr(entry, "name", None) or "").strip()
        first = (getattr(entry, "first_name", None) or "").strip()
        last = (getattr(entry, "last_name", None) or "").strip()
        given = first or name
        if given and last and last not in given:
            return f"{given} {last}"
        if given:
            return given
        if last:
            return last
    return fallback


def telegram_display_name(user: types.User | None, fallback: str = "Telegram") -> str:
    if user is None:
        return fallback
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()
    if first and last:
        return f"{first} {last}"
    return first or last or fallback


def build_text(author: str, text: str | None, chat_title: str | None = None) -> str:
    text = (text or "").strip()
    prefix = f"[{chat_title}] " if chat_title else ""
    return f"{prefix}{author}: {text}" if text else f"{prefix}{author}:"


def build_caption(author: str, text: str | None, chat_title: str | None = None) -> str:
    return build_text(author, text, chat_title)[:1024]


async def author_name_from_sender(sender: int | None, fallback: str = "MAX") -> str:
    if sender is None:
        return fallback

    try:
        user = await client.get_user(sender)
    except Exception:
        logger.exception("Не удалось получить имя MAX-пользователя %s", sender)
        return fallback

    return build_author_name(user, fallback)


async def get_chat_title(chat_id: int, sender_id: int | None = None) -> str:
    """Получает человекочитаемое название чата MAX (с кэшированием)."""
    if chat_id in CHAT_TITLE_CACHE:
        return CHAT_TITLE_CACHE[chat_id]

    title: str | None = None

    # Проверка client.chats
    for c in getattr(client, "chats", []):
        if c.id == chat_id and getattr(c, "title", None):
            title = c.title.strip()
            break

    # Запрос get_chat в API
    if not title:
        try:
            chat = await client.get_chat(chat_id)
            if chat and getattr(chat, "title", None):
                title = chat.title.strip()
        except Exception:
            pass

    # Проверка диалогов (1-на-1)
    if not title:
        for d in getattr(client, "dialogs", []):
            if d.id == chat_id:
                other_user_id = None
                if hasattr(d, "participants") and isinstance(d.participants, dict):
                    for p_id_str in d.participants.keys():
                        try:
                            p_id = int(p_id_str)
                            if client.me and p_id != client.me.id:
                                other_user_id = p_id
                                break
                        except (ValueError, TypeError):
                            pass
                if other_user_id:
                    name = await author_name_from_sender(other_user_id)
                    if name and name != "MAX":
                        title = f"ЛС: {name}"
                break

    # Если chat_id может быть user_id
    if not title:
        try:
            user = await client.get_user(chat_id)
            if user:
                name = build_author_name(user)
                if name and name != "MAX":
                    title = f"ЛС: {name}"
        except Exception:
            pass

    if not title and sender_id is not None and chat_id == sender_id:
        name = await author_name_from_sender(sender_id)
        if name and name != "MAX":
            title = f"ЛС: {name}"

    if not title:
        title = f"Чат {chat_id}"

    CHAT_TITLE_CACHE[chat_id] = title
    KNOWN_CHATS[chat_id] = {"title": title, "id": chat_id}
    return title


async def resolve_content_message(message: Message) -> tuple[Message, int]:
    """Сообщение для пересылки: свои attaches важнее link; REPLY не разворачиваем."""
    incoming_chat_id = message.chat_id
    if incoming_chat_id is None:
        raise RuntimeError("У MAX-сообщения нет chat_id")

    if message.attaches:
        return message, incoming_chat_id

    current = message
    source_chat_id = incoming_chat_id
    for _ in range(20):
        link = current.link
        if link is None or link.message is None:
            break
        if (link.type or "").upper() == "REPLY":
            break
        source_chat_id = link.chat_id or source_chat_id
        current = link.message

    return current, source_chat_id


def filename_from_response(response: aiohttp.ClientResponse, fallback: str) -> str:
    return response.headers.get("X-File-Name") or fallback


async def download_bytes(url: str, fallback_name: str) -> tuple[bytes, str]:
    timeout = aiohttp.ClientTimeout(total=90, connect=20, sock_read=60)
    last_error: BaseException | None = None

    for attempt in range(1, 4):
        logger.info("Скачиваю файл из MAX, попытка %s/3: %s", attempt, url)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=DOWNLOAD_HEADERS) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    attach_bytes = BytesIO(await response.read())
                    attach_bytes.name = filename_from_response(response, fallback_name)
                    logger.info(
                        "Файл из MAX скачан: %s, %s байт",
                        attach_bytes.name,
                        len(attach_bytes.getvalue()),
                    )
                    return attach_bytes.getvalue(), attach_bytes.name
        except Exception as error:
            last_error = error
            logger.exception("Не удалось скачать файл из MAX, попытка %s/3: %s", attempt, url)
            if attempt < 3:
                await asyncio.sleep(attempt)

    if last_error is not None:
        raise RuntimeError("Не удалось скачать файл из MAX после 3 попыток") from last_error
    raise RuntimeError("Не удалось скачать файл из MAX после 3 попыток")


async def resolve_max_media_url(
    *,
    kind: str,
    primary_chat_id: int,
    primary_message_id: int | str,
    media_id: int,
    fallback_chat_id: int | None = None,
    fallback_message_id: int | str | None = None,
) -> Any:
    """FILE/VIDEO download: сначала ids сообщения с вложением, затем ids входящего."""
    attempts: list[tuple[int, int | str]] = [(primary_chat_id, primary_message_id)]
    if (
        fallback_chat_id is not None
        and fallback_message_id is not None
        and (fallback_chat_id, fallback_message_id) not in attempts
    ):
        attempts.append((fallback_chat_id, fallback_message_id))

    last_error: BaseException | None = None
    for chat_id, message_id in attempts:
        try:
            if kind == "file":
                result = await client.get_file_by_id(
                    chat_id=chat_id,
                    message_id=message_id,
                    file_id=media_id,
                )
            else:
                result = await client.get_video_by_id(
                    chat_id=chat_id,
                    message_id=message_id,
                    video_id=media_id,
                )
            if result is None:
                raise RuntimeError(f"MAX не вернул ссылку на {kind} {media_id}")
            return result
        except Exception as error:
            last_error = error
            logger.warning(
                "Не удалось получить %s_id=%s через chat_id=%s message_id=%s: %s",
                kind,
                media_id,
                chat_id,
                message_id,
                error,
            )

    assert last_error is not None
    raise last_error


async def send_to_telegram(
    tg_id: int,
    text: str,
    reply_to_tg_msg_id: int | None = None,
) -> types.Message:
    reply_params = (
        types.ReplyParameters(message_id=reply_to_tg_msg_id)
        if reply_to_tg_msg_id
        else None
    )
    try:
        return await telegram_bot.send_message(
            chat_id=tg_id,
            text=text,
            reply_parameters=reply_params,
        )
    except TelegramBadRequest as error:
        if reply_params and (
            "message to reply not found" in str(error).lower()
            or "replied message not found" in str(error).lower()
        ):
            return await telegram_bot.send_message(chat_id=tg_id, text=text)
        raise


async def send_max_attachment_to_telegram(
    tg_id: int,
    max_chat_id: int,
    max_message: Message,
    attach: Any,
    caption: str,
    fallback_chat_id: int | None = None,
    fallback_message_id: int | str | None = None,
    reply_to_tg_msg_id: int | None = None,
) -> types.Message | None:
    reply_params = (
        types.ReplyParameters(message_id=reply_to_tg_msg_id)
        if reply_to_tg_msg_id
        else None
    )

    if isinstance(attach, PhotoAttach):
        photo_bytes, filename = await download_bytes(attach.base_url, "photo.jpg")
        logger.info("Отправляю фото в Telegram: %s, %s байт", filename, len(photo_bytes))
        try:
            return await telegram_bot.send_photo(
                chat_id=tg_id,
                caption=caption,
                photo=types.BufferedInputFile(photo_bytes, filename=filename),
                reply_parameters=reply_params,
            )
        except TelegramBadRequest:
            logger.exception("Telegram не принял файл как фото, отправляю документом: %s", filename)
            return await telegram_bot.send_document(
                chat_id=tg_id,
                caption=caption,
                document=types.BufferedInputFile(photo_bytes, filename=filename),
                reply_parameters=reply_params,
            )

    if isinstance(attach, VideoAttach):
        video = await resolve_max_media_url(
            kind="video",
            primary_chat_id=max_chat_id,
            primary_message_id=max_message.id,
            media_id=attach.video_id,
            fallback_chat_id=fallback_chat_id,
            fallback_message_id=fallback_message_id,
        )
        video_bytes, filename = await download_bytes(video.url, "video.mp4")
        logger.info("Отправляю видео в Telegram: %s, %s байт", filename, len(video_bytes))
        try:
            return await telegram_bot.send_video(
                chat_id=tg_id,
                caption=caption,
                video=types.BufferedInputFile(video_bytes, filename=filename),
                reply_parameters=reply_params,
            )
        except TelegramBadRequest:
            return await telegram_bot.send_document(
                chat_id=tg_id,
                caption=caption,
                document=types.BufferedInputFile(video_bytes, filename=filename),
                reply_parameters=reply_params,
            )

    if isinstance(attach, FileAttach):
        file = await resolve_max_media_url(
            kind="file",
            primary_chat_id=max_chat_id,
            primary_message_id=max_message.id,
            media_id=attach.file_id,
            fallback_chat_id=fallback_chat_id,
            fallback_message_id=fallback_message_id,
        )
        file_bytes, filename = await download_bytes(file.url, attach.name or "file")
        logger.info("Отправляю документ в Telegram: %s, %s байт", filename, len(file_bytes))
        return await telegram_bot.send_document(
            chat_id=tg_id,
            caption=caption,
            document=types.BufferedInputFile(file_bytes, filename=filename),
            reply_parameters=reply_params,
        )

    if isinstance(attach, AudioAttach):
        audio_bytes, filename = await download_bytes(attach.url, "voice.ogg")
        logger.info("Отправляю аудио/голосовое в Telegram: %s, %s байт", filename, len(audio_bytes))
        duration = getattr(attach, "duration", None)
        try:
            return await telegram_bot.send_voice(
                chat_id=tg_id,
                caption=caption,
                voice=types.BufferedInputFile(audio_bytes, filename=filename),
                duration=duration,
                reply_parameters=reply_params,
            )
        except TelegramBadRequest:
            return await telegram_bot.send_audio(
                chat_id=tg_id,
                caption=caption,
                audio=types.BufferedInputFile(audio_bytes, filename=filename),
                duration=duration,
                reply_parameters=reply_params,
            )

    if isinstance(attach, StickerAttach):
        sticker_url = attach.url or getattr(attach, "lottie_url", None)
        if sticker_url:
            try:
                stk_bytes, filename = await download_bytes(sticker_url, "sticker.webp")
                return await telegram_bot.send_sticker(
                    chat_id=tg_id,
                    sticker=types.BufferedInputFile(stk_bytes, filename=filename),
                    reply_parameters=reply_params,
                )
            except Exception:
                logger.warning("Не удалось отправить стикер: %s", sticker_url)

    if isinstance(attach, ContactAttach):
        contact_name = attach.name or f"{attach.first_name} {attach.last_name}".strip()
        return await send_to_telegram(
            tg_id=tg_id,
            text=f"{caption}\n👤 Контакт: {contact_name} (ID: {attach.contact_id})",
            reply_to_tg_msg_id=reply_to_tg_msg_id,
        )

    logger.warning("Пропускаю неподдерживаемое вложение MAX: %r", attach)
    return None


@client.on_start
async def handle_max_start() -> None:
    logger.info("MAX клиент успешно запущен и авторизован.")
    try:
        chats = await client.fetch_chats()
        logger.info("Предзагружено %d чатов MAX", len(chats))
        for c in chats:
            if getattr(c, "id", None) and getattr(c, "title", None):
                CHAT_TITLE_CACHE[c.id] = c.title.strip()
                KNOWN_CHATS[c.id] = {"title": c.title.strip(), "id": c.id}
    except Exception as error:
        logger.warning("Не удалось предзагрузить чаты MAX при старте: %s", error)


@client.on_chat_update
async def handle_chat_update(chat: Any) -> None:
    if getattr(chat, "id", None) and getattr(chat, "title", None):
        CHAT_TITLE_CACHE[chat.id] = chat.title.strip()
        KNOWN_CHATS[chat.id] = {"title": chat.title.strip(), "id": chat.id}


@client.on_message()
async def handle_max_message(message: Message) -> None:
    try:
        incoming_chat_id = message.chat_id
        if incoming_chat_id is None:
            return

        # Игнорируем собственные сообщения, отправленные аккаунтом
        if client.me and message.sender == client.me.id:
            logger.debug("Skip own outgoing MAX message: id=%s", message.id)
            return

        # Проверка списка исключений
        if incoming_chat_id in EXCLUDED_CHATS:
            logger.debug("MAX-чат %s в списке исключений, пропускаем", incoming_chat_id)
            return

        # Определение целевого Telegram-чата
        tg_id = CHATS.get(incoming_chat_id)
        is_common_target = False

        if tg_id is None:
            if CURRENT_COMMON_TG_CHAT is not None:
                tg_id = CURRENT_COMMON_TG_CHAT
                is_common_target = True
            else:
                logger.debug("Нет целевого TG-чата для MAX-сообщения (CURRENT_COMMON_TG_CHAT не задан)")
                return

        max_message, max_chat_id = await resolve_content_message(message)
        author = await author_name_from_sender(message.sender)

        chat_title = await get_chat_title(incoming_chat_id, message.sender)

        # Показываем префикс [Название чата], если это общий чат или включена опция SHOW_CHAT_TITLE
        show_title_prefix = SHOW_CHAT_TITLE and (is_common_target or len(CHATS) > 1)
        title_prefix = chat_title if show_title_prefix else None

        caption = build_caption(author, max_message.text, title_prefix)

        # Проверяем, является ли сообщение ответом (REPLY)
        reply_to_tg_msg_id: int | None = None
        if message.link and (message.link.type or "").upper() == "REPLY":
            replied_msg_id = message.link.message_id or (
                message.link.message.id if message.link.message else None
            )
            if replied_msg_id is not None:
                reply_to_tg_msg_id = MAX_TO_TG_MSG.get((incoming_chat_id, replied_msg_id))
                if reply_to_tg_msg_id is None:
                    try:
                        reply_to_tg_msg_id = MAX_TO_TG_MSG.get((incoming_chat_id, int(replied_msg_id)))
                    except (ValueError, TypeError):
                        pass

        if max_message.attaches:
            for attach in max_message.attaches:
                sent_msg = await send_max_attachment_to_telegram(
                    tg_id=tg_id,
                    max_chat_id=max_chat_id,
                    max_message=max_message,
                    attach=attach,
                    caption=caption,
                    fallback_chat_id=incoming_chat_id,
                    fallback_message_id=message.id,
                    reply_to_tg_msg_id=reply_to_tg_msg_id,
                )
                if sent_msg is not None:
                    MAX_TO_TG_MSG[(incoming_chat_id, message.id)] = sent_msg.message_id
                    TG_TO_MAX_MSG[sent_msg.message_id] = (incoming_chat_id, message.id)
            return

        text = build_text(author, max_message.text, title_prefix)
        sent_msg = await send_to_telegram(
            tg_id=tg_id,
            text=text,
            reply_to_tg_msg_id=reply_to_tg_msg_id,
        )
        if sent_msg is not None:
            MAX_TO_TG_MSG[(incoming_chat_id, message.id)] = sent_msg.message_id
            TG_TO_MAX_MSG[sent_msg.message_id] = (incoming_chat_id, message.id)

    except aiohttp.ClientError:
        logger.exception("Не удалось скачать вложение из MAX")
    except Exception:
        logger.exception("Не удалось переслать сообщение из MAX в Telegram")


@dp.my_chat_member()
async def handle_my_chat_member(update: types.ChatMemberUpdated) -> None:
    """Автоматическая привязка общего чата при добавлении бота в группу/канал."""
    global CURRENT_COMMON_TG_CHAT
    logger.info(
        "Статус бота обновлен: chat_id=%s title=%s status=%s",
        update.chat.id,
        update.chat.title,
        update.new_chat_member.status,
    )
    if update.new_chat_member.status in ("member", "administrator"):
        CURRENT_COMMON_TG_CHAT = update.chat.id
        save_common_chat_id(update.chat.id)
        try:
            await telegram_bot.send_message(
                chat_id=update.chat.id,
                text=(
                    f"✅ <b>MAX Bridge успешно привязан к этой группе!</b>\n\n"
                    f"• ID чата: <code>{update.chat.id}</code>\n"
                    f"• Все входящие сообщения из MAX будут пересылаться сюда.\n"
                    f"• Пересылка в MAX: <b>{'ВКЛЮЧЕНА' if FORWARD_TG_TO_MAX else 'ОТКЛЮЧЕНА (безопасно)'}</b>\n\n"
                    "Команды управления: /chats, /exclude &lt;id&gt;, /include &lt;id&gt;, /excluded"
                ),
                parse_mode="HTML",
            )
        except Exception as error:
            logger.warning("Не удалось отправить приветствие в привязанный чат: %s", error)


@dp.message()
async def handle_telegram_message(message: types.Message, bot: Bot) -> None:
    global CURRENT_COMMON_TG_CHAT
    text = (message.text or message.caption or "").strip()

    # Автопривязка первого группового чата, если еще не настроен
    if message.chat.type in ("group", "supergroup") and CURRENT_COMMON_TG_CHAT is None:
        CURRENT_COMMON_TG_CHAT = message.chat.id
        save_common_chat_id(message.chat.id)
        logger.info("Автоматически привязан общий чат Telegram: %s (%s)", message.chat.id, message.chat.title)

    # Обработка команд бота в Telegram
    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        if cmd in ("/bind", "/bind_here", "/set_group"):
            CURRENT_COMMON_TG_CHAT = message.chat.id
            save_common_chat_id(message.chat.id)
            await message.reply(
                f"✅ Этот чат <b>{message.chat.title or message.chat.full_name}</b> (<code>{message.chat.id}</code>) "
                "установлен как основной общий чат для трансляции MAX.",
                parse_mode="HTML",
            )
            return

        if cmd in ("/chats", "/list_chats"):
            if not KNOWN_CHATS:
                await message.reply("Список чатов пока пуст (сообщения еще не поступали).")
                return
            lines = ["📋 <b>Список чатов MAX:</b>\n"]
            for cid, info in sorted(KNOWN_CHATS.items(), key=lambda x: str(x[1].get("title", ""))):
                status = "🔴 В исключениях" if cid in EXCLUDED_CHATS else "🟢 Активен"
                title = info.get("title", f"Чат {cid}")
                lines.append(f"• <b>{title}</b>\n  ID: <code>{cid}</code> — {status}")
            lines.append("\n<i>Используйте /exclude &lt;ID&gt; или /include &lt;ID&gt;</i>")
            await message.reply("\n".join(lines), parse_mode="HTML")
            return

        if cmd in ("/exclude", "/ignore"):
            if not args:
                await message.reply("Использование: <code>/exclude &lt;max_chat_id&gt;</code>", parse_mode="HTML")
                return
            try:
                target_id = int(args[0])
                EXCLUDED_CHATS.add(target_id)
                save_persisted_exclusions(EXCLUDED_CHATS)
                title = CHAT_TITLE_CACHE.get(target_id, f"Чат {target_id}")
                await message.reply(
                    f"✅ Чат <b>{title}</b> (<code>{target_id}</code>) добавлен в список исключений.",
                    parse_mode="HTML",
                )
            except ValueError:
                await message.reply("❌ ID чата должен быть числом.")
            return

        if cmd in ("/include", "/unignore"):
            if not args:
                await message.reply("Использование: <code>/include &lt;max_chat_id&gt;</code>", parse_mode="HTML")
                return
            try:
                target_id = int(args[0])
                EXCLUDED_CHATS.discard(target_id)
                save_persisted_exclusions(EXCLUDED_CHATS)
                title = CHAT_TITLE_CACHE.get(target_id, f"Чат {target_id}")
                await message.reply(
                    f"✅ Чат <b>{title}</b> (<code>{target_id}</code>) возвращен в пересылку.",
                    parse_mode="HTML",
                )
            except ValueError:
                await message.reply("❌ ID чата должен быть числом.")
            return

        if cmd in ("/excluded", "/ignored"):
            if not EXCLUDED_CHATS:
                await message.reply("Список исключений пуст.")
                return
            lines = ["🔴 <b>Исключенные чаты MAX:</b>\n"]
            for cid in sorted(EXCLUDED_CHATS):
                title = CHAT_TITLE_CACHE.get(cid, f"Чат {cid}")
                lines.append(f"• <b>{title}</b> (<code>{cid}</code>)")
            await message.reply("\n".join(lines), parse_mode="HTML")
            return

        if cmd in ("/help", "/start"):
            status_chat = f"<code>{CURRENT_COMMON_TG_CHAT}</code>" if CURRENT_COMMON_TG_CHAT else "<i>не привязан</i>"
            help_text = (
                "🤖 <b>MAX ⇄ Telegram Bridge</b>\n\n"
                f"• Текущий общий чат: {status_chat}\n"
                f"• Пересылка в MAX: <b>{'ВКЛЮЧЕНА' if FORWARD_TG_TO_MAX else 'ОТКЛЮЧЕНА'}</b>\n\n"
                "Команды управления:\n"
                "• /bind — привязать текущую группу как общий чат\n"
                "• /chats — список известных чатов MAX\n"
                "• /exclude &lt;id&gt; — добавить чат MAX в исключения\n"
                "• /include &lt;id&gt; — убрать чат из исключений\n"
                "• /excluded — список исключенных чатов"
            )
            await message.reply(help_text, parse_mode="HTML")
            return

    # ВАЖНО: Если пересылка в MAX выключена — никогда ничего не пересылаем!
    if not FORWARD_TG_TO_MAX:
        return


async def main() -> None:
    telegram_task = asyncio.create_task(dp.start_polling(telegram_bot))

    try:
        await client.start()
    finally:
        telegram_task.cancel()
        await client.close()
        await telegram_bot.session.close()
        try:
            await telegram_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Программа остановлена пользователем.")
