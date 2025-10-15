# bot.py — Telethon + OpenAI репостер для инсайдов (финальная версия)
import os
import re
import asyncio
import random
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import pytz
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
import openai
from rapidfuzz import fuzz

# ---------------------------
# Настройка окружения / loop fix (Windows)
# ---------------------------
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

load_dotenv()

# ---------------------------
# Конфигурация из .env
# ---------------------------
# Telethon session: prefer user session string (to read any channels).
TELETHON_SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")  # StringSession, рекомендую
BOT_TOKEN = os.getenv("BOT_TOKEN")  # fallback (бот)
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH") or ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SOURCE_IDS = [int(x) for x in os.getenv("SOURCE_IDS", "").split(",") if x.strip()]
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL_ID")
try:
    TARGET_CHANNEL = int(TARGET_CHANNEL)
except Exception:
    pass
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
USERNAME_TAG = os.getenv("USERNAME_TAG", "@insideryyy")

TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Europe/Moscow"))
POST_INTERVAL_MINUTES = int(os.getenv("POST_INTERVAL_MINUTES", 40))
START_HOUR = int(os.getenv("START_HOUR", 8))
END_HOUR = int(os.getenv("END_HOUR", 23))

SIMILARITY_THRESHOLD = int(os.getenv("SIMILARITY_THRESHOLD", 85))
SKIP_FORWARDS = os.getenv("SKIP_FORWARDS", "True").lower() in ("1", "true", "yes")

# Files
PUBLISHED_FILE = Path("published.txt")
QUEUE_FILE = Path("queue.txt")
LOG_FILE = Path("log.txt")

# ---------------------------
# Проверки
# ---------------------------
def log_console(msg: str):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

log_console("Проверка .env:")
log_console(f"API_ID: {API_ID if API_ID else 'None'}")
log_console(f"API_HASH: {'set' if API_HASH else 'None'}")
log_console(f"BOT_TOKEN: {'set' if BOT_TOKEN else 'None'}")
log_console(f"OPENAI_API_KEY: {'set' if OPENAI_API_KEY else 'None'}")
log_console(f"SOURCE_IDS: {SOURCE_IDS}")
log_console(f"TARGET_CHANNEL: {TARGET_CHANNEL}")
log_console(f"OWNER_ID: {OWNER_ID}")
log_console(f"USERNAME_TAG: {USERNAME_TAG}")
log_console("-" * 40)

if not OPENAI_API_KEY:
    log_console("Warning: OPENAI_API_KEY not set. AI features will fail.")
openai.api_key = OPENAI_API_KEY

# ---------------------------
# Инициализация Telethon client
# ---------------------------
if TELETHON_SESSION_STRING:
    client = TelegramClient(StringSession(TELETHON_SESSION_STRING), API_ID, API_HASH)
    log_console("Используем TELETHON_SESSION_STRING (user session).")
elif BOT_TOKEN:
    # bot session (ограничено: читать чужие каналы только если бот — админ)
    client = TelegramClient("bot_session", API_ID, API_HASH)
    log_console("TELETHON_SESSION_STRING не найден — используется BOT_TOKEN (ограниченная функциональность).")
else:
    raise SystemExit("Нужен TELETHON_SESSION_STRING или BOT_TOKEN и API_ID/API_HASH.")

# ---------------------------
# Подготовка persistent storage
# ---------------------------
if not PUBLISHED_FILE.exists():
    PUBLISHED_FILE.write_text("", encoding="utf-8")
if not QUEUE_FILE.exists():
    QUEUE_FILE.write_text("", encoding="utf-8")
if not LOG_FILE.exists():
    LOG_FILE.write_text("", encoding="utf-8")

published = set(line.rstrip("\n") for line in PUBLISHED_FILE.read_text(encoding="utf-8").splitlines() if line.strip())
queued = [b.strip() for b in QUEUE_FILE.read_text(encoding="utf-8").split("\n\n---\n\n") if b.strip()]

stats = {"posted": 0, "filtered": 0}
last_post_info = {"message_id": None, "channel_username": None}
last_post_time = datetime.now(TIMEZONE) - timedelta(minutes=POST_INTERVAL_MINUTES)

# ---------------------------
# Текстовые утилиты и фильтры
# ---------------------------
link_regex = re.compile(r"(https?:\/\/\S+|www\.\S+|t\.me\/\S+)", flags=re.IGNORECASE)
mention_regex = re.compile(r"@\w+", flags=re.IGNORECASE)
hashtag_regex = re.compile(r"#\w+", flags=re.IGNORECASE)

