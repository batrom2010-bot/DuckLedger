import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
)

from openpyxl import Workbook

# ==============================
# НАСТРОЙКИ
# ==============================

DB_FILE = "budget.db"

# Жёстко считаем локальное время = UTC+3
LOCAL_UTC_OFFSET = 3  # в часах


def get_local_now() -> datetime:
    """Текущее локальное время (UTC+3), без таймзоны."""
    return datetime.utcnow() + timedelta(hours=LOCAL_UTC_OFFSET)


BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ==============================
# FSM
# ==============================


class InsertStates(StatesGroup):
    waiting_for_expenses = State()


class LimitStates(StatesGroup):
    waiting_for_limits = State()


# ==============================
# РАБОТА С БАЗОЙ
# ==============================


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            category  TEXT    NOT NULL,
            amount    REAL    NOT NULL,
            timestamp TEXT    NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS limits (
            category     TEXT PRIMARY KEY,
            limit_amount REAL NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()
    logger.info("Инициализация базы данных...")


def add_expense(category: str, amount: float):
    ts = get_local_now().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (category, amount, timestamp) VALUES (?, ?, ?)",
        (category, amount, ts),
    )
    conn.commit()
    conn.close()


def set_limits(pairs: List[Tuple[str, float]]):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for category, limit_amount in pairs:
        cursor.execute(
            """
            INSERT INTO limits (category, limit_amount)
            VALUES (?, ?)
            ON CONFLICT(category) DO UPDATE SET limit_amount = excluded.limit_amount
            """,
            (category, limit_amount),
        )
    conn.commit()
    conn.close()


@dataclass
class MonthStats:
    total: float
    by_category: Dict[str, float]
    limits: Dict[str, float]


