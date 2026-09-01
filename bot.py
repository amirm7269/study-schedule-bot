import asyncio
import logging
import os
import re
from datetime import datetime, date, time as dtime

from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TZ = ZoneInfo("Asia/Tehran")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("study-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)

LINE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+(.+?)\s*$")


# ---------------------------------------------------------------------------
# Database (PostgreSQL - persists across Railway redeploys, unlike local files)
# ---------------------------------------------------------------------------

def db_connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def db_init():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            user_id BIGINT NOT NULL,
            report_date DATE NOT NULL,
            tests_count INTEGER,
            study_hours REAL,
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (user_id, report_date)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_items (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            item_date DATE NOT NULL,
            item_time TEXT NOT NULL,
            subject TEXT NOT NULL
        )
        """
    )
    cur.close()
    conn.close()


def save_schedule_item(user_id: int, item_date: str, item_time: str, subject: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO schedule_items (user_id, item_date, item_time, subject) VALUES (%s, %s, %s, %s)",
        (user_id, item_date, item_time, subject),
    )
    cur.close()
    conn.close()


def get_schedule_items(user_id: int, item_date: str):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT item_time, subject FROM schedule_items WHERE user_id=%s AND item_date=%s ORDER BY item_time",
        (user_id, item_date),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def save_report(user_id: int, report_date: str, tests_count: int, study_hours: float, note: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reports (user_id, report_date, tests_count, study_hours, note, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, report_date) DO UPDATE SET
            tests_count = EXCLUDED.tests_count,
            study_hours = EXCLUDED.study_hours,
            note = EXCLUDED.note,
            created_at = EXCLUDED.created_at
        """,
        (user_id, report_date, tests_count, study_hours, note, datetime.now(TZ)),
    )
    cur.close()
    conn.close()


def get_report(user_id: int, report_date: str):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM reports WHERE user_id=%s AND report_date=%s",
        (user_id, report_date),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class ReportForm(StatesGroup):
    tests_count = State()
    study_hours = State()
    note = State()


class ScheduleForm(StatesGroup):
    waiting_for_lines = State()


class AlarmForm(StatesGroup):
    waiting_for_time = State()


class HistoryForm(StatesGroup):
    waiting_for_date = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 برنامه امشب", callback_data="menu_schedule")],
            [InlineKeyboardButton(text="⏰ تنظیم آلارم", callback_data="menu_alarm")],
            [InlineKeyboardButton(text="📊 ثبت گزارش امشب", callback_data="menu_report")],
            [InlineKeyboardButton(text="📈 عملکرد یک تاریخ", callback_data="menu_history")],
        ]
    )


# ---------------------------------------------------------------------------
# Reminder job
# ---------------------------------------------------------------------------

async def send_reminder(chat_id: int, subject: str):
    try:
        await bot.send_message(chat_id, f"⏰ وقتشه: {subject}")
    except Exception as e:
        log.warning("failed to send reminder: %s", e)


async def send_alarm(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, f"🔔 آلارم: {text}")
    except Exception as e:
        log.warning("failed to send alarm: %s", e)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "سلام! من ربات برنامه‌ی درسیتم.\n\n"
        "هر شب می‌تونی برنامه‌ت رو بدی تا سر وقت یادآوری بفرستم، "
        "آلارم دستی هم می‌تونی بذاری، و آخر شب گزارش بدی تا بعداً بتونی عملکرد هر روز رو ببینی.",
        reply_markup=main_menu_kb(),
    )


@dp.callback_query(F.data == "menu_schedule")
async def cb_menu_schedule(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(ScheduleForm.waiting_for_lines)
    await call.message.answer(
        "برنامه امشب رو بفرست، هر خط یه درس با این فرمت:\n\n"
        "`21:00 ریاضی`\n`22:00 فیزیک`\n`23:30 پایان مطالعه`\n\n"
        "همه خط‌ها رو تو یه پیام بفرست.",
        parse_mode="Markdown",
    )


@dp.message(ScheduleForm.waiting_for_lines)
async def handle_schedule_lines(message: Message, state: FSMContext):
    lines = message.text.strip().splitlines()
    today = date.today().isoformat()
    now = datetime.now(TZ)

    scheduled = []
    skipped = []
    invalid = []

    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            invalid.append(line)
            continue
        hh, mm, subject = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            run_dt = datetime.combine(now.date(), dtime(hour=hh, minute=mm), tzinfo=TZ)
        except ValueError:
            invalid.append(line)
            continue

        if run_dt <= now:
            skipped.append(line)
            continue

        save_schedule_item(message.from_user.id, today, f"{hh:02d}:{mm:02d}", subject)
        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=run_dt),
            args=[message.chat.id, subject],
        )
        scheduled.append(f"{hh:02d}:{mm:02d} — {subject}")

    reply = ""
    if scheduled:
        reply += "✅ یادآوری‌های زیر تنظیم شد:\n" + "\n".join(scheduled) + "\n\n"
    if skipped:
        reply += "⏭ این‌ها چون ساعتشون گذشته رد شدن:\n" + "\n".join(skipped) + "\n\n"
    if invalid:
        reply += "⚠️ فرمت این خط‌ها درست نبود:\n" + "\n".join(invalid)

    await state.clear()
    await message.answer(reply.strip() or "چیزی برای تنظیم پیدا نشد.", reply_markup=main_menu_kb())


