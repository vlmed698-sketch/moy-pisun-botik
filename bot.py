import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
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
DUEL_BONUS = 5          # фиксированный бонус победителю дуэли (см)
DUEL_COOLDOWN_MIN = 5   # антиспам для дуэлей, минут между вызовами

DISEASE_CHANCE = 0.15   # 15% шанс подхватить болезнь при теребонькании
DISEASE_MIN_DAYS = 1
DISEASE_MAX_DAYS = 3
CURE_PRICE_STARS = 1    # стоимость полного лечения в Telegram Stars

ADMIN_USERNAME = "I9451"  # только этот пользователь имеет доступ к админ-командам

DB_PATH = "game.db"

# ==================== БОЛЕЗНИ ====================
# effect: "half"     — рост от потеребонькивания делится пополам
#         "flat_neg" — к каждому результату теребонькивания добавляется штраф (см)
#         "cap_low"  — рост ограничен сверху небольшим значением
#         "invert"   — положительный результат становится отрицательным
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

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# user_id аккаунта, которым Telegram подписывает анонимные сообщения от админов групп
GROUP_ANONYMOUS_BOT_ID = 1087968824
# user_id аккаунта, которым подписываются анонимные сообщения от лица канала
CHANNEL_ANONYMOUS_BOT_ID = 136817688


@dp.message.middleware()
async def block_anonymous_and_bots_middleware(handler, event: Message, data: dict):
    """Не пускает дальше сообщения, отправленные анонимно от лица группы/канала
    или от других ботов — такие сообщения не должны попадать в игровую логику."""
    user = event.from_user

    # сообщение отправлено анонимно от имени группы (chat.sender_chat заполнен)
    if event.sender_chat is not None:
        await event.answer(
            "🚫 Команды от анонимных админов группы не поддерживаются.\n"
            "Отключи анонимность в настройках группы или напиши боту в личку."
        )
        return

    # на всякий случай — сам служебный аккаунт GroupAnonymousBot/ChannelBot
    if user and user.id in (GROUP_ANONYMOUS_BOT_ID, CHANNEL_ANONYMOUS_BOT_ID):
        await event.answer(
            "🚫 Команды от анонимных админов группы не поддерживаются.\n"
            "Отключи анонимность в настройках группы или напиши боту в личку."
        )
        return

    # сообщения от других ботов игнорируем полностью (без ответа, чтобы не спамить)
    if user and user.is_bot:
        return

    # авто-регистрация: любое сообщение (не только команды) добавляет юзера в базу,
    # чтобы бот "видел" как можно больше участников группы, а не только тех,
    # кто явно пользовался игровыми командами
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

    # миграция: если таблица уже существовала без новых колонок — добавим их
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
        # обновим имя/юзернейм на случай, если поменялись
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
    """Применяет эффект болезни к изначальному результату теребонькивания."""
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


def maybe_catch_disease(user_id: int) -> str | None:
    """С шансом DISEASE_CHANCE заражает пользователя случайной болезнью.
    Возвращает название болезни, если заражение произошло, иначе None."""
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


# ==================== ХЕЛПЕРЫ ====================
def is_admin(message: Message) -> bool:
    return message.from_user.username == ADMIN_USERNAME


def find_user_by_username(username: str):
    username = username.lstrip("@")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
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
    """Возвращает список (user_id, username, full_name, length) для всех игроков."""
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
        get_or_create_user(u.id, u.username, u.full_name)
        return get_user(u.id)
    if arg:
        return find_user_by_username(arg)
    return None


# ==================== КЛАВИАТУРЫ ====================
def main_menu_keyboard(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🍆 Потеребонькать"), KeyboardButton(text="📏 Мой размер")],
        [KeyboardButton(text="🏥 Статус"), KeyboardButton(text="💊 Лечиться")],
        [KeyboardButton(text="🏆 Топ игроков")],
    ]
    if is_admin_user:
        keyboard.append([KeyboardButton(text="⚙️ Админка")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выбери действие...",
    )


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


def format_name(username: str, full_name: str) -> str:
    if username:
        return f"@{username}"
    return full_name or "Аноним"


def time_left_str(seconds: float) -> str:
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours} ч {minutes} мин"


