import asyncio
import logging
import os
import random
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

DIVIDER = "─" * 16


def progress_bar(current, total, length=10):
    """Текстовий прогрес-бар виду ▓▓▓▓░░░░░░ для тесту."""
    if total <= 0:
        return ""
    filled = round(length * current / total)
    filled = max(0, min(length, filled))
    return "▓" * filled + "░" * (length - filled)


# Розділи-"стрічки": кожен читається з окремого листа Google Таблиці
# (колонки Назва | Опис | Фото — так само, як у Довіднику, тільки без категорій).
# Щоб додати сюди контент, просто заповніть відповідний лист у таблиці.
FEEDS = {
    "promo": {"sheet": "Акції", "title": "🔥 Акції", "cache_key": "promo"},
    "news": {"sheet": "Новини", "title": "📰 Новини", "cache_key": "news"},
    "newitems": {"sheet": "Новинки", "title": "🆕 Новинки", "cache_key": "newitems"},
    "motivation": {"sheet": "Мотивація", "title": "💪 Мотивація", "cache_key": "motivation"},
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_cache = {
    "handbook": {"data": None, "ts": 0},
    "promo": {"data": None, "ts": 0},
    "news": {"data": None, "ts": 0},
    "newitems": {"data": None, "ts": 0},
    "motivation": {"data": None, "ts": 0},
    "services": {"data": None, "ts": 0},
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
    # Ищем ID файла в любом из распространённых форматов ссылок Google Drive:
    # /file/d/ID/view, ?id=ID, &id=ID
    match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if not match:
        match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if match:
        file_id = match.group(1)
        # thumbnail-эндпоинт отдаёт картинку напрямую и, в отличие от
        # uc?export=download, не упирається у попередження Google Drive
        # про перевірку великих файлів на віруси (яке віддає HTML замість фото).
        return f"https://drive.google.com/thumbnail?id={file_id}&sz=w1600"
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
            cat = str(row.get("Категорія", "")).strip()
            name = str(row.get("Назва", "")).strip()
            desc = str(row.get("Опис", "")).strip()
            photo = str(row.get("Фото", "")).strip()
            if cat and name:
                data.setdefault(cat, []).append((name, desc, photo))
        _cache["handbook"] = {"data": data, "ts": now}
        return data
    except Exception as e:
        logger.error(f"Помилка читання довідника: {e}")
        return _cache["handbook"]["data"] or {}


def get_services_data(force=False):
    """
    Читає лист "Сервіси" — авторизовані сервісні центри.
    Колонки: Категорія | Місто | Бренд | Опис | Фото.
    Структура даних — трирівнева: {категорія: {місто: [(бренд, опис, фото), ...]}}.
    Порядок категорій/міст/брендів у меню відповідає порядку рядків у листі.
    """
    now = time.time()
    if not force and _cache["services"]["data"] is not None and now - _cache["services"]["ts"] < CACHE_TTL:
        return _cache["services"]["data"]
    try:
        sheet = get_sheet().worksheet("Сервіси")
        rows = sheet.get_all_records()
        data = {}
        for row in rows:
            cat = str(row.get("Категорія", "")).strip()
            city = str(row.get("Місто", "")).strip()
            brand = str(row.get("Бренд", "")).strip()
            desc = str(row.get("Опис", "")).strip()
            photo = str(row.get("Фото", "")).strip()
            if cat and city and brand:
                data.setdefault(cat, {}).setdefault(city, []).append((brand, desc, photo))
        _cache["services"] = {"data": data, "ts": now}
        return data
    except Exception as e:
        logger.error(f"Помилка читання листа «Сервіси»: {e}")
        return _cache["services"]["data"] or {}


_quiz_index_cache = {"data": None, "ts": 0}
_quiz_sheet_cache = {}  # назва листа з питаннями -> {"data": [...], "ts": ...}


def get_quiz_index(force=False):
    """
    Читає лист "Тести" — це перелік усіх тестів для головного меню тестування.
    Колонки: Назва (текст кнопки) | Лист (назва листа з питаннями цього тесту).
    Якщо колонку "Лист" залишити порожньою — це "загальний" тест: бот сам
    збере і перемішає питання з усіх інших тестів у цьому переліку.
    Якщо лист "Тести" не створено — використовується єдиний тест з листа "Тест"
    (сумісність зі старими таблицями).
    """
    now = time.time()
    if not force and _quiz_index_cache["data"] is not None and now - _quiz_index_cache["ts"] < CACHE_TTL:
        return _quiz_index_cache["data"]
    try:
        sheet = get_sheet().worksheet("Тести")
        rows = sheet.get_all_records()
        index = []
        for row in rows:
            name = str(row.get("Назва", "")).strip()
            quiz_sheet = str(row.get("Лист", "")).strip()
            if name:
                index.append({"name": name, "sheet": quiz_sheet})
        if not index:
            index = [{"name": "📝 Тест", "sheet": "Тест"}]
        _quiz_index_cache.update({"data": index, "ts": now})
        return index
    except Exception as e:
        logger.error(f"Помилка читання листа «Тести»: {e}")
        return _quiz_index_cache["data"] or [{"name": "📝 Тест", "sheet": "Тест"}]


def get_quiz_questions_from_sheet(sheet_name, force=False):
    now = time.time()
    cached = _quiz_sheet_cache.get(sheet_name)
    if not force and cached and now - cached["ts"] < CACHE_TTL:
        return cached["data"]
    try:
        sheet = get_sheet().worksheet(sheet_name)
        rows = sheet.get_all_records()
        questions = []
        for row in rows:
            q = {
                "question": str(row.get("Питання", "")).strip(),
                "a": str(row.get("A", "")).strip(),
                "b": str(row.get("B", "")).strip(),
                "c": str(row.get("C", "")).strip(),
                "d": str(row.get("D", "")).strip(),
                "correct": str(row.get("Правильна відповідь", "")).strip().upper(),
                "explanation": str(row.get("Пояснення", "")).strip(),
            }
            if q["question"]:
                questions.append(q)
        _quiz_sheet_cache[sheet_name] = {"data": questions, "ts": now}
        return questions
    except Exception as e:
        logger.error(f"Помилка читання листа «{sheet_name}»: {e}")
        return (cached["data"] if cached else []) or []


def get_combined_quiz_questions(index, force=False):
    """Збирає й перемішує питання з усіх тестів переліку (для "загального" тесту)."""
    combined = []
    for entry in index:
        if entry["sheet"]:
            combined.extend(get_quiz_questions_from_sheet(entry["sheet"], force=force))
    random.shuffle(combined)
    return combined


def get_feed_data(sheet_name, cache_key, force=False):
    """Читає плаский список (Акції / Новини / Новинки) з окремого листа таблиці."""
    now = time.time()
    if not force and _cache[cache_key]["data"] is not None and now - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]
    try:
        sheet = get_sheet().worksheet(sheet_name)
        rows = sheet.get_all_records()
        items = []
        for row in rows:
            name = str(row.get("Назва", "")).strip()
            desc = str(row.get("Опис", "")).strip()
            photo = str(row.get("Фото", "")).strip()
            if name:
                items.append((name, desc, photo))
        items.reverse()  # найновіші записи внизу листа — показуємо їх першими
        _cache[cache_key] = {"data": items, "ts": now}
        return items
    except Exception as e:
        logger.error(f"Помилка читання листа «{sheet_name}»: {e}")
        return _cache[cache_key]["data"] or []


def save_quiz_result(user_id, username, full_name, quiz_name, score, total, wrong_topics):
    try:
        sheet = get_sheet().worksheet("Результати")
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        percent = round(score / total * 100) if total else 0
        sheet.append_row([
            now, user_id, username or "", full_name or "", quiz_name or "—",
            score, total, f"{percent}%",
            ", ".join(wrong_topics) if wrong_topics else "—",
        ])
    except Exception as e:
        logger.error(f"Помилка збереження результату: {e}")


class QuizState(StatesGroup):
    in_quiz = State()


def main_menu_kb():
    handbook = get_handbook_data()
    quiz_index = get_quiz_index()
    cat_count = len(handbook)
    handbook_label = f"📚 Довідник ({cat_count})" if cat_count else "📚 Довідник"
    quiz_label = f"📝 Тестування ({len(quiz_index)})" if len(quiz_index) > 1 else "📝 Тестування"
    services = get_services_data()
    services_count = sum(len(cities) for cities in services.values())
    services_label = f"🔧 Сервісні центри ({services_count})" if services_count else "🔧 Сервісні центри"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=handbook_label, callback_data="handbook")],
        [InlineKeyboardButton(text=services_label, callback_data="svc")],
        [InlineKeyboardButton(text=quiz_label, callback_data="quiz_menu")],
        [InlineKeyboardButton(text="🔥 Акції", callback_data="feed:promo")],
        [InlineKeyboardButton(text="📰 Новини", callback_data="feed:news")],
        [InlineKeyboardButton(text="🆕 Новинки", callback_data="feed:newitems")],
        [InlineKeyboardButton(text="💪 Мотивація", callback_data="feed:motivation")],
        [InlineKeyboardButton(text="🔍 Пошук", callback_data="search")],
        [InlineKeyboardButton(text="🔄 Оновити дані", callback_data="refresh")],
    ])