@dp.callback_query(F.data == "menu_alarm")
async def cb_menu_alarm(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AlarmForm.waiting_for_time)
    await call.message.answer(
        "ساعت آلارم رو بفرست، به فرمت `HH:MM` یا `HH:MM متن پیام`.\nمثال: `23:15` یا `23:15 وقت جمع‌بندیه`",
        parse_mode="Markdown",
    )


@dp.message(AlarmForm.waiting_for_time)
async def handle_alarm_time(message: Message, state: FSMContext):
    m = LINE_RE.match(message.text.strip()) or re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", message.text.strip())
    if not m:
        await message.answer("فرمت درست نیست، دوباره امتحان کن. مثال: `23:15` یا `23:15 وقت استراحت`", parse_mode="Markdown")
        return

    hh, mm = int(m.group(1)), int(m.group(2))
    text = m.group(3) if len(m.groups()) >= 3 and m.group(3) else "وقتشه!"
    now = datetime.now(TZ)
    run_dt = datetime.combine(now.date(), dtime(hour=hh, minute=mm), tzinfo=TZ)
    if run_dt <= now:
        run_dt = run_dt.replace(day=run_dt.day)  # same day only, per current design
        await message.answer("این ساعت گذشته، یه ساعت جلوتر رو بفرست.")
        await state.clear()
        return

    scheduler.add_job(send_alarm, trigger=DateTrigger(run_date=run_dt), args=[message.chat.id, text])
    await state.clear()
    await message.answer(f"🔔 آلارم برای {hh:02d}:{mm:02d} تنظیم شد.", reply_markup=main_menu_kb())


@dp.callback_query(F.data == "menu_report")
async def cb_menu_report(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(ReportForm.tests_count)
    await call.message.answer("امشب چند تا تست زدی؟ (عدد بفرست، اگه نزدی 0)")


@dp.message(ReportForm.tests_count)
async def handle_tests_count(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("لطفاً یه عدد بفرست.")
        return
    await state.update_data(tests_count=int(message.text.strip()))
    await state.set_state(ReportForm.study_hours)
    await message.answer("چند ساعت مطالعه کردی؟ (مثلاً 3.5)")


@dp.message(ReportForm.study_hours)
async def handle_study_hours(message: Message, state: FSMContext):
    try:
        hours = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("لطفاً یه عدد بفرست، مثلاً 3.5")
        return
    await state.update_data(study_hours=hours)
    await state.set_state(ReportForm.note)
    await message.answer("یه توضیح کوتاه اگه بخوای بنویس، یا بنویس - اگه نداری")


@dp.message(ReportForm.note)
async def handle_note(message: Message, state: FSMContext):
    data = await state.get_data()
    note = message.text.strip()
    if note == "-":
        note = ""
    today = date.today().isoformat()
    save_report(message.from_user.id, today, data["tests_count"], data["study_hours"], note)
    await state.clear()
    await message.answer(
        f"✅ گزارش امروز ({today}) ثبت شد:\n"
        f"تعداد تست: {data['tests_count']}\n"
        f"ساعت مطالعه: {data['study_hours']}\n"
        f"توضیح: {note or '—'}",
        reply_markup=main_menu_kb(),
    )


@dp.callback_query(F.data == "menu_history")
async def cb_menu_history(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(HistoryForm.waiting_for_date)
    await call.message.answer("تاریخ رو به فرمت `YYYY-MM-DD` بفرست، مثلاً `2026-08-30`", parse_mode="Markdown")


@dp.message(HistoryForm.waiting_for_date)
async def handle_history_date(message: Message, state: FSMContext):
    q = message.text.strip()
    try:
        datetime.strptime(q, "%Y-%m-%d")
    except ValueError:
        await message.answer("فرمت تاریخ درست نیست. مثال: `2026-08-30`", parse_mode="Markdown")
        return

    await state.clear()
    row = get_report(message.from_user.id, q)
    items = get_schedule_items(message.from_user.id, q)

    if not row and not items:
        await message.answer(f"برای {q} چیزی ثبت نشده.", reply_markup=main_menu_kb())
        return

    reply = f"📈 عملکرد {q}\n\n"
    if row:
        reply += (
            f"تعداد تست: {row['tests_count']}\n"
            f"ساعت مطالعه: {row['study_hours']}\n"
            f"توضیح: {row['note'] or '—'}\n\n"
        )
    else:
        reply += "گزارش پایان‌شب ثبت نشده.\n\n"

    if items:
        reply += "برنامه اون شب:\n" + "\n".join(f"{r['item_time']} — {r['subject']}" for r in items)

    await message.answer(reply.strip(), reply_markup=main_menu_kb())


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("لغو شد.", reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    db_init()
    scheduler.start()
    log.info("bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is not set")
    asyncio.run(main())
