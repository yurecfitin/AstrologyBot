import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import vk_api
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from vk_api.utils import get_random_id
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor


TG_TOKEN = os.getenv("BOT_TOKEN")
TG_ADMIN_IDS = [6113518001, 1815133569]

VK_TOKEN = os.getenv("TOKEN_VK")
VK_GROUP_ID = 241995822
VK_ADMIN_IDS = [673432989, 517757702]

DB_PATH = "bot.db"
TIMEZONE = "Europe/Moscow"

HOME_BUTTON_TEXT = "🏠 Главное меню"
ADMIN_BUTTON_TEXT = "🛠 Админ-панель"

MAX_REQUEST_LEN = 2000
MIN_REQUEST_LEN = 2
REQUEST_COOLDOWN_SECONDS = 300

DEFAULT_INFO = (
    "✨ Тема, о которой вспоминают редко.\n"
    "Об этике говорят мало. Повышают экспертность, осваивают инструменты, "
    "а вопрос «как себя вести с человеком, который вам доверился» часто "
    "остаётся без ответа.\n\n"
    "❇️ Этика — это не мораль и не набор запретов. "
    "Это то, что защищает и клиента, и вас.\n\n"
    "✅ Разберём на уроке:\n"
    "• где заканчивается ваша ответственность и начинается ответственность клиента;\n"
    "• как говорить о сложном, не пугая человека;\n"
    "• границы: время, деньги, личное общение, соцсети;\n"
    "• когда консультацию нужно остановить или передать другому специалисту;\n"
    "• как не забирать чужие проблемы себе.\n\n"
    "👥 Кому будет полезно? Всем, кто работает с людьми: астрологам, "
    "психологам, коучам, тренерам, консультантам, тарологам, нутрициологам, "
    "HR-специалистам, наставникам, преподавателям — и тем, кто только планирует "
    "консультировать.\n\n"
    "📑 Ведёт урок Вячеслав Болотов — сертифицированный бизнес-тренер, "
    "профессиональный астролог, основатель Школы практической астрологии.\n\n"
    "🎉 Участие БЕСПЛАТНОЕ, предварительная регистрация ОБЯЗАТЕЛЬНА."
)

WELCOME_TEXT = (
    "👋 Добро пожаловать в бота Школы практической астрологии!\n"
    "Хочешь заглянуть в мир звёзд и понять, как планеты влияют на твою жизнь?\n\n"
    "У нас есть:\n"
    "✨ Бесплатный урок — чтобы почувствовать стиль обучения и увидеть реальные разборы.\n"
    "🗣 Консультация с астрологом — разберём твой запрос и дадим персональные рекомендации.\n"
    "📚 Обучение с практикой — от основ до уверенной работы с картами."
)


# ==================== ВАЛИДАЦИЯ / ХЕЛПЕРЫ ====================
NAME_PATTERN = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s\-]{1,49}$")


def is_valid_name(text: str) -> bool:
    t = (text or "").strip()
    if not (2 <= len(t) <= 50):
        return False
    return bool(NAME_PATTERN.match(t))


def is_tg_admin(user_id: int) -> bool:
    return user_id in TG_ADMIN_IDS


def is_vk_admin(user_id: int) -> bool:
    return user_id in VK_ADMIN_IDS


def now_str() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M:%S МСК")


def strip_html_for_vk(text: str) -> str:
    """Превращает HTML-текст в plain-text для VK."""
    text = re.sub(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', r'\2 (\1)', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = (text.replace('&lt;', '<')
                .replace('&gt;', '>')
                .replace('&amp;', '&')
                .replace('&quot;', '"')
                .replace('&#39;', "'"))
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def format_user_card(u: dict, idx: int = None, for_tg: bool = True) -> str:
    """Формирует карточку пользователя для списка регистраций."""
    nm = f"{u.get('surname') or ''} {u.get('name') or ''}".strip() or "—"
    platform = u.get("platform", "?")
    uid = u.get("user_id", "?")
    uname = u.get("username")
    qual = u.get("qualification") or "—"
    prefix = f"{idx}. " if idx is not None else "• "

    if for_tg:
        uname_str = f"@{uname}" if uname else "—"
        return (
            f"{prefix}[{platform}] <b>{nm}</b>\n"
            f"   🆔 <code>{uid}</code>\n"
            f"   🔗 {uname_str}\n"
            f"   🎓 {qual}"
        )
    else:
        uname_str = f"@{uname}" if uname else "—"
        return (
            f"{prefix}[{platform}] {nm}\n"
            f"   id: {uid}\n"
            f"   ник: {uname_str}\n"
            f"   ур.: {qual}"
        )


# ==================== VK USER INFO (кеш) ====================
_vk_user_cache: dict[int, dict] = {}


def vk_get_user_info(user_id: int) -> dict:
    """Возвращает {id, first_name, last_name, screen_name}. Кеширует результат."""
    if user_id in _vk_user_cache:
        return _vk_user_cache[user_id]
    try:
        result = get_vk().users.get(user_ids=user_id, fields="screen_name")
        if result and isinstance(result, list):
            info = result[0]
            _vk_user_cache[user_id] = info
            return info
    except Exception as e:
        logging.warning(f"VK users.get {user_id}: {e}")
    _vk_user_cache[user_id] = {}
    return {}


def vk_get_username(user_id: int) -> str | None:
    """Возвращает screen_name пользователя VK или None."""
    info = vk_get_user_info(user_id)
    return info.get("screen_name") or info.get("domain") or None


def vk_get_profile_name(user_id: int) -> str:
    """Возвращает «Имя Фамилия» из VK-профиля или пустую строку."""
    info = vk_get_user_info(user_id)
    first = info.get("first_name") or ""
    last = info.get("last_name") or ""
    return f"{first} {last}".strip()


