import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    CallbackQuery,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ParseMode

# ==================== НАСТРОЙКИ ====================
# Токен берётся из переменной окружения BOT_TOKEN (задаётся в Railway → Variables).
# Для локального запуска можно временно вписать токен вторым аргументом os.getenv ниже.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8660567352:AAGtgPE9AvYeJ9UZJIpTIm7ngR_Q2O-hX8s")

MIN_GROWTH = -6
MAX_GROWTH = 10
COOLDOWN_HOURS = 24

DUEL_WIN_MIN = 1        # минимальный выигрыш победителя дуэли (см)
DUEL_WIN_MAX = 3        # максимальный выигрыш победителя дуэли (см)
DUEL_LOSE_MIN = 1       # минимальная потеря проигравшего дуэль (см)
DUEL_LOSE_MAX = 3       # максимальная потеря проигравшего дуэль (см)
DUEL_COOLDOWN_MIN = 5   # антиспам для дуэлей, минут между вызовами

DISEASE_CHANCE = 0.15   # 15% шанс подхватить болезнь при теребонькании
DISEASE_MIN_DAYS = 1
DISEASE_MAX_DAYS = 3
CURE_PRICE_STARS = 1    # стоимость полного лечения в Telegram Stars

BLACKJACK_MIN_BET = 1   # минимальная ставка в блекджеке (см)

ADMIN_USERNAME = "I9451"  # только этот пользователь имеет доступ к админ-командам

DB_PATH = "game.db"

# ==================== БОЛЕЗНИ ====================
# effect: "half"     — рост от потеребонькивания делится пополам
#         "flat_neg" — к каждому результату теребонькивания добавляется штраф (см)
#         "cap_low"  — рост ограничен сверху небольшим значением
#         "invert"   — положительный результат превращается в отрицательный
DISEASES = {
    "Простудный писюн": {
        "emoji": "🤧",
        "effect": "half",
        "description": "рост от теребонькивания уменьшается вдвое",
    },
    "Воспаление достоинства": {
        "emoji": "🔥",
        "effect": "flat_neg",
        "value": 2,
        "description": "каждое теребонькивание — дополнительно -2 см",
    },
    "Хроническая вялость": {
        "emoji": "😮‍💨",
        "effect": "cap_low",
        "value": 2,
        "description": "рост не может превышать +2 см за раз",
    },
    "Кривизна ствола": {
        "emoji": "🌀",
        "effect": "invert",
        "description": "любой прирост превращается в убыток той же величины",
    },
    "Бородавочная лихорадка": {
        "emoji": "🐸",
        "effect": "flat_neg",
        "value": 4,
        "description": "каждое теребонькивание — дополнительно -4 см",
    },
}

# ==================== БЛЕКДЖЕК: КАРТЫ ====================
CARD_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]


class BlackjackStates(StatesGroup):
    waiting_bet = State()
    playing = State()


def new_deck() -> list:
    deck = [f"{rank}{suit}" for rank in CARD_RANKS for suit in CARD_SUITS]
    random.shuffle(deck)
    return deck


def card_value(card: str) -> int:
    rank = card[:-1]  # последний символ — масть, всё остальное — номинал
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_total(hand: list) -> int:
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_hand(hand: list) -> str:
    return " ".join(hand)


logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# user_id аккаунта, которым Telegram подписывает анонимные сообщения от админов групп
GROUP_ANONYMOUS_BOT_ID = 1087968824
# user_id аккаунта, которым подписываются анонимные сообщения от лица канала
CHANNEL_ANONYMOUS_BOT_ID = 136817688


@dp.message.middleware()
async def block_anonymous_and_bots_middleware(handler, event: Message, data: dict):
    """Не пускает дальше сообщения, отправленные анонимно от лица группы/канала
    или от других ботов, и регистрирует остальных пользователей в базе."""
    user = event.from_user

    if event.sender_chat is not None:
        await event.answer(
            "🚫 Команды от анонимных админов группы не поддерживаются.\n"
            "Отключи анонимность в настройках группы или напиши боту в личку."
        )
        return

    if user and user.id in (GROUP_ANONYMOUS_BOT_ID, CHANNEL_ANONYMOUS_BOT_ID):
        await event.answer(
            "🚫 Команды от анонимных админов группы не поддерживаются.\n"
            "Отключи анонимность в настройках группы или напиши боту в личку."
        )
        return

    if user and user.is_bot:
        return

    if user:
        get_or_create_user(user.id, user.username, user.full_name)

    return await handler(event, data)