def feed_list_kb(feed_key, items):
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"feeditem:{feed_key}:{i}")]
               for i, (name, _, _) in enumerate(items)]
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def feed_back_kb(feed_key):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"feed:{feed_key}")],
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
    ])


def categories_kb(data):
    # ВАЖНО: в callback_data кладём индекс категории, а не её текст.
    # Раньше здесь было callback_data=f"cat:{cat}" — при длинном названии
    # категории (особенно на кириллице) это превышало лимит Telegram
    # в 64 байта и вызывало ошибку BUTTON_DATA_INVALID.
    categories = list(data.keys())
    buttons = [[InlineKeyboardButton(text=f"{cat} ({len(data[cat])})", callback_data=f"cat:{i}")]
               for i, cat in enumerate(categories)]
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_categories_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад до категорій", callback_data="handbook")],
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
    ])


def items_kb(items, cat_idx):
    # Аналогично: раньше было callback_data=f"item:{category}:{i}",
    # теперь используем индекс категории вместо её названия.
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"item:{cat_idx}:{i}")]
               for i, (name, _, _) in enumerate(items)]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="handbook")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_categories_kb(data):
    categories = list(data.keys())
    buttons = [[InlineKeyboardButton(text=f"{cat} ({sum(len(v) for v in data[cat].values())})", callback_data=f"svccat:{i}")]
               for i, cat in enumerate(categories)]
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_cities_kb(data, cat_idx):
    category = list(data.keys())[cat_idx]
    cities = list(data[category].keys())
    buttons = [[InlineKeyboardButton(text=f"{city} ({len(data[category][city])})", callback_data=f"svccity:{cat_idx}:{i}")]
               for i, city in enumerate(cities)]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="svc")])
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_brands_kb(data, cat_idx, city_idx):
    category = list(data.keys())[cat_idx]
    city = list(data[category].keys())[city_idx]
    items = data[category][city]
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"svcitem:{cat_idx}:{city_idx}:{i}")]
               for i, (name, _, _) in enumerate(items)]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"svccat:{cat_idx}")])
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_item_kb(cat_idx, city_idx):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"svccity:{cat_idx}:{city_idx}")],
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
    ])


