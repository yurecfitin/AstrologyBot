import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [6113518001, 1815133569]
DB_PATH = "bot.db"
TIMEZONE = "Europe/Moscow"

HOME_BUTTON_TEXT = "🏠 Главное меню"
ADMIN_BUTTON_TEXT = "🛠 Админ-панель"
TAG_REG = "reg"
TAG_MAIN = "main"

MAX_REQUEST_LEN = 2000
MIN_REQUEST_LEN = 2
REQUEST_COOLDOWN_SECONDS = 300

DEFAULT_INFO = (
    "✨ <b>Тема, о которой вспоминают редко.</b>\n"
    "Об этике говорят мало. Повышают экспертность, осваивают инструменты, "
    "а вопрос «как себя вести с человеком, который вам доверился» часто "
    "остаётся без ответа.\n\n"
    "❇️ <b>Этика — это не мораль и не набор запретов. "
    "Это то, что защищает и клиента, и вас.</b>\n\n"
    "✅ <b>Разберём на уроке:</b>\n"
    "• где заканчивается ваша ответственность и начинается ответственность клиента;\n"
    "• как говорить о сложном, не пугая человека;\n"
    "• границы: время, деньги, личное общение, соцсети;\n"
    "• когда консультацию нужно остановить или передать другому специалисту;\n"
    "• как не забирать чужие проблемы себе.\n\n"
    "👥 <b>Кому будет полезно?</b> Всем, кто работает с людьми: астрологам, "
    "психологам, коучам, тренерам, консультантам, тарологам, нутрициологам, "
    "HR-специалистам, наставникам, преподавателям — и тем, кто только планирует "
    "консультировать.\n\n"
    "📑 <b>Ведёт урок Вячеслав Болотов</b> — сертифицированный бизнес-тренер, "
    "профессиональный астролог, основатель Школы практической астрологии.\n\n"
    "🎉 <b>Участие БЕСПЛАТНОЕ</b>, предварительная регистрация ОБЯЗАТЕЛЬНА."
)

WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в бота Школы практической астрологии!</b>\n"
    "Хочешь заглянуть в мир звёзд и понять, как планеты влияют на твою жизнь?\n\n"
    "У нас есть:\n"
    "✨ <b>Бесплатный урок</b> — чтобы почувствовать стиль обучения и увидеть реальные разборы.\n"
    "🗣 <b>Консультация с астрологом</b> — разберём твой запрос и дадим персональные рекомендации.\n"
    "📚 <b>Обучение с практикой</b> — от основ до уверенной работы с картами."
)


# ---------- ВАЛИДАЦИЯ ----------
NAME_PATTERN = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s\-]{1,49}$")


def is_valid_name(text: str) -> bool:
    t = (text or "").strip()
    if not (2 <= len(t) <= 50):
        return False
    return bool(NAME_PATTERN.match(t))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==================== STATES ====================
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