@dp.callback_query.middleware()
async def block_bots_callback_middleware(handler, event: CallbackQuery, data: dict):
    if event.from_user and event.from_user.is_bot:
        return
    return await handler(event, data)


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            length REAL DEFAULT 10.0,
            last_teasing TEXT,
            last_duel TEXT,
            disease_name TEXT,
            disease_expires TEXT
        )
    """)
    conn.commit()

    cur.execute("PRAGMA table_info(users)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "disease_name" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN disease_name TEXT")
    if "disease_expires" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN disease_expires TEXT")
    conn.commit()
    conn.close()


def get_conn():
    return sqlite3.connect(DB_PATH)


def get_or_create_user(user_id: int, username: str, full_name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (user_id, username, full_name, length) VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, 10.0),
        )
        conn.commit()
        row = (user_id, username, full_name, 10.0, None, None, None, None)
    else:
        cur.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
        conn.commit()
    conn.close()
    return row


def get_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def update_length(user_id: int, new_length: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET length = ? WHERE user_id = ?", (new_length, user_id))
    conn.commit()
    conn.close()


def set_last_teasing(user_id: int, dt: datetime):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_teasing = ? WHERE user_id = ?", (dt.isoformat(), user_id))
    conn.commit()
    conn.close()


def set_last_duel(user_id: int, dt: datetime):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_duel = ? WHERE user_id = ?", (dt.isoformat(), user_id))
    conn.commit()
    conn.close()


def set_disease(user_id: int, disease_name: str, expires: datetime):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET disease_name = ?, disease_expires = ? WHERE user_id = ?",
        (disease_name, expires.isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def clear_disease(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET disease_name = NULL, disease_expires = NULL WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_active_disease(user_id: int):
    """Возвращает (название, инфо из DISEASES) если болезнь ещё активна, иначе None.
    Автоматически лечит пользователя, если срок болезни истёк."""
    user = get_user(user_id)
    disease_name = user[6]
    disease_expires = user[7]

    if not disease_name or not disease_expires:
        return None

    expires_dt = datetime.fromisoformat(disease_expires)
    if datetime.now() >= expires_dt:
        clear_disease(user_id)
        return None

    return disease_name, DISEASES.get(disease_name)


def apply_disease_effect(disease_info: dict, change: int) -> int:
    effect = disease_info["effect"]

    if effect == "half":
        change = change // 2 if change >= 0 else -(-change // 2)
    elif effect == "flat_neg":
        change -= disease_info["value"]
    elif effect == "cap_low":
        change = min(change, disease_info["value"])
    elif effect == "invert":
        if change > 0:
            change = -change

    return change


def maybe_catch_disease(user_id: int):
    if random.random() >= DISEASE_CHANCE:
        return None

    disease_name = random.choice(list(DISEASES.keys()))
    days = random.randint(DISEASE_MIN_DAYS, DISEASE_MAX_DAYS)
    expires = datetime.now() + timedelta(days=days)
    set_disease(user_id, disease_name, expires)
    return disease_name


def get_top_users(limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username, full_name, length FROM users ORDER BY length DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def settle_blackjack(user_id: int, bet: float, result: str):
    """result: 'win' | 'lose' | 'push' | 'blackjack'. Возвращает (новая_длина, дельта)."""
    user = get_user(user_id)
    length = user[3]
    if result == "win":
        delta = bet
    elif result == "lose":
        delta = -bet
    elif result == "blackjack":
        delta = bet * 1.5
    else:
        delta = 0.0
    new_length = max(0.0, length + delta)
    update_length(user_id, new_length)
    return new_length, delta


# ==================== ХЕЛПЕРЫ ====================
def is_admin(message: Message) -> bool:
    return message.from_user.username == ADMIN_USERNAME


def find_user_by_username(username: str):
    username = username.lstrip("@")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,))
    row = cur.fetchone()
    conn.close()
    return row


def get_all_user_ids():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def get_all_users_brief():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, full_name, length FROM users ORDER BY length DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def resolve_target(message: Message, arg: str):
    """Найти целевого пользователя: по ответу на сообщение или по @username в аргументе."""
    if message.reply_to_message:
        u = message.reply_to_message.from_user
        if u is None:
            return None
        get_or_create_user(u.id, u.username, u.full_name)
        return get_user(u.id)
    if arg:
        return find_user_by_username(arg)
    return None


def build_users_list_text() -> str:
    users = get_all_users_brief()
    if not users:
        return "В базе пока нет пользователей."
    lines = [f"👥 <b>Всего пользователей: {len(users)}</b>\n"]
    for user_id, username, full_name, length in users:
        name = format_name(username, full_name)
        lines.append(f"{name} — {length:.1f} см (<code>{user_id}</code>)")
    return "\n".join(lines)


def build_diseases_list_text() -> str:
    lines = ["📋 <b>Доступные болезни:</b>\n"]
    for name, info in DISEASES.items():
        lines.append(f"{info['emoji']} <b>{name}</b> — {info['description']}")
    return "\n".join(lines)


# ==================== КЛАВИАТУРЫ ====================
def main_inline_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="🍆 Потеребонькать", callback_data="act:tease"),
            InlineKeyboardButton(text="📏 Мой размер", callback_data="act:my"),
        ],
        [
            InlineKeyboardButton(text="🏥 Статус", callback_data="act:status"),
            InlineKeyboardButton(text="💊 Лечиться", callback_data="act:cure"),
        ],
        [
            InlineKeyboardButton(text="🏆 Топ игроков", callback_data="act:top"),
            InlineKeyboardButton(text="🃏 Блекджек", callback_data="act:bj"),
        ],
    ]
    if is_admin_user:
        keyboard.append([InlineKeyboardButton(text="⚙️ Админка", callback_data="act:admin")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm_users"),
                InlineKeyboardButton(text="📋 Список болезней", callback_data="adm_diseases"),
            ],
            [
                InlineKeyboardButton(text="ℹ️ Инфо о юзере", callback_data="adm_info_hint"),
                InlineKeyboardButton(text="💊 Вылечить юзера", callback_data="adm_cure_hint"),
            ],
            [
                InlineKeyboardButton(text="📐 Установить длину", callback_data="adm_set_hint"),
                InlineKeyboardButton(text="🦠 Заразить юзера", callback_data="adm_disease_hint"),
            ],
            [
                InlineKeyboardButton(text="♻️ Сбросить юзера", callback_data="adm_reset_hint"),
                InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast_hint"),
            ],
        ]
    )


def blackjack_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🃏 Взять карту", callback_data=f"bj:hit:{owner_id}"),
            InlineKeyboardButton(text="✋ Хватит", callback_data=f"bj:stand:{owner_id}"),
        ]]
    )


def format_name(username: str, full_name: str) -> str:
    if username:
        return f"@{username}"
    return full_name or "Аноним"


def time_left_str(seconds: float) -> str:
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours} ч {minutes} мин"


# ==================== ИГРОВАЯ ЛОГИКА (ОБЩАЯ ДЛЯ КОМАНД И INLINE-КНОПОК) ====================
def build_teasing_text(user_id: int, username: str, full_name: str) -> str:
    user = get_or_create_user(user_id, username, full_name)
    last_teasing = user[4]
    now = datetime.now()

    if last_teasing:
        last_dt = datetime.fromisoformat(last_teasing)
        elapsed = now - last_dt
        cooldown = timedelta(hours=COOLDOWN_HOURS)
        if elapsed < cooldown:
            remaining = (cooldown - elapsed).total_seconds()
            return f"⏳ Ты уже теребонькал сегодня! Приходи через {time_left_str(remaining)}."

    change = random.randint(MIN_GROWTH, MAX_GROWTH)

    active = get_active_disease(user_id)
    disease_note = ""
    if active:
        disease_name, disease_info = active
        original_change = change
        change = apply_disease_effect(disease_info, change)
        disease_note = (
            f"\n{disease_info['emoji']} <i>Болезнь «{disease_name}» повлияла на результат "
            f"({original_change:+d} → {change:+d})</i>"
        )

    current_length = user[3]
    new_length = max(0.0, current_length + change)

    update_length(user_id, new_length)
    set_last_teasing(user_id, now)

    if change > 0:
        emoji, verdict = "📈", f"вырос на {change} см"
    elif change < 0:
        emoji, verdict = "📉", f"уменьшился на {abs(change)} см"
    else:
        emoji, verdict = "➖", "не изменился"

    text = (
        f"{emoji} Ты потеребонькал... результат: <b>{verdict}</b>!\n"
        f"Текущая длина: <b>{new_length:.1f} см</b>"
        f"{disease_note}"
    )

    if not active:
        caught = maybe_catch_disease(user_id)
        if caught:
            info = DISEASES[caught]
            text += (
                f"\n\n{info['emoji']} <b>Ты подхватил болезнь: {caught}!</b>\n"
                f"<i>{info['description']}</i>\n"
                f"Используй /статус, чтобы следить за лечением, или /лечиться, чтобы вылечиться сразу."
            )

    return text


def build_my_size_text(user_id: int, username: str, full_name: str) -> str:
    user = get_or_create_user(user_id, username, full_name)
    return f"Твоя текущая длина: <b>{user[3]:.1f} см</b>"


def build_status_text(user_id: int, username: str, full_name: str) -> str:
    get_or_create_user(user_id, username, full_name)
    active = get_active_disease(user_id)

    if not active:
        return "✅ Ты полностью здоров! Никаких болезней."

    disease_name, disease_info = active
    user = get_user(user_id)
    expires_dt = datetime.fromisoformat(user[7])
    remaining = (expires_dt - datetime.now()).total_seconds()

    return (
        f"{disease_info['emoji']} Ты болен: <b>{disease_name}</b>\n"
        f"<i>{disease_info['description']}</i>\n"
        f"Пройдёт через: {time_left_str(remaining)}\n\n"
        f"Хочешь вылечиться сразу? Используй /лечиться ({CURE_PRICE_STARS} ⭐)"
    )


async def send_cure_invoice_or_message(chat_id: int, user_id: int, username: str, full_name: str):
    """Отправляет инвойс на лечение, если пользователь болен.
    Возвращает текст сообщения, если лечить нечего, иначе None (инвойс уже отправлен)."""
    get_or_create_user(user_id, username, full_name)
    active = get_active_disease(user_id)
    if not active:
        return "✅ Ты и так здоров, лечить нечего!"

    disease_name, disease_info = active
    await bot.send_invoice(
        chat_id=chat_id,
        title="Полное исцеление",
        description=f"Мгновенно вылечивает «{disease_name}» ({disease_info['description']})",
        payload=f"cure_disease:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Лечение", amount=CURE_PRICE_STARS)],
    )
    return None


def build_top_text() -> str:
    top = get_top_users(10)
    if not top:
        return "Пока никто не участвует в игре 😢"

    lines = ["🏆 <b>Таблица лидеров:</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (username, full_name, length) in enumerate(top):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = format_name(username, full_name)
        lines.append(f"{prefix} {name} — {length:.1f} см")

    return "\n".join(lines)


def admin_help_text() -> str:
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        "Жми кнопки ниже или используй команды вручную.\n"
        "Цель команды указывается ответом на сообщение пользователя ИЛИ через @username.\n\n"
        "/admin_set @user 25 — установить длину\n"
        "/admin_add @user 10 — прибавить к длине (можно отрицательное)\n"
        "/admin_cure @user — вылечить от всех болезней\n"
        "/admin_disease @user Название болезни — заразить конкретной болезнью на 1 день\n"
        "/admin_reset @user — сбросить пользователя (длина 10, без болезней, без кулдаунов)\n"
        "/admin_info @user — показать всю инфу о пользователе\n"
        "/admin_users — список всех пользователей в базе\n"
        "/admin_broadcast текст — разослать сообщение всем игрокам\n"
        "/admin_diseases — список всех доступных болезней"
    )


async def blackjack_bet_prompt(user_id: int, username: str, full_name: str):
    user = get_or_create_user(user_id, username, full_name)
    length = user[3]
    if length < BLACKJACK_MIN_BET:
        return (
            f"Недостаточно см для игры — нужно хотя бы {BLACKJACK_MIN_BET} см, "
            f"а у тебя {length:.1f} см.",
            False,
        )
    return (
        f"🃏 <b>Блекджек!</b>\nТвоя текущая длина: {length:.1f} см.\n"
        f"Напиши число — сколько см поставить (от {BLACKJACK_MIN_BET} до {length:.1f}).\n"
        f"<i>В группе лучше ответить (Reply) на это сообщение — так бот точно его увидит.</i>",
        True,
    )


# ==================== ХЕНДЛЕРЫ ====================
@dp.message(F.new_chat_members)
async def on_new_chat_members(message: Message):
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        get_or_create_user(member.id, member.username, member.full_name)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(
        "Добро пожаловать в игру! 🍆\n\n"
        "Пользуйся кнопками под следующим сообщением или командами:\n"
        "/потеребонькать — раз в сутки менять свою длину (есть шанс подхватить болезнь!)\n"
        "/статус — узнать, чем болеешь и когда пройдёт\n"
        "/лечиться — вылечить все болезни за Telegram Stars\n"
        "/топ — таблица лидеров\n"
        "/бой (ответом на сообщение соперника) — дуэль на кубиках\n"
        "/блекджек — сыграть в 21 на свои см\n"
        "/мой — узнать свою текущую длину\n\n"
        "💡 Если в группе бот не отвечает на что-то — сделай Reply на его сообщение.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Выбери действие:",
        reply_markup=main_inline_keyboard(is_admin(message)),
    )


@dp.message(Command("меню"))
async def cmd_menu(message: Message):
    get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer("Выбери действие:", reply_markup=main_inline_keyboard(is_admin(message)))


@dp.message(Command("мой"))
async def cmd_my(message: Message):
    await message.answer(
        build_my_size_text(message.from_user.id, message.from_user.username, message.from_user.full_name)
    )


@dp.callback_query(F.data == "act:my")
async def cb_my(callback: CallbackQuery):
    await callback.message.answer(
        build_my_size_text(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    )
    await callback.answer()


@dp.message(Command("потеребонькать"))
async def cmd_teasing(message: Message):
    await message.answer(
        build_teasing_text(message.from_user.id, message.from_user.username, message.from_user.full_name)
    )


@dp.callback_query(F.data == "act:tease")
async def cb_teasing(callback: CallbackQuery):
    await callback.message.answer(
        build_teasing_text(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    )
    await callback.answer()


@dp.message(Command("статус"))
async def cmd_status(message: Message):
    await message.answer(
        build_status_text(message.from_user.id, message.from_user.username, message.from_user.full_name)
    )


@dp.callback_query(F.data == "act:status")
async def cb_status(callback: CallbackQuery):
    await callback.message.answer(
        build_status_text(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    )
    await callback.answer()


@dp.message(Command("лечиться"))
async def cmd_cure(message: Message):
    result_text = await send_cure_invoice_or_message(
        message.chat.id, message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    if result_text:
        await message.answer(result_text)


@dp.callback_query(F.data == "act:cure")
async def cb_cure(callback: CallbackQuery):
    result_text = await send_cure_invoice_or_message(
        callback.message.chat.id, callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    if result_text:
        await callback.message.answer(result_text)
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("cure_disease:"):
        user_id = int(payload.split(":")[1])
        clear_disease(user_id)
        await message.answer("💊 Оплата прошла успешно! Ты полностью излечен от всех болезней.")


@dp.message(Command("топ"))
async def cmd_top(message: Message):
    await message.answer(build_top_text())


@dp.callback_query(F.data == "act:top")
async def cb_top(callback: CallbackQuery):
    await callback.message.answer(build_top_text())
    await callback.answer()


@dp.message(Command("бой"))
async def cmd_duel(message: Message):
    if not message.reply_to_message:
        await message.answer("Чтобы вызвать на дуэль, ответь командой /бой на сообщение соперника!")
        return

    challenger = message.from_user
    opponent = message.reply_to_message.from_user

    if opponent is None:
        await message.answer("Нельзя вызвать на дуэль анонимного отправителя.")
        return

    if opponent.id == challenger.id:
        await message.answer("Нельзя сражаться самим с собой 🙃")
        return

    if opponent.is_bot:
        await message.answer("Нельзя сражаться с ботом 🤖")
        return

    challenger_row = get_or_create_user(challenger.id, challenger.username, challenger.full_name)
    last_duel = challenger_row[5]
    if last_duel:
        last_dt = datetime.fromisoformat(last_duel)
        elapsed = (datetime.now() - last_dt).total_seconds()
        cooldown_sec = DUEL_COOLDOWN_MIN * 60
        if elapsed < cooldown_sec:
            remaining = cooldown_sec - elapsed
            await message.answer(
                f"⏳ Не части с дуэлями! Подожди ещё {int(remaining // 60)} мин {int(remaining % 60)} сек."
            )
            return

    get_or_create_user(opponent.id, opponent.username, opponent.full_name)

    roll1 = random.randint(1, 6)
    roll2 = random.randint(1, 6)

    name1 = format_name(challenger.username, challenger.full_name)
    name2 = format_name(opponent.username, opponent.full_name)

    text = (
        f"⚔️ Дуэль началась!\n\n"
        f"{name1} кидает кубик... 🎲 <b>{roll1}</b>\n"
        f"{name2} кидает кубик... 🎲 <b>{roll2}</b>\n\n"
    )

    set_last_duel(challenger.id, datetime.now())

    if roll1 == roll2:
        text += "🤝 Ничья! Оба остаются при своём."
    else:
        if roll1 > roll2:
            winner_id, winner_name = challenger.id, name1
            loser_id, loser_name = opponent.id, name2
        else:
            winner_id, winner_name = opponent.id, name2
            loser_id, loser_name = challenger.id, name1

        win_amount = random.randint(DUEL_WIN_MIN, DUEL_WIN_MAX)
        lose_amount = random.randint(DUEL_LOSE_MIN, DUEL_LOSE_MAX)

        winner_row = get_user(winner_id)
        loser_row = get_user(loser_id)

        new_winner_length = winner_row[3] + win_amount
        new_loser_length = max(0.0, loser_row[3] - lose_amount)

        update_length(winner_id, new_winner_length)
        update_length(loser_id, new_loser_length)

        text += (
            f"🎉 Победил {winner_name}! +{win_amount} см (теперь {new_winner_length:.1f} см)\n"
            f"💔 {loser_name} проиграл: -{lose_amount} см (теперь {new_loser_length:.1f} см)"
        )

    await message.answer(text)


# ==================== БЛЕКДЖЕК: ХЕНДЛЕРЫ ====================
@dp.message(Command("блекджек"))
async def cmd_blackjack_start(message: Message, state: FSMContext):
    text, ok = await blackjack_bet_prompt(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    if ok:
        await state.set_state(BlackjackStates.waiting_bet)
    await message.answer(text)


@dp.callback_query(F.data == "act:bj")
async def cb_blackjack_start(callback: CallbackQuery, state: FSMContext):
    text, ok = await blackjack_bet_prompt(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    if ok:
        await state.set_state(BlackjackStates.waiting_bet)
    await callback.message.answer(text)
    await callback.answer()


@dp.message(BlackjackStates.waiting_bet)
async def process_blackjack_bet(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    length = user[3] if user else 0.0

    raw = (message.text or "").replace(",", ".").strip()
    try:
        bet = float(raw)
    except ValueError:
        await message.answer("Нужно прислать просто число. Сколько см ставишь?")
        return

    if bet < BLACKJACK_MIN_BET:
        await message.answer(f"Минимальная ставка — {BLACKJACK_MIN_BET} см.")
        return
    if bet > length:
        await message.answer(f"У тебя только {length:.1f} см, столько поставить нельзя.")
        return

    deck = new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]

    player_total = hand_total(player_hand)
    dealer_total = hand_total(dealer_hand)

    # природный блекджек сразу после раздачи
    if player_total == 21 or dealer_total == 21:
        await state.clear()
        if player_total == 21 and dealer_total == 21:
            result, verdict = "push", "🤝 У обоих блекджек с первых карт — ничья, ставка возвращена."
        elif player_total == 21:
            result, verdict = "blackjack", "🎉 У тебя блекджек с первых карт! Выигрыш x1.5!"
        else:
            result, verdict = "lose", "💀 У дилера блекджек с первых карт. Ставка проиграна."

        new_length, delta = settle_blackjack(message.from_user.id, bet, result)
        await message.answer(
            f"🃏 Твои карты: {format_hand(player_hand)} ({player_total})\n"
            f"🂠 Карты дилера: {format_hand(dealer_hand)} ({dealer_total})\n\n"
            f"{verdict}\n"
            f"Изменение: {delta:+.1f} см → теперь {new_length:.1f} см"
        )
        return

    await state.set_state(BlackjackStates.playing)
    await state.update_data(bet=bet, deck=deck, player_hand=player_hand, dealer_hand=dealer_hand)

    await message.answer(
        f"🃏 Твои карты: {format_hand(player_hand)} (сумма: {player_total})\n"
        f"Карта дилера: {dealer_hand[0]} и 🂠 (скрыта)\n\n"
        f"Ставка: {bet:.1f} см",
        reply_markup=blackjack_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data.startswith("bj:hit:"))
async def cb_blackjack_hit(callback: CallbackQuery, state: FSMContext):
    owner_id = int(callback.data.split(":")[2])
    if callback.from_user.id != owner_id:
        await callback.answer("Это чужая игра! Начни свою через /блекджек", show_alert=True)
        return

    current_state = await state.get_state()
    if current_state != BlackjackStates.playing.state:
        await callback.answer("Эта игра уже завершена.", show_alert=True)
        return

    data = await state.get_data()
    deck = data["deck"]
    player_hand = data["player_hand"]
    dealer_hand = data["dealer_hand"]
    bet = data["bet"]

    player_hand.append(deck.pop())
    player_total = hand_total(player_hand)
    await state.update_data(deck=deck, player_hand=player_hand)

    if player_total > 21:
        await state.clear()
        new_length, delta = settle_blackjack(callback.from_user.id, bet, "lose")
        await callback.message.edit_text(
            f"🃏 Твои карты: {format_hand(player_hand)} ({player_total}) — ПЕРЕБОР!\n"
            f"Ты проиграл ставку {bet:.1f} см.\n"
            f"Изменение: {delta:+.1f} см → теперь {new_length:.1f} см"
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"🃏 Твои карты: {format_hand(player_hand)} (сумма: {player_total})\n"
        f"Карта дилера: {dealer_hand[0]} и 🂠 (скрыта)\n\n"
        f"Ставка: {bet:.1f} см",
        reply_markup=blackjack_keyboard(owner_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("bj:stand:"))
async def cb_blackjack_stand(callback: CallbackQuery, state: FSMContext):
    owner_id = int(callback.data.split(":")[2])
    if callback.from_user.id != owner_id:
        await callback.answer("Это чужая игра! Начни свою через /блекджек", show_alert=True)
        return

    current_state = await state.get_state()
    if current_state != BlackjackStates.playing.state:
        await callback.answer("Эта игра уже завершена.", show_alert=True)
        return

    data = await state.get_data()
    deck = data["deck"]
    player_hand = data["player_hand"]
    dealer_hand = data["dealer_hand"]
    bet = data["bet"]

    player_total = hand_total(player_hand)

    while hand_total(dealer_hand) < 17:
        dealer_hand.append(deck.pop())

    dealer_total = hand_total(dealer_hand)

    if dealer_total > 21:
        result, verdict = "win", "💥 У дилера перебор! Ты выиграл!"
    elif dealer_total > player_total:
        result, verdict = "lose", "😔 Дилер набрал больше. Ты проиграл."
    elif dealer_total < player_total:
        result, verdict = "win", "🎉 Ты набрал больше! Победа!"
    else:
        result, verdict = "push", "🤝 Ничья. Ставка возвращена."

    await state.clear()
    new_length, delta = settle_blackjack(callback.from_user.id, bet, result)

    await callback.message.edit_text(
        f"🃏 Твои карты: {format_hand(player_hand)} ({player_total})\n"
        f"🂠 Карты дилера: {format_hand(dealer_hand)} ({dealer_total})\n\n"
        f"{verdict}\n"
        f"Изменение: {delta:+.1f} см → теперь {new_length:.1f} см"
    )
    await callback.answer()


# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.message(Command("whoami"))
async def cmd_whoami(message: Message):
    await message.answer(
        f"Твой username по мнению бота: <code>{message.from_user.username}</code>\n"
        f"Твой user_id: <code>{message.from_user.id}</code>\n"
        f"Ожидаемый админский username: <code>{ADMIN_USERNAME}</code>\n"
        f"Совпадение: <b>{'да' if message.from_user.username == ADMIN_USERNAME else 'нет'}</b>"
    )


@dp.message(Command("admin"))
async def cmd_admin_help(message: Message):
    if not is_admin(message):
        return
    await message.answer(admin_help_text(), reply_markup=admin_menu_keyboard())


@dp.callback_query(F.data == "act:admin")
async def cb_admin_panel(callback: CallbackQuery):
    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await callback.message.answer(admin_help_text(), reply_markup=admin_menu_keyboard())
    await callback.answer()


@dp.message(Command("admin_set"))
async def cmd_admin_set(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: /admin_set @user 25 (или ответом на сообщение: /admin_set 25)")
        return

    if message.reply_to_message:
        target = resolve_target(message, "")
        value_str = args[0]
    else:
        if len(args) < 2:
            await message.answer("Укажи и пользователя, и значение: /admin_set @user 25")
            return
        target = resolve_target(message, args[0])
        value_str = args[1]

    if not target:
        await message.answer("Пользователь не найден в базе.")
        return

    try:
        value = float(value_str)
    except ValueError:
        await message.answer("Значение должно быть числом.")
        return

    update_length(target[0], max(0.0, value))
    await message.answer(f"✅ Длина пользователя {format_name(target[1], target[2])} установлена: {value:.1f} см")


@dp.message(Command("admin_add"))
async def cmd_admin_add(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: /admin_add @user 10 (или ответом на сообщение: /admin_add 10)")
        return

    if message.reply_to_message:
        target = resolve_target(message, "")
        value_str = args[0]
    else:
        if len(args) < 2:
            await message.answer("Укажи и пользователя, и значение: /admin_add @user 10")
            return
        target = resolve_target(message, args[0])
        value_str = args[1]

    if not target:
        await message.answer("Пользователь не найден в базе.")
        return

    try:
        value = float(value_str)
    except ValueError:
        await message.answer("Значение должно быть числом.")
        return

    new_length = max(0.0, target[3] + value)
    update_length(target[0], new_length)
    await message.answer(f"✅ {format_name(target[1], target[2])}: {target[3]:.1f} → {new_length:.1f} см")


@dp.message(Command("admin_cure"))
async def cmd_admin_cure(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    target = resolve_target(message, (command.args or "").strip())
    if not target:
        await message.answer("Использование: /admin_cure @user (или ответом на сообщение)")
        return

    clear_disease(target[0])
    await message.answer(f"✅ {format_name(target[1], target[2])} вылечен от всех болезней.")


@dp.message(Command("admin_disease"))
async def cmd_admin_disease(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    args = (command.args or "").split(maxsplit=1)

    if message.reply_to_message:
        target = resolve_target(message, "")
        disease_name = args[0] if args else None
    else:
        if len(args) < 2:
            await message.answer(
                "Использование: /admin_disease @user Название болезни\n"
                "Доступные названия смотри в /admin_diseases"
            )
            return
        target = resolve_target(message, args[0])
        disease_name = args[1]

    if not target:
        await message.answer("Пользователь не найден в базе.")
        return

    if disease_name not in DISEASES:
        await message.answer(f"Неизвестная болезнь. Доступные: {', '.join(DISEASES.keys())}")
        return

    expires = datetime.now() + timedelta(days=1)
    set_disease(target[0], disease_name, expires)
    await message.answer(f"✅ {format_name(target[1], target[2])} заражён болезнью «{disease_name}» на 1 день.")


@dp.message(Command("admin_reset"))
async def cmd_admin_reset(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    target = resolve_target(message, (command.args or "").strip())
    if not target:
        await message.answer("Использование: /admin_reset @user (или ответом на сообщение)")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE users SET length = 10.0, last_teasing = NULL, last_duel = NULL,
           disease_name = NULL, disease_expires = NULL WHERE user_id = ?""",
        (target[0],),
    )
    conn.commit()
    conn.close()
    await message.answer(f"✅ {format_name(target[1], target[2])} полностью сброшен.")