def quiz_menu_kb(index):
    buttons = [[InlineKeyboardButton(text=entry["name"], callback_data=f"quiz_pick:{i}")]
               for i, entry in enumerate(index)]
    buttons.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def quiz_answer_kb(q_index):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="A", callback_data=f"ans:{q_index}:A"),
         InlineKeyboardButton(text="B", callback_data=f"ans:{q_index}:B")],
        [InlineKeyboardButton(text="C", callback_data=f"ans:{q_index}:C"),
         InlineKeyboardButton(text="D", callback_data=f"ans:{q_index}:D")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")],
    ])


def quiz_finish_kb(quiz_idx):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пройти ще раз", callback_data=f"quiz_pick:{quiz_idx}")],
        [InlineKeyboardButton(text="📝 Інші тести", callback_data="quiz_menu")],
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
    ])


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Кеш file_id вже надісланих фото: url -> telegram_file_id.
# Перше відкриття картинки повільне (Telegram сам качає файл з Google Drive),
# але далі можна показувати те саме фото миттєво, без повторного скачування.
_photo_file_id_cache = {}


async def render(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup, photo: str = None):
    """
    Показывает новый экран взамен старого: сначала удаляет предыдущее
    сообщение бота, затем отправляет новое (текст или фото с подписью).
    Так в чате не копятся старые фото/тексты — виден только текущий экран.
    Editing нельзя использовать, потому что Telegram не даёт превратить
    текстовое сообщение в фото (и наоборот) через edit_text/edit_media.
    """
    try:
        await call.message.delete()
    except Exception as e:
        logger.warning(f"Не вдалося видалити повідомлення: {e}")

    if photo:
        # Якщо це фото вже надсилалося раніше — використовуємо готовий
        # file_id замість посилання, це миттєво (без звернення до Google Drive).
        to_send = _photo_file_id_cache.get(photo, photo)
        try:
            if len(text) <= 1024:
                sent = await call.message.answer_photo(
                    photo=to_send, caption=text, parse_mode="HTML", reply_markup=kb
                )
            else:
                # Подпис до фото в Telegram обмежена 1024 символами —
                # якщо опис довший, надсилаємо фото окремо, а текст нижче.
                sent = await call.message.answer_photo(photo=to_send)
                await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
            if photo not in _photo_file_id_cache and sent.photo:
                _photo_file_id_cache[photo] = sent.photo[-1].file_id
            return
        except Exception as e:
            logger.error(f"Помилка фото, показую без фото: {e}")
            # Якщо кешований file_id раптом став невалідним (фото видалили
            # на боці Telegram) — прибираємо з кешу, щоб не зациклюватись.
            _photo_file_id_cache.pop(photo, None)

    await call.message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = message.from_user.first_name or "колего"
    await message.answer(
        f"👋 Привіт, {name}! Я чат-бот Фокстрот — помічник, який завжди поруч.\n\n"
        "Обери розділ нижче 👇",
        reply_markup=main_menu_kb(),
    )


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await render(call, "🏠 Головне меню:", main_menu_kb())