# ==================== DATABASE ====================
class Database:
    def __init__(self, path: str = DB_PATH):
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
                    sent_24h INTEGER DEFAULT 0,
                    sent_1h INTEGER DEFAULT 0,
                    sent_10min INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS users (
                    tg_id INTEGER PRIMARY KEY,
                    username TEXT,
                    surname TEXT,
                    name TEXT,
                    phone TEXT,
                    qualification TEXT,
                    registered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER,
                    event_type TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS user_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER,
                    message_id INTEGER,
                    tag TEXT DEFAULT 'main'
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER,
                    username TEXT,
                    kind TEXT,
                    text TEXT,
                    created_at TEXT
                );
            """)
            # Миграции для старых БД
            for alter_sql in (
                "ALTER TABLE user_messages ADD COLUMN tag TEXT DEFAULT 'main'",
                "ALTER TABLE lessons ADD COLUMN sent_1h INTEGER DEFAULT 0",
            ):
                try:
                    await db.execute(alter_sql)
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
            await db.execute(
                "UPDATE lessons SET sent_24h=0, sent_1h=0, sent_10min=0 WHERE id=1"
            )
            await db.commit()

    async def mark_sent(self, field: str):
        if field not in ("sent_24h", "sent_1h", "sent_10min"):
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE lessons SET {field}=1 WHERE id=1")
            await db.commit()

    async def save_user(self, tg_id, username, surname, name, phone, qualification):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO users (tg_id, username, surname, name, phone, qualification, registered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    username=excluded.username,
                    surname=excluded.surname,
                    name=excluded.name,
                    phone=excluded.phone,
                    qualification=excluded.qualification,
                    registered_at=excluded.registered_at""",
                (
                    tg_id, username, surname, name, phone, qualification,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            await db.commit()

    async def get_all_users(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users ORDER BY registered_at DESC") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def log_event(self, tg_id: int, event_type: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO events (tg_id, event_type, created_at) VALUES (?, ?, ?)",
                (tg_id, event_type, datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    # ---------- REQUESTS ----------
    async def save_request(self, tg_id, username, kind, text) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO requests (tg_id, username, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, username, kind, text, datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()
            return cur.lastrowid

    async def get_last_requests(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_last_request_time(self, tg_id: int) -> datetime | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT created_at FROM requests WHERE tg_id=? AND kind IN ('consult','question') ORDER BY id DESC LIMIT 1",
                (tg_id,),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                try:
                    return datetime.fromisoformat(row[0])
                except Exception:
                    return None

    # ---------- MESSAGE TRACKING ----------
    async def add_message_id(self, tg_id: int, message_id: int, tag: str = TAG_MAIN):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO user_messages (tg_id, message_id, tag) VALUES (?, ?, ?)",
                (tg_id, message_id, tag),
            )
            await db.commit()

    async def get_message_ids_by_tag(self, tg_id: int, tag: str) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT message_id FROM user_messages WHERE tg_id=? AND tag=?",
                (tg_id, tag),
            ) as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows]

    async def clear_by_tag(self, tg_id: int, tag: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM user_messages WHERE tg_id=? AND tag=?", (tg_id, tag)
            )
            await db.commit()


# ==================== KEYBOARDS ====================
def home_reply_kb(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=HOME_BUTTON_TEXT)]]
    if is_admin_user:
        rows.append([KeyboardButton(text=ADMIN_BUTTON_TEXT)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def home_kb_for(user_id: int) -> ReplyKeyboardMarkup:
    return home_reply_kb(is_admin_user=is_admin(user_id))


def main_menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Стать астрологом", callback_data="go_astrologer")
    b.button(text="🔥 Бесплатный урок", callback_data="free_lesson")
    b.button(text="✨ Получить консультацию", callback_data="go_consult")
    b.button(text="❓ Задать вопрос", callback_data="go_ask")
    b.button(text="📝 Отзывы", url="https://t.me/otzyvy_bolotov")
    b.adjust(1)
    return b.as_markup()


def astrologer_kb():
    """Меню целей: что хочет обрести."""
    b = InlineKeyboardBuilder()
    b.button(text="🪞 Понимание себя", callback_data="astro:understand")
    b.button(text="💼 Новая профессия", callback_data="astro:career")
    b.button(text="💰 Источник дохода", callback_data="astro:income")
    b.button(text="🧰 Дополнительный инструмент", callback_data="astro:tool")
    b.adjust(1)
    return b.as_markup()


def free_lesson_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Регистрируюсь!", callback_data="reg_start")
    b.button(text="❓ Что за урок?", callback_data="lesson_info")
    b.adjust(1)
    return b.as_markup()


def lesson_info_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Регистрируюсь!", callback_data="reg_start")
    b.button(text="⏪ Назад", callback_data="free_lesson")
    b.adjust(1)
    return b.as_markup()


def qualification_kb():
    b = InlineKeyboardBuilder()
    b.button(text="😎 Разбираюсь, профи", callback_data="qual:pro")
    b.button(text="🤔 Интересуюсь", callback_data="qual:interest")
    b.button(text="🤩 Хочу изучать", callback_data="qual:learn")
    b.adjust(1)
    return b.as_markup()


def surname_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⏪ Назад", callback_data="reg_qual_back")
    b.adjust(1)
    return b.as_markup()


def name_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⏪ Назад", callback_data="reg_surname_back")
    b.adjust(1)
    return b.as_markup()


def confirm_kb(prefix: str):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Исправить", callback_data=f"{prefix}_fix")
    b.button(text="✅ Далее", callback_data=f"{prefix}_confirm")
    b.adjust(2)
    return b.as_markup()


def admin_kb():
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
    b.button(text="📤 Выгрузить CSV", callback_data="admin:export")
    b.adjust(1)
    return b.as_markup()


def admin_cancel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⏪ Отмена", callback_data="admin:cancel")
    b.adjust(1)
    return b.as_markup()


def admin_request_notification_kb(user_id: int):
    rows = [
        [InlineKeyboardButton(text="💬 Ответить пользователю", url=f"tg://user?id={user_id}")],
        [InlineKeyboardButton(text="📥 Все обращения", callback_data="admin:requests")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_broadcast_confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить всем", callback_data="admin:broadcast_confirm")
    b.button(text="⏪ Отмена", callback_data="admin:cancel")
    b.adjust(1)
    return b.as_markup()


# ==================== HANDLERS ====================
router = Router()


async def cleanup_registration(bot: Bot, db: Database, chat_id: int):
    ids = await db.get_message_ids_by_tag(chat_id, TAG_REG)
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    await db.clear_by_tag(chat_id, TAG_REG)


async def track(bot: Bot, db: Database, chat_id: int, message, tag: str):
    await db.add_message_id(chat_id, message.message_id, tag)


# ---------- УВЕДОМЛЕНИЯ АДМИНАМ ----------
async def notify_admins(bot: Bot, text: str, disable_notification: bool = False,
                        reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                text,
                disable_notification=disable_notification,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление админу {admin_id}: {e}")


def user_signature(user) -> str:
    full_name = (user.full_name or "").strip() or "Без имени"
    username = f"@{user.username}" if user.username else "—"
    return (
        f"👤 <b>{full_name}</b>\n"
        f"🔗 {username}\n"
        f"🆔 <code>{user.id}</code>\n"
        f'💬 <a href="tg://user?id={user.id}">Открыть профиль</a>'
    )


def now_str() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M:%S МСК")


async def notify_button_click(bot: Bot, user, button_name: str, event_type: str):
    if user.id in ADMIN_IDS:
        return
    await notify_admins(
        bot,
        f"🔔 <b>Нажатие кнопки</b>\n\n"
        f"Кнопка: <b>{button_name}</b>\n"
        f"Время: {now_str()}\n\n"
        f"{user_signature(user)}"
    )


# ---------- УНИВЕРСАЛЬНЫЕ ОТПРАВЛЯЛКИ ----------
async def send_screen(bot: Bot, db: Database, chat_id: int, text: str,
                      inline_kb=None, reply_kb=None, tag: str = TAG_MAIN):
    if tag == TAG_REG:
        await cleanup_registration(bot, db, chat_id)

    if reply_kb is not None:
        main = await bot.send_message(chat_id, text, reply_markup=reply_kb)
        await track(bot, db, chat_id, main, tag)
        if inline_kb is not None:
            extra = await bot.send_message(
                chat_id, "👇 Выберите действие:", reply_markup=inline_kb
            )
            await track(bot, db, chat_id, extra, tag)
    else:
        main = await bot.send_message(chat_id, text, reply_markup=inline_kb)
        await track(bot, db, chat_id, main, tag)


async def send_screen_with_input(bot: Bot, db: Database, chat_id: int, text: str,
                                 inline_kb, placeholder: str, reply_kb=None,
                                 tag: str = TAG_REG):
    if tag == TAG_REG:
        await cleanup_registration(bot, db, chat_id)

    if reply_kb is not None:
        main = await bot.send_message(chat_id, text, reply_markup=reply_kb)
        await track(bot, db, chat_id, main, tag)
        if inline_kb is not None:
            extra = await bot.send_message(
                chat_id, "👇 Выберите действие:", reply_markup=inline_kb
            )
            await track(bot, db, chat_id, extra, tag)
    else:
        main = await bot.send_message(chat_id, text, reply_markup=inline_kb)
        await track(bot, db, chat_id, main, tag)

    prompt = await bot.send_message(
        chat_id,
        f"✏️ {placeholder}",
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
    )
    await track(bot, db, chat_id, prompt, tag)


async def show_welcome(bot: Bot, db: Database, chat_id: int):
    await cleanup_registration(bot, db, chat_id)

    welcome = await bot.send_message(
        chat_id, WELCOME_TEXT, reply_markup=home_kb_for(chat_id)
    )
    await track(bot, db, chat_id, welcome, TAG_MAIN)
    menu = await bot.send_message(
        chat_id, "👇 Выберите действие:", reply_markup=main_menu_kb()
    )
    await track(bot, db, chat_id, menu, TAG_MAIN)


# ---------- START ----------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    await db.log_event(message.from_user.id, "start")
    await show_welcome(message.bot, db, message.chat.id)


# ---------- REPLY-КНОПКА «🏠 ГЛАВНОЕ МЕНЮ» ----------
@router.message(F.text == HOME_BUTTON_TEXT)
async def home_button_handler(message: Message, state: FSMContext, db: Database):
    await state.clear()
    await show_welcome(message.bot, db, message.chat.id)


# ---------- REPLY-КНОПКА «🛠 АДМИН-ПАНЕЛЬ» ----------
@router.message(F.text == ADMIN_BUTTON_TEXT)
async def admin_button_handler(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🛠 <b>Админ-панель</b>\nВыберите действие:",
        reply_markup=admin_kb(),
    )


# ---------- «СТАТЬ АСТРОЛОГОМ» — меню целей ----------
@router.callback_query(F.data == "go_astrologer")
async def cb_astrologer(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.clear()
    await db.log_event(cb.from_user.id, "astrologer_open")
    await notify_button_click(cb.bot, cb.from_user, "🚀 Стать астрологом", "go_astrologer")

    text = "🚀 <b>Что вы хотите обрести благодаря астрологии?</b>"
    await send_screen(
        cb.bot, db, cb.from_user.id, text,
        astrologer_kb(), home_kb_for(cb.from_user.id), tag=TAG_MAIN,
    )


@router.callback_query(F.data.startswith("astro:"))
async def cb_astrologer_choice(cb: CallbackQuery, state: FSMContext, db: Database):
    choice_map = {
        "understand": "Понимание себя",
        "career": "Новая профессия",
        "income": "Источник дохода",
        "tool": "Дополнительный инструмент",
    }
    key = cb.data.split(":", 1)[1]
    choice = choice_map.get(key, "—")
    await cb.answer()
    await state.clear()
    await db.log_event(cb.from_user.id, f"astro_choice:{key}")

    # Сохраняем в requests как отдельный вид
    request_id = await db.save_request(
        tg_id=cb.from_user.id,
        username=cb.from_user.username,
        kind="astrologer",
        text=choice,
    )

    # Уведомление админам
    if cb.from_user.id not in ADMIN_IDS:
        await notify_admins(
            cb.bot,
            f"🚀 <b>Заявка «Стать астрологом» #{request_id}</b>\n"
            f"Время: {now_str()}\n\n"
            f"🎯 <b>Цель:</b> {choice}\n\n"
            f"{user_signature(cb.from_user)}",
            reply_markup=admin_request_notification_kb(cb.from_user.id),
        )

    thanks = (
        "🙏 <b>Спасибо! Ваше сообщение отправлено.</b>\n\n"
        "Вячеслав прочитает его лично и напишет вам в ближайшее время."
    )
    await send_screen(
        cb.bot, db, cb.from_user.id, thanks,
        None, home_kb_for(cb.from_user.id), tag=TAG_MAIN,
    )


# ---------- «ПОЛУЧИТЬ КОНСУЛЬТАЦИЮ» — форма ----------
@router.callback_query(F.data == "go_consult")
async def cb_consult(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.clear()
    await db.log_event(cb.from_user.id, "consult_open")
    await notify_button_click(cb.bot, cb.from_user, "✨ Получить консультацию", "go_consult")

    await state.set_state(RequestFlow.waiting_text)
    await state.update_data(kind="consult")

    text = (
        "✨ <b>Запрос на консультацию</b>\n\n"
        "Опишите одним сообщением, что вы хотите разобрать: какой вопрос, "
        "ситуация или тема. Вячеслав лично прочитает и свяжется с вами "
        "в ближайшее время.\n\n"
        f"<i>Максимум {MAX_REQUEST_LEN} символов.</i>"
    )
    await send_screen(
        cb.bot, db, cb.from_user.id, text,
        None, home_kb_for(cb.from_user.id), tag=TAG_MAIN,
    )

    prompt = await cb.bot.send_message(
        cb.from_user.id,
        "✏️ Напишите ваш запрос:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш запрос…"),
    )
    await db.add_message_id(cb.from_user.id, prompt.message_id, TAG_MAIN)


# ---------- «ЗАДАТЬ ВОПРОС» — форма ----------
@router.callback_query(F.data == "go_ask")
async def cb_ask(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.clear()
    await db.log_event(cb.from_user.id, "ask_open")
    await notify_button_click(cb.bot, cb.from_user, "❓ Задать вопрос", "go_ask")

    await state.set_state(RequestFlow.waiting_text)
    await state.update_data(kind="question")

    text = (
        "❓ <b>Ваш вопрос</b>\n\n"
        "Напишите вопрос одним сообщением. Вячеслав лично прочитает и "
        "ответит вам в ближайшее время.\n\n"
        f"<i>Максимум {MAX_REQUEST_LEN} символов.</i>"
    )
    await send_screen(
        cb.bot, db, cb.from_user.id, text,
        None, home_kb_for(cb.from_user.id), tag=TAG_MAIN,
    )

    prompt = await cb.bot.send_message(
        cb.from_user.id,
        "✏️ Напишите ваш вопрос:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш вопрос…"),
    )
    await db.add_message_id(cb.from_user.id, prompt.message_id, TAG_MAIN)


# ---------- ПРИЁМ ТЕКСТА КОНСУЛЬТАЦИИ / ВОПРОСА ----------
@router.message(RequestFlow.waiting_text, F.text)
async def receive_request_text(message: Message, state: FSMContext, db: Database):
    await db.add_message_id(message.chat.id, message.message_id, TAG_MAIN)

    data = await state.get_data()
    kind = data.get("kind", "question")
    text = message.text.strip()

    if len(text) < MIN_REQUEST_LEN:
        sent = await message.answer(
            "❌ Слишком коротко. Напишите, пожалуйста, чуть подробнее:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш текст…"),
        )
        await db.add_message_id(message.chat.id, sent.message_id, TAG_MAIN)
        return

    if len(text) > MAX_REQUEST_LEN:
        sent = await message.answer(
            f"❌ Слишком длинное сообщение ({len(text)} символов). "
            f"Сократите, пожалуйста, до {MAX_REQUEST_LEN} символов:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш текст…"),
        )
        await db.add_message_id(message.chat.id, sent.message_id, TAG_MAIN)
        return

    last_time = await db.get_last_request_time(message.from_user.id)
    if last_time is not None:
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < REQUEST_COOLDOWN_SECONDS:
            wait_sec = int(REQUEST_COOLDOWN_SECONDS - elapsed)
            wait_min = max(1, (wait_sec + 59) // 60)
            sent = await message.answer(
                f"⏳ Вы недавно уже отправляли обращение. "
                f"Подождите ещё ~{wait_min} мин. Вячеслав обязательно ответит.",
            )
            await db.add_message_id(message.chat.id, sent.message_id, TAG_MAIN)
            await state.clear()
            await send_screen(
                message.bot, db, message.chat.id,
                "👇 Выберите действие:",
                main_menu_kb(),
                home_kb_for(message.from_user.id),
                tag=TAG_MAIN,
            )
            return

    request_id = await db.save_request(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        kind=kind,
        text=text,
    )

    kind_label = "✨ Заявка на консультацию" if kind == "consult" else "❓ Вопрос от пользователя"
    if message.from_user.id not in ADMIN_IDS:
        await notify_admins(
            message.bot,
            f"{kind_label} <b>#{request_id}</b>\n"
            f"Время: {now_str()}\n\n"
            f"💬 <b>Текст:</b>\n{text}\n\n"
            f"{user_signature(message.from_user)}",
            reply_markup=admin_request_notification_kb(message.from_user.id),
        )

    await state.clear()

    reply_text = (
        "🙏 <b>Спасибо!</b> Ваше сообщение отправлено.\n\n"
        "Вячеслав прочитает его лично и напишет вам в ближайшее время."
    )
    await send_screen(
        message.bot, db, message.chat.id, reply_text,
        None, home_kb_for(message.from_user.id), tag=TAG_MAIN,
    )


@router.message(RequestFlow.waiting_text)
async def receive_request_nontext(message: Message, state: FSMContext, db: Database):
    await db.add_message_id(message.chat.id, message.message_id, TAG_MAIN)
    sent = await message.answer(
        "🙏 Пожалуйста, напишите текстом — так Вячеславу будет удобнее вам ответить:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Ваш текст…"),
    )
    await db.add_message_id(message.chat.id, sent.message_id, TAG_MAIN)


# ---------- БЕСПЛАТНЫЙ УРОК ----------
@router.callback_query(F.data == "free_lesson")
async def cb_free_lesson(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.clear()
    await db.log_event(cb.from_user.id, "free_lesson_click")
    lesson = await db.get_lesson()
    text = (
        f"Я помогу зарегистрироваться на бесплатный онлайн урок "
        f"«{lesson['title']}», который пройдёт {lesson['lesson_date']} "
        f"в {lesson['lesson_time']} по московскому времени."
    )
    await send_screen(cb.bot, db, cb.from_user.id, text,
                      free_lesson_kb(), home_kb_for(cb.from_user.id), tag=TAG_REG)


@router.callback_query(F.data == "lesson_info")
async def cb_lesson_info(cb: CallbackQuery, db: Database):
    await cb.answer()
    lesson = await db.get_lesson()
    text = (
        f"💎 <b>Бесплатный онлайн урок {lesson['lesson_date']} "
        f"в {lesson['lesson_time']} по московскому времени "
        f"«{lesson['title']}»</b>\n\n"
        f"{lesson['info_text']}"
    )
    await send_screen(cb.bot, db, cb.from_user.id, text,
                      lesson_info_kb(), home_kb_for(cb.from_user.id), tag=TAG_MAIN)


# ---------- РЕГИСТРАЦИЯ ----------
@router.callback_query(F.data == "reg_start")
async def reg_start(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.set_state(Reg.qualification)
    await state.update_data(qualification=None, surname=None, name=None)
    await send_screen(
        cb.bot, db, cb.from_user.id,
        "Расскажите о вашей квалификации в астрологии",
        qualification_kb(),
        home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


@router.callback_query(F.data.startswith("qual:"))
async def reg_qual(cb: CallbackQuery, state: FSMContext, db: Database):
    qual_map = {"pro": "Разбираюсь, профи", "interest": "Интересуюсь", "learn": "Хочу изучать"}
    qual = qual_map.get(cb.data.split(":", 1)[1], "")
    await cb.answer()
    await state.update_data(qualification=qual)
    await state.set_state(Reg.surname)
    await send_screen_with_input(
        cb.bot, db, cb.from_user.id,
        f"Квалификация: <b>{qual}</b>\n\nВведите Вашу фамилию",
        surname_kb(),
        "Ваша фамилия",
        reply_kb=home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


async def show_confirmation(bot: Bot, db: Database, chat_id: int, user_id: int,
                            label: str, value: str, prefix: str):
    text = f"📝 <b>Проверьте, всё верно?</b>\n\n<b>{label}:</b> {value}"
    await send_screen(bot, db, chat_id, text, confirm_kb(prefix),
                      home_kb_for(user_id), tag=TAG_REG)


# ---------- ФАМИЛИЯ ----------
@router.message(Reg.surname, F.text)
async def reg_surname_input(message: Message, state: FSMContext, db: Database):
    await db.add_message_id(message.chat.id, message.message_id, TAG_REG)
    text = message.text.strip()
    if not is_valid_name(text):
        await send_screen_with_input(
            message.bot, db, message.chat.id,
            "❌ <b>Фамилия должна содержать только буквы</b>, пробел или дефис "
            "(от 2 до 50 символов, без цифр). Попробуйте ещё раз.",
            surname_kb(), "Ваша фамилия",
            reply_kb=home_kb_for(message.from_user.id),
            tag=TAG_REG,
        )
        return
    await state.update_data(surname=text)
    await show_confirmation(message.bot, db, message.chat.id, message.from_user.id,
                            "Фамилия", text, "surname")


@router.callback_query(F.data == "surname_fix")
async def surname_fix(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    data = await state.get_data()
    await state.set_state(Reg.surname)
    await state.update_data(surname=None)
    await send_screen_with_input(
        cb.bot, db, cb.from_user.id,
        f"Квалификация: <b>{data.get('qualification') or '—'}</b>\n\n"
        f"Введите Вашу фамилию заново",
        surname_kb(), "Ваша фамилия",
        reply_kb=home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


@router.callback_query(F.data == "surname_confirm")
async def surname_confirm(cb: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    if not data.get("surname"):
        await cb.answer("Сначала введите фамилию", show_alert=True)
        return
    await cb.answer()
    await state.set_state(Reg.name)
    await send_screen_with_input(
        cb.bot, db, cb.from_user.id,
        f"Фамилия: <b>{data['surname']}</b>\n\nВведите ваше имя",
        name_kb(), "Ваше имя",
        reply_kb=home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


# ---------- ИМЯ → сразу финал ----------
@router.message(Reg.name, F.text)
async def reg_name_input(message: Message, state: FSMContext, db: Database):
    await db.add_message_id(message.chat.id, message.message_id, TAG_REG)
    text = message.text.strip()
    if not is_valid_name(text):
        await send_screen_with_input(
            message.bot, db, message.chat.id,
            "❌ <b>Имя должно содержать только буквы</b>, пробел или дефис "
            "(от 2 до 50 символов, без цифр). Попробуйте ещё раз.",
            name_kb(), "Ваше имя",
            reply_kb=home_kb_for(message.from_user.id),
            tag=TAG_REG,
        )
        return
    await state.update_data(name=text)
    # Сразу финал, без подтверждения и без телефона
    await finish_registration(message.bot, db, state, message.chat.id, message.from_user)


@router.callback_query(F.data == "name_fix")
async def name_fix(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    data = await state.get_data()
    await state.set_state(Reg.name)
    await state.update_data(name=None)
    await send_screen_with_input(
        cb.bot, db, cb.from_user.id,
        f"Фамилия: <b>{data.get('surname') or '—'}</b>\n\nВведите ваше имя заново",
        name_kb(), "Ваше имя",
        reply_kb=home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


# ---------- ЗАВЕРШЕНИЕ РЕГИСТРАЦИИ ----------
async def finish_registration(bot: Bot, db: Database, state: FSMContext, chat_id: int, user):
    data = await state.get_data()

    surname = data.get("surname") or "—"
    name = data.get("name") or "—"
    qualification = data.get("qualification") or "—"

    await db.save_user(
        tg_id=user.id,
        username=user.username,
        surname=surname,
        name=name,
        phone="",  # телефон больше не собираем
        qualification=qualification,
    )
    await state.clear()
    await cleanup_registration(bot, db, chat_id)

    if user.id not in ADMIN_IDS:
        await notify_admins(
            bot,
            f"🎉 <b>Новая регистрация на урок</b>\n\n"
            f"<b>Фамилия:</b> {surname}\n"
            f"<b>Имя:</b> {name}\n"
            f"<b>Квалификация:</b> {qualification}\n"
            f"Время: {now_str()}\n\n"
            f"{user_signature(user)}"
        )

    lesson = await db.get_lesson()
    text = (
        "🙏 <b>Спасибо, вы зарегистрировались!</b>\n\n"
        f"Вы записаны на бесплатный онлайн урок «{lesson['title']}», "
        f"который пройдёт {lesson['lesson_date']} в {lesson['lesson_time']} "
        f"по московскому времени. За 10 минут до начала я пришлю Вам ссылку "
        f"на онлайн-трансляцию.\n\n"
        f"<b>📋 Ваши данные:</b>\n"
        f"• <b>Фамилия:</b> {surname}\n"
        f"• <b>Имя:</b> {name}\n"
        f"• <b>Квалификация:</b> {qualification}"
    )
    await send_screen(bot, db, chat_id, text, None, home_kb_for(user.id), tag=TAG_MAIN)


# ---------- НАЗАД ----------
@router.callback_query(F.data == "reg_qual_back")
async def back_to_qualification(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.set_state(Reg.qualification)
    await state.update_data(surname=None, name=None)
    await send_screen(
        cb.bot, db, cb.from_user.id,
        "Расскажите о вашей квалификации в астрологии",
        qualification_kb(),
        home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


@router.callback_query(F.data == "reg_surname_back")
async def back_to_surname(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    data = await state.get_data()
    await state.set_state(Reg.surname)
    await state.update_data(name=None)
    await send_screen_with_input(
        cb.bot, db, cb.from_user.id,
        f"Квалификация: <b>{data.get('qualification') or '—'}</b>\n\n"
        f"Введите Вашу фамилию",
        surname_kb(), "Ваша фамилия",
        reply_kb=home_kb_for(cb.from_user.id),
        tag=TAG_REG,
    )


# ---------- АДМИН ----------
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database):
    if not is_admin(message.from_user.id):
        return
    users = await db.get_all_users()
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT event_type, COUNT(*) as cnt FROM events GROUP BY event_type ORDER BY cnt DESC"
        ) as cur:
            rows = await cur.fetchall()
            events = [dict(r) for r in rows]
    lines = [f"📊 <b>Статистика</b>\n",
             f"👥 Зарегистрировано: <b>{len(users)}</b>\n",
             "🔔 <b>Нажатия кнопок:</b>"]
    for e in events:
        lines.append(f"• {e['event_type']}: <b>{e['cnt']}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("requests"))
async def cmd_requests(message: Message, db: Database):
    if not is_admin(message.from_user.id):
        return
    await show_requests(message, db)


async def show_requests(message: Message, db: Database):
    reqs = await db.get_last_requests(limit=20)
    if not reqs:
        await message.answer("Пока нет обращений.")
        return
    lines = [f"📥 <b>Последние обращения</b> ({len(reqs)})\n"]
    for r in reqs:
        kind_map = {
            "consult": "✨ Консультация",
            "question": "❓ Вопрос",
            "astrologer": "🚀 Стать астрологом",
        }
        kind = kind_map.get(r["kind"], r["kind"])
        text = (r["text"] or "").strip()
        if len(text) > 200:
            text = text[:200] + "…"
        lines.append(
            f"<b>#{r['id']} {kind}</b> от @{r.get('username') or r['tg_id']}\n"
            f"{r['created_at']}\n"
            f"{text}\n"
            f"—"
        )
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("admin:"))
async def admin_action(cb: CallbackQuery, state: FSMContext, db: Database):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    action = cb.data.split(":", 1)[1]

    if action == "title":
        await state.set_state(AdminStates.edit_title)
        await cb.message.answer("Введите новое название урока:", reply_markup=admin_cancel_kb())
    elif action == "date":
        await state.set_state(AdminStates.edit_date)
        await cb.message.answer("Введите новую дату ДД.ММ.ГГГГ:", reply_markup=admin_cancel_kb())
    elif action == "time":
        await state.set_state(AdminStates.edit_time)
        await cb.message.answer("Введите новое время ЧЧ:ММ:", reply_markup=admin_cancel_kb())
    elif action == "info":
        await state.set_state(AdminStates.edit_info)
        await cb.message.answer("Введите новый текст «Что за урок?»:", reply_markup=admin_cancel_kb())
    elif action == "link":
        await state.set_state(AdminStates.edit_link)
        await cb.message.answer("Введите ссылку на трансляцию:", reply_markup=admin_cancel_kb())
    elif action == "cancel":
        await state.clear()
        await cb.message.answer("Отменено.", reply_markup=admin_kb())
    elif action == "export":
        await export_csv(cb.message, db)
    elif action == "list":
        await list_users(cb.message, db)
    elif action == "requests":
        await show_requests(cb.message, db)
    elif action == "send_link":
        await send_link_now(cb, db)
    elif action == "broadcast":
        await state.set_state(AdminStates.broadcast)
        await cb.message.answer(
            "📢 <b>Рассылка всем пользователям</b>\n\n"
            "Отправьте текст сообщения. Поддерживается HTML "
            "(<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
            "<code>&lt;a href='...'&gt;</code>).\n\n"
            "После отправки я покажу превью и попрошу подтвердить.",
            reply_markup=admin_cancel_kb(),
        )
    elif action == "broadcast_confirm":
        data = await state.get_data()
        text = data.get("broadcast_text")
        if not text:
            await cb.answer("Текст потерян, начните заново", show_alert=True)
            return
        await state.clear()
        sent, failed = await broadcast_all(cb.bot, db, text)
        await cb.message.answer(
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"Отправлено: <b>{sent}</b>\n"
            f"Ошибок: <b>{failed}</b>",
            reply_markup=admin_kb(),
        )
    await cb.answer()


@router.message(AdminStates.edit_title, F.text)
async def admin_set_title(message: Message, state: FSMContext, db: Database):
    await db.update_lesson("title", message.text.strip())
    await state.clear()
    await message.answer("✅ Название обновлено.", reply_markup=admin_kb())


@router.message(AdminStates.edit_date, F.text)
async def admin_set_date(message: Message, state: FSMContext, db: Database):
    value = message.text.strip()
    try:
        datetime.strptime(value, "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте ДД.ММ.ГГГГ.")
        return
    await db.update_lesson("lesson_date", value)
    await state.clear()
    await message.answer("✅ Дата обновлена.", reply_markup=admin_kb())


@router.message(AdminStates.edit_time, F.text)
async def admin_set_time(message: Message, state: FSMContext, db: Database):
    value = message.text.strip()
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте ЧЧ:ММ.")
        return
    await db.update_lesson("lesson_time", value)
    await state.clear()
    await message.answer("✅ Время обновлено.", reply_markup=admin_kb())


@router.message(AdminStates.edit_info, F.text)
async def admin_set_info(message: Message, state: FSMContext, db: Database):
    await db.update_lesson("info_text", message.html_text)
    await state.clear()
    await message.answer("✅ Текст «Что за урок?» обновлён.", reply_markup=admin_kb())


@router.message(AdminStates.edit_link, F.text)
async def admin_set_link(message: Message, state: FSMContext, db: Database):
    await db.update_lesson("broadcast_link", message.text.strip())
    await state.clear()
    await message.answer("✅ Ссылка обновлена.", reply_markup=admin_kb())


# ---------- BROADCAST ----------
@router.message(AdminStates.broadcast, F.text)
async def admin_broadcast_preview(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    text = message.html_text or message.text
    await state.update_data(broadcast_text=text)
    users = await db.get_all_users()
    await message.answer(
        f"📢 <b>Превью рассылки</b>\n"
        f"Получателей: <b>{len(users)}</b>\n\n"
        f"—————\n{text}\n—————",
        reply_markup=admin_broadcast_confirm_kb(),
    )


async def broadcast_all(bot: Bot, db: Database, text: str):
    users = await db.get_all_users()
    sent = 0
    failed = 0
    for u in users:
        try:
            await bot.send_message(u["tg_id"], text, disable_web_page_preview=True)
            sent += 1
            # Небольшая пауза, чтобы не улететь в лимиты Telegram
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed


# ---------- АДМИН-УТИЛИТЫ ----------
async def list_users(message: Message, db: Database):
    users = await db.get_all_users()
    if not users:
        await message.answer("Пока нет зарегистрированных.")
        return
    lines = [f"👥 Всего зарегистрировано: <b>{len(users)}</b>\n"]
    for u in users[:50]:
        name = f"{u.get('surname') or ''} {u.get('name') or ''}".strip() or "—"
        lines.append(
            f"• {name} | {u.get('qualification') or '—'} | "
            f"@{u.get('username') or '—'}"
        )
    if len(users) > 50:
        lines.append(f"\n… и ещё {len(users) - 50}")
    await message.answer("\n".join(lines))


async def export_csv(message: Message, db: Database):
    users = await db.get_all_users()
    if not users:
        await message.answer("Нет данных для выгрузки.")
        return
    header = "tg_id,username,surname,name,qualification,registered_at\n"
    rows = ""
    for u in users:
        rows += (
            f"{u.get('tg_id','')},{u.get('username','') or ''},"
            f"{u.get('surname','') or ''},{u.get('name','') or ''},"
            f"{u.get('qualification','') or ''},"
            f"{u.get('registered_at','') or ''}\n"
        )
    data = (header + rows).encode("utf-8-sig")
    file = BufferedInputFile(data, filename="registrations.csv")
    await message.answer_document(file, caption=f"📤 Выгрузка: {len(users)} чел.")


async def send_link_now(cb: CallbackQuery, db: Database):
    lesson = await db.get_lesson()
    link = lesson.get("broadcast_link") or ""
    if not link:
        await cb.message.answer("⚠️ Сначала задайте ссылку на трансляцию.")
        return
    users = await db.get_all_users()
    sent = 0
    for u in users:
        try:
            await cb.bot.send_message(
                u["tg_id"],
                f"🚀 Ссылка на онлайн-трансляцию урока «{lesson['title']}»:\n{link}"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await cb.message.answer(f"📨 Ссылка отправлена {sent} из {len(users)}.")


# ==================== SCHEDULER ====================
async def check_lessons(bot: Bot, db: Database):
    lesson = await db.get_lesson()
    if not lesson:
        return

    date_str = lesson.get("lesson_date") or ""
    time_str = lesson.get("lesson_time") or ""
    if not date_str or not time_str or date_str == "00.00.2026":
        return

    try:
        lesson_dt = datetime.strptime(
            f"{date_str} {time_str}", "%d.%m.%Y %H:%M"
        ).replace(tzinfo=ZoneInfo(TIMEZONE))
    except ValueError:
        return

    now = datetime.now(ZoneInfo(TIMEZONE))
    users = await db.get_all_users()

    # За 24 часа
    if not lesson.get("sent_24h") and lesson_dt - timedelta(hours=24) <= now < lesson_dt:
        text = (
            f"🔔 Напоминаю: завтра бесплатный онлайн урок "
            f"«{lesson['title']}» в {lesson['lesson_time']} по московскому времени."
        )
        for u in users:
            try:
                await bot.send_message(u["tg_id"], text)
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await db.mark_sent("sent_24h")

    # За 1 час
    if not lesson.get("sent_1h") and lesson_dt - timedelta(hours=1) <= now < lesson_dt:
        text = (
            f"⏰ Напоминаю: через час начнётся бесплатный онлайн урок "
            f"«{lesson['title']}» в {lesson['lesson_time']} по московскому времени."
        )
        for u in users:
            try:
                await bot.send_message(u["tg_id"], text)
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await db.mark_sent("sent_1h")

    # За 10 минут
    if (
        not lesson.get("sent_10min")
        and lesson_dt - timedelta(minutes=10) <= now < lesson_dt + timedelta(minutes=30)
    ):
        link = lesson.get("broadcast_link") or ""
        text = (
            "🚀 Урок начинается через 10 минут!\n"
            + (f"Ссылка на трансляцию: {link}" if link else "Ссылка появится в следующем сообщении.")
        )
        for u in users:
            try:
                await bot.send_message(u["tg_id"], text)
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await db.mark_sent("sent_10min")


# ==================== MAIN ====================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    db = Database()
    await db.init()
    dp["db"] = db

    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_lessons, "interval", minutes=1, args=[bot, db])
    scheduler.start()

    logging.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