@dp.message(Command("admin_info"))
async def cmd_admin_info(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    target = resolve_target(message, (command.args or "").strip())
    if not target:
        await message.answer("Использование: /admin_info @user (или ответом на сообщение)")
        return

    user_id, username, full_name, length, last_teasing, last_duel, disease_name, disease_expires = target

    text = (
        f"👤 <b>{format_name(username, full_name)}</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"Длина: {length:.1f} см\n"
        f"Последнее теребонькивание: {last_teasing or '—'}\n"
        f"Последняя дуэль: {last_duel or '—'}\n"
        f"Болезнь: {disease_name or 'нет'}\n"
        f"Болезнь пройдёт: {disease_expires or '—'}"
    )
    await message.answer(text)


@dp.message(Command("admin_users"))
async def cmd_admin_users(message: Message):
    if not is_admin(message):
        return
    await message.answer(build_users_list_text())


@dp.message(Command("admin_broadcast"))
async def cmd_admin_broadcast(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /admin_broadcast текст сообщения")
        return

    ids = get_all_user_ids()
    sent, failed = 0, 0
    for uid in ids:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление:</b>\n{text}")
            sent += 1
        except Exception:
            failed += 1

    await message.answer(f"Рассылка завершена. Успешно: {sent}, ошибок: {failed}")


@dp.message(Command("admin_diseases"))
async def cmd_admin_diseases(message: Message):
    if not is_admin(message):
        return
    await message.answer(build_diseases_list_text())


# ==================== АДМИН CALLBACK-КНОПКИ ====================
@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(callback: CallbackQuery):
    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await callback.message.answer(build_users_list_text())
    await callback.answer()


@dp.callback_query(F.data == "adm_diseases")
async def cb_adm_diseases(callback: CallbackQuery):
    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await callback.message.answer(build_diseases_list_text())
    await callback.answer()


@dp.callback_query(F.data.endswith("_hint"))
async def cb_adm_hints(callback: CallbackQuery):
    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    hints = {
        "adm_info_hint": "Ответь на сообщение пользователя командой:\n<code>/admin_info</code>",
        "adm_cure_hint": "Ответь на сообщение пользователя командой:\n<code>/admin_cure</code>",
        "adm_set_hint": "Ответь на сообщение пользователя командой:\n<code>/admin_set 25</code>\n(число — новая длина в см)",
        "adm_disease_hint": "Ответь на сообщение пользователя командой:\n<code>/admin_disease Простудный писюн</code>",
        "adm_reset_hint": "Ответь на сообщение пользователя командой:\n<code>/admin_reset</code>",
        "adm_broadcast_hint": "Напиши команду с текстом рассылки:\n<code>/admin_broadcast Привет всем!</code>",
    }
    await callback.message.answer(hints.get(callback.data, "Используй соответствующую команду вручную."))
    await callback.answer()


# ==================== ЗАПУСК ====================
async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