@dp.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery):
    get_handbook_data(force=True)
    get_services_data(force=True)
    quiz_index = get_quiz_index(force=True)
    for entry in quiz_index:
        if entry["sheet"]:
            get_quiz_questions_from_sheet(entry["sheet"], force=True)
    for feed in FEEDS.values():
        get_feed_data(feed["sheet"], feed["cache_key"], force=True)
    await call.answer("✅ Дані оновлено!", show_alert=True)


@dp.callback_query(F.data == "handbook")
async def cb_handbook(call: CallbackQuery):
    data = get_handbook_data()
    if not data:
        await call.answer("⚠️ Довідник порожній або помилка з'єднання", show_alert=True)
        return
    await call.answer()
    text = f"🏠 Головне меню › 📚 Довідник\n{DIVIDER}\n\nОберіть категорію:"
    await render(call, text, categories_kb(data))


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery):
    idx_str = call.data[4:]
    if not idx_str.isdigit():
        await call.answer("Некоректна категорія", show_alert=True)
        return
    idx = int(idx_str)
    data = get_handbook_data()
    categories = list(data.keys())
    if idx >= len(categories):
        await call.answer("Категорія не знайдена", show_alert=True)
        return
    category = categories[idx]
    items = data.get(category, [])
    if not items:
        await call.answer("Категорія порожня", show_alert=True)
        return
    await call.answer()
    text = f"🏠 Головне меню › 📚 Довідник › {category}\n{DIVIDER}\n\nОберіть пункт ({len(items)}):"
    await render(call, text, items_kb(items, idx))


