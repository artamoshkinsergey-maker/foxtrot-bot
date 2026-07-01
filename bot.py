import asyncio
import logging
import os
import re
import json
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import gspread
from google.oauth2.service_account import Credentials

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
CACHE_TTL = 300

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_cache = {
    "handbook": {"data": None, "ts": 0},
    "quiz":     {"data": None, "ts": 0},
}


def get_sheet():
    google_creds = os.getenv("GOOGLE_CREDENTIALS")
    if google_creds:
        creds = Credentials.from_service_account_info(json.loads(google_creds), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def convert_drive_link(url):
    if not url:
        return None
    match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if match:
        return f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    if url.startswith("http"):
        return url
    return None


def get_handbook_data(force=False):
    now = time.time()
    if not force and _cache["handbook"]["data"] and now - _cache["handbook"]["ts"] < CACHE_TTL:
        return _cache["handbook"]["data"]
    try:
        sheet = get_sheet().worksheet("Довідник")
        rows = sheet.get_all_records()
        data = {}
        for row in rows:
            cat   = str(row.get("Категорія", "")).strip()
            name  = str(row.get("Назва", "")).strip()
            desc  = str(row.get("Опис", "")).strip()
            photo = str(row.get("Фото", "")).strip()
            if cat and name:
                data.setdefault(cat, []).append((name, desc, photo))
        _cache["handbook"] = {"data": data, "ts": now}
        return data
    except Exception as e:
        logger.error(f"Помилка читання довідника: {e}")
        return _cache["handbook"]["data"] or {}


def get_quiz_questions(force=False):
    now = time.time()
    if not force and _cache["quiz"]["data"] and now - _cache["quiz"]["ts"] < CACHE_TTL:
        return _cache["quiz"]["data"]
    try:
        sheet = get_sheet().worksheet("Тест")
        rows = sheet.get_all_records()
        questions = []
        for row in rows:
            q = {
                "question":    str(row.get("Питання", "")).strip(),
                "a":           str(row.get("A", "")).strip(),
                "b":           str(row.get("B", "")).strip(),
                "c":           str(row.get("C", "")).strip(),
                "d":           str(row.get("D", "")).strip(),
                "correct":     str(row.get("Правильна відповідь", "")).strip().upper(),
                "explanation": str(row.get("Пояснення", "")).strip(),
            }
            if q["question"]:
                questions.append(q)
        _cache["quiz"] = {"data": questions, "ts": now}
        return questions
    except Exception as e:
        logger.error(f"Помилка читання тесту: {e}")
        return _cache["quiz"]["data"] or []


def save_quiz_result(user_id, username, full_name, score, total, wrong_topics):
    try:
        sheet = get_sheet().worksheet("Результати")
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        percent = round(score / total * 100) if total else 0
        sheet.append_row([
            now, user_id, username or "", full_name or "",
            score, total, f"{percent}%",
            ", ".join(wrong_topics) if wrong_topics else "—",
        ])
    except Exception as e:
        logger.error(f"Помилка збереження результату: {e}")


class QuizState(StatesGroup):
    in_quiz = State()


def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Довідник",     callback_data="handbook")],
        [InlineKeyboardButton(text="🔍 Пошук",        callback_data="search")],
        [InlineKeyboardButton(text="📝 Пройти тест",  callback_data="quiz_start")],
        [InlineKeyboardButton(text="🔄 Оновити дані", callback_data="refresh")],
    ])


def categories_kb(categories):
    buttons = [[InlineKeyboardButton(text=cat, callback_data=f"cat:{cat}")] for cat in categories]
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_categories_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад до категорій", callback_data="handbook")],
        [InlineKeyboardButton(text="🏠 Головне меню",       callback_data="main_menu")],
    ])


def items_kb(items, category):
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"item:{category}:{i}")]
               for i, (name, _, _) in enumerate(items)]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="handbook")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def quiz_answer_kb(q_index):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="A", callback_data=f"ans:{q_index}:A"),
         InlineKeyboardButton(text="B", callback_data=f"ans:{q_index}:B")],
        [InlineKeyboardButton(text="C", callback_data=f"ans:{q_index}:C"),
         InlineKeyboardButton(text="D", callback_data=f"ans:{q_index}:D")],
    ])


def quiz_finish_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пройти ще раз", callback_data="quiz_start")],
        [InlineKeyboardButton(text="🏠 Головне меню",   callback_data="main_menu")],
    ])


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привіт! Я довідник продавця Фокстрот.\n\n"
        "Тут ти знайдеш інформацію по послугам, пакетам та зможеш перевірити свої знання.",
        reply_markup=main_menu_kb(),
    )


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("🏠 Головне меню:", reply_markup=main_menu_kb())


@dp.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery):
    get_handbook_data(force=True)
    get_quiz_questions(force=True)
    await call.answer("✅ Дані оновлено!", show_alert=True)