# ==================== ХЕНДЛЕРЫ ====================
@dp.message(F.new_chat_members)
async def on_new_chat_members(message: Message):
    """Регистрируем в базе всех, кто вступает в группу с ботом,
    даже если они ещё не написали ни одной команды."""
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
        "Можешь пользоваться кнопками снизу или командами:\n"
        "/потеребонькать — раз в сутки менять свою длину (есть шанс подхватить болезнь!)\n"
        "/статус — узнать, чем болеешь и когда пройдёт\n"
        "/лечиться — вылечить все болезни за Telegram Stars\n"
        "/топ — таблица лидеров\n"
        "/бой (ответом на сообщение соперника) — дуэль на кубиках\n"
        "/мой — узнать свою текущую длину",
        reply_markup=main_menu_keyboard(is_admin(message)),
    )


@dp.message(Command("мой"))
@dp.message(F.text == "📏 Мой размер")
async def cmd_my(message: Message):
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    length = user[3]
    await message.answer(f"Твоя текущая длина: <b>{length:.1f} см</b>")


@dp.message(Command("потеребонькать"))
@dp.message(F.text == "🍆 Потеребонькать")
async def cmd_teasing(message: Message):
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    last_teasing = user[4]
    now = datetime.now()

    if last_teasing:
        last_dt = datetime.fromisoformat(last_teasing)
        elapsed = now - last_dt
        cooldown = timedelta(hours=COOLDOWN_HOURS)
        if elapsed < cooldown:
            remaining = (cooldown - elapsed).total_seconds()
            await message.answer(
                f"⏳ Ты уже теребонькал сегодня! Приходи через {time_left_str(remaining)}."
            )
            return

    change = random.randint(MIN_GROWTH, MAX_GROWTH)

    # проверяем активную болезнь и применяем её эффект к результату
    active = get_active_disease(message.from_user.id)
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

    update_length(message.from_user.id, new_length)
    set_last_teasing(message.from_user.id, now)

    if change > 0:
        emoji = "📈"
        verdict = f"вырос на {change} см"
    elif change < 0:
        emoji = "📉"
        verdict = f"уменьшился на {abs(change)} см"
    else:
        emoji = "➖"
        verdict = "не изменился"

    text = (
        f"{emoji} Ты потеребонькал... результат: <b>{verdict}</b>!\n"
        f"Текущая длина: <b>{new_length:.1f} см</b>"
        f"{disease_note}"
    )

    # шанс подхватить новую болезнь (только если сейчас здоров)
    if not active:
        caught = maybe_catch_disease(message.from_user.id)
        if caught:
            info = DISEASES[caught]
            text += (
                f"\n\n{info['emoji']} <b>Ты подхватил болезнь: {caught}!</b>\n"
                f"<i>{info['description']}</i>\n"
                f"Используй /статус, чтобы следить за лечением, или /лечиться, чтобы вылечиться сразу."
            )

    await message.answer(text)


@dp.message(Command("статус"))
@dp.message(F.text == "🏥 Статус")
async def cmd_status(message: Message):
    get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    active = get_active_disease(message.from_user.id)

    if not active:
        await message.answer("✅ Ты полностью здоров! Никаких болезней.")
        return

    disease_name, disease_info = active
    user = get_user(message.from_user.id)
    expires_dt = datetime.fromisoformat(user[7])
    remaining = (expires_dt - datetime.now()).total_seconds()

    await message.answer(
        f"{disease_info['emoji']} Ты болен: <b>{disease_name}</b>\n"
        f"<i>{disease_info['description']}</i>\n"
        f"Пройдёт через: {time_left_str(remaining)}\n\n"
        f"Хочешь вылечиться сразу? Используй /лечиться ({CURE_PRICE_STARS} ⭐)"
    )


@dp.message(Command("лечиться"))
@dp.message(F.text == "💊 Лечиться")
async def cmd_cure(message: Message):
    get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    active = get_active_disease(message.from_user.id)
    if not active:
        await message.answer("✅ Ты и так здоров, лечить нечего!")
        return

    disease_name, disease_info = active

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Полное исцеление",
        description=f"Мгновенно вылечивает «{disease_name}» ({disease_info['description']})",
        payload=f"cure_disease:{message.from_user.id}",
        currency="XTR",  # XTR — валюта Telegram Stars
        prices=[LabeledPrice(label="Лечение", amount=CURE_PRICE_STARS)],
        # amount указывается в звёздах напрямую (не в копейках, как для обычных валют)
    )


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    # подтверждаем платёж перед списанием звёзд
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("cure_disease:"):
        user_id = int(payload.split(":")[1])
        clear_disease(user_id)
        await message.answer(
            "💊 Оплата прошла успешно! Ты полностью излечен от всех болезней."
        )