@dp.callback_query(F.data.startswith("item:"))
async def cb_item(call: CallbackQuery):
    _, cat_idx_str, idx_str = call.data.split(":", 2)
    cat_idx = int(cat_idx_str)
    idx = int(idx_str)
    data = get_handbook_data()
    categories = list(data.keys())
    if cat_idx >= len(categories):
        await call.answer("Не знайдено", show_alert=True)
        return
    category = categories[cat_idx]
    items = data.get(category, [])
    if idx >= len(items):
        await call.answer("Не знайдено", show_alert=True)
        return
    name, desc, photo_url = items[idx]
    text = f"📌 <b>{name}</b>\n🗂 {category}\n{DIVIDER}\n\n{desc}"
    photo = convert_drive_link(photo_url)
    await call.answer()
    await render(call, text, back_to_categories_kb(), photo=photo)


@dp.callback_query(F.data == "svc")
async def cb_services(call: CallbackQuery):
    data = get_services_data()
    if not data:
        await call.answer("⚠️ Розділ порожній або помилка з'єднання", show_alert=True)
        return
    await call.answer()
    text = f"🏠 Головне меню › 🔧 Сервісні центри\n{DIVIDER}\n\nОберіть категорію техніки:"
    await render(call, text, services_categories_kb(data))


@dp.callback_query(F.data.startswith("svccat:"))
async def cb_services_category(call: CallbackQuery):
    idx_str = call.data.split(":", 1)[1]
    if not idx_str.isdigit():
        await call.answer("Помилка категорії", show_alert=True)
        return
    idx = int(idx_str)
    data = get_services_data()
    categories = list(data.keys())
    if idx >= len(categories):
        await call.answer("Категорія не знайдена", show_alert=True)
        return
    category = categories[idx]
    await call.answer()
    text = f"🏠 Головне меню › 🔧 Сервісні центри › {category}\n{DIVIDER}\n\nОберіть місто:"
    await render(call, text, services_cities_kb(data, idx))


@dp.callback_query(F.data.startswith("svccity:"))
async def cb_services_city(call: CallbackQuery):
    _, cat_idx_str, city_idx_str = call.data.split(":", 2)
    cat_idx, city_idx = int(cat_idx_str), int(city_idx_str)
    data = get_services_data()
    categories = list(data.keys())
    if cat_idx >= len(categories):
        await call.answer("Не знайдено", show_alert=True)
        return
    category = categories[cat_idx]
    cities = list(data[category].keys())
    if city_idx >= len(cities):
        await call.answer("Не знайдено", show_alert=True)
        return
    city = cities[city_idx]
    items = data[category][city]
    await call.answer()
    text = f"🏠 Головне меню › 🔧 Сервісні центри › {category} › {city}\n{DIVIDER}\n\nОберіть бренд ({len(items)}):"
    await render(call, text, services_brands_kb(data, cat_idx, city_idx))


@dp.callback_query(F.data.startswith("svcitem:"))
async def cb_services_item(call: CallbackQuery):
    _, cat_idx_str, city_idx_str, item_idx_str = call.data.split(":", 3)
    cat_idx, city_idx, item_idx = int(cat_idx_str), int(city_idx_str), int(item_idx_str)
    data = get_services_data()
    categories = list(data.keys())
    if cat_idx >= len(categories):
        await call.answer("Не знайдено", show_alert=True)
        return
    category = categories[cat_idx]
    cities = list(data[category].keys())
    if city_idx >= len(cities):
        await call.answer("Не знайдено", show_alert=True)
        return
    city = cities[city_idx]
    items = data[category][city]
    if item_idx >= len(items):
        await call.answer("Не знайдено", show_alert=True)
        return
    brand, desc, photo_url = items[item_idx]
    text = f"🔧 <b>{brand}</b>\n🗂 {category} · 📍 {city}\n{DIVIDER}\n\n{desc}"
    photo = convert_drive_link(photo_url)
    await call.answer()
    await render(call, text, services_item_kb(cat_idx, city_idx), photo=photo)


