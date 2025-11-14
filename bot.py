import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    FSInputFile,
    Message,
    ReplyKeyboardRemove,
)
from openpyxl import Workbook

# ==========================
#  НАСТРОЙКИ И ЛОГИРОВАНИЕ
# ==========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DB_FILE = "budget.db"

# Токен берём из переменных окружения (Render) или из константы
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""


# ==========================
#  FSM СОСТОЯНИЯ
# ==========================

class InsertStates(StatesGroup):
    waiting_for_expenses = State()


class LimitStates(StatesGroup):
    waiting_for_limits = State()


# ==========================
#  РАБОТА С БАЗОЙ
# ==========================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. Создаём таблицу расходов без timestamp (чтобы не падать на старых базах)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            amount REAL
        )
    """)

    # 2. Проверяем, есть ли колонка timestamp, и добавляем её при необходимости
    cursor.execute("PRAGMA table_info(expenses)")
    columns = [row[1] for row in cursor.fetchall()]

    if "timestamp" not in columns:
        cursor.execute("ALTER TABLE expenses ADD COLUMN timestamp TEXT")

    # 3. Таблица лимитов — как и было
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS limits (
            category TEXT PRIMARY KEY,
            limit_amount REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Инициализация базы данных...")



def add_expense(category: str, amount: float):
    ts = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (category, amount, timestamp) VALUES (?, ?, ?)",
        (category, amount, ts),
    )
    conn.commit()
    conn.close()


def set_limits(limits: dict[str, float]):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for category, limit_value in limits.items():
        cursor.execute(
            """
            INSERT INTO limits (category, limit_amount)
            VALUES (?, ?)
            ON CONFLICT(category) DO UPDATE SET limit_amount = excluded.limit_amount
            """,
            (category, limit_value),
        )
    conn.commit()
    conn.close()


def get_limits() -> dict[str, float]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT category, limit_amount FROM limits")
    rows = cursor.fetchall()
    conn.close()
    return {cat: lim for cat, lim in rows}


def get_month_range_utc() -> tuple[str, str]:
    """Возвращает начало и конец текущего месяца в UTC в виде ISO-строк."""
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1)
    else:
        next_month = datetime(now.year, now.month + 1, 1)
    return month_start.isoformat(), next_month.isoformat()


def get_month_stats():
    """Статистика по текущему месяцу: сумма по категориям, общий итог."""
    start_iso, end_iso = get_month_range_utc()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT category, SUM(amount)
        FROM expenses
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY category
        ORDER BY SUM(amount) DESC
        """,
        (start_iso, end_iso),
    )
    rows = cursor.fetchall()
    conn.close()

    stats = {cat: float(total) for cat, total in rows}
    total_sum = sum(stats.values())
    return stats, total_sum


def get_full_stats():
    """Статистика за всё время."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT category, SUM(amount)
        FROM expenses
        GROUP BY category
        ORDER BY SUM(amount) DESC
        """
    )
    rows = cursor.fetchall()

    cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM expenses")
    date_row = cursor.fetchone()
    conn.close()

    stats = {cat: float(total) for cat, total in rows}
    total_sum = sum(stats.values())
    min_ts, max_ts = date_row if date_row else (None, None)
    return stats, total_sum, min_ts, max_ts


# ==========================
#  ЭКСПОРТ В EXCEL
# ==========================

def export_to_excel() -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "Расходы"

    # Заголовки в нужном формате
    ws.append(["Дата", "Категория", "Сумма"])

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category, amount, timestamp FROM expenses ORDER BY timestamp ASC"
    )
    rows = cursor.fetchall()
    conn.close()

    for category, amount, timestamp in rows:
        dt = datetime.fromisoformat(timestamp)
        date_str = dt.strftime("%d/%m/%Y")  # ДД/ММ/ГГГГ
        ws.append([date_str, category, amount])

    # Автоширина
    for col in ws.columns:
        max_len = 0
        column = col[0].column_letter
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[column].width = max_len + 2

    export_file = "export.xlsx"
    wb.save(export_file)
    return export_file


# ==========================
#  ПАРСИНГ СТРОК
# ==========================

