# MaxBridge — Двусторонний мост Telegram <-> MAX Messenger

> Асинхронный шлюз-мост между Telegram и корпоративным мессенджером MAX для автоматической двусторонней синхронизации сообщений, медиа и каналов.

- **Репозиторий:** [Vicrorege/maxbridge](https://github.com/Vicrorege/maxbridge)
- **Владелец:** Vicrorege (Личный форк)
- **Стек:** Python 3.11+, aiogram 3, pymax (`MaxClient`), aiohttp, Docker
- **Локальный путь:** `/root/projects/maxbridge`

---

## 📌 Архитектура и функционал

1. **Двусторонняя трансляция:**
   - Пересылка текстовых сообщений, форматирования (Markdown, HTML), цитат и ответов между чатами Telegram и диалогами/каналами MAX.
2. **Медиа-пайплайн:**
   - Потоковая загрузка и конвертация медиафайлов (фотографии, голосовые сообщения, видео, документы) через временный кэш `./cache`.
3. **Сетевая маршрутизация:**
   - Поддержка `network_mode: host` и работы через SOCKS5/HTTP прокси для преодоления геоблокировок Telegram API.

---

## ⚙️ Переменные окружения (`.env`)

```ini
TELEGRAM_BOT_TOKEN="generic_telegram_bot_token"
TELEGRAM_CHAT_ID="-1001234567890"
TELEGRAM_PROXY="socks5://127.0.0.1:1080"  # Опционально
MAX_PHONE="+79990000000"
MAX_CHAT_ID="generic_max_chat_id"
CACHE_DIR="./cache"
```

---

## 🚀 Запуск через Docker

```bash
docker compose up -d --build
# Для первой интерактивной авторизации (ввод SMS-кода / QR MAX):
docker attach maxbridge
```