@dp.callback_query(F.data.startswith("feed:"))
async def cb_feed(call: CallbackQuery):
    feed_key = call.data.split(":", 1)[1]
    feed = FEEDS.get(feed_key)
    if not feed:
        await call.answer("Розділ не знайдено", show_alert=True)
        return
    items = get_feed_data(feed["sheet"], feed["cache_key"])
    await call.answer()
    if not items:
        text = f"🏠 Головне меню › {feed['title']}\n{DIVIDER}\n\nПоки що тут порожньо. Зазирни трохи пізніше!"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
        ])
        await render(call, text, kb)
        return
    text = f"🏠 Головне меню › {feed['title']}\n{DIVIDER}\n\nОберіть пункт ({len(items)}):"
    await render(call, text, feed_list_kb(feed_key, items))


@dp.callback_query(F.data.startswith("feeditem:"))
async def cb_feed_item(call: CallbackQuery):
    _, feed_key, idx_str = call.data.split(":", 2)
    feed = FEEDS.get(feed_key)
    if not feed or not idx_str.isdigit():
        await call.answer("Не знайдено", show_alert=True)
        return
    idx = int(idx_str)
    items = get_feed_data(feed["sheet"], feed["cache_key"])
    if idx >= len(items):
        await call.answer("Не знайдено", show_alert=True)
        return
    name, desc, photo_url = items[idx]
    text = f"📌 <b>{name}</b>\n{DIVIDER}\n\n{desc}" if desc else f"📌 <b>{name}</b>"
    photo = convert_drive_link(photo_url)
    await call.answer()
    await render(call, text, feed_back_kb(feed_key), photo=photo)


@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(QuizState.in_quiz)
    await state.update_data(mode="search")
    await call.message.edit_text(
        f"🔍 Пошук по довіднику\n{DIVIDER}\n\n"
        "Введіть слово або фразу (мінімум 2 символи):",
        reply_markup=cancel_kb(),
    )


@dp.message(QuizState.in_quiz, F.text)
async def handle_text_in_state(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode")
    if mode == "search":
        query = message.text.strip().lower()
        if len(query) < 2:
            await message.answer(
                "⚠️ Запит закороткий. Введіть хоча б 2 символи:",
                reply_markup=cancel_kb(),
            )
            return
        handbook = get_handbook_data()
        results = []
        for cat, items in handbook.items():
            for name, desc, _ in items:
                if query in name.lower() or query in desc.lower():
                    results.append(f"📂 <b>{cat}</b> › {name}\n{desc}")
        await state.clear()
        if results:
            shown = results[:5]
            numbered = [f"{i+1}. {r}" for i, r in enumerate(shown)]
            header = f"🔍 Знайдено: {len(results)}"
            if len(results) > 5:
                header += f" (показано перші 5)"
            text = f"{header}\n{DIVIDER}\n\n" + f"\n\n{DIVIDER}\n\n".join(numbered)
        else:
            text = f"❌ Нічого не знайдено за запитом «{query}».\nСпробуйте інше слово."
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())
    elif mode == "quiz":
        await message.answer("⬆️ Обери варіант відповіді кнопкою вище.")


@dp.callback_query(F.data == "quiz_menu")
async def cb_quiz_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    index = get_quiz_index()
    await call.answer()
    text = f"🏠 Головне меню › 📝 Тестування\n{DIVIDER}\n\nОберіть тест ({len(index)}):"
    await render(call, text, quiz_menu_kb(index))


@dp.callback_query(F.data.startswith("quiz_pick:"))
async def cb_quiz_pick(call: CallbackQuery, state: FSMContext):
    idx_str = call.data.split(":", 1)[1]
    if not idx_str.isdigit():
        await call.answer("Помилка тесту", show_alert=True)
        return
    idx = int(idx_str)
    index = get_quiz_index()
    if idx >= len(index):
        await call.answer("Тест не знайдено", show_alert=True)
        return
    entry = index[idx]
    if entry["sheet"]:
        questions = get_quiz_questions_from_sheet(entry["sheet"])
    else:
        questions = get_combined_quiz_questions(index)
    if not questions:
        await call.answer("⚠️ Питання не знайдено.", show_alert=True)
        return
    await state.set_state(QuizState.in_quiz)
    await state.update_data(
        mode="quiz", questions=questions, q_index=0, score=0, wrong_topics=[],
        quiz_idx=idx, quiz_name=entry["name"],
    )
    await send_question(call.message, state, edit=True)


