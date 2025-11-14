import asyncio
import logging
import sqlite3
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import List, Tuple, Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BufferedInputFile,
)
from openpyxl import Workbook

# ===================== НАСТРОЙКИ =====================

BOT_TOKEN = "8368098253:AAEU2FWiQkiQTR42GKgg_8OCqm7mOXdsvOA"
DB_PATH = "budget.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()


# ===================== FSM СОСТОЯНИЯ =====================

class InsertState(StatesGroup):
    waiting_data = State()


class LimitState(StatesGroup):
    waiting_limits = State()


# ===================== РАБОТА С БД =====================

def init_db():
    logger.info("Инициализация базы данных...")
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                dt TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                limit_amount REAL NOT NULL,
                UNIQUE(user_id, category)
            )
            """
        )
        conn.commit()


def add_expenses_db(user_id: int, items: List[Tuple[str, float]], dt: date | None = None):
    """Добавить несколько расходов за одну дату."""
    if dt is None:
        dt = date.today()
    dt_str = dt.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        for category, amount in items:
            cur.execute(
                """
                INSERT INTO expenses (user_id, dt, category, amount)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, dt_str, category, amount),
            )
        conn.commit()


def set_limits_db(user_id: int, items: List[Tuple[str, float]]):
    """Установить/обновить лимиты по категориям."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        for category, limit_amount in items:
            cur.execute(
                """
                INSERT INTO limits (user_id, category, limit_amount)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, category)
                DO UPDATE SET limit_amount = excluded.limit_amount
                """,
                (user_id, category, limit_amount),
            )
        conn.commit()


def get_limits_db(user_id: int) -> List[Tuple[str, float]]:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT category, limit_amount
            FROM limits
            WHERE user_id = ?
            ORDER BY category
            """,
            (user_id,),
        )
        return cur.fetchall()


def get_current_month_range() -> Tuple[date, date]:
    today = date.today()
    start = date(today.year, today.month, 1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    end = next_month - timedelta(days=1)
    return start, end


def get_month_expenses_by_category(user_id: int) -> Dict[str, float]:
    start, end = get_current_month_range()
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT category, SUM(amount)
            FROM expenses
            WHERE user_id = ? AND dt BETWEEN ? AND ?
            GROUP BY category
            ORDER BY SUM(amount) DESC
            """,
            (user_id, start_str, end_str),
        )
        rows = cur.fetchall()

    return {cat: total for cat, total in rows}


def get_month_dates_and_categories(user_id: int):
    start, end = get_current_month_range()
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT dt, category, SUM(amount)
            FROM expenses
            WHERE user_id = ? AND dt BETWEEN ? AND ?
            GROUP BY dt, category
            ORDER BY dt ASC
            """,
            (user_id, start_str, end_str),
        )
        rows = cur.fetchall()

    # dt -> {category: amount}
    data: Dict[str, Dict[str, float]] = {}
    categories = set()
    for dt_str, category, amount in rows:
        categories.add(category)
        data.setdefault(dt_str, {})
        data[dt_str][category] = amount

    dates_sorted = sorted(data.keys())
    categories_sorted = sorted(categories)
    return dates_sorted, categories_sorted, data


def get_month_categories(user_id: int) -> List[str]:
    start, end = get_current_month_range()
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT category
            FROM expenses
            WHERE user_id = ? AND dt BETWEEN ? AND ?
            ORDER BY category
            """,
            (user_id, start_str, end_str),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]


def clear_user_data(user_id: int):
    """Удалить ВСЕ расходы пользователя (лимиты НЕ трогаем)."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
        conn.commit()


# ===================== ПАРСИНГ ВВОДА =====================

class ParseError(Exception):
    pass


