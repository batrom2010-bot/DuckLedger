import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, FSInputFile
from openpyxl import Workbook

# ==========================
# НАСТРОЙКИ
# ==========================

# Можно хранить токен в переменной окружения TELEGRAM_TOKEN
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")

# Имя файла базы
DB_PATH = "бюджет.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ==========================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ
# ==========================


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        # таблица расходов
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # таблица лимитов
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                limit_amount REAL NOT NULL,
                UNIQUE (user_id, category)
            )
            """
        )

        conn.commit()


def add_expense(user_id: int, category: str, amount: float):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO expenses (user_id, category, amount, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, category, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()


def set_limit(user_id: int, category: str, limit_amount: float):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO limits (user_id, category, limit_amount)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET
                limit_amount = excluded.limit_amount
            """,
            (user_id, category, limit_amount),
        )
        conn.commit()


def get_expenses_for_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT category, amount, created_at
            FROM expenses
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def get_month_sum_by_category(user_id: int, category: str) -> float:
    """Сумма по категории за текущий месяц (UTC)."""
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE user_id = ?
              AND category = ?
              AND created_at >= ?
            """,
            (user_id, category, month_start),
        )
        row = cur.fetchone()
        return float(row[0] or 0)


def get_limit_for_category(user_id: int, category: str):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT limit_amount
            FROM limits
            WHERE user_id = ? AND category = ?
            """,
            (user_id, category),
        )
        row = cur.fetchone()
        return float(row[0]) if row else None


# ==========================
# СТЕЙТЫ
# ==========================


class InsertStates(StatesGroup):
    waiting_for_data = State()


class LimitStates(StatesGroup):
    waiting_for_data = State()


# ==========================
# УТИЛИТЫ
# ==========================