async def send_question(message, state, edit=False):
    data = await state.get_data()
    questions = data["questions"]
    idx = data["q_index"]
    q = questions[idx]
    total = len(questions)
    bar = progress_bar(idx, total)
    text = (
        f"📝 Питання {idx+1}/{total}   {bar}\n{DIVIDER}\n\n"
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
    quiz_idx = data.get("quiz_idx", 0)
    quiz_name = data.get("quiz_name", "")
    await state.update_data(q_index=next_idx, score=score, wrong_topics=wrong_topics)
    if next_idx >= total:
        await state.clear()
        percent = round(score / total * 100)
        emoji = "🏆" if percent >= 80 else "👍" if percent >= 60 else "📖"
        verdict = "Відмінний результат!" if percent >= 80 else "Непогано, але є що підтягнути." if percent >= 60 else "Варто повторити матеріал."
        user = call.from_user
        save_quiz_result(user.id, user.username, user.full_name, quiz_name, score, total, wrong_topics)
        result_text = (
            f"{feedback}\n\n{DIVIDER}\n"
            f"{emoji} {quiz_name} — тест завершено!\n\n"
            f"{progress_bar(score, total)} {percent}%\n"
            f"Результат: <b>{score}/{total}</b>\n{verdict}"
        )
        if wrong_topics:
            result_text += "\n\n⚠️ Помилки у темах:\n" + "\n".join(f"• {t}" for t in wrong_topics)
        await call.message.edit_text(result_text, parse_mode="HTML", reply_markup=quiz_finish_kb(quiz_idx))
    else:
        await call.message.edit_text(feedback, parse_mode="HTML")
        await asyncio.sleep(1.5)
        await send_question(call.message, state, edit=False)
    await call.answer()


async def preload_cache():
    logger.info("Завантаження кешу...")
    data = get_handbook_data(force=True)
    services_data = get_services_data(force=True)
    quiz_index = get_quiz_index(force=True)
    for entry in quiz_index:
        if entry["sheet"]:
            get_quiz_questions_from_sheet(entry["sheet"], force=True)
    feed_items = []
    for feed in FEEDS.values():
        feed_items.extend(get_feed_data(feed["sheet"], feed["cache_key"], force=True))
    logger.info("Кеш завантажено!")

    # Опційно: якщо задати свій Telegram ID у змінній оточення ADMIN_CHAT_ID,
    # бот при старті "прожене" всі фото довідника через себе і збереже їх
    # file_id — тоді навіть перше відкриття картинки користувачем буде
    # миттєвим, без очікування завантаження з Google Drive.
    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if not admin_chat_id:
        return
    logger.info("Прогрів кешу фото...")
    warmup_msg = await bot.send_message(admin_chat_id, "🔄 Прогрів кешу фото довідника почато...")
    count = 0
    all_items = list(feed_items)
    for items in data.values():
        all_items.extend(items)
    for cities in services_data.values():
        for items in cities.values():
            all_items.extend(items)
    for _, _, photo_url in all_items:
        photo = convert_drive_link(photo_url)
        if not photo or photo in _photo_file_id_cache:
            continue
        try:
            sent = await bot.send_photo(admin_chat_id, photo=photo)
            if sent.photo:
                _photo_file_id_cache[photo] = sent.photo[-1].file_id
                count += 1
            await bot.delete_message(admin_chat_id, sent.message_id)
        except Exception as e:
            logger.error(f"Не вдалося прогріти фото {photo}: {e}")
    await warmup_msg.edit_text(f"✅ Прогрів завершено: {count} фото закешовано.")
    logger.info(f"Прогрів кешу фото завершено: {count} фото.")


async def main():
    asyncio.create_task(preload_cache())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