# расширенный blacklist рекламной лексики
ad_keywords = [
    r"\bподпис\w*\b", r"\bреклам\w*\b", r"\bпиар\w*\b", r"\bскидк\w*\b",
    r"\bкуп\w*\b", r"\bакц\w*\b", r"\bспонс\w*\b", r"\bдонат\w*\b",
    r"\bссылка\b", r"\bссылка в профиле\b", r"\bпартн\w*\b"
]

def clean_text(text: str) -> str:
    if not text:
        return ""
    t = re.sub(link_regex, "", text)
    t = re.sub(mention_regex, "", t)
    t = re.sub(hashtag_regex, "", t)
    for kw in ad_keywords:
        t = re.sub(kw, "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t

# ключевые слова для релевантности (обновлённый список)
KEYWORDS = [
    "блогер", "тикток", "тиктокер", "стример", "ютубер", "шо", "шоу",
    "хайп", "скандал", "конфликт", "развод", "отношения", "слух", "слив",
    "утечк", "инсайд", "инфа", "реакц", "фанат", "подписчик", "драма",
    "роман", "расставан", "разоблач", "инфлюенсер", "коллаб", "тренд",
    "хайпхаус", "фейк", "фолловер", "подкаст", "видео", "скрин"
]

def is_relevant(text: str) -> bool:
    low = text.lower()
    found = any(k in low for k in KEYWORDS)
    ok_len = len(low) >= 30
    return found and ok_len

def is_local_duplicate(text: str) -> bool:
    # быстрое сравнение exact or fuzzy
    if text in published:
        return True
    for old in published:
        try:
            score = fuzz.partial_ratio(text, old)
            if score >= SIMILARITY_THRESHOLD:
                return True
        except Exception:
            continue
    return False

# ---------------------------
# OpenAI helpers (async wrapper)
# ---------------------------
async def ai_is_relevant(text: str) -> bool:
    prompt = (
        "Ты — модератор канала про инсайды/сливы/хайп вокруг блогеров. "
        "Ответь одним словом 'Да' если текст релевантен (инсайд/слив/важная новость про блогера), "
        "иначе ответь 'Нет'.\n\n"
        f"Текст: {text}\n\nОтвечай только 'Да' или 'Нет'."
    )
    try:
        resp = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0
        )
        ans = resp.choices[0].message["content"].strip().lower()
        return ans.startswith("д") or ans.startswith("y")
    except Exception as e:
        log_console(f"OpenAI relevance error: {e}")
        return False

async def ai_paraphrase(text: str) -> str:
    cleaned = clean_text(text)
    prompt = (
        "Ты — автор популярного Telegram-канала про блогеров и тиктокеров. "
        "Перепиши текст в разговорном, молодёжном стиле (18+), добавь лёгкий сарказм, эмодзи (👀, 😭, 💅 и т.д.), "
        "короткие шутки и мемный сленг там, где уместно. "
        "Убери любые ссылки/упоминания/хэштеги и не вставляй их. "
        "Не выдумывай фактов — только подача. "
        f"В конце добавь подпись {USERNAME_TAG}.\n\nИсходный текст:\n{cleaned}"
    )
    try:
        resp = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.8,
            max_tokens=700
        )
        out = resp.choices[0].message["content"].strip()
        out = re.sub(link_regex, "", out)  # лишние осторожно убрать
        out = re.sub(r"\s+", " ", out).strip()
        return out
    except Exception as e:
        log_console(f"OpenAI paraphrase error: {e}")
        return cleaned + f"\n\n{USERNAME_TAG}"

# ---------------------------
# Queue / publish helpers
# ---------------------------
def save_published():
    with PUBLISHED_FILE.open("w", encoding="utf-8") as f:
        for item in published:
            f.write(item + "\n")

def save_queue():
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        f.write("\n\n---\n\n".join(queued))

async def add_to_queue(block: str):
    queued.append(block)
    save_queue()
    log_console(f"Добавлено в очередь (в очереди: {len(queued)})")

async def post_one_from_queue():
    global last_post_time, last_post_info, stats
    if not queued:
        log_console("Очередь пуста.")
        return
    block = queued.pop(0)
    save_queue()
    now = datetime.now(TIMEZONE)
    if now.hour < START_HOUR or now.hour >= END_HOUR:
        queued.insert(0, block)
        save_queue()
        log_console("За пределами окна публикации — откладываем.")
        return
    try:
        sent = await client.send_message(TARGET_CHANNEL, block)
        stats["posted"] += 1
        log_console("Опубликовано пост из очереди.")
        try:
            ent = await client.get_entity(TARGET_CHANNEL)
            username = getattr(ent, "username", None)
        except Exception:
            username = None
        last_post_info["message_id"] = getattr(sent, "id", None)
        last_post_info["channel_username"] = username
        last_post_time = now
    except Exception as e:
        log_console(f"Ошибка при отправке: {e}")
        queued.insert(0, block)
        save_queue()