def parse_category_amount_list(text: str):
    """
    Парсим сообщение формата:

    Еда-500
    Такси-300
    Кофе-200

    Работает и с одной строкой, и с несколькими.
    Пустые строки игнорируются.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("Пустое сообщение. Нечего разбирать.")

    parsed = []
    for line in lines:
        if "-" not in line:
            raise ValueError(f"Не удалось найти разделитель '-' в строке: «{line}»")
        category, amount = line.split("-", 1)
        category = category.strip()
        amount = amount.strip().replace(",", ".")
        if not category:
            raise ValueError(f"Пустая категория в строке: «{line}»")
        if not amount:
            raise ValueError(f"Пустая сумма в строке: «{line}»")
        try:
            value = float(amount)
        except ValueError:
            raise ValueError(f"Сумма должна быть числом в строке: «{line}»")
        parsed.append((category, value))

    return parsed


# ==========================
# ХЕНДЛЕРЫ
# ==========================


async def cmd_start(message: Message):
    text = (
        "Привет, {name}!\n\n"
        "Я бот для учёта расходов.\n\n"
        "📥 Ввод расходов:\n"
        "— Просто отправь строки вида:\n"
        "  <b>Категория-Сумма</b>\n"
        "  Можно сразу несколько строк.\n"
        "  Пример:\n"
        "  <code>Еда-500\\nТакси-300\\nКофе-200</code>\n\n"
        "Или используй команду /insert.\n\n"
        "💰 Лимиты по категориям:\n"
        "— Команда /limit, формат такой же:\n"
        "  <code>Еда-15000\\nТакси-5000</code>\n\n"
        "ℹ️ Подробности смотри в /help"
    ).format(name=message.from_user.first_name or "")
    await message.answer(text, parse_mode="HTML")


async def cmd_help(message: Message):
    text = (
        "<b>Команды бота DuckLedger</b>\n\n"
        "• /start — краткая инструкция.\n"
        "• /help — это сообщение.\n"
        "• /insert — добавление расходов списком.\n"
        "   Формат сообщения после команды:\n"
        "   <code>Категория-Сумма</code>\n"
        "   Можно одной строкой или несколькими, например:\n"
        "   <code>Еда-500\\nТакси-300\\nКофе-200</code>\n\n"
        "• /limit — установка лимитов по категориям.\n"
        "   Формат такой же, можно сразу несколько категорий:\n"
        "   <code>Еда-15000\\nТакси-5000</code>\n\n"
        "• /export — выгрузить все ваши расходы в .xlsx файл.\n"
    )
    await message.answer(text, parse_mode="HTML")


# ---------- /insert ----------


async def cmd_insert(message: Message, state: FSMContext):
    await state.set_state(InsertStates.waiting_for_data)
    await message.answer(
        "Отправь список расходов в формате:\n"
        "<code>Категория-Сумма</code>\n"
        "Можно одной строкой или несколькими. Пример:\n"
        "<code>Еда-500\nТакси-300\nКофе-200</code>",
        parse_mode="HTML",
    )


async def process_insert(message: Message, state: FSMContext):
    try:
        parsed_rows = parse_category_amount_list(message.text)
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\nПопробуй ещё раз.")
        return

    warnings = []
    for category, amount in parsed_rows:
        add_expense(message.from_user.id, category, amount)

        # Проверка лимита по категории, если задан
        limit = get_limit_for_category(message.from_user.id, category)
        if limit is not None:
            total = get_month_sum_by_category(message.from_user.id, category)
            if total > limit:
                warnings.append(
                    f"Категория <b>{category}</b>: "
                    f"расход за месяц {total:.2f}, лимит {limit:.2f}"
                )

    await state.clear()

    base_text = "✅ Расходы сохранены."
    if warnings:
        base_text += "\n\n⚠️ Превышены лимиты:\n" + "\n".join(f"— {w}" for w in warnings)

    await message.answer(base_text, parse_mode="HTML")


# ---------- /limit ----------


async def cmd_limit(message: Message, state: FSMContext):
    await state.set_state(LimitStates.waiting_for_data)
    await message.answer(
        "Отправь список лимитов в формате:\n"
        "<code>Категория-Сумма</code>\n"
        "Можно сразу несколько строк. Пример:\n"
        "<code>Еда-15000\nТакси-5000</code>",
        parse_mode="HTML",
    )


async def process_limit(message: Message, state: FSMContext):
    try:
        parsed_rows = parse_category_amount_list(message.text)
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\nПопробуй ещё раз.")
        return

    for category, amount in parsed_rows:
        set_limit(message.from_user.id, category, amount)

    await state.clear()
    await message.answer("✅ Лимиты по категориям обновлены.", parse_mode="HTML")


# ---------- /export ----------


async def cmd_export(message: Message):
    """Экспорт всех расходов пользователя в Excel."""
    rows = get_expenses_for_user(message.from_user.id)
    if not rows:
        await message.answer("У тебя ещё нет записанных расходов.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "Расходы"

    ws.append(["Категория", "Сумма", "Дата (UTC)"])
    for category, amount, created_at in rows:
        ws.append([category, amount, created_at])

    filename = f"expenses_{message.from_user.id}.xlsx"
    wb.save(filename)

    await message.answer_document(FSInputFile(filename))
    os.remove(filename)


# ---------- Фолбэк на обычный текст ----------


async def fallback_message(message: Message):
    """
    Если пользователь без команды сразу шлёт 'Еда-500' и т.п.,
    пробуем распарсить как расходы.
    """
    try:
        parsed_rows = parse_category_amount_list(message.text)
    except Exception:
        # Не парсим — это точно не наш формат
        await message.answer(
            "Я не понял сообщение.\n"
            "Для ввода расходов используй /insert "
            "или отправь строки вида <code>Категория-Сумма</code>.",
            parse_mode="HTML",
        )
        return

    # Если успешно распарсили — считаем как insert без состояния
    for category, amount in parsed_rows:
        add_expense(message.from_user.id, category, amount)

    await message.answer("✅ Расходы сохранены (распознал без команды).")


# ==========================
# ЗАПУСК БОТА
# ==========================


async def main():
    if BOT_TOKEN == "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН":
        raise RuntimeError("Укажи токен бота в BOT_TOKEN или переменной TELEGRAM_TOKEN")

    logger.info("Инициализация базы данных...")
    init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    # Команды
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_insert, Command("insert"))
    dp.message.register(cmd_limit, Command("limit"))
    dp.message.register(cmd_export, Command("export"))

    # Стейты
    dp.message.register(process_insert, InsertStates.waiting_for_data)
    dp.message.register(process_limit, LimitStates.waiting_for_data)

    # Фолбэк на любой текст
    dp.message.register(fallback_message, F.text)

    logger.info("Запускаем DuckLedger...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