def parse_lines_to_pairs(text: str):
    """
    Парсит блок текста в формат:
    Категория-Сумма
    Категория2-Сумма2
    Возвращает (список_пар, список_ошибок).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    pairs: list[tuple[str, float]] = []
    errors: list[str] = []

    for line in lines:
        if "-" not in line:
            errors.append(f"Не найден разделитель '-' в строке: «{line}»")
            continue
        cat_part, amount_part = line.split("-", 1)
        category = cat_part.strip()
        amount_str = amount_part.replace(",", ".").strip()

        if not category or not amount_str:
            errors.append(f"Неверный формат строки: «{line}»")
            continue

        try:
            amount = float(amount_str)
        except ValueError:
            errors.append(f"Сумма не число в строке: «{line}»")
            continue

        if amount <= 0:
            errors.append(f"Сумма должна быть > 0 в строке: «{line}»")
            continue

        pairs.append((category, amount))

    return pairs, errors


# ==========================
#  ХЕНДЛЕРЫ
# ==========================

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "Привет, {name}! Я бот для учёта расходов.\n\n"
        "Отправь мне строку в формате:\n"
        "`Категория-Сумма`\n"
        "или используй команду /insert для ввода списка.\n\n"
        "Команда /limit — для установки лимитов по категориям.\n"
        "Команда /stats — краткая статистика по месяцу.\n"
        "Команда /analitick — расширенная аналитика.\n"
        "Команда /make — сформировать Excel-отчёт.\n"
        "Команда /export — выгрузить Excel-таблицу."
    ).format(name=message.from_user.first_name or "")

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📋 *Команды бота:*\n\n"
        "*Обычный ввод:*\n"
        "`Категория-Сумма`\n"
        "Например: `Еда-500`\n\n"
        "*/insert* — режим ввода списка расходов.\n"
        "После команды отправь несколько строк вида:\n"
        "`Еда-500`\n"
        "`Такси-300`\n"
        "`Кофе-200`\n\n"
        "*(/limit)* — установка лимитов по категориям.\n"
        "Формат такой же, можно несколько строк.\n"
        "Пример:\n"
        "`Еда-20000`\n"
        "`Такси-5000`\n\n"
        "*(/stats)* — статистика за текущий месяц по категориям "
        "и сравнение с лимитами.\n"
        "*(/analitick)* — расширенная аналитика: доли категорий, средний расход в день.\n"
        "*(/make)* — сформировать и отправить Excel-отчёт (то же самое, что /export).\n"
        "*(/export)* — выгрузка всех расходов в Excel."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


# --------- /insert ----------

@router.message(Command("insert"))
async def cmd_insert(message: Message, state: FSMContext):
    await state.set_state(InsertStates.waiting_for_expenses)
    text = (
        "Отправь список расходов в формате:\n"
        "`Категория-Сумма`\n"
        "Можно сразу несколько строк:\n"
        "`Еда-500`\n"
        "`Такси-300`\n"
        "`Кофе-200`"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(InsertStates.waiting_for_expenses)
async def process_insert_list(message: Message, state: FSMContext):
    pairs, errors = parse_lines_to_pairs(message.text)

    if not pairs and errors:
        err_text = "⚠ Ошибка при разборе списка:\n" + "\n".join(errors)
        await message.answer(err_text)
        return

    for category, amount in pairs:
        add_expense(category, amount)

    resp_lines = [f"✅ Добавлено записей: {len(pairs)}"]
    if errors:
        resp_lines.append("\n⚠ Не удалось обработать некоторые строки:")
        resp_lines.extend(errors)

    await message.answer("\n".join(resp_lines))
    await state.clear()


# --------- /limit ----------

@router.message(Command("limit"))
async def cmd_limit(message: Message, state: FSMContext):
    await state.set_state(LimitStates.waiting_for_limits)
    text = (
        "Отправь список лимитов в формате:\n"
        "`Категория-Сумма`\n"
        "Можно сразу несколько строк:\n"
        "`Еда-20000`\n"
        "`Такси-5000`"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(LimitStates.waiting_for_limits)
async def process_limit_list(message: Message, state: FSMContext):
    pairs, errors = parse_lines_to_pairs(message.text)

    if not pairs and errors:
        err_text = "⚠ Ошибка при разборе списка лимитов:\n" + "\n".join(errors)
        await message.answer(err_text)
        return

    limits_dict = {cat: amount for cat, amount in pairs}
    set_limits(limits_dict)

    resp_lines = [f"✅ Обновлено лимитов: {len(limits_dict)}"]
    if errors:
        resp_lines.append("\n⚠ Не удалось обработать некоторые строки:")
        resp_lines.extend(errors)

    await message.answer("\n".join(resp_lines))
    await state.clear()


# --------- /stats ----------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    stats, total_sum = get_month_stats()
    limits = get_limits()

    if not stats:
        await message.answer("За текущий месяц расходов пока нет.")
        return

    lines = ["📊 *Статистика за текущий месяц:*", ""]
    for cat, spent in stats.items():
        line = f"• {cat}: {spent:.2f}"
        if cat in limits:
            limit_val = limits[cat]
            diff = limit_val - spent
            if diff >= 0:
                line += f" из {limit_val:.2f} (осталось {diff:.2f})"
            else:
                line += f" из {limit_val:.2f} (перерасход {abs(diff):.2f})"
        lines.append(line)

    lines.append("")
    lines.append(f"Итого: *{total_sum:.2f}*")

    await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# --------- /analitick ----------

@router.message(Command("analitick"))
async def cmd_analitick(message: Message):
    stats, total_sum, min_ts, max_ts = get_full_stats()

    if not stats:
        await message.answer("Пока нет ни одной записи о расходах.")
        return

    # Период
    if min_ts and max_ts:
        start_dt = datetime.fromisoformat(min_ts)
        end_dt = datetime.fromisoformat(max_ts)
        days = max((end_dt.date() - start_dt.date()).days + 1, 1)
    else:
        days = 1

    avg_per_day = total_sum / days

    lines = [
        "📈 *Аналитика расходов за всё время:*",
        "",
        f"Всего потрачено: *{total_sum:.2f}*",
        f"Период: ~{days} дн.",
        f"Средний расход в день: *{avg_per_day:.2f}*",
        "",
        "Доли категорий:",
    ]

    for cat, value in sorted(stats.items(), key=lambda x: x[1], reverse=True):
        share = (value / total_sum) * 100 if total_sum > 0 else 0
        lines.append(f"• {cat}: {value:.2f} ({share:.1f}%)")

    await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# --------- /export и /make ----------

@router.message(Command("export"))
async def cmd_export(message: Message):
    file_path = export_to_excel()
    doc = FSInputFile(file_path)
    await message.answer_document(doc, caption="Экспорт расходов в Excel.")


@router.message(Command("make"))
async def cmd_make(message: Message):
    """
    Делает то же самое, что /export — формирует Excel-отчёт.
    Если захочешь другое поведение — переделаем.
    """
    file_path = export_to_excel()
    doc = FSInputFile(file_path)
    await message.answer_document(doc, caption="Сформирован отчёт (Excel).")


# --------- ОБЫЧНЫЙ ВВОД "Категория-Сумма" ----------

@router.message(F.text)
async def handle_single_line(message: Message, state: FSMContext):
    """
    Обрабатывает одиночную строку вне режимов /insert и /limit.
    Формат: Категория-Сумма
    """
    # если мы в каком-то состоянии FSM — не трогаем (там свои хендлеры)
    current_state = await state.get_state()
    if current_state is not None:
        return

    pairs, errors = parse_lines_to_pairs(message.text)

    if not pairs and errors:
        err_text = (
            "⚠ Ошибка: Не удалось найти корректную строку в сообщении.\n\n"
            "Пример правильного формата:\n"
            "`Еда-500`\n"
            "`Такси-300`\n"
            "`Кофе-200`"
        )
        await message.answer(err_text, parse_mode=ParseMode.MARKDOWN)
        return

    # Здесь ожидаем, что пользователь отправил одну строку
    category, amount = pairs[0]
    add_expense(category, amount)
    await message.answer(f"✅ Записал: {category} — {amount:.2f}")


# ==========================
#  MAIN
# ==========================

async def main():
    logger.info("Запускаем DuckLedger...")

    if not BOT_TOKEN:
        raise RuntimeError("Укажи токен бота в BOT_TOKEN или переменной TELEGRAM_TOKEN")

    init_db()

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Для Render: обычный polling (без вебхуков)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())