# ==================== DATABASE ====================
class Database:
    def __init__(self, path=DB_PATH):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    title TEXT,
                    lesson_date TEXT,
                    lesson_time TEXT,
                    info_text TEXT,
                    broadcast_link TEXT,
                    sent_24h_tg INTEGER DEFAULT 0,
                    sent_1h_tg INTEGER DEFAULT 0,
                    sent_10min_tg INTEGER DEFAULT 0,
                    sent_24h_vk INTEGER DEFAULT 0,
                    sent_1h_vk INTEGER DEFAULT 0,
                    sent_10min_vk INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    surname TEXT,
                    name TEXT,
                    qualification TEXT,
                    registered_at TEXT,
                    UNIQUE(platform, user_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT,
                    user_id INTEGER,
                    event_type TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT,
                    user_id INTEGER,
                    username TEXT,
                    kind TEXT,
                    text TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS states (
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    state TEXT,
                    data TEXT,
                    PRIMARY KEY (platform, user_id)
                );
                CREATE TABLE IF NOT EXISTS user_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT,
                    user_id INTEGER,
                    message_id INTEGER,
                    tag TEXT DEFAULT 'main'
                );
            """)
            for sql in (
                "ALTER TABLE lessons ADD COLUMN sent_24h_tg INTEGER DEFAULT 0",
                "ALTER TABLE lessons ADD COLUMN sent_1h_tg INTEGER DEFAULT 0",
                "ALTER TABLE lessons ADD COLUMN sent_10min_tg INTEGER DEFAULT 0",
                "ALTER TABLE lessons ADD COLUMN sent_24h_vk INTEGER DEFAULT 0",
                "ALTER TABLE lessons ADD COLUMN sent_1h_vk INTEGER DEFAULT 0",
                "ALTER TABLE lessons ADD COLUMN sent_10min_vk INTEGER DEFAULT 0",
            ):
                try:
                    await db.execute(sql)
                except Exception:
                    pass

            await db.execute(
                """INSERT OR IGNORE INTO lessons
                (id, title, lesson_date, lesson_time, info_text, broadcast_link)
                VALUES (1, ?, ?, ?, ?, ?)""",
                ("Этика в консультировании", "00.00.2026", "00:00", DEFAULT_INFO, "")
            )
            await db.commit()

    async def get_lesson(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM lessons WHERE id=1") as cur:
                row = await cur.fetchone()
                return dict(row) if row else {}

    async def update_lesson(self, field: str, value: str):
        allowed = {"title", "lesson_date", "lesson_time", "info_text", "broadcast_link"}
        if field not in allowed:
            raise ValueError(f"Invalid field: {field}")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE lessons SET {field}=? WHERE id=1", (value,))
            await db.execute("""UPDATE lessons SET
                sent_24h_tg=0, sent_1h_tg=0, sent_10min_tg=0,
                sent_24h_vk=0, sent_1h_vk=0, sent_10min_vk=0
                WHERE id=1""")
            await db.commit()

    async def mark_sent(self, field: str):
        allowed = {
            "sent_24h_tg", "sent_1h_tg", "sent_10min_tg",
            "sent_24h_vk", "sent_1h_vk", "sent_10min_vk",
        }
        if field not in allowed:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE lessons SET {field}=1 WHERE id=1")
            await db.commit()

    async def save_user(self, platform, user_id, username, surname, name, qualification):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO users (platform, user_id, username, surname, name, qualification, registered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    username=excluded.username,
                    surname=excluded.surname,
                    name=excluded.name,
                    qualification=excluded.qualification,
                    registered_at=excluded.registered_at""",
                (platform, user_id, username, surname, name, qualification,
                 datetime.now().isoformat(timespec="seconds"))
            )
            await db.commit()

    async def get_all_users(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users ORDER BY registered_at DESC") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_users_by_platform(self, platform: str) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE platform=? ORDER BY registered_at DESC", (platform,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def log_event(self, platform: str, user_id: int, event_type: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO events (platform, user_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                (platform, user_id, event_type, datetime.now().isoformat(timespec="seconds"))
            )
            await db.commit()

    async def save_request(self, platform, user_id, username, kind, text) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO requests (platform, user_id, username, kind, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (platform, user_id, username, kind, text, datetime.now().isoformat(timespec="seconds"))
            )
            await db.commit()
            return cur.lastrowid

    async def get_last_requests(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_last_request_time(self, platform, user_id):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """SELECT created_at FROM requests
                WHERE platform=? AND user_id=? AND kind IN ('consult','question')
                ORDER BY id DESC LIMIT 1""",
                (platform, user_id)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                try:
                    return datetime.fromisoformat(row[0])
                except Exception:
                    return None

    async def set_state(self, platform, user_id, state, data=None):
        payload = json.dumps(data or {}, ensure_ascii=False)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO states (platform, user_id, state, data) VALUES (?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET state=excluded.state, data=excluded.data""",
                (platform, user_id, state, payload)
            )
            await db.commit()

    async def get_state(self, platform, user_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT state, data FROM states WHERE platform=? AND user_id=?",
                (platform, user_id)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None, {}
                try:
                    data = json.loads(row["data"]) if row["data"] else {}
                except Exception:
                    data = {}
                return row["state"], data

    async def clear_state(self, platform, user_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM states WHERE platform=? AND user_id=?", (platform, user_id)
            )
            await db.commit()

    async def add_message_id(self, platform, user_id, message_id, tag="main"):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO user_messages (platform, user_id, message_id, tag) VALUES (?, ?, ?, ?)",
                (platform, user_id, message_id, tag)
            )
            await db.commit()

    async def get_message_ids_by_tag(self, platform, user_id, tag):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT message_id FROM user_messages WHERE platform=? AND user_id=? AND tag=?",
                (platform, user_id, tag)
            ) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def clear_by_tag(self, platform, user_id, tag):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM user_messages WHERE platform=? AND user_id=? AND tag=?",
                (platform, user_id, tag)
            )
            await db.commit()


db = Database()

# ==================== VK INIT ====================
_vk_session = None
_vk = None
_vk_longpoll = None


def get_vk():
    global _vk_session, _vk, _vk_longpoll
    if _vk is None:
        _vk_session = vk_api.VkApi(token=VK_TOKEN)
        _vk = _vk_session.get_api()
        _vk_longpoll = VkBotLongPoll(_vk_session, VK_GROUP_ID)
    return _vk


# ==================== GLOBAL LOOP ====================
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
tg_bot_global: Bot | None = None


def db_sync(coro):
    if MAIN_LOOP is None:
        raise RuntimeError("Main loop не готов")
    return asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP).result(timeout=15)


# ==================== TG STATES ====================
class Reg(StatesGroup):
    qualification = State()
    surname = State()
    name = State()


class RequestFlow(StatesGroup):
    waiting_text = State()


class AdminStates(StatesGroup):
    edit_title = State()
    edit_date = State()
    edit_time = State()
    edit_info = State()
    edit_link = State()
    broadcast = State()


# ==================== ЕДИНЫЕ УВЕДОМЛЕНИЯ АДМИНАМ ====================
async def notify_admins_event_async(text_html, source="tg", vk_user_id=None,
                                    vk_username=None, tg_reply_markup=None):
    """
    Отправляет уведомление всем админам — и TG, и VK.
    source='tg'  → TG-событие, TG-админам уходит tg_reply_markup как есть.
    source='vk'  → VK-событие, TG-админам добавляется кнопка «Открыть профиль VK».
    """
    # --- TG ---
    if tg_bot_global is not None:
        markup = tg_reply_markup
        if source == "vk" and vk_user_id is not None:
            url = f"https://vk.com/{vk_username}" if vk_username else f"https://vk.com/id{vk_user_id}"
            rows = [[InlineKeyboardButton(text="💬 Открыть профиль VK", url=url)]]
            rows.append([InlineKeyboardButton(text="📥 Все обращения", callback_data="admin:requests")])
            markup = InlineKeyboardMarkup(inline_keyboard=rows)

        for admin_id in TG_ADMIN_IDS:
            try:
                await tg_bot_global.send_message(admin_id, text_html, reply_markup=markup)
            except Exception as e:
                logging.warning(f"TG notify admin {admin_id}: {e}")

    # --- VK ---
    text_plain = strip_html_for_vk(text_html)
    for admin_id in VK_ADMIN_IDS:
        try:
            await asyncio.to_thread(
                get_vk().messages.send,
                user_id=admin_id,
                message=text_plain,
                random_id=get_random_id(),
            )
        except Exception as e:
            logging.warning(f"VK notify admin {admin_id}: {e}")


def notify_admins_event_sync(text_html, source="tg", vk_user_id=None,
                             vk_username=None, tg_reply_markup=None):
    if MAIN_LOOP is None:
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(
            notify_admins_event_async(
                text_html, source, vk_user_id, vk_username, tg_reply_markup
            ),
            MAIN_LOOP,
        )
        fut.result(timeout=15)
    except Exception as e:
        logging.warning(f"notify sync error: {e}")


# ==================== TG KEYBOARDS ====================
def tg_home_kb(user_id: int):
    rows = [[KeyboardButton(text=HOME_BUTTON_TEXT)]]
    if is_tg_admin(user_id):
        rows.append([KeyboardButton(text=ADMIN_BUTTON_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def tg_main_menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Стать астрологом", callback_data="go_astrologer")
    b.button(text="🔥 Бесплатный урок", callback_data="free_lesson")
    b.button(text="✨ Получить консультацию", callback_data="go_consult")
    b.button(text="❓ Задать вопрос", callback_data="go_ask")
    b.button(text="📝 Отзывы", url="https://vk.ru/topic-221211406_49213877")
    b.adjust(1)
    return b.as_markup()


def tg_astrologer_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🪞 Понимание себя", callback_data="astro:understand")
    b.button(text="💼 Новая профессия", callback_data="astro:career")
    b.button(text="💰 Источник дохода", callback_data="astro:income")
    b.button(text="🧰 Дополнительный инструмент", callback_data="astro:tool")
    b.adjust(1)
    return b.as_markup()


def tg_free_lesson_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Регистрируюсь!", callback_data="reg_start")
    b.button(text="❓ Что за урок?", callback_data="lesson_info")
    b.adjust(1)
    return b.as_markup()


def tg_lesson_info_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Регистрируюсь!", callback_data="reg_start")
    b.button(text="⏪ Назад", callback_data="free_lesson")
    b.adjust(1)
    return b.as_markup()


def tg_qualification_kb():
    b = InlineKeyboardBuilder()
    b.button(text="😎 Разбираюсь, профи", callback_data="qual:pro")
    b.button(text="🤔 Интересуюсь", callback_data="qual:interest")
    b.button(text="🤩 Хочу изучать", callback_data="qual:learn")
    b.adjust(1)
    return b.as_markup()


def tg_surname_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⏪ Назад", callback_data="reg_qual_back")
    b.adjust(1)
    return b.as_markup()


def tg_name_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⏪ Назад", callback_data="reg_surname_back")
    b.adjust(1)
    return b.as_markup()


def tg_admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название урока", callback_data="admin:title")
    b.button(text="📅 Дата", callback_data="admin:date")
    b.button(text="🕐 Время", callback_data="admin:time")
    b.button(text="📝 Текст «Что за урок?»", callback_data="admin:info")
    b.button(text="🔗 Ссылка на трансляцию", callback_data="admin:link")
    b.button(text="👥 Список регистраций", callback_data="admin:list")
    b.button(text="📥 Обращения", callback_data="admin:requests")
    b.button(text="📢 Сделать рассылку", callback_data="admin:broadcast")
    b.button(text="📨 Разослать ссылку сейчас", callback_data="admin:send_link")
    b.adjust(1)
    return b.as_markup()


def tg_admin_cancel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⏪ Отмена", callback_data="admin:cancel")
    b.adjust(1)
    return b.as_markup()


def tg_admin_broadcast_confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить всем", callback_data="admin:broadcast_confirm")
    b.button(text="⏪ Отмена", callback_data="admin:cancel")
    b.adjust(1)
    return b.as_markup()


def tg_admin_request_notification_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить пользователю", url=f"tg://user?id={user_id}")],
        [InlineKeyboardButton(text="📥 Все обращения", callback_data="admin:requests")],
    ])


# ==================== TG ROUTER ====================
tg_router = Router()


async def tg_cleanup_reg(bot, chat_id):
    ids = await db.get_message_ids_by_tag("tg", chat_id, "reg")
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    await db.clear_by_tag("tg", chat_id, "reg")


async def tg_send_screen(bot, chat_id, text, inline_kb=None, reply_kb=None, tag="main"):
    if tag == "reg":
        await tg_cleanup_reg(bot, chat_id)
    if reply_kb is not None:
        m = await bot.send_message(chat_id, text, reply_markup=reply_kb)
        await db.add_message_id("tg", chat_id, m.message_id, tag)
        if inline_kb is not None:
            e = await bot.send_message(chat_id, "👇 Выберите действие:", reply_markup=inline_kb)
            await db.add_message_id("tg", chat_id, e.message_id, tag)
    else:
        m = await bot.send_message(chat_id, text, reply_markup=inline_kb)
        await db.add_message_id("tg", chat_id, m.message_id, tag)


async def tg_send_with_input(bot, chat_id, text, inline_kb, placeholder, reply_kb=None, tag="reg"):
    if tag == "reg":
        await tg_cleanup_reg(bot, chat_id)
    if reply_kb is not None:
        m = await bot.send_message(chat_id, text, reply_markup=reply_kb)
        await db.add_message_id("tg", chat_id, m.message_id, tag)
        if inline_kb is not None:
            e = await bot.send_message(chat_id, "👇 Выберите действие:", reply_markup=inline_kb)
            await db.add_message_id("tg", chat_id, e.message_id, tag)
    else:
        m = await bot.send_message(chat_id, text, reply_markup=inline_kb)
        await db.add_message_id("tg", chat_id, m.message_id, tag)
    prompt = await bot.send_message(
        chat_id, f"✏️ {placeholder}",
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder)
    )
    await db.add_message_id("tg", chat_id, prompt.message_id, tag)


def tg_user_signature(user) -> str:
    full_name = (user.full_name or "").strip() or "Без имени"
    username = f"@{user.username}" if user.username else "—"
    return (
        f"👤 <b>{full_name}</b>\n🔗 {username}\n🆔 <code>{user.id}</code>\n"
        f'💬 <a href="tg://user?id={user.id}">Открыть профиль в TG</a>'
    )


async def tg_show_welcome(bot, chat_id):
    await tg_cleanup_reg(bot, chat_id)
    w = await bot.send_message(chat_id, WELCOME_TEXT, reply_markup=tg_home_kb(chat_id))
    await db.add_message_id("tg", chat_id, w.message_id, "main")
    m = await bot.send_message(chat_id, "👇 Выберите действие:", reply_markup=tg_main_menu_kb())
    await db.add_message_id("tg", chat_id, m.message_id, "main")


@tg_router.message(CommandStart())
async def tg_cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.log_event("tg", message.from_user.id, "start")
    await tg_show_welcome(message.bot, message.chat.id)


@tg_router.message(Command("admin"))
async def tg_cmd_admin(message: Message):
    if not is_tg_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=tg_admin_kb())


@tg_router.message(F.text == HOME_BUTTON_TEXT)
async def tg_home_button(message: Message, state: FSMContext):
    await state.clear()
    await tg_show_welcome(message.bot, message.chat.id)


@tg_router.message(F.text == ADMIN_BUTTON_TEXT)
async def tg_admin_button(message: Message, state: FSMContext):
    if not is_tg_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=tg_admin_kb())


@tg_router.callback_query(F.data == "go_astrologer")
async def tg_astrologer(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await db.log_event("tg", cb.from_user.id, "astrologer_open")
    await tg_send_screen(cb.bot, cb.from_user.id,
                         "🚀 <b>Что вы хотите обрести благодаря астрологии?</b>",
                         tg_astrologer_kb(), tg_home_kb(cb.from_user.id))


@tg_router.callback_query(F.data.startswith("astro:"))
async def tg_astrologer_choice(cb: CallbackQuery, state: FSMContext):
    choice_map = {
        "understand": "Понимание себя",
        "career": "Новая профессия",
        "income": "Источник дохода",
        "tool": "Дополнительный инструмент",
    }
    choice = choice_map.get(cb.data.split(":", 1)[1], "—")
    await cb.answer()
    await state.clear()
    req_id = await db.save_request("tg", cb.from_user.id, cb.from_user.username, "astrologer", choice)

    if cb.from_user.id not in TG_ADMIN_IDS:
        await notify_admins_event_async(
            f"🚀 <b>Заявка «Стать астрологом» #{req_id}</b> [TG]\n"
            f"Время: {now_str()}\n\n🎯 <b>Цель:</b> {choice}\n\n"
            f"{tg_user_signature(cb.from_user)}",
            source="tg",
            tg_reply_markup=tg_admin_request_notification_kb(cb.from_user.id),
        )

    await tg_send_screen(cb.bot, cb.from_user.id,
        "🙏 <b>Спасибо! Ваше сообщение отправлено.</b>\n\n"
        "Вячеслав прочитает его лично и напишет вам в ближайшее время.",
        None, tg_home_kb(cb.from_user.id))


@tg_router.callback_query(F.data == "go_consult")
async def tg_consult(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(RequestFlow.waiting_text)
    await state.update_data(kind="consult")
    await db.log_event("tg", cb.from_user.id, "consult_open")
    await tg_send_screen(cb.bot, cb.from_user.id,
        "✨ <b>Запрос на консультацию</b>\n\n"
        "Опишите одним сообщением, что вы хотите разобрать: какой вопрос, "
        "ситуация или тема. Вячеслав лично прочитает и свяжется с вами "
        f"в ближайшее время.\n\n<i>Максимум {MAX_REQUEST_LEN} символов.</i>",
        None, tg_home_kb(cb.from_user.id))
    prompt = await cb.bot.send_message(cb.from_user.id, "✏️ Напишите ваш запрос:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш запрос…"))
    await db.add_message_id("tg", cb.from_user.id, prompt.message_id, "main")


@tg_router.callback_query(F.data == "go_ask")
async def tg_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(RequestFlow.waiting_text)
    await state.update_data(kind="question")
    await db.log_event("tg", cb.from_user.id, "ask_open")
    await tg_send_screen(cb.bot, cb.from_user.id,
        "❓ <b>Ваш вопрос</b>\n\n"
        "Напишите вопрос одним сообщением. Вячеслав лично прочитает и "
        f"ответит вам в ближайшее время.\n\n<i>Максимум {MAX_REQUEST_LEN} символов.</i>",
        None, tg_home_kb(cb.from_user.id))
    prompt = await cb.bot.send_message(cb.from_user.id, "✏️ Напишите ваш вопрос:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш вопрос…"))
    await db.add_message_id("tg", cb.from_user.id, prompt.message_id, "main")


@tg_router.message(RequestFlow.waiting_text, F.text)
async def tg_receive_request(message: Message, state: FSMContext):
    await db.add_message_id("tg", message.chat.id, message.message_id, "main")
    data = await state.get_data()
    kind = data.get("kind", "question")
    text = message.text.strip()
    if len(text) < MIN_REQUEST_LEN:
        await message.answer("❌ Слишком коротко. Напишите подробнее:")
        return
    if len(text) > MAX_REQUEST_LEN:
        await message.answer(f"❌ Слишком длинное ({len(text)}). Сократите до {MAX_REQUEST_LEN}:")
        return
    last = await db.get_last_request_time("tg", message.from_user.id)
    if last is not None:
        elapsed = (datetime.now() - last).total_seconds()
        if elapsed < REQUEST_COOLDOWN_SECONDS:
            wait_min = max(1, int((REQUEST_COOLDOWN_SECONDS - elapsed + 59) // 60))
            await message.answer(f"⏳ Вы недавно отправляли обращение. Подождите ~{wait_min} мин.")
            await state.clear()
            await tg_show_welcome(message.bot, message.chat.id)
            return
    req_id = await db.save_request("tg", message.from_user.id, message.from_user.username, kind, text)
    label = "✨ Заявка на консультацию" if kind == "consult" else "❓ Вопрос от пользователя"

    if message.from_user.id not in TG_ADMIN_IDS:
        await notify_admins_event_async(
            f"{label} <b>#{req_id}</b> [TG]\nВремя: {now_str()}\n\n"
            f"💬 <b>Текст:</b>\n{text}\n\n{tg_user_signature(message.from_user)}",
            source="tg",
            tg_reply_markup=tg_admin_request_notification_kb(message.from_user.id),
        )

    await state.clear()
    await tg_send_screen(message.bot, message.chat.id,
        "🙏 <b>Спасибо!</b> Ваше сообщение отправлено.\n\n"
        "Вячеслав прочитает его лично и напишет вам в ближайшее время.",
        None, tg_home_kb(message.from_user.id))


@tg_router.message(RequestFlow.waiting_text)
async def tg_receive_request_nontext(message: Message):
    await message.answer("🙏 Пожалуйста, напишите текстом:")


@tg_router.callback_query(F.data == "free_lesson")
async def tg_free_lesson(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    lesson = await db.get_lesson()
    await tg_send_screen(cb.bot, cb.from_user.id,
        f"Я помогу зарегистрироваться на бесплатный онлайн урок "
        f"«{lesson['title']}», который пройдёт {lesson['lesson_date']} "
        f"в {lesson['lesson_time']} по московскому времени.",
        tg_free_lesson_kb(), tg_home_kb(cb.from_user.id))


@tg_router.callback_query(F.data == "lesson_info")
async def tg_lesson_info(cb: CallbackQuery):
    await cb.answer()
    lesson = await db.get_lesson()
    await tg_send_screen(cb.bot, cb.from_user.id,
        f"💎 <b>Бесплатный онлайн урок {lesson['lesson_date']} "
        f"в {lesson['lesson_time']} по московскому времени "
        f"«{lesson['title']}»</b>\n\n{lesson['info_text']}",
        tg_lesson_info_kb(), tg_home_kb(cb.from_user.id))


@tg_router.callback_query(F.data == "reg_start")
async def tg_reg_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Reg.qualification)
    await state.update_data(qualification=None, surname=None, name=None)
    await tg_send_screen(cb.bot, cb.from_user.id,
        "Расскажите о вашей квалификации в астрологии",
        tg_qualification_kb(), tg_home_kb(cb.from_user.id), tag="reg")


@tg_router.callback_query(F.data.startswith("qual:"))
async def tg_reg_qual(cb: CallbackQuery, state: FSMContext):
    qmap = {"pro": "Разбираюсь, профи", "interest": "Интересуюсь", "learn": "Хочу изучать"}
    qual = qmap.get(cb.data.split(":", 1)[1], "")
    await cb.answer()
    await state.update_data(qualification=qual)
    await state.set_state(Reg.surname)
    await tg_send_with_input(cb.bot, cb.from_user.id,
        f"Квалификация: <b>{qual}</b>\n\nВведите Вашу фамилию",
        tg_surname_kb(), "Ваша фамилия",
        reply_kb=tg_home_kb(cb.from_user.id), tag="reg")


@tg_router.message(Reg.surname, F.text)
async def tg_reg_surname(message: Message, state: FSMContext):
    await db.add_message_id("tg", message.chat.id, message.message_id, "reg")
    text = message.text.strip()
    if not is_valid_name(text):
        await tg_send_with_input(message.bot, message.chat.id,
            "❌ <b>Фамилия должна содержать только буквы</b>, пробел или дефис "
            "(от 2 до 50 символов, без цифр). Попробуйте ещё раз.",
            tg_surname_kb(), "Ваша фамилия",
            reply_kb=tg_home_kb(message.from_user.id), tag="reg")
        return
    await state.update_data(surname=text)
    await state.set_state(Reg.name)
    await tg_send_with_input(message.bot, message.chat.id,
        f"Фамилия: <b>{text}</b>\n\nВведите ваше имя",
        tg_name_kb(), "Ваше имя",
        reply_kb=tg_home_kb(message.from_user.id), tag="reg")


@tg_router.callback_query(F.data == "reg_qual_back")
async def tg_back_qual(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Reg.qualification)
    await state.update_data(surname=None, name=None)
    await tg_send_screen(cb.bot, cb.from_user.id,
        "Расскажите о вашей квалификации в астрологии",
        tg_qualification_kb(), tg_home_kb(cb.from_user.id), tag="reg")


@tg_router.callback_query(F.data == "reg_surname_back")
async def tg_back_surname(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await state.set_state(Reg.surname)
    await state.update_data(name=None)
    await tg_send_with_input(cb.bot, cb.from_user.id,
        f"Квалификация: <b>{data.get('qualification') or '—'}</b>\n\nВведите Вашу фамилию",
        tg_surname_kb(), "Ваша фамилия",
        reply_kb=tg_home_kb(cb.from_user.id), tag="reg")


@tg_router.message(Reg.name, F.text)
async def tg_reg_name(message: Message, state: FSMContext):
    await db.add_message_id("tg", message.chat.id, message.message_id, "reg")
    text = message.text.strip()
    if not is_valid_name(text):
        await tg_send_with_input(message.bot, message.chat.id,
            "❌ <b>Имя должно содержать только буквы</b>, пробел или дефис "
            "(от 2 до 50 символов, без цифр). Попробуйте ещё раз.",
            tg_name_kb(), "Ваше имя",
            reply_kb=tg_home_kb(message.from_user.id), tag="reg")
        return
    data = await state.get_data()
    surname = data.get("surname") or "—"
    qual = data.get("qualification") or "—"
    await db.save_user("tg", message.from_user.id, message.from_user.username,
                       surname, text, qual)
    await state.clear()
    await tg_cleanup_reg(message.bot, message.chat.id)

    if message.from_user.id not in TG_ADMIN_IDS:
        await notify_admins_event_async(
            f"🎉 <b>Новая регистрация [TG]</b>\n\n"
            f"<b>Фамилия:</b> {surname}\n<b>Имя:</b> {text}\n"
            f"<b>Квалификация:</b> {qual}\nВремя: {now_str()}\n\n"
            f"{tg_user_signature(message.from_user)}",
            source="tg",
            tg_reply_markup=tg_admin_request_notification_kb(message.from_user.id),
        )

    lesson = await db.get_lesson()
    await tg_send_screen(message.bot, message.chat.id,
        "🙏 <b>Спасибо, вы зарегистрировались!</b>\n\n"
        f"Вы записаны на бесплатный онлайн урок «{lesson['title']}», "
        f"который пройдёт {lesson['lesson_date']} в {lesson['lesson_time']} "
        f"по московскому времени. За 10 минут до начала я пришлю Вам ссылку "
        f"на онлайн-трансляцию.\n\n"
        f"<b>📋 Ваши данные:</b>\n"
        f"• <b>Фамилия:</b> {surname}\n• <b>Имя:</b> {text}\n"
        f"• <b>Квалификация:</b> {qual}",
        None, tg_home_kb(message.from_user.id))


@tg_router.callback_query(F.data.startswith("admin:"))
async def tg_admin_action(cb: CallbackQuery, state: FSMContext):
    if not is_tg_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]
    if action == "title":
        await state.set_state(AdminStates.edit_title)
        await cb.message.answer("Введите новое название урока:", reply_markup=tg_admin_cancel_kb())
    elif action == "date":
        await state.set_state(AdminStates.edit_date)
        await cb.message.answer("Введите новую дату ДД.ММ.ГГГГ:", reply_markup=tg_admin_cancel_kb())
    elif action == "time":
        await state.set_state(AdminStates.edit_time)
        await cb.message.answer("Введите новое время ЧЧ:ММ:", reply_markup=tg_admin_cancel_kb())
    elif action == "info":
        await state.set_state(AdminStates.edit_info)
        await cb.message.answer("Введите новый текст «Что за урок?»:", reply_markup=tg_admin_cancel_kb())
    elif action == "link":
        await state.set_state(AdminStates.edit_link)
        await cb.message.answer("Введите ссылку на трансляцию:", reply_markup=tg_admin_cancel_kb())
    elif action == "cancel":
        await state.clear()
        await cb.message.answer("Отменено.", reply_markup=tg_admin_kb())
    elif action == "list":
        users = await db.get_all_users()
        if not users:
            await cb.message.answer("Пока нет зарегистрированных.")
        else:
            chunks = [f"👥 <b>Всего: {len(users)}</b>\n"]
            for idx, u in enumerate(users[:50], start=1):
                chunks.append(format_user_card(u, idx=idx, for_tg=True))
            if len(users) > 50:
                chunks.append(f"\n… и ещё {len(users) - 50}")
            buffer = ""
            for chunk in chunks:
                piece = (chunk + "\n") if buffer else chunk
                if len(buffer) + len(piece) > 3800:
                    await cb.message.answer(buffer)
                    buffer = piece
                else:
                    buffer += piece
            if buffer.strip():
                await cb.message.answer(buffer)
    elif action == "requests":
        reqs = await db.get_last_requests(20)
        if not reqs:
            await cb.message.answer("Пока нет обращений.")
        else:
            lines = [f"📥 Последние обращения ({len(reqs)}):\n"]
            for r in reqs:
                km = {"consult": "✨ Консультация", "question": "❓ Вопрос", "astrologer": "🚀 Астролог"}
                t = (r["text"] or "").strip()
                if len(t) > 150:
                    t = t[:150] + "…"
                lines.append(
                    f"#{r['id']} [{r['platform']}] {km.get(r['kind'], r['kind'])}\n"
                    f"🆔 <code>{r['user_id']}</code>\n"
                    f"{t}\n—"
                )
            await cb.message.answer("\n".join(lines))
    elif action == "broadcast":
        await state.set_state(AdminStates.broadcast)
        await cb.message.answer(
            "📢 Рассылка во все мессенджеры (TG + VK)\n\n"
            "Отправьте текст. После — покажу превью.",
            reply_markup=tg_admin_cancel_kb())
    elif action == "broadcast_confirm":
        data = await state.get_data()
        text = data.get("broadcast_text")
        if not text:
            await cb.answer("Текст потерян", show_alert=True)
            return
        await state.clear()
        await cb.message.answer("⏳ Отправляю…")
        sent_tg, fail_tg = await broadcast_tg(cb.bot, text)
        sent_vk, fail_vk = await broadcast_vk(text)
        await cb.message.answer(
            f"✅ <b>Готово</b>\n\nTG: {sent_tg} ок, {fail_tg} ошибок\n"
            f"VK: {sent_vk} ок, {fail_vk} ошибок",
            reply_markup=tg_admin_kb())
    elif action == "send_link":
        lesson = await db.get_lesson()
        link = lesson.get("broadcast_link") or ""
        if not link:
            await cb.message.answer("⚠️ Сначала задайте ссылку.")
            return
        text = f"🚀 Ссылка на онлайн-трансляцию урока «{lesson['title']}»:\n{link}"
        sent_tg, fail_tg = await broadcast_tg(cb.bot, text)
        sent_vk, fail_vk = await broadcast_vk(text)
        await cb.message.answer(f"📨 TG: {sent_tg}/{fail_tg} | VK: {sent_vk}/{fail_vk}")
    await cb.answer()


@tg_router.message(AdminStates.edit_title, F.text)
async def tg_admin_title(message: Message, state: FSMContext):
    await db.update_lesson("title", message.text.strip())
    await state.clear()
    await message.answer("✅ Название обновлено.", reply_markup=tg_admin_kb())


@tg_router.message(AdminStates.edit_date, F.text)
async def tg_admin_date(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Формат ДД.ММ.ГГГГ")
        return
    await db.update_lesson("lesson_date", message.text.strip())
    await state.clear()
    await message.answer("✅ Дата обновлена.", reply_markup=tg_admin_kb())


@tg_router.message(AdminStates.edit_time, F.text)
async def tg_admin_time(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%H:%M")
    except ValueError:
        await message.answer("❌ Формат ЧЧ:ММ")
        return
    await db.update_lesson("lesson_time", message.text.strip())
    await state.clear()
    await message.answer("✅ Время обновлено.", reply_markup=tg_admin_kb())


@tg_router.message(AdminStates.edit_info, F.text)
async def tg_admin_info(message: Message, state: FSMContext):
    await db.update_lesson("info_text", message.html_text)
    await state.clear()
    await message.answer("✅ Обновлено.", reply_markup=tg_admin_kb())


@tg_router.message(AdminStates.edit_link, F.text)
async def tg_admin_link(message: Message, state: FSMContext):
    await db.update_lesson("broadcast_link", message.text.strip())
    await state.clear()
    await message.answer("✅ Ссылка обновлена.", reply_markup=tg_admin_kb())


@tg_router.message(AdminStates.broadcast, F.text)
async def tg_admin_broadcast_preview(message: Message, state: FSMContext):
    text = message.html_text or message.text
    await state.update_data(broadcast_text=text)
    tg_users = await db.get_users_by_platform("tg")
    vk_users = await db.get_users_by_platform("vk")
    await message.answer(
        f"📢 Превью\nTG: {len(tg_users)} получателей\nVK: {len(vk_users)} получателей\n\n"
        f"—————\n{text}\n—————",
        reply_markup=tg_admin_broadcast_confirm_kb())


# ==================== ГРАНИЦЫ РАССЫЛОК ====================
async def get_recipients_async(platform: str) -> list[dict]:
    users = await db.get_users_by_platform(platform)
    ids = {int(u["user_id"]) for u in users}
    if platform == "tg":
        for admin_id in TG_ADMIN_IDS:
            if admin_id not in ids:
                users.append({"user_id": admin_id, "platform": "tg"})
    elif platform == "vk":
        for admin_id in VK_ADMIN_IDS:
            if admin_id not in ids:
                users.append({"user_id": admin_id, "platform": "vk"})
    return users


async def broadcast_tg(bot, text_html):
    users = await get_recipients_async("tg")
    sent = 0
    failed = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], text_html)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed


async def broadcast_vk(text_any):
    text_plain = strip_html_for_vk(text_any)
    users = await get_recipients_async("vk")
    sent = 0
    failed = 0
    for u in users:
        try:
            await asyncio.to_thread(
                get_vk().messages.send,
                user_id=u["user_id"], message=text_plain, random_id=get_random_id())
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed


# ==================== VK KEYBOARDS ====================
def vk_home_kb(user_id):
    kb = VkKeyboard(one_time=False)
    kb.add_button(HOME_BUTTON_TEXT, color=VkKeyboardColor.PRIMARY)
    if is_vk_admin(user_id):
        kb.add_line()
        kb.add_button(ADMIN_BUTTON_TEXT, color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def vk_main_menu_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("🚀 Стать астрологом", color=VkKeyboardColor.PRIMARY, payload={"cmd": "go_astrologer"})
    kb.add_line()
    kb.add_button("🔥 Бесплатный урок", color=VkKeyboardColor.PRIMARY, payload={"cmd": "free_lesson"})
    kb.add_line()
    kb.add_button("✨ Получить консультацию", color=VkKeyboardColor.POSITIVE, payload={"cmd": "go_consult"})
    kb.add_line()
    kb.add_button("❓ Задать вопрос", color=VkKeyboardColor.POSITIVE, payload={"cmd": "go_ask"})
    kb.add_line()
    kb.add_button("📝 Отзывы", color=VkKeyboardColor.SECONDARY,
                  payload={"cmd": "open_link", "url": "https://t.me/otzyvy_bolotov"})
    return kb.get_keyboard()


def vk_astrologer_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("🪞 Понимание себя", color=VkKeyboardColor.PRIMARY, payload={"cmd": "astro", "value": "understand"})
    kb.add_line()
    kb.add_button("💼 Новая профессия", color=VkKeyboardColor.PRIMARY, payload={"cmd": "astro", "value": "career"})
    kb.add_line()
    kb.add_button("💰 Источник дохода", color=VkKeyboardColor.PRIMARY, payload={"cmd": "astro", "value": "income"})
    kb.add_line()
    kb.add_button("🧰 Дополнительный инструмент", color=VkKeyboardColor.PRIMARY, payload={"cmd": "astro", "value": "tool"})
    return kb.get_keyboard()


def vk_free_lesson_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("✅ Регистрируюсь!", color=VkKeyboardColor.POSITIVE, payload={"cmd": "reg_start"})
    kb.add_line()
    kb.add_button("❓ Что за урок?", color=VkKeyboardColor.SECONDARY, payload={"cmd": "lesson_info"})
    return kb.get_keyboard()


def vk_lesson_info_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("✅ Регистрируюсь!", color=VkKeyboardColor.POSITIVE, payload={"cmd": "reg_start"})
    kb.add_line()
    kb.add_button("⏪ Назад", color=VkKeyboardColor.SECONDARY, payload={"cmd": "free_lesson"})
    return kb.get_keyboard()


def vk_qualification_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("😎 Разбираюсь, профи", color=VkKeyboardColor.PRIMARY, payload={"cmd": "qual", "value": "pro"})
    kb.add_line()
    kb.add_button("🤔 Интересуюсь", color=VkKeyboardColor.PRIMARY, payload={"cmd": "qual", "value": "interest"})
    kb.add_line()
    kb.add_button("🤩 Хочу изучать", color=VkKeyboardColor.PRIMARY, payload={"cmd": "qual", "value": "learn"})
    return kb.get_keyboard()


def vk_back_only_kb(cmd, label="⏪ Назад"):
    kb = VkKeyboard(inline=True)
    kb.add_button(label, color=VkKeyboardColor.SECONDARY, payload={"cmd": cmd})
    return kb.get_keyboard()


def vk_confirm_kb(prefix):
    kb = VkKeyboard(inline=True)
    kb.add_button("✏️ Исправить", color=VkKeyboardColor.SECONDARY, payload={"cmd": f"{prefix}_fix"})
    kb.add_button("✅ Далее", color=VkKeyboardColor.POSITIVE, payload={"cmd": f"{prefix}_confirm"})
    return kb.get_keyboard()


def vk_admin_kb():
    """5 строк по 2 кнопки — укладываемся в лимит VK (max 6 lines)."""
    kb = VkKeyboard(inline=True)
    kb.add_button("✏️ Название", color=VkKeyboardColor.PRIMARY, payload={"cmd": "admin", "value": "title"})
    kb.add_button("📅 Дата", color=VkKeyboardColor.PRIMARY, payload={"cmd": "admin", "value": "date"})
    kb.add_line()
    kb.add_button("🕐 Время", color=VkKeyboardColor.PRIMARY, payload={"cmd": "admin", "value": "time"})
    kb.add_button("📝 Текст", color=VkKeyboardColor.PRIMARY, payload={"cmd": "admin", "value": "info"})
    kb.add_line()
    kb.add_button("🔗 Ссылка", color=VkKeyboardColor.PRIMARY, payload={"cmd": "admin", "value": "link"})
    kb.add_button("👥 Список", color=VkKeyboardColor.SECONDARY, payload={"cmd": "admin", "value": "list"})
    kb.add_line()
    kb.add_button("📥 Обращения", color=VkKeyboardColor.SECONDARY, payload={"cmd": "admin", "value": "requests"})
    kb.add_button("📢 Рассылка", color=VkKeyboardColor.POSITIVE, payload={"cmd": "admin", "value": "broadcast"})
    kb.add_line()
    kb.add_button("📨 Ссылка сейчас", color=VkKeyboardColor.POSITIVE, payload={"cmd": "admin", "value": "send_link"})
    kb.add_button("📤 CSV", color=VkKeyboardColor.SECONDARY, payload={"cmd": "admin", "value": "export"})
    return kb.get_keyboard()


def vk_admin_cancel_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("⏪ Отмена", color=VkKeyboardColor.SECONDARY, payload={"cmd": "admin", "value": "cancel"})
    return kb.get_keyboard()


def vk_admin_broadcast_confirm_kb():
    kb = VkKeyboard(inline=True)
    kb.add_button("✅ Отправить всем", color=VkKeyboardColor.POSITIVE, payload={"cmd": "admin", "value": "broadcast_confirm"})
    kb.add_line()
    kb.add_button("⏪ Отмена", color=VkKeyboardColor.SECONDARY, payload={"cmd": "admin", "value": "cancel"})
    return kb.get_keyboard()


# ==================== VK SENDING ====================
def vk_send(user_id, text, keyboard=None):
    params = {"user_id": user_id, "message": text, "random_id": get_random_id()}
    if keyboard:
        params["keyboard"] = keyboard
    try:
        get_vk().messages.send(**params)
    except Exception as e:
        logging.warning(f"VK send {user_id}: {e}")


def vk_user_link(vk_id, username=None):
    return f"https://vk.com/{username}" if username else f"https://vk.com/id{vk_id}"


def vk_user_signature(vk_id, username=None, profile_name=None):
    lines = ["👤 <b>VK-пользователь</b>"]
    if profile_name:
        lines.append(f"📛 {profile_name}")
    lines.append(f"🆔 <code>{vk_id}</code>")
    if username:
        lines.append(f'🔗 <a href="https://vk.com/{username}">vk.com/{username}</a>')
    else:
        lines.append(f'🔗 <a href="https://vk.com/id{vk_id}">vk.com/id{vk_id}</a>')
    return "\n".join(lines)


# ==================== VK HANDLERS ====================
def vk_show_welcome(user_id):
    db_sync(db.clear_state("vk", user_id))
    vk_send(user_id, WELCOME_TEXT, keyboard=vk_home_kb(user_id))
    vk_send(user_id, "👇 Выберите действие:", keyboard=vk_main_menu_kb())


def vk_handle_payload(user_id, payload):
    cmd = payload.get("cmd")

    if cmd == "go_astrologer":
        db_sync(db.clear_state("vk", user_id))
        db_sync(db.log_event("vk", user_id, "astrologer_open"))
        vk_send(user_id, "🚀 Что вы хотите обрести благодаря астрологии?",
                keyboard=vk_astrologer_kb())
        return

    if cmd == "astro":
        cmap = {"understand": "Понимание себя", "career": "Новая профессия",
                "income": "Источник дохода", "tool": "Дополнительный инструмент"}
        choice = cmap.get(payload.get("value"), "—")
        db_sync(db.clear_state("vk", user_id))

        # Получаем username и имя из VK
        vk_username = vk_get_username(user_id)
        vk_profile = vk_get_profile_name(user_id)

        req_id = db_sync(db.save_request("vk", user_id, vk_username, "astrologer", choice))

        if not is_vk_admin(user_id):
            notify_admins_event_sync(
                f"🚀 <b>Заявка «Стать астрологом» #{req_id}</b> [VK]\n"
                f"Время: {now_str()}\n\n🎯 <b>Цель:</b> {choice}\n\n"
                f"{vk_user_signature(user_id, vk_username, vk_profile)}",
                source="vk",
                vk_user_id=user_id,
                vk_username=vk_username,
            )

        vk_send(user_id,
                "🙏 Спасибо! Ваше сообщение отправлено.\n\n"
                "Вячеслав прочитает его лично и напишет вам в ближайшее время.",
                keyboard=vk_home_kb(user_id))
        return

    if cmd == "free_lesson":
        db_sync(db.clear_state("vk", user_id))
        lesson = db_sync(db.get_lesson())
        vk_send(user_id,
            f"Я помогу зарегистрироваться на бесплатный онлайн урок "
            f"«{lesson['title']}», который пройдёт {lesson['lesson_date']} "
            f"в {lesson['lesson_time']} по московскому времени.",
            keyboard=vk_home_kb(user_id))
        vk_send(user_id, "👇 Выберите действие:", keyboard=vk_free_lesson_kb())
        return

    if cmd == "lesson_info":
        lesson = db_sync(db.get_lesson())
        vk_send(user_id,
            f"💎 Бесплатный онлайн урок {lesson['lesson_date']} "
            f"в {lesson['lesson_time']} по московскому времени "
            f"«{lesson['title']}»\n\n{lesson['info_text']}",
            keyboard=vk_home_kb(user_id))
        vk_send(user_id, "👇 Выберите действие:", keyboard=vk_lesson_info_kb())
        return

    if cmd == "reg_start":
        db_sync(db.set_state("vk", user_id, "reg_qual", {}))
        vk_send(user_id, "Расскажите о вашей квалификации в астрологии",
                keyboard=vk_qualification_kb())
        return

    if cmd == "qual":
        qmap = {"pro": "Разбираюсь, профи", "interest": "Интересуюсь", "learn": "Хочу изучать"}
        qual = qmap.get(payload.get("value"), "")
        db_sync(db.set_state("vk", user_id, "reg_surname", {"qualification": qual}))
        vk_send(user_id, f"Квалификация: {qual}\n\nВведите Вашу фамилию",
                keyboard=vk_back_only_kb("reg_qual_back"))
        return

    if cmd == "reg_qual_back":
        db_sync(db.set_state("vk", user_id, "reg_qual", {}))
        vk_send(user_id, "Расскажите о вашей квалификации в астрологии",
                keyboard=vk_qualification_kb())
        return

    if cmd == "surname_fix":
        _, data = db_sync(db.get_state("vk", user_id))
        db_sync(db.set_state("vk", user_id, "reg_surname", {"qualification": data.get("qualification")}))
        vk_send(user_id,
                f"Квалификация: {data.get('qualification') or '—'}\n\nВведите Вашу фамилию заново",
                keyboard=vk_back_only_kb("reg_qual_back"))
        return

    if cmd == "surname_confirm":
        _, data = db_sync(db.get_state("vk", user_id))
        if not data.get("surname"):
            vk_send(user_id, "Сначала введите фамилию.")
            return
        db_sync(db.set_state("vk", user_id, "reg_name", data))
        vk_send(user_id, f"Фамилия: {data['surname']}\n\nВведите ваше имя",
                keyboard=vk_back_only_kb("reg_surname_back"))
        return

    if cmd == "reg_surname_back":
        _, data = db_sync(db.get_state("vk", user_id))
        db_sync(db.set_state("vk", user_id, "reg_surname", {"qualification": data.get("qualification")}))
        vk_send(user_id,
                f"Квалификация: {data.get('qualification') or '—'}\n\nВведите Вашу фамилию",
                keyboard=vk_back_only_kb("reg_qual_back"))
        return

    if cmd == "go_consult":
        db_sync(db.set_state("vk", user_id, "request_text", {"kind": "consult"}))
        vk_send(user_id,
            "✨ Запрос на консультацию\n\n"
            "Опишите одним сообщением, что вы хотите разобрать: какой вопрос, "
            "ситуация или тема. Вячеслав лично прочитает и свяжется с вами "
            f"в ближайшее время.\n\nМаксимум {MAX_REQUEST_LEN} символов.\n\n"
            "✏️ Напишите ваш запрос:",
            keyboard=vk_home_kb(user_id))
        return

    if cmd == "go_ask":
        db_sync(db.set_state("vk", user_id, "request_text", {"kind": "question"}))
        vk_send(user_id,
            "❓ Ваш вопрос\n\nНапишите вопрос одним сообщением. Вячеслав лично "
            "прочитает и ответит вам в ближайшее время.\n\n"
            f"Максимум {MAX_REQUEST_LEN} символов.\n\n✏️ Напишите ваш вопрос:",
            keyboard=vk_home_kb(user_id))
        return

    if cmd == "open_link":
        vk_send(user_id, f"🔗 {payload.get('url', '')}", keyboard=vk_home_kb(user_id))
        return

    if cmd == "admin" and is_vk_admin(user_id):
        val = payload.get("value")
        if val == "title":
            db_sync(db.set_state("vk", user_id, "admin_title", {}))
            vk_send(user_id, "Введите новое название урока:", keyboard=vk_admin_cancel_kb())
        elif val == "date":
            db_sync(db.set_state("vk", user_id, "admin_date", {}))
            vk_send(user_id, "Введите новую дату ДД.ММ.ГГГГ:", keyboard=vk_admin_cancel_kb())
        elif val == "time":
            db_sync(db.set_state("vk", user_id, "admin_time", {}))
            vk_send(user_id, "Введите новое время ЧЧ:ММ:", keyboard=vk_admin_cancel_kb())
        elif val == "info":
            db_sync(db.set_state("vk", user_id, "admin_info", {}))
            vk_send(user_id, "Введите новый текст «Что за урок?»:", keyboard=vk_admin_cancel_kb())
        elif val == "link":
            db_sync(db.set_state("vk", user_id, "admin_link", {}))
            vk_send(user_id, "Введите ссылку на трансляцию:", keyboard=vk_admin_cancel_kb())
        elif val == "list":
            users = db_sync(db.get_all_users())
            if not users:
                vk_send(user_id, "Пока нет зарегистрированных.", keyboard=vk_admin_kb())
            else:
                chunks = [f"👥 Всего: {len(users)}\n"]
                for idx, u in enumerate(users[:50], start=1):
                    chunks.append(format_user_card(u, idx=idx, for_tg=False))
                if len(users) > 50:
                    chunks.append(f"\n… и ещё {len(users) - 50}")
                buffer = ""
                for chunk in chunks:
                    piece = (chunk + "\n") if buffer else chunk
                    if len(buffer) + len(piece) > 3500:
                        vk_send(user_id, buffer, keyboard=vk_admin_kb())
                        buffer = piece
                    else:
                        buffer += piece
                if buffer.strip():
                    vk_send(user_id, buffer, keyboard=vk_admin_kb())
        elif val == "requests":
            reqs = db_sync(db.get_last_requests(20))
            if not reqs:
                vk_send(user_id, "Пока нет обращений.", keyboard=vk_admin_kb())
            else:
                lines = [f"📥 Последние ({len(reqs)}):\n"]
                for r in reqs:
                    t = (r["text"] or "").strip()
                    if len(t) > 150:
                        t = t[:150] + "…"
                    lines.append(f"#{r['id']} [{r['platform']}] {r['kind']}\nid: {r['user_id']}\n{t}\n—")
                vk_send(user_id, "\n".join(lines), keyboard=vk_admin_kb())
        elif val == "export":
            users = db_sync(db.get_all_users())
            lines = ["platform,user_id,surname,name,qualification,registered_at"]
            for u in users:
                lines.append(f"{u['platform']},{u['user_id']},{u.get('surname') or ''},"
                             f"{u.get('name') or ''},{u.get('qualification') or ''},"
                             f"{u.get('registered_at') or ''}")
            vk_send(user_id, "📤 CSV:\n\n" + "\n".join(lines[:60]), keyboard=vk_admin_kb())
        elif val == "broadcast":
            db_sync(db.set_state("vk", user_id, "admin_broadcast", {}))
            vk_send(user_id,
                "📢 Рассылка во все мессенджеры (TG + VK).\n\n"
                "Отправьте текст сообщения.",
                keyboard=vk_admin_cancel_kb())
        elif val == "broadcast_confirm":
            _, data = db_sync(db.get_state("vk", user_id))
            text = data.get("broadcast_text")
            if not text:
                vk_send(user_id, "Текст потерян, начните заново.", keyboard=vk_admin_kb())
                return
            db_sync(db.clear_state("vk", user_id))
            sent_tg, fail_tg = db_sync(broadcast_tg(tg_bot_global, text))
            sent_vk, fail_vk = db_sync(broadcast_vk(text))
            vk_send(user_id,
                f"✅ Рассылка завершена\nTG: {sent_tg}/{fail_tg}\nVK: {sent_vk}/{fail_vk}",
                keyboard=vk_admin_kb())
        elif val == "send_link":
            lesson = db_sync(db.get_lesson())
            link = lesson.get("broadcast_link") or ""
            if not link:
                vk_send(user_id, "⚠️ Сначала задайте ссылку.", keyboard=vk_admin_kb())
                return
            text = f"🚀 Ссылка на онлайн-трансляцию урока «{lesson['title']}»:\n{link}"
            sent_tg, fail_tg = db_sync(broadcast_tg(tg_bot_global, text))
            sent_vk, fail_vk = db_sync(broadcast_vk(text))
            vk_send(user_id, f"📨 TG: {sent_tg}/{fail_tg} | VK: {sent_vk}/{fail_vk}",
                    keyboard=vk_admin_kb())
        elif val == "cancel":
            db_sync(db.clear_state("vk", user_id))
            vk_send(user_id, "Отменено.", keyboard=vk_admin_kb())
        return


def vk_handle_text(user_id, text):
    text_stripped = text.strip()

    if text_stripped == HOME_BUTTON_TEXT:
        vk_show_welcome(user_id)
        return

    if text_stripped == ADMIN_BUTTON_TEXT and is_vk_admin(user_id):
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id, "🛠 Админ-панель\nВыберите действие:", keyboard=vk_admin_kb())
        return

    state, data = db_sync(db.get_state("vk", user_id))

    if state == "reg_surname":
        if not is_valid_name(text_stripped):
            vk_send(user_id,
                "❌ Фамилия должна содержать только буквы, пробел или дефис "
                "(от 2 до 50 символов, без цифр). Попробуйте ещё раз.",
                keyboard=vk_back_only_kb("reg_qual_back"))
            return
        data["surname"] = text_stripped
        db_sync(db.set_state("vk", user_id, "reg_surname_confirm", data))
        vk_send(user_id, f"📝 Проверьте, всё верно?\n\nФамилия: {data['surname']}",
                keyboard=vk_confirm_kb("surname"))
        return

    if state == "reg_name":
        if not is_valid_name(text_stripped):
            vk_send(user_id,
                "❌ Имя должно содержать только буквы, пробел или дефис "
                "(от 2 до 50 символов, без цифр). Попробуйте ещё раз.",
                keyboard=vk_back_only_kb("reg_surname_back"))
            return
        data["name"] = text_stripped
        vk_finish_registration(user_id, data)
        return

    if state == "request_text":
        kind = data.get("kind", "question")
        if len(text_stripped) < MIN_REQUEST_LEN:
            vk_send(user_id, "❌ Слишком коротко. Напишите чуть подробнее:")
            return
        if len(text_stripped) > MAX_REQUEST_LEN:
            vk_send(user_id, f"❌ Слишком длинное ({len(text_stripped)}). Сократите до {MAX_REQUEST_LEN}:")
            return
        last = db_sync(db.get_last_request_time("vk", user_id))
        if last is not None:
            elapsed = (datetime.now() - last).total_seconds()
            if elapsed < REQUEST_COOLDOWN_SECONDS:
                wait_min = max(1, int((REQUEST_COOLDOWN_SECONDS - elapsed + 59) // 60))
                vk_send(user_id, f"⏳ Вы недавно отправляли обращение. Подождите ~{wait_min} мин.")
                db_sync(db.clear_state("vk", user_id))
                vk_show_welcome(user_id)
                return

        vk_username = vk_get_username(user_id)
        vk_profile = vk_get_profile_name(user_id)

        req_id = db_sync(db.save_request("vk", user_id, vk_username, kind, text_stripped))
        label = "✨ Заявка на консультацию" if kind == "consult" else "❓ Вопрос от пользователя"
        if not is_vk_admin(user_id):
            notify_admins_event_sync(
                f"{label} <b>#{req_id}</b> [VK]\n"
                f"Время: {now_str()}\n\n💬 <b>Текст:</b>\n{text_stripped}\n\n"
                f"{vk_user_signature(user_id, vk_username, vk_profile)}",
                source="vk",
                vk_user_id=user_id,
                vk_username=vk_username,
            )
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id,
            "🙏 Спасибо! Ваше сообщение отправлено.\n\n"
            "Вячеслав прочитает его лично и напишет вам в ближайшее время.",
            keyboard=vk_home_kb(user_id))
        return

    if state == "admin_title":
        db_sync(db.update_lesson("title", text_stripped))
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id, "✅ Название обновлено.", keyboard=vk_admin_kb())
        return

    if state == "admin_date":
        try:
            datetime.strptime(text_stripped, "%d.%m.%Y")
        except ValueError:
            vk_send(user_id, "❌ Формат ДД.ММ.ГГГГ")
            return
        db_sync(db.update_lesson("lesson_date", text_stripped))
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id, "✅ Дата обновлена.", keyboard=vk_admin_kb())
        return

    if state == "admin_time":
        try:
            datetime.strptime(text_stripped, "%H:%M")
        except ValueError:
            vk_send(user_id, "❌ Формат ЧЧ:ММ")
            return
        db_sync(db.update_lesson("lesson_time", text_stripped))
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id, "✅ Время обновлено.", keyboard=vk_admin_kb())
        return

    if state == "admin_info":
        db_sync(db.update_lesson("info_text", text_stripped))
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id, "✅ Обновлено.", keyboard=vk_admin_kb())
        return

    if state == "admin_link":
        db_sync(db.update_lesson("broadcast_link", text_stripped))
        db_sync(db.clear_state("vk", user_id))
        vk_send(user_id, "✅ Ссылка обновлена.", keyboard=vk_admin_kb())
        return

    if state == "admin_broadcast":
        data["broadcast_text"] = text_stripped
        db_sync(db.set_state("vk", user_id, "admin_broadcast", data))
        tg_users = db_sync(db.get_users_by_platform("tg"))
        vk_users = db_sync(db.get_users_by_platform("vk"))
        vk_send(user_id,
            f"📢 Превью\nTG: {len(tg_users)} | VK: {len(vk_users)}\n\n"
            f"—————\n{text_stripped}\n—————",
            keyboard=vk_admin_broadcast_confirm_kb())
        return

    vk_show_welcome(user_id)


def vk_finish_registration(user_id, data):
    surname = data.get("surname") or "—"
    name = data.get("name") or "—"
    qual = data.get("qualification") or "—"

    # Данные из VK-профиля
    vk_username = vk_get_username(user_id)
    vk_profile = vk_get_profile_name(user_id)

    db_sync(db.save_user("vk", user_id, vk_username, surname, name, qual))
    db_sync(db.clear_state("vk", user_id))

    if not is_vk_admin(user_id):
        notify_admins_event_sync(
            f"🎉 <b>Новая регистрация [VK]</b>\n\n"
            f"<b>Фамилия:</b> {surname}\n"
            f"<b>Имя:</b> {name}\n"
            f"<b>Квалификация:</b> {qual}\n"
            f"<b>Профиль VK:</b> {vk_profile or '—'}\n"
            f"Время: {now_str()}\n\n"
            f"{vk_user_signature(user_id, vk_username, vk_profile)}",
            source="vk",
            vk_user_id=user_id,
            vk_username=vk_username,
        )

    lesson = db_sync(db.get_lesson())
    vk_send(user_id,
        "🙏 Спасибо, вы зарегистрировались!\n\n"
        f"Вы записаны на бесплатный онлайн урок «{lesson['title']}», "
        f"который пройдёт {lesson['lesson_date']} в {lesson['lesson_time']} "
        f"по московскому времени. За 10 минут до начала я пришлю Вам ссылку "
        f"на онлайн-трансляцию.\n\n"
        f"📋 Ваши данные:\n• Фамилия: {surname}\n• Имя: {name}\n"
        f"• Квалификация: {qual}",
        keyboard=vk_home_kb(user_id))


def vk_longpoll_thread():
    logging.info("VK longpoll стартовал")
    while True:
        try:
            for event in _vk_longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    obj = event.object.message
                    user_id = obj.get("from_id")
                    text = obj.get("text", "")
                    payload_raw = obj.get("payload")
                    if payload_raw:
                        try:
                            payload = json.loads(payload_raw)
                        except Exception:
                            payload = {}
                        vk_handle_payload(user_id, payload)
                    else:
                        vk_handle_text(user_id, text)
        except Exception as e:
            logging.exception(f"VK longpoll ошибка: {e}")
            time.sleep(3)


# ==================== ПЛАНИРОВЩИК ====================
async def check_lessons(bot):
    lesson = await db.get_lesson()
    if not lesson:
        return
    date_str = lesson.get("lesson_date") or ""
    time_str = lesson.get("lesson_time") or ""
    if not date_str or not time_str or date_str == "00.00.2026":
        return
    try:
        lesson_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M").replace(tzinfo=ZoneInfo(TIMEZONE))
    except ValueError:
        return
    now = datetime.now(ZoneInfo(TIMEZONE))

    if not lesson.get("sent_24h_tg") and lesson_dt - timedelta(hours=24) <= now < lesson_dt:
        await broadcast_tg(bot, f"🔔 Напоминаю: завтра бесплатный онлайн урок «{lesson['title']}» в {lesson['lesson_time']} МСК.")
        await db.mark_sent("sent_24h_tg")
    if not lesson.get("sent_24h_vk") and lesson_dt - timedelta(hours=24) <= now < lesson_dt:
        await broadcast_vk(f"🔔 Напоминаю: завтра бесплатный онлайн урок «{lesson['title']}» в {lesson['lesson_time']} МСК.")
        await db.mark_sent("sent_24h_vk")

    if not lesson.get("sent_1h_tg") and lesson_dt - timedelta(hours=1) <= now < lesson_dt:
        await broadcast_tg(bot, f"⏰ Через час начнётся бесплатный онлайн урок «{lesson['title']}» в {lesson['lesson_time']} МСК.")
        await db.mark_sent("sent_1h_tg")
    if not lesson.get("sent_1h_vk") and lesson_dt - timedelta(hours=1) <= now < lesson_dt:
        await broadcast_vk(f"⏰ Через час начнётся бесплатный онлайн урок «{lesson['title']}» в {lesson['lesson_time']} МСК.")
        await db.mark_sent("sent_1h_vk")

    if not lesson.get("sent_10min_tg") and lesson_dt - timedelta(minutes=10) <= now < lesson_dt + timedelta(minutes=30):
        link = lesson.get("broadcast_link") or ""
        text = ("🚀 Урок начинается через 10 минут!\n" +
                (f"Ссылка: {link}" if link else "Ссылка появится в следующем сообщении."))
        await broadcast_tg(bot, text)
        await db.mark_sent("sent_10min_tg")
    if not lesson.get("sent_10min_vk") and lesson_dt - timedelta(minutes=10) <= now < lesson_dt + timedelta(minutes=30):
        link = lesson.get("broadcast_link") or ""
        text = ("🚀 Урок начинается через 10 минут!\n" +
                (f"Ссылка: {link}" if link else "Ссылка появится в следующем сообщении."))
        await broadcast_vk(text)
        await db.mark_sent("sent_10min_vk")


# ==================== MAIN ====================
async def main():
    global MAIN_LOOP, tg_bot_global

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    MAIN_LOOP = asyncio.get_running_loop()

    await db.init()

    get_vk()
    threading.Thread(target=vk_longpoll_thread, daemon=True).start()

    bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    tg_bot_global = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(tg_router)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_lessons, "interval", minutes=1, args=[bot])
    scheduler.start()

    logging.info("Оба бота запущены")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