@dp.callback_query(F.data == "handbook")
async def cb_handbook(call: CallbackQuery):
    data = get_handbook_data()
    if not data:
        await call.answer("⚠️ Довідник порожній або помилка з'єднання", show_alert=True)
        return
    await call.message.edit_text("📚 Оберіть категорію:", reply_markup=categories_kb(list(data.keys())))


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery):
    category = call.data[4:]
    data = get_handbook_data()
    items = data.get(category, [])
    if not items:
        await call.answer("Категорія порожня", show_alert=True)
        return
    await call.message.edit_text(f"📂 {category}\n\nОберіть пункт:", reply_markup=items_kb(items, category))


@dp.callback_query(F.data.startswith("item:"))
async def cb_item(call: CallbackQuery):
    _, category, idx_str = call.data.split(":", 2)
    idx = int(idx_str)
    data = get_handbook_data()
    items = data.get(category, [])
    if idx >= len(items):
        await call.answer("Не знайдено", show_alert=True)
        return
    name, desc, photo_url = items[idx]
    text = f"📌 <b>{name}</b>\n\n{desc}"
    photo = convert_drive_link(photo_url)
    if photo:
        try:
            await call.message.answer_photo(photo=photo)
        except Exception as e:
            logger.error(f"Помилка фото: {e}")
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=back_to_categories_kb())
    await call.answer()


@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(QuizState.in_quiz)
    await state.update_data(mode="search")
    await call.message.edit_text("🔍 Введіть слово для пошуку:")


@dp.message(QuizState.in_quiz, F.text)
async def handle_text_in_state(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode")
    if mode == "search":
        query = message.text.lower()
        handbook = get_handbook_data()
        results = []
        for cat, items in handbook.items():
            for name, desc, _ in items:
                if query in name.lower() or query in desc.lower():
                    results.append(f"📂 <b>{cat}</b> › {name}\n{desc}")
        await state.clear()
        if results:
            text = "\n\n─────────────\n\n".join(results[:5])
        else:
            text = "❌ Нічого не знайдено. Спробуйте інше слово."
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())
    elif mode == "quiz":
        await message.answer("⬆️ Обери варіант відповіді кнопкою вище.")


@dp.callback_query(F.data == "quiz_start")
async def cb_quiz_start(call: CallbackQuery, state: FSMContext):
    questions = get_quiz_questions()
    if not questions:
        await call.answer("⚠️ Питання не знайдено.", show_alert=True)
        return
    await state.set_state(QuizState.in_quiz)
    await state.update_data(mode="quiz", questions=questions, q_index=0, score=0, wrong_topics=[])
    await send_question(call.message, state, edit=True)


async def send_question(message, state, edit=False):
    data = await state.get_data()
    questions = data["questions"]
    idx = data["q_index"]
    q = questions[idx]
    total = len(questions)
    text = (
        f"📝 Питання {idx+1}/{total}\n\n"
        f"<b>{q['question']}</b>\n\n"
        f"A) {q['a']}\nB) {q['b']}\nC) {q['c']}\nD) {q['d']}"
    )
    kb = quiz_answer_kb(idx)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("ans:"))
async def cb_answer(call: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != QuizState.in_quiz:
        await call.answer("Тест не активний.", show_alert=True)
        return
    _, q_idx_str, chosen = call.data.split(":")
    q_idx = int(q_idx_str)
    data = await state.get_data()
    if data.get("mode") != "quiz":
        return
    questions = data["questions"]
    idx = data["q_index"]
    if q_idx != idx:
        await call.answer()
        return
    q = questions[idx]
    correct = q["correct"]
    score = data["score"]
    wrong_topics = data["wrong_topics"]
    if chosen == correct:
        score += 1
        feedback = "✅ Правильно!"
    else:
        wrong_topics.append(q["question"][:50])
        feedback = f"❌ Неправильно! Правильна відповідь: <b>{correct}</b>"
    if q.get("explanation"):
        feedback += f"\n\n💡 {q['explanation']}"
    next_idx = idx + 1
    total = len(questions)
    await state.update_data(q_index=next_idx, score=score, wrong_topics=wrong_topics)
    if next_idx >= total:
        await state.clear()
        percent = round(score / total * 100)
        emoji = "🏆" if percent >= 80 else "👍" if percent >= 60 else "📖"
        verdict = "Відмінний результат!" if percent >= 80 else "Непогано, але є що підтягнути." if percent >= 60 else "Варто повторити матеріал."
        user = call.from_user
        save_quiz_result(user.id, user.username, user.full_name, score, total, wrong_topics)
        result_text = (
            f"{feedback}\n\n─────────────\n"
            f"{emoji} Тест завершено!\n\n"
            f"Результат: <b>{score}/{total}</b> ({percent}%)\n{verdict}"
        )
        if wrong_topics:
            result_text += "\n\n⚠️ Помилки у темах:\n" + "\n".join(f"• {t}" for t in wrong_topics)
        await call.message.edit_text(result_text, parse_mode="HTML", reply_markup=quiz_finish_kb())
    else:
        await call.message.edit_text(feedback, parse_mode="HTML")
        await asyncio.sleep(1.5)
        await send_question(call.message, state, edit=False)
    await call.answer()


async def preload_cache():
    logger.info("Завантаження кешу...")
    get_handbook_data(force=True)
    get_quiz_questions(force=True)
    logger.info("Кеш завантажено!")


async def main():
    asyncio.create_task(preload_cache())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