def _load_all_expenses() -> List[Tuple[str, float, str]]:
    """Все расходы: (category, amount, timestamp_str)."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT category, amount, timestamp FROM expenses")
    rows = cursor.fetchall()
    conn.close()
    return rows


def _load_limits() -> Dict[str, float]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT category, limit_amount FROM limits")
    rows = cursor.fetchall()
    conn.close()
    return {cat: float(limit) for cat, limit in rows}


def get_month_stats(now: datetime | None = None) -> MonthStats:
    """
    Статистика за текущий месяц по локальному времени.
    Фильтрация по месяцу идёт в Python, чтобы не зависеть от формата дат в SQLite.
    """
    if now is None:
        now = get_local_now()

    year, month = now.year, now.month

    rows = _load_all_expenses()
    by_category: Dict[str, float] = {}
    total = 0.0

    for category, amount, ts_str in rows:
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            # На всякий случай пропускаем битые даты
            continue

        if ts.year == year and ts.month == month:
            total += float(amount)
            by_category[category] = by_category.get(category, 0.0) + float(amount)

    limits = _load_limits()
    return MonthStats(total=total, by_category=by_category, limits=limits)


def get_full_stats() -> Dict[str, float]:
    """
    Общая аналитика по всем расходам (без ограничений по дате).
    """
    rows = _load_all_expenses()
    by_category: Dict[str, float] = {}
    for category, amount, _ in rows:
        by_category[category] = by_category.get(category, 0.0) + float(amount)
    return by_category


def export_to_excel(clear_after: bool = False) -> Path:
    """
    Формирование Excel:
    - первая колонка: Дата (дд/мм/гггг)
    - дальше по колонкам категории
    - по строкам суммы за день и категорию
    Если clear_after=True — после сохранения очищаем базу.
    """
    rows = _load_all_expenses()
    if not rows:
        # Пустой файл тоже создадим, чтобы пользователь видел структуру
        wb = Workbook()
        ws = wb.active
        ws.title = "Расходы"
        ws.append(["Дата", "Категория", "Сумма"])
        export_dir = Path("export")
        export_dir.mkdir(exist_ok=True)
        filename = export_dir / f"expenses_{get_local_now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        wb.save(filename)
        return filename

    # Агрегация: дата -> категория -> сумма
    data: Dict[str, Dict[str, float]] = {}
    categories_set = set()

    for category, amount, ts_str in rows:
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        local_date_str = ts.strftime("%d/%m/%Y")
        if local_date_str not in data:
            data[local_date_str] = {}
        data[local_date_str][category] = data[local_date_str].get(category, 0.0) + float(
            amount
        )
        categories_set.add(category)

    categories = sorted(categories_set, key=str.lower)
    dates_sorted = sorted(
        data.keys(),
        key=lambda d: datetime.strptime(d, "%d/%m/%Y"),
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "Расходы"

    # Заголовки
    header = ["Дата"] + categories
    ws.append(header)

    # Строки по датам
    for date_str in dates_sorted:
        row = [date_str]
        row_data = data[date_str]
        for cat in categories:
            value = row_data.get(cat)
            row.append(value if value is not None else "")
        ws.append(row)

    export_dir = Path("export")
    export_dir.mkdir(exist_ok=True)
    filename = export_dir / f"expenses_{get_local_now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)

    if clear_after:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM expenses")
        cursor.execute("DELETE FROM limits")
        conn.commit()
        conn.close()

    return filename


def parse_lines_to_pairs(text: str) -> List[Tuple[str, float]]:
    pairs: List[Tuple[str, float]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "-" not in line:
            raise ValueError(f"Не удалось найти разделитель '-' в строке: «{line}»")
        category, amount_str = line.split("-", 1)
        category = category.strip()
        amount_str = amount_str.strip().replace(",", ".")
        if not category:
            raise ValueError(f"Пустая категория в строке: «{line}»")
        try:
            amount = float(amount_str)
        except ValueError:
            raise ValueError(f"Не удалось прочитать сумму в строке: «{line}»")
        pairs.append((category, amount))
    return pairs


# ==============================
# BOT
# ==============================

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет, ! Я бот для учёта расходов.\n\n"
        "Просто отправь мне строку в формате:\n"
        "<b>Категория-Сумма</b>\n"
        "или используй команду <b>/insert</b> для ввода списка.\n\n"
        "Команда <b>/limit</b> — для установки лимитов по категориям.\n"
        "Команда <b>/stats</b> — краткая статистика по месяцу.\n"
        "Команда <b>/analitick</b> — расширенная аналитика.\n"
        "Команда <b>/categories</b> — список категорий с расходами в этом месяце.\n"
        "Команда <b>/make</b> — сформировать Excel-отчёт и очистить базу.\n"
        "Команда <b>/export</b> — выгрузить Excel-таблицу (без очистки)."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)


# --------- Ввод расходов ---------


@router.message(Command("insert"))
async def cmd_insert(message: Message, state: FSMContext):
    await state.set_state(InsertStates.waiting_for_expenses)
    await message.answer(
        "Отправь список расходов в формате:\n"
        "<b>Категория-Сумма</b>\n"
        "Можно сразу несколько строк:\n"
        "Еда-500\nТакси-300\nКофе-200"
    )


@router.message(InsertStates.waiting_for_expenses)
async def process_insert_list(message: Message, state: FSMContext):
    try:
        pairs = parse_lines_to_pairs(message.text)
    except ValueError as e:
        await message.answer(
            f"⚠️ Ошибка: {e}\n\n"
            "Пример правильного формата:\n"
            "Еда-500\nТакси-300\nКофе-200"
        )
        return

    for category, amount in pairs:
        add_expense(category, amount)

    total = sum(a for _, a in pairs)
    await state.clear()
    await message.answer(
        f"Записал {len(pairs)} расходов на сумму {int(total) if total.is_integer() else total}. "
        "Можешь отправлять новые строки в формате Категория-Сумма."
    )


# --------- Лимиты ---------


@router.message(Command("limit"))
async def cmd_limit(message: Message, state: FSMContext):
    await state.set_state(LimitStates.waiting_for_limits)
    await message.answer(
        "Отправь список лимитов в формате:\n"
        "<b>Категория-Сумма</b>\n"
        "Можно сразу несколько строк:\n"
        "Еда-20000\nТакси-5000"
    )


@router.message(LimitStates.waiting_for_limits)
async def process_limit_list(message: Message, state: FSMContext):
    try:
        pairs = parse_lines_to_pairs(message.text)
    except ValueError as e:
        await message.answer(
            f"⚠️ Ошибка: {e}\n\n"
            "Пример правильного формата:\n"
            "Еда-20000\nТакси-5000"
        )
        return

    set_limits(pairs)
    await state.clear()
    await message.answer("Лимиты обновлены.")


# --------- Статистика ---------


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = get_month_stats()
    if stats.total <= 0:
        await message.answer("За этот месяц расходов ещё нет.")
        return

    lines: List[str] = ["Статистика за текущий месяц:"]
    for cat in sorted(stats.by_category.keys(), key=str.lower):
        amount = stats.by_category[cat]
        line = f"{cat}: {int(amount) if amount.is_integer() else amount}"

        if cat in stats.limits:
            limit = stats.limits[cat]
            diff = limit - amount
            limit_str = int(limit) if limit.is_integer() else limit
            line += f" / лимит {limit_str}"
            if diff < 0:
                over = -diff
                over_str = int(over) if over.is_integer() else over
                line += f" (перерасход {over_str})"
            else:
                left = diff
                left_str = int(left) if left.is_integer() else left
                line += f" (осталось {left_str})"

        lines.append(line)

    total_str = int(stats.total) if stats.total.is_integer() else stats.total
    lines.append(f"\nВсего за месяц: {total_str}")
    await message.answer("\n".join(lines))


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    stats = get_month_stats()
    if not stats.by_category:
        await message.answer("Категорий с расходами в этом месяце ещё нет.")
        return

    cats = sorted(stats.by_category.keys(), key=str.lower)
    text_lines = ["Категории с расходами в этом месяце:"]
    text_lines += [f"• {c}" for c in cats]
    await message.answer("\n".join(text_lines))


@router.message(Command("analitick"))
async def cmd_analitick(message: Message):
    all_stats = get_full_stats()
    if not all_stats:
        await message.answer("Расходов пока нет.")
        return

    lines = ["Общая аналитика по всем расходам:"]
    total = 0.0
    for cat in sorted(all_stats.keys(), key=str.lower):
        amount = all_stats[cat]
        total += amount
        amount_str = int(amount) if amount.is_integer() else amount
        lines.append(f"{cat}: {amount_str}")

    total_str = int(total) if total.is_integer() else total
    lines.append(f"\nВсего за всё время: {total_str}")
    await message.answer("\n".join(lines))


# --------- Excel / очистка ---------


@router.message(Command("export"))
async def cmd_export(message: Message):
    file_path = export_to_excel(clear_after=False)
    await message.answer_document(
        FSInputFile(file_path),
        caption="Вот твоя таблица расходов 📊",
    )


@router.message(Command("make"))
async def cmd_make(message: Message):
    file_path = export_to_excel(clear_after=True)
    await message.answer_document(
        FSInputFile(file_path),
        caption=(
            "Сформировал Excel-отчёт и очистил базу.\n"
            "Не забудь сохранить файл у себя."
        ),
    )
    await message.answer("Все данные по расходам и лимитам очищены. Можно начинать заново.")


# ==============================
# MAIN
# ==============================


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Укажи токен бота в BOT_TOKEN или переменной TELEGRAM_TOKEN"
        )

    logger.info("Запускаем DuckLedger...")
    init_db()

    dp = Dispatcher()
    dp.include_router(router)

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)

    logger.info("Старт polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())