@dp.message(Command("топ"))
@dp.message(F.text == "🏆 Топ игроков")
async def cmd_top(message: Message):
    top = get_top_users(10)
    if not top:
        await message.answer("Пока никто не участвует в игре 😢")
        return

    lines = ["🏆 <b>Таблица лидеров:</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (username, full_name, length) in enumerate(top):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = format_name(username, full_name)
        lines.append(f"{prefix} {name} — {length:.1f} см")

    await message.answer("\n".join(lines))


@dp.message(Command("бой"))
async def cmd_duel(message: Message):
    if not message.reply_to_message:
        await message.answer(
            "Чтобы вызвать на дуэль, ответь командой /бой на сообщение соперника!"
        )
        return

    challenger = message.from_user
    opponent = message.reply_to_message.from_user

    if opponent.id == challenger.id:
        await message.answer("Нельзя сражаться самим с собой 🙃")
        return

    if opponent.is_bot:
        await message.answer("Нельзя сражаться с ботом 🤖")
        return

    # антиспам
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
        f"⚔️ Дуэль началось!\n\n"
        f"{name1} кидает кубик... 🎲 <b>{roll1}</b>\n"
        f"{name2} кидает кубик... 🎲 <b>{roll2}</b>\n\n"
    )

    set_last_duel(challenger.id, datetime.now())

    if roll1 == roll2:
        text += "🤝 Ничья! Оба остаются при своём."
    else:
        if roll1 > roll2:
            winner_id, winner_name = challenger.id, name1
        else:
            winner_id, winner_name = opponent.id, name2

        winner_row = get_user(winner_id)
        new_length = winner_row[3] + DUEL_BONUS
        update_length(winner_id, new_length)

        text += f"🎉 Победил {winner_name}! Бонус: +{DUEL_BONUS} см (теперь {new_length:.1f} см)"

    await message.answer(text)


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
@dp.message(F.text == "⚙️ Админка")
async def cmd_admin_help(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "Жми кнопки ниже или используй команды вручную.\n"
        "Цель команды указывается ответом на сообщение пользователя ИЛИ через @username.\n\n"
        "/admin_set @user 25 — установить длину\n"
        "/admin_add @user 10 — прибавить к длине (можно отрицательное)\n"
        "/admin_cure @user — вылечить от всех болезней\n"
        "/admin_disease @user Название болезни — заразить конкретной болезнью на 1 день\n"
        "/admin_reset @user — сбросить пользователя (длина 10, без болезней, без кулдаунов)\n"
        "/admin_info @user — показать всю инфу о пользователе\n"
        "/admin_users — список всех user_id в базе\n"
        "/admin_broadcast текст — разослать сообщение всем игрокам\n"
        "/admin_diseases — список всех доступных болезней",
        reply_markup=admin_menu_keyboard(),
    )


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
    await message.answer(
        f"✅ {format_name(target[1], target[2])}: {target[3]:.1f} → {new_length:.1f} см"
    )


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
    await message.answer(
        f"✅ {format_name(target[1], target[2])} заражён болезнью «{disease_name}» на 1 день."
    )


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
    users = get_all_users_brief()
    if not users:
        await message.answer("В базе пока нет пользователей.")
        return

    lines = [f"👥 <b>Всего пользователей: {len(users)}</b>\n"]
    for user_id, username, full_name, length in users:
        name = format_name(username, full_name)
        lines.append(f"{name} — {length:.1f} см (<code>{user_id}</code>)")

    await message.answer("\n".join(lines))


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
    lines = ["📋 <b>Доступные болезни:</b>\n"]
    for name, info in DISEASES.items():
        lines.append(f"{info['emoji']} <b>{name}</b> — {info['description']}")
    await message.answer("\n".join(lines))


# ==================== АДМИН CALLBACK-КНОПКИ ====================
@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(callback: CallbackQuery):
    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    users = get_all_users_brief()
    if not users:
        await callback.message.answer("В базе пока нет пользователей.")
        await callback.answer()
        return

    lines = [f"👥 <b>Всего пользователей: {len(users)}</b>\n"]
    for user_id, username, full_name, length in users:
        name = format_name(username, full_name)
        lines.append(f"{name} — {length:.1f} см (<code>{user_id}</code>)")

    await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.callback_query(F.data == "adm_diseases")
async def cb_adm_diseases(callback: CallbackQuery):
    if callback.from_user.username != ADMIN_USERNAME:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    lines = ["📋 <b>Доступные болезни:</b>\n"]
    for name, info in DISEASES.items():
        lines.append(f"{info['emoji']} <b>{name}</b> — {info['description']}")
    await callback.message.answer("\n".join(lines))
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