# ---------------------------
# Handlers
# ---------------------------
@client.on(events.NewMessage(chats=SOURCE_IDS))
async def on_new_message(event):
    try:
        # Пропускаем forwarded
        if SKIP_FORWARDS and event.message.fwd_from:
            log_console("Пропущено пересланное сообщение.")
            return

        raw = event.raw_text or ""
        if event.message and getattr(event.message, "message", None):
            raw = event.message.message

        if not raw or not raw.strip():
            return

        cleaned = clean_text(raw)
        if not cleaned:
            log_console("Пустой текст после очистки — пропускаем.")
            return

        # локальный дубликат
        if cleaned in published or is_local_duplicate(cleaned):
            log_console("Найден дубликат (локально) — пропускаем.")
            return

        # AI релевантность (отдельный check)
        is_rel = await ai_is_relevant(cleaned)
        if not is_rel:
            stats["filtered"] += 1
            log_console("AI пометил как нерелевантное — пропускаем.")
            return

        # AI переписывает + делает заголовок
        rewritten = await ai_paraphrase(cleaned)

        # Добавляем в очередь
        await add_to_queue(rewritten)

        # Запоминаем оригинал как опубликованный для дедупа
        published.add(cleaned)
        save_published()

        log_console("Сообщение принято и поставлено в очередь.")
    except Exception as e:
        log_console(f"Ошибка в обработчике: {e}")

# ---------------------------
# Owner commands (личка) через Telethon
# ---------------------------
@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/status"))
async def owner_status(event):
    try:
        uptime = datetime.now(TIMEZONE) - (last_post_time or datetime.now(TIMEZONE))
        text = (
            f"✅ Бот онлайн\n"
            f"Последний пост: {last_post_time.strftime('%Y-%m-%d %H:%M:%S') if last_post_time else 'нет'}\n"
            f"В очереди: {len(queued)}\n"
            f"Опубликовано сегодня: {stats['posted']}\n"
        )
        await event.reply(text)
    except Exception as e:
        log_console(f"owner_status error: {e}")

paused = False

@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/pause"))
async def owner_pause(event):
    global paused
    paused = True
    await event.reply("⏸ Бот поставлен на паузу (публикации остановлены).")

@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/resume"))
async def owner_resume(event):
    global paused
    paused = False
    await event.reply("▶️ Бот возобновил публикации.")

@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/stats"))
async def owner_stats(event):
    try:
        text = f"Stats: posted={stats['posted']}, filtered={stats['filtered']}, queue={len(queued)}"
        await event.reply(text)
    except Exception as e:
        log_console(f"owner_stats error: {e}")

@client.on(events.NewMessage(from_users=OWNER_ID, pattern=r"^/forcepost"))
async def owner_forcepost(event):
    # форсить публикацию одной позиции из очереди (если есть)
    if queued:
        block = queued.pop(0)
        save_queue()
        try:
            sent = await client.send_message(TARGET_CHANNEL, block)
            await event.reply("✅ Форс-пост опубликован.")
            stats["posted"] += 1
            # не забываем last_post_time
        except Exception as e:
            await event.reply(f"Ошибка при отправке: {e}")
    else:
        await event.reply("Очередь пуста.")

# ---------------------------
# Планировщик публикаций (пост каждые POST_INTERVAL_MINUTES)
# ---------------------------
async def schedule_poster():
    await asyncio.sleep(random.randint(1, 8))
    while True:
        try:
            if not paused:
                await post_one_from_queue()
            else:
                log_console("Пауза включена — публикации пропущены.")
            await asyncio.sleep(POST_INTERVAL_MINUTES * 60 + random.randint(-60, 120))
        except Exception as e:
            log_console(f"Ошибка в schedule_poster: {e}")
            await asyncio.sleep(30)

# ---------------------------
# Уведомление владельцу при старте
# ---------------------------
async def notify_owner_on_start():
    try:
        if OWNER_ID:
            await client.send_message(OWNER_ID, "✅ Бот успешно запущен и слушает источники.")
            log_console("Уведомление владельцу отправлено.")
    except Exception as e:
        log_console(f"Не удалось отправить уведомление владельцу: {e}")

# ---------------------------
# Главная: старт клиента и таски
# ---------------------------
async def main():
    log_console("Запуск бота...")
    # старт Telethon
    if TELETHON_SESSION_STRING:
        # с user session: start() без аргументов
        await client.start()
    else:
        # bot mode: старт через bot token
        await client.start(bot_token=BOT_TOKEN)
    log_console("Telethon клиент запущен.")
    # notify owner
    await notify_owner_on_start()
    # задачи
    asyncio.create_task(schedule_poster())
    # run until disconnected
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_console("Остановка бота (KeyboardInterrupt).")
    except Exception as ex:
        log_console(f"Fatal error: {ex}")