def parse_lines_category_amount(text: str) -> List[Tuple[str, float]]:
    """
    Парсит многострочный текст формата:
    Категория-Сумма
    Категория - Сумма
    Категория—Сумма
    Возвращает список (category, amount).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ParseError("Пустой ввод.")

    result: List[Tuple[str, float]] = []

    for line in lines:
        sep_index = -1
        for sep in ["-", "—", "–"]:
            if sep in line:
                sep_index = line.find(sep)
                break
        if sep_index == -1:
            raise ParseError(f"Не удалось найти разделитель '-' в строке: «{line}»")

        category = line[:sep_index].strip()
        amount_str = line[sep_index + 1 :].strip().replace(" ", "").replace(",", ".")

        if not category:
            raise ParseError(f"Не указана категория в строке: «{line}»")
        if not amount_str:
            raise ParseError(f"Не указана сумма в строке: «{line}»")

        try:
            amount = float(amount_str)
        except ValueError:
            raise ParseError(f"Не удалось распознать сумму в строке: «{line}»")

        if amount <= 0:
            raise ParseError(f"Сумма должна быть больше 0: «{line}»")

        result.append((category, amount))

    return result


# ===================== КЛАВИАТУРА =====================

def main_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [
            KeyboardButton(text="/insert"),
            KeyboardButton(text="/analitick"),
        ],
        [
            KeyboardButton(text="/stats"),
            KeyboardButton(text="/categories"),
        ],
        [
            KeyboardButton(text="/export"),
            KeyboardButton(text="/make"),
        ],
        [
            KeyboardButton(text="/limit"),
            KeyboardButton(text="/help"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Категория-Сумма или команда...",
    )


# ===================== ХЕНДЛЕРЫ =====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n\n"
        "Я бот для учёта расходов.\n"
        "Просто отправь мне строки формата:\n"
        "<b>Категория-Сумма</b>\n"
        "или используй команду /insert.\n\n"
        "Команда /limit — для установки лимитов по категориям.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "<b>Справка по DuckLedger</b>\n\n"
        "📌 <b>Как вносить расходы</b>\n"
        "— Просто напиши сообщение вида:\n"
        "  <code>Еда-500</code>\n"
        "— Можно сразу несколько строк в одном сообщении:\n"
        "  <code>Еда-500\nТакси-300\nКофе-200</code>\n"
        "— Либо используй команду /insert — бот сам попросит формат.\n\n"
        "📌 <b>Лимиты по категориям</b> — /limit\n"
        "Отправь список строк:\n"
        "<code>Еда-20000\nТакси-5000\nРазвлечения-10000</code>\n"
        "Лимиты можно обновлять — старое значение перезапишется.\n\n"
        "📌 <b>Команды</b>\n"
        "/start — стартовое сообщение и клавиатура\n"
        "/insert — пошаговый ввод расходов (одним или несколькими рядами)\n"
        "/analitick — аналитика за текущий месяц (итог, топ-3, проценты)\n"
        "/stats — краткая сводка по категориям за месяц\n"
        "/categories — список категорий за месяц\n"
        "/export — выгрузка таблицы (даты по строкам, категории по столбцам)\n"
        "/make — выгрузка текущей таблицы и очистка данных (начать с нуля)\n"
        "/limit — задать/обновить лимит по категориям\n"
        "/help — это сообщение\n"
    )
    await message.answer(text)


# ---------- /insert ----------

@router.message(Command("insert"))
async def cmd_insert(message: Message, state: FSMContext):
    await state.set_state(InsertState.waiting_data)
    await message.answer(
        "Отправь список расходов в формате:\n"
        "<code>Категория-Сумма</code>\n"
        "Можно сразу несколько строк:\n"
        "<code>Еда-500\nТакси-300\nКофе-200</code>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(InsertState.waiting_data)
async def process_insert_data(message: Message, state: FSMContext):
    try:
        items = parse_lines_category_amount(message.text)
    except ParseError as e:
        await message.answer(
            f"⚠ Ошибка: {e}\n\n"
            "Пример правильного формата:\n"
            "<code>Еда-500\nТакси-300\nКофе-200</code>"
        )
        return

    add_expenses_db(message.from_user.id, items)
    total = sum(a for _, a in items)
    lines = [f"• {cat}: {amount:.2f} ₽" for cat, amount in items]
    await message.answer(
        "✅ Добавлены расходы:\n" + "\n".join(lines) + f"\n\nИтого по сообщению: {total:.2f} ₽",
        reply_markup=main_keyboard(),
    )
    await state.clear()


# ---------- /limit ----------

@router.message(Command("limit"))
async def cmd_limit(message: Message, state: FSMContext):
    await state.set_state(LimitState.waiting_limits)
    await message.answer(
        "Отправь лимиты по категориям в формате:\n"
        "<code>Еда-20000\nТакси-5000\nРазвлечения-10000</code>\n\n"
        "Каждая строка: <code>Категория-Лимит</code>.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(LimitState.waiting_limits)
async def process_limits(message: Message, state: FSMContext):
    try:
        items = parse_lines_category_amount(message.text)
    except ParseError as e:
        await message.answer(
            f"⚠ Ошибка: {e}\n\n"
            "Пример правильного формата:\n"
            "<code>Еда-20000\nТакси-5000</code>"
        )
        return

    set_limits_db(message.from_user.id, items)
    limits = get_limits_db(message.from_user.id)

    lines_new = [f"• {cat}: {limit:.2f} ₽" for cat, limit in items]
    lines_all = [f"• {cat}: {limit:.2f} ₽" for cat, limit in limits]

    text = (
        "✅ Лимиты обновлены:\n" + "\n".join(lines_new) +
        "\n\nТекущие лимиты по всем категориям:\n" +
        ("\n".join(lines_all) if lines_all else "— пока нет ни одного лимита.")
    )

    await message.answer(text, reply_markup=main_keyboard())
    await state.clear()


# ---------- /analitick ----------

@router.message(Command("analitick"))
async def cmd_analitick(message: Message):
    stats = get_month_expenses_by_category(message.from_user.id)
    if not stats:
        await message.answer("За текущий месяц пока нет данных.")
        return

    total = sum(stats.values())
    start, _ = get_current_month_range()
    month_str = start.strftime("%m.%Y")

    # Топ-3
    sorted_items = sorted(stats.items(), key=lambda x: x[1], reverse=True)
    top3 = sorted_items[:3]

    lines_top = [f"{i+1}) {cat}: {amount:.2f} ₽" for i, (cat, amount) in enumerate(top3)]
    lines_pct = [
        f"• {cat}: {amount:.2f} ₽ ({amount / total * 100:.1f}%)"
        for cat, amount in sorted_items
    ]

    text = (
        f"<b>📊 Аналитика за {month_str}</b>\n\n"
        f"Всего потрачено: <b>{total:.2f} ₽</b>\n\n"
        "Топ-3 категории:\n" +
        ("\n".join(lines_top) if lines_top else "—") +
        "\n\nВсе категории:\n" +
        "\n".join(lines_pct)
    )
    await message.answer(text)


# ---------- /stats ----------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = get_month_expenses_by_category(message.from_user.id)
    if not stats:
        await message.answer("За текущий месяц пока нет данных.")
        return

    total = sum(stats.values())
    start, _ = get_current_month_range()
    month_str = start.strftime("%m.%Y")

    lines = [f"• {cat}: {amount:.2f} ₽" for cat, amount in stats.items()]

    text = (
        f"<b>Краткая сводка за {month_str}</b>\n\n"
        f"Всего: <b>{total:.2f} ₽</b>\n\n" +
        "\n".join(lines)
    )
    await message.answer(text)


# ---------- /categories ----------

@router.message(Command("categories"))
async def cmd_categories(message: Message):
    cats = get_month_categories(message.from_user.id)
    if not cats:
        await message.answer("За текущий месяц нет категорий.")
        return

    text = "<b>Категории за текущий месяц:</b>\n" + "\n".join(f"• {c}" for c in cats)
    await message.answer(text)


# ---------- /export ----------

@router.message(Command("export"))
async def cmd_export(message: Message):
    dates, categories, data = get_month_dates_and_categories(message.from_user.id)
    if not dates or not categories:
        await message.answer("Нет данных для экспорта за текущий месяц.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "Expenses"

    # Заголовок
    ws.cell(row=1, column=1, value="Дата")
    for col, cat in enumerate(categories, start=2):
        ws.cell(row=1, column=col, value=cat)

    # Данные
    for row_idx, dt_str in enumerate(dates, start=2):
        # дата текстом в формате dd.MM.yyyy
        d = datetime.strptime(dt_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        ws.cell(row=row_idx, column=1, value=d)

        for col_idx, cat in enumerate(categories, start=2):
            value = data.get(dt_str, {}).get(cat)
            if value is not None:
                ws.cell(row=row_idx, column=col_idx, value=float(value))

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    start, _ = get_current_month_range()
    fname = f"duckledger_{start.year}_{start.month:02d}.xlsx"

    await message.answer_document(
        BufferedInputFile(bio.read(), filename=fname),
        caption="Экспорт за текущий месяц. Сохрани файл на ПК, чтобы не потерять данные.",
    )


# ---------- /make ----------

@router.message(Command("make"))
async def cmd_make(message: Message):
    """
    Логика:
    1) Сначала делаем экспорт как в /export.
    2) Отправляем файл.
    3) После этого очищаем все расходы пользователя.
    """
    user_id = message.from_user.id
    dates, categories, data = get_month_dates_and_categories(user_id)

    if dates and categories:
        wb = Workbook()
        ws = wb.active
        ws.title = "Expenses"

        ws.cell(row=1, column=1, value="Дата")
        for col, cat in enumerate(categories, start=2):
            ws.cell(row=1, column=col, value=cat)

        for row_idx, dt_str in enumerate(dates, start=2):
            d = datetime.strptime(dt_str, "%Y-%m-%d").strftime("%d.%m.%Y")
            ws.cell(row=row_idx, column=1, value=d)

            for col_idx, cat in enumerate(categories, start=2):
                value = data.get(dt_str, {}).get(cat)
                if value is not None:
                    ws.cell(row=row_idx, column=col_idx, value=float(value))

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        start_d, _ = get_current_month_range()
        fname = f"duckledger_{start_d.year}_{start_d.month:02d}_final.xlsx"

        await message.answer_document(
            BufferedInputFile(bio.read(), filename=fname),
            caption=(
                "Финальный экспорт текущей таблицы.\n"
                "Сохрани файл на ПК. После этого данные в боте будут очищены."
            ),
        )
    else:
        await message.answer("Данных за текущий месяц мало или нет, но я всё равно очищу таблицу.")

    # Очистка расходов
    clear_user_data(user_id)
    await message.answer(
        "🧹 Все данные по расходам в боте очищены.\n"
        "Можно начинать новый период с чистого листа — просто отправляй новые записи.",
        reply_markup=main_keyboard(),
    )


# ---------- АВТО-ВВОД БЕЗ КОМАНД ----------

@router.message(
    F.text & ~F.text.startswith("/")  # любые тексты, не команды
)
async def auto_insert(message: Message, state: FSMContext):
    """
    Автоматический ввод: если текст похож на "Категория-Сумма" (одна или несколько строк),
    пытаемся распарсить и записать как расходы.
    """
    # Если сейчас ждём данные /insert или /limit — не перехватываем здесь
    current_state = await state.get_state()
    if current_state in (InsertState.waiting_data.state, LimitState.waiting_limits.state):
        return

    try:
        items = parse_lines_category_amount(message.text)
    except ParseError:
        # Просто игнорируем, не засоряем чат
        return

    add_expenses_db(message.from_user.id, items)
    total = sum(a for _, a in items)
    lines = [f"• {cat}: {amount:.2f} ₽" for cat, amount in items]

    await message.answer(
        "✅ Добавлены расходы:\n" + "\n".join(lines) + f"\n\nИтого по сообщению: {total:.2f} ₽",
        reply_markup=main_keyboard(),
    )


# ===================== MAIN =====================

async def main():
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Запускаем DuckLedger...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
