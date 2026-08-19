import asyncio
import html
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

# Пакет "google-genai" потрібен лише для двох кнопок зі ШІ (Порівняння / ХОЧ?).
# Якщо його ще не встановлено на хостингу — решта бота однаково працює,
# просто ці дві кнопки не з'являться в меню (див. ai_client нижче).
try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
CACHE_TTL = 300

# ШІ-функції (порівняння товарів, "ХОЧ?") працюють через Google Gemini.
# Потрібен GEMINI_API_KEY — якщо його немає, ai_client лишається None
# і кнопки просто не показуються в меню.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
ai_client = genai.Client(api_key=GEMINI_API_KEY) if (genai and GEMINI_API_KEY) else None

SPECS_SYSTEM_PROMPT = (
    "Ти — консультант мережі магазинів побутової техніки та електроніки "
    "«Фокстрот». Продавець надсилає тобі фото цінника або самого товару. "
    "Визнач модель товару і коротко, українською мовою, виділи "
    "найважливіші характеристики, які варто озвучити клієнту під час "
    "презентації товару в магазині. В кінці окремим абзацом напиши, яку "
    "конкретну вигоду отримає клієнт від цих характеристик. Пиши стисло і "
    "по суті, без зайвої води, можна короткими пунктами. "
    "Пиши ЗВИЧАЙНИМ ТЕКСТОМ без будь-якої markdown-розмітки: не використовуй "
    "зірочки **, решітки #, дефіси-списки на початку рядка. Якщо потрібні "
    "пункти — став перед ними символ «•»."
)
COMPARE_SYSTEM_PROMPT = (
    "Ти — консультант мережі магазинів побутової техніки та електроніки "
    "«Фокстрот». Продавець надсилає тобі два фото (цінники або самі "
    "товари). Визнач моделі обох товарів і коротко, українською мовою, "
    "порівняй їх за ключовими характеристиками. В кінці одним-двома "
    "реченнями напиши, якому товару варто віддати перевагу і що саме "
    "отримає клієнт, якщо обере саме його. Пиши стисло і по суті, без "
    "зайвої води. "
    "Пиши ЗВИЧАЙНИМ ТЕКСТОМ без будь-якої markdown-розмітки: не використовуй "
    "зірочки **, решітки #, дефіси-списки на початку рядка. Якщо потрібні "
    "пункти — став перед ними символ «•»."
)


def _ai_text_to_html(text):
    """Готує сирий текст від Gemini до безпечної відправки в Telegram із
    parse_mode=HTML: спершу екранує всі спецсимволи (щоб випадкові < > &
    у відповіді ШІ не ламали розбір HTML і не обрізали повідомлення), а
    потім конвертує залишки markdown (**жирний**), якщо модель все ж їх
    вставила, у справжні HTML-теги."""
    text = html.escape(text.strip())
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
    return text


async def ask_ai(system_prompt, images, user_text):
    """Надсилає фото (одне чи два) у Gemini Vision і повертає готовий
    текст відповіді українською, безпечний для parse_mode=HTML.
    images — список фото у вигляді bytes."""
    parts = [genai_types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images]
    parts.append(genai_types.Part.from_text(text=user_text))

    async def _call(with_thinking_off):
        kwargs = dict(
            system_instruction=system_prompt,
            temperature=0.4,
            max_output_tokens=2048,
        )
        if with_thinking_off:
            kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)
        return await ai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=parts,
            config=genai_types.GenerateContentConfig(**kwargs),
        )

    try:
        # Спершу пробуємо вимкнути "роздуми" моделі, щоб увесь ліміт
        # токенів ішов у видиму відповідь (швидше й без обрізання).
        resp = await _call(with_thinking_off=True)
    except Exception as e:
        # Якщо конкретна модель не підтримує thinking_config — не падаємо,
        # а повторюємо запит без цього параметра (просто з великим лімітом).
        logger.warning(f"thinking_config не підтримується, повторюю без нього: {e}")
        resp = await _call(with_thinking_off=False)

    return _ai_text_to_html(resp.text)

# Фірмове оформлення Фокстрот: помаранчевий акцент 🟧 у заголовках,
# картках і роздільнику — щоб бот візуально впізнавався з першого екрана.
BRAND = "🟧 <b>ФОКСТРОТ</b>"
BRAND_FOOTER = "🟧 <i>Фокстрот — Оновлюйся!</i>"
DIVIDER = "──────  🟧  ──────"


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
    "handbook_sections": {"data": None, "ts": 0},
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


def _norm_get(row, key):
    """
    Стійкий до зайвих пробілів/регістру пошук значення колонки за назвою.
    Google Таблиці іноді зберігають заголовок із невидимим пробілом чи іншим
    регістром після копіювання — звичайний row.get("Назва") в такому разі
    мовчки не спрацьовує. Спочатку пробуємо точний збіг (швидко), і лише
    якщо не знайшли — шукаємо серед заголовків без урахування пробілів/регістру.
    """
    if key in row:
        return str(row[key]).strip()
    key_norm = key.strip().lower()
    for k, v in row.items():
        if str(k).strip().lower() == key_norm:
            return str(v).strip()
    return ""


def get_handbook_sections(force=False):
    """
    Необов'язкова колонка "Розділ" у листі "Довідник" — групує категорії
    в додатковий верхній рівень меню (наприклад "Послуги" -> Розстрочка,
    Гарантія+; "Техніка" -> Пральна машина, Холодильник...).
    Повертає {розділ: [категорія, ...]}.
    Якщо колонки "Розділ" немає або вона порожня для всіх рядків —
    повертає {} і Довідник показується як і раніше, без цього рівня.
    """
    now = time.time()
    if not force and _cache["handbook_sections"]["data"] is not None and now - _cache["handbook_sections"]["ts"] < CACHE_TTL:
        return _cache["handbook_sections"]["data"]
    try:
        sheet = get_sheet().worksheet("Довідник")
        rows = sheet.get_all_records()
        sections = {}
        has_any_section = False
        for row in rows:
            section = _norm_get(row, "Розділ")
            cat = _norm_get(row, "Категорія")
            name = _norm_get(row, "Назва")
            if not (cat and name):
                continue
            if section:
                has_any_section = True
            key = section or "Інше"
            cats = sections.setdefault(key, [])
            if cat not in cats:
                cats.append(cat)
        if not has_any_section and rows:
            # Діагностика на випадок, якщо колонку "Розділ" додали, а бот її
            # не бачить — тут буде видно точні назви заголовків з таблиці.
            logger.info(f"Колонку «Розділ» не знайдено заповненою. Заголовки листа «Довідник»: {list(rows[0].keys())}")
        result = sections if has_any_section else {}
        _cache["handbook_sections"] = {"data": result, "ts": now}
        return result
    except Exception as e:
        logger.error(f"Помилка читання розділів довідника: {e}")
        return _cache["handbook_sections"]["data"] or {}


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


# ---- Форма зворотного зв'язку (Відгук / Скарга / Пропозиція / Ідея) ----

FEEDBACK_SHEET = "Зворотній звʼязок"
FEEDBACK_HEADERS = ["Дата", "Telegram ID", "Username", "Ім'я", "Тип", "Текст"]
FEEDBACK_TYPES = [
    ("review", "💬 Відгук"),
    ("complaint", "⚠️ Скарга"),
    ("suggestion", "💡 Пропозиція"),
    ("idea", "✨ Ідея"),
]


def _get_or_create_worksheet(name, headers):
    """Відкриває лист, а якщо його ще немає в таблиці — створює сам, із
    заголовками. Так власнику таблиці не треба вручну готувати лист
    «Зворотній звʼязок» — бот зробить це при першому відгуку."""
    ss = get_sheet()
    try:
        return ss.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=name, rows=200, cols=max(len(headers), 1))
        ws.append_row(headers)
        return ws


def save_feedback(user_id, username, full_name, fb_type, text):
    try:
        sheet = _get_or_create_worksheet(FEEDBACK_SHEET, FEEDBACK_HEADERS)
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        sheet.append_row([now, user_id, username or "", full_name or "", fb_type, text])
    except Exception as e:
        logger.error(f"Помилка збереження зворотного зв'язку: {e}")


# ============================================================================
# Фонове оновлення даних
#
# gspread — синхронна бібліотека: кожен виклик sheet.get_all_records() — це
# блокуючий HTTP-запит. Якщо викликати його прямо в async-обробнику, на час
# запиту "замерзає" весь бот — усі користувачі одночасно, а не тільки той,
# хто натиснув кнопку. Тому важкі масові перечитування (старт бота, кнопка
# "Оновити дані", фонове оновлення) виконуються в окремому потоці через
# asyncio.to_thread — це не блокує обробку інших повідомлень.
# ============================================================================

async def refresh_all_data(force=True):
    def _do():
        get_handbook_data(force=force)
        get_handbook_sections(force=force)
        get_services_data(force=force)
        quiz_index = get_quiz_index(force=force)
        for entry in quiz_index:
            if entry["sheet"]:
                get_quiz_questions_from_sheet(entry["sheet"], force=force)
        for feed in FEEDS.values():
            get_feed_data(feed["sheet"], feed["cache_key"], force=force)
    await asyncio.to_thread(_do)


async def background_refresh_loop():
    """
    Перечитує всі листи таблиці ще до того, як кеш встигне протухнути
    (CACHE_TTL), тому користувач фактично ніколи не чекає на мережу —
    відповідь завжди йде з уже готового, заздалегідь оновленого кешу.
    """
    interval = max(CACHE_TTL - 30, 60)
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_all_data(force=True)
            logger.info("Фонове оновлення кешу виконано.")
        except Exception as e:
            logger.error(f"Помилка фонового оновлення кешу: {e}")


class QuizState(StatesGroup):
    in_quiz = State()


# ============================================================================
# Клавіатури
# ============================================================================

def chunk_rows(buttons, per_row=2):
    """Розкладає список кнопок по `per_row` в рядок — компактніша клавіатура
    замість однієї кнопки на весь рядок."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _short_label(text, limit=20):
    """Обрізає задовгий текст кнопки самостійно, акуратним «…» в кінці —
    інакше при 2 кнопках у рядок Telegram сам ріже текст потворним
    «…» посередині слова (напр. «Сер...три»)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def list_kb(buttons):
    """Стандартна клавіатура для списку пунктів: по 2 кнопки в рядок,
    знизу — «Головне меню». Кнопки «Назад»/«Вперед» додає render()
    автоматично, на основі історії переходів користувача."""
    buttons = [InlineKeyboardButton(text=_short_label(b.text), callback_data=b.callback_data) for b in buttons]
    rows = chunk_rows(buttons, 2)
    rows.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
    ])


def main_menu_kb():
    handbook = get_handbook_data()
    quiz_index = get_quiz_index()
    services = get_services_data()
    cat_count = len(handbook)
    services_count = sum(len(cities) for cities in services.values())
    handbook_label = f"📚 Довідник ({cat_count})" if cat_count else "📚 Довідник"
    services_label = f"🔧 Перелік СЦ ({services_count})" if services_count else "🔧 Перелік СЦ"
    quiz_label = f"📝 Тест ({len(quiz_index)})" if len(quiz_index) > 1 else "📝 Тест"
    rows = [
        # "Оновити дані" — окремим рядком зверху, як постійна утиліта.
        [InlineKeyboardButton(text="🔄 Оновити дані", callback_data="refresh")],
        [InlineKeyboardButton(text=handbook_label, callback_data="handbook"),
         InlineKeyboardButton(text=services_label, callback_data="svc")],
        [InlineKeyboardButton(text=quiz_label, callback_data="quiz_menu"),
         InlineKeyboardButton(text="🔥 Акції", callback_data="feed:promo")],
        [InlineKeyboardButton(text="🆕 Новинки", callback_data="feed:newitems"),
         InlineKeyboardButton(text="📰 Новини", callback_data="feed:news")],
    ]
    # Кнопки ШІ показуємо, лише якщо на хостингу справді налаштовано
    # GEMINI_API_KEY — інакше вони просто не з'являються в меню.
    if ai_client:
        rows.append([
            InlineKeyboardButton(text="⚖️ Порівняння", callback_data="ai_compare"),
            InlineKeyboardButton(text="🤖 ХОЧ? (by AI)", callback_data="ai_specs"),
        ])
    # "Відгуки" навмисно в нижньому правому куті — за проханням.
    rows.append([
        InlineKeyboardButton(text="💪 Мотивація", callback_data="feed:motivation"),
        InlineKeyboardButton(text="💬 Відгуки", callback_data="feedback"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def feedback_types_kb():
    buttons = [InlineKeyboardButton(text=label, callback_data=f"fbtype:{key}")
               for key, label in FEEDBACK_TYPES]
    return list_kb(buttons)


def feed_list_kb(feed_key, items):
    buttons = [InlineKeyboardButton(text=name, callback_data=f"feeditem:{feed_key}:{i}")
               for i, (name, _, _) in enumerate(items)]
    return list_kb(buttons)


def categories_kb(data):
    # ВАЖНО: в callback_data кладём индекс категории, а не её текст.
    # Раньше здесь было callback_data=f"cat:{cat}" — при длинном названии
    # категории (особенно на кириллице) это превышало лимит Telegram
    # в 64 байта и вызывало ошибку BUTTON_DATA_INVALID.
    categories = list(data.keys())
    buttons = [InlineKeyboardButton(text=f"{cat} ({len(data[cat])})", callback_data=f"cat:{i}")
               for i, cat in enumerate(categories)]
    return list_kb(buttons)


def sections_kb(sections):
    names = list(sections.keys())
    buttons = [InlineKeyboardButton(text=f"{s} ({len(sections[s])})", callback_data=f"hbsec:{i}")
               for i, s in enumerate(names)]
    return list_kb(buttons)


def section_categories_kb(data, sections, sec_idx):
    # Кнопки категорій усередині розділу ведуть на ті самі callback_data
    # "cat:{idx}", що й звичайний список категорій (idx — глобальний індекс
    # у списку категорій довідника) — тому cb_category/cb_item не змінюються.
    categories_all = list(data.keys())
    section_name = list(sections.keys())[sec_idx]
    buttons = []
    for cat in sections[section_name]:
        if cat in categories_all:
            idx = categories_all.index(cat)
            buttons.append(InlineKeyboardButton(text=f"{cat} ({len(data[cat])})", callback_data=f"cat:{idx}"))
    return list_kb(buttons)


def items_kb(items, cat_idx):
    # Аналогично: раньше было callback_data=f"item:{category}:{i}",
    # теперь используем индекс категории вместо её названия.
    buttons = [InlineKeyboardButton(text=name, callback_data=f"item:{cat_idx}:{i}")
               for i, (name, _, _) in enumerate(items)]
    return list_kb(buttons)


def services_categories_kb(data):
    categories = list(data.keys())
    buttons = [InlineKeyboardButton(text=f"{cat} ({sum(len(v) for v in data[cat].values())})", callback_data=f"svccat:{i}")
               for i, cat in enumerate(categories)]
    return list_kb(buttons)


def services_cities_kb(data, cat_idx):
    category = list(data.keys())[cat_idx]
    cities = list(data[category].keys())
    buttons = [InlineKeyboardButton(text=f"{city} ({len(data[category][city])})", callback_data=f"svccity:{cat_idx}:{i}")
               for i, city in enumerate(cities)]
    return list_kb(buttons)


def services_brands_kb(data, cat_idx, city_idx):
    category = list(data.keys())[cat_idx]
    city = list(data[category].keys())[city_idx]
    items = data[category][city]
    buttons = [InlineKeyboardButton(text=name, callback_data=f"svcitem:{cat_idx}:{city_idx}:{i}")
               for i, (name, _, _) in enumerate(items)]
    return list_kb(buttons)


def quiz_menu_kb(index):
    buttons = [InlineKeyboardButton(text=entry["name"], callback_data=f"quiz_pick:{i}")
               for i, entry in enumerate(index)]
    return list_kb(buttons)


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
        [InlineKeyboardButton(text="🔄 Ще раз", callback_data=f"quiz_pick:{quiz_idx}"),
         InlineKeyboardButton(text="📝 Інші тести", callback_data="quiz_menu")],
        [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")],
    ])


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Кеш file_id вже надісланих фото: url -> telegram_file_id.
# Перше відкриття картинки повільне (Telegram сам качає файл з Google Drive),
# але далі можна показувати те саме фото миттєво, без повторного скачування.
_photo_file_id_cache = {}

# ============================================================================
# Навігація «Назад» / «Вперед»
#
# Для кожного чату зберігаємо стек попередніх екранів (back) і стек екранів,
# з яких щойно пішли назад (forward) — так само, як історія в браузері.
# hist["extra"] — id "осиротілого" фото-повідомлення (коли підпис довший за
# ліміт Telegram і фото надсилається окремим повідомленням без кнопок):
# його потрібно прибирати вручну, бо звичайне видалення чистить лише
# повідомлення з кнопками.
# ============================================================================

_nav = {}


def _hist(chat_id):
    return _nav.setdefault(chat_id, {"back": [], "forward": [], "current": None, "extra": None})


def _nav_row(hist):
    row = []
    if hist["back"]:
        row.append(InlineKeyboardButton(text="◀️ Назад", callback_data="nav_back"))
    if hist["forward"]:
        row.append(InlineKeyboardButton(text="▶️ Вперед", callback_data="nav_forward"))
    return [row] if row else []


async def _clear_screen(call: CallbackQuery, hist: dict):
    """Прибирає поточний екран повністю: саме повідомлення з кнопками і,
    якщо було, окреме фото-повідомлення без підпису — щоб на екрані не
    лишалось жодних слідів попереднього стану."""
    try:
        await call.message.delete()
    except Exception as e:
        logger.warning(f"Не вдалося видалити повідомлення: {e}")
    if hist["extra"]:
        try:
            await bot.delete_message(call.message.chat.id, hist["extra"])
        except Exception as e:
            logger.warning(f"Не вдалося видалити фото попереднього екрана: {e}")
        hist["extra"] = None


async def _deliver(message: Message, text: str, kb: InlineKeyboardMarkup, photo: str, hist: dict):
    if photo:
        # Якщо це фото вже надсилалося раніше — використовуємо готовий
        # file_id замість посилання, це миттєво (без звернення до Google Drive).
        to_send = _photo_file_id_cache.get(photo, photo)
        try:
            if len(text) <= 1024:
                sent = await message.answer_photo(
                    photo=to_send, caption=text, parse_mode="HTML", reply_markup=kb
                )
                if photo not in _photo_file_id_cache and sent.photo:
                    _photo_file_id_cache[photo] = sent.photo[-1].file_id
            else:
                # Подпис до фото в Telegram обмежена 1024 символами —
                # якщо опис довший, надсилаємо фото окремо, а текст нижче.
                # Id цього фото-повідомлення запам'ятовуємо в hist["extra"],
                # щоб прибрати його разом з екраном при наступному переході.
                sent_photo = await message.answer_photo(photo=to_send)
                hist["extra"] = sent_photo.message_id
                if photo not in _photo_file_id_cache and sent_photo.photo:
                    _photo_file_id_cache[photo] = sent_photo.photo[-1].file_id
                await message.answer(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            logger.error(f"Помилка фото, показую без фото: {e}")
            # Якщо кешований file_id раптом став невалідним (фото видалили
            # на боці Telegram) — прибираємо з кешу, щоб не зациклюватись.
            _photo_file_id_cache.pop(photo, None)

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def render(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup, photo: str = None, *, push_history: bool = True):
    """
    Показує новий екран замість старого: видаляє попереднє повідомлення
    (і його "хвіст", якщо був) і надсилає нове. Так у чаті не лишається
    жодних слідів попередніх екранів — ані тексту, ані фото.
    Editing нельзя использовать, потому что Telegram не даёт превратить
    текстовое сообщение в фото (и наоборот) через edit_text/edit_media.

    Кожен виклик з push_history=True (за замовчуванням) додає попередній
    екран у стек "Назад" і скидає стек "Вперед" — так само, як перехід за
    новим посиланням у браузері.
    """
    hist = _hist(call.message.chat.id)
    await _clear_screen(call, hist)

    if push_history and hist["current"] is not None:
        hist["back"].append(hist["current"])
        hist["forward"].clear()
    hist["current"] = {"text": text, "kb": kb, "photo": photo}

    final_kb = InlineKeyboardMarkup(inline_keyboard=list(kb.inline_keyboard) + _nav_row(hist))
    await _deliver(call.message, text, final_kb, photo, hist)


async def _go_history(call: CallbackQuery, hist: dict, snapshot: dict):
    await _clear_screen(call, hist)
    hist["current"] = snapshot
    final_kb = InlineKeyboardMarkup(inline_keyboard=list(snapshot["kb"].inline_keyboard) + _nav_row(hist))
    await _deliver(call.message, snapshot["text"], final_kb, snapshot["photo"], hist)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    chat_id = message.chat.id
    _nav[chat_id] = {"back": [], "forward": [], "current": None, "extra": None}
    hist = _nav[chat_id]
    name = message.from_user.first_name or "колего"
    text = (
        f"{BRAND} · Помічник продавця\n{DIVIDER}\n\n"
        f"👋 Привіт, {name}! Я поруч, щоб швидко підказати потрібну інформацію.\n\n"
        "Обери розділ нижче 👇"
    )
    kb = main_menu_kb()
    hist["current"] = {"text": text, "kb": kb, "photo": None}
    final_kb = InlineKeyboardMarkup(inline_keyboard=list(kb.inline_keyboard) + _nav_row(hist))
    await message.answer(text, parse_mode="HTML", reply_markup=final_kb)


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    text = f"{BRAND}\n{DIVIDER}\n\n🏠 Головне меню — обери розділ:"
    await render(call, text, main_menu_kb())


@dp.callback_query(F.data == "nav_back")
async def cb_nav_back(call: CallbackQuery, state: FSMContext):
    hist = _hist(call.message.chat.id)
    if not hist["back"]:
        await call.answer("Це перший екран", show_alert=True)
        return
    await state.clear()
    prev = hist["back"].pop()
    if hist["current"] is not None:
        hist["forward"].append(hist["current"])
    await call.answer()
    await _go_history(call, hist, prev)


@dp.callback_query(F.data == "nav_forward")
async def cb_nav_forward(call: CallbackQuery, state: FSMContext):
    hist = _hist(call.message.chat.id)
    if not hist["forward"]:
        await call.answer("Далі нічого немає", show_alert=True)
        return
    await state.clear()
    nxt = hist["forward"].pop()
    if hist["current"] is not None:
        hist["back"].append(hist["current"])
    await call.answer()
    await _go_history(call, hist, nxt)


@dp.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery):
    await refresh_all_data(force=True)
    await call.answer("✅ Дані оновлено!", show_alert=True)


@dp.callback_query(F.data == "handbook")
async def cb_handbook(call: CallbackQuery):
    data = get_handbook_data()
    if not data:
        await call.answer("⚠️ Довідник порожній або помилка з'єднання", show_alert=True)
        return
    sections = get_handbook_sections()
    await call.answer()
    if sections:
        text = f"🏠 Головне меню › 📚 Довідник\n{DIVIDER}\n\nОберіть розділ:"
        await render(call, text, sections_kb(sections))
    else:
        text = f"🏠 Головне меню › 📚 Довідник\n{DIVIDER}\n\nОберіть категорію:"
        await render(call, text, categories_kb(data))


@dp.callback_query(F.data.startswith("hbsec:"))
async def cb_handbook_section(call: CallbackQuery):
    idx_str = call.data.split(":", 1)[1]
    if not idx_str.isdigit():
        await call.answer("Помилка розділу", show_alert=True)
        return
    idx = int(idx_str)
    sections = get_handbook_sections()
    names = list(sections.keys())
    if idx >= len(names):
        await call.answer("Розділ не знайдено", show_alert=True)
        return
    data = get_handbook_data()
    section_name = names[idx]
    await call.answer()
    text = f"🏠 Головне меню › 📚 Довідник › {section_name}\n{DIVIDER}\n\nОберіть категорію:"
    await render(call, text, section_categories_kb(data, sections, idx))


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
    text = f"📌 <b>{name}</b>\n🗂 {category}\n{DIVIDER}\n\n{desc}\n\n{BRAND_FOOTER}"
    photo = convert_drive_link(photo_url)
    await call.answer()
    await render(call, text, home_kb(), photo=photo)


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
    text = f"🔧 <b>{brand}</b>\n🗂 {category} · 📍 {city}\n{DIVIDER}\n\n{desc}\n\n{BRAND_FOOTER}"
    photo = convert_drive_link(photo_url)
    await call.answer()
    await render(call, text, home_kb(), photo=photo)


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
        await render(call, text, home_kb())
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
    text = f"📌 <b>{name}</b>\n{DIVIDER}\n\n{desc}\n\n{BRAND_FOOTER}" if desc else f"📌 <b>{name}</b>\n\n{BRAND_FOOTER}"
    photo = convert_drive_link(photo_url)
    await call.answer()
    await render(call, text, home_kb(), photo=photo)


@dp.message(QuizState.in_quiz, F.text)
async def handle_text_in_state(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode")
    if mode == "feedback":
        text_in = message.text.strip()
        if len(text_in) < 3:
            await message.answer(
                "⚠️ Занадто коротко. Опишіть трохи детальніше:",
                reply_markup=cancel_kb(),
            )
            return
        fb_type = data.get("fb_type", "💬 Відгук")
        prompt_msg_id = data.get("fb_prompt_msg_id")
        await state.clear()
        # Прибираємо запит форми й повідомлення користувача — на екрані
        # лишається тільки підтвердження, без зайвих слідів.
        if prompt_msg_id:
            try:
                await bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception as e:
                logger.warning(f"Не вдалося видалити повідомлення форми: {e}")
        user = message.from_user
        save_feedback(user.id, user.username, user.full_name, fb_type, text_in)
        try:
            await message.delete()
        except Exception as e:
            logger.warning(f"Не вдалося видалити повідомлення користувача: {e}")

        admin_chat_id = os.getenv("ADMIN_CHAT_ID")
        if admin_chat_id:
            who = f"@{user.username}" if user.username else (user.full_name or f"ID {user.id}")
            try:
                await bot.send_message(
                    admin_chat_id,
                    f"{fb_type}\n{DIVIDER}\n\nВід: {who}\n\n{text_in}",
                )
            except Exception as e:
                logger.error(f"Не вдалося надіслати сповіщення адміну: {e}")

        text = f"✅ Дякуємо! Ваше повідомлення надіслано.\n\n{BRAND_FOOTER}"
        hist = _hist(message.chat.id)
        kb = main_menu_kb()
        hist["current"] = {"text": text, "kb": kb, "photo": None}
        final_kb = InlineKeyboardMarkup(inline_keyboard=list(kb.inline_keyboard) + _nav_row(hist))
        await message.answer(text, parse_mode="HTML", reply_markup=final_kb)
    elif mode in ("compare", "specs"):
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("📷 Будь ласка, надішліть фото (не текст).", reply_markup=cancel_kb())
    elif mode == "quiz":
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("⬆️ Обери варіант відповіді кнопкою вище.")


@dp.callback_query(F.data == "feedback")
async def cb_feedback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    text = f"🏠 Головне меню › 💬 Зворотний зв'язок\n{DIVIDER}\n\nЩо хочете залишити?"
    await render(call, text, feedback_types_kb())


@dp.callback_query(F.data.startswith("fbtype:"))
async def cb_feedback_type(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    label = dict(FEEDBACK_TYPES).get(key)
    if not label:
        await call.answer("Помилка типу", show_alert=True)
        return
    await state.set_state(QuizState.in_quiz)
    await state.update_data(mode="feedback", fb_type=label, fb_prompt_msg_id=call.message.message_id)
    await call.answer()
    await call.message.edit_text(
        f"{label}\n{DIVIDER}\n\nНапишіть текст повідомлення:",
        reply_markup=cancel_kb(),
    )


@dp.callback_query(F.data == "ai_compare")
async def cb_ai_compare(call: CallbackQuery, state: FSMContext):
    if not ai_client:
        await call.answer("⚠️ ШІ не налаштовано на цьому боті", show_alert=True)
        return
    await state.set_state(QuizState.in_quiz)
    await state.update_data(mode="compare", photos=[], prompt_msg_id=call.message.message_id)
    await call.answer()
    await call.message.edit_text(
        f"⚖️ Порівняння товарів (ШІ)\n{DIVIDER}\n\n"
        "Надішліть перше фото — цінник або товар, де видно модель 📷",
        reply_markup=cancel_kb(),
    )


@dp.callback_query(F.data == "ai_specs")
async def cb_ai_specs(call: CallbackQuery, state: FSMContext):
    if not ai_client:
        await call.answer("⚠️ ШІ не налаштовано на цьому боті", show_alert=True)
        return
    await state.set_state(QuizState.in_quiz)
    await state.update_data(mode="specs", photos=[], prompt_msg_id=call.message.message_id)
    await call.answer()
    await call.message.edit_text(
        f"🤖 ХОЧ? (by AI)\n{DIVIDER}\n\n"
        "Надішліть фото цінника або товару 📷",
        reply_markup=cancel_kb(),
    )


async def _finish_ai_flow(message: Message, state: FSMContext, mode: str, photos: list, prompt_msg_id):
    """Спільна логіка завершення ШІ-сценарію: прибирає запит і фото
    користувача (без слідів — так само, як і всюди в боті), запитує
    Gemini Vision і показує готову відповідь із домашньою кнопкою."""
    await state.clear()
    if prompt_msg_id:
        try:
            await bot.delete_message(message.chat.id, prompt_msg_id)
        except Exception as e:
            logger.warning(f"Не вдалося видалити запит ШІ: {e}")
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Не вдалося видалити фото користувача: {e}")

    wait_msg = await message.answer("🤖 Аналізую фото... це займе кілька секунд")
    try:
        if mode == "specs":
            answer = await ask_ai(SPECS_SYSTEM_PROMPT, photos, "Ось фото товару/цінника. Дай характеристики і вигоду для клієнта.")
            header = "🤖 ХОЧ? (by AI)"
        else:
            answer = await ask_ai(COMPARE_SYSTEM_PROMPT, photos, "Ось фото двох товарів. Порівняй їх і дай рекомендацію.")
            header = "⚖️ Порівняння товарів (ШІ)"
        text = f"{header}\n{DIVIDER}\n\n{answer}\n\n{BRAND_FOOTER}"
    except Exception as e:
        logger.error(f"Помилка запиту до ШІ: {e}")
        text = (
            "⚠️ Не вдалося отримати відповідь від ШІ. Спробуйте ще раз "
            "трохи пізніше або перевірте фото (має бути чітким і не задовгим)."
        )
    try:
        await wait_msg.delete()
    except Exception:
        pass

    hist = _hist(message.chat.id)
    kb = home_kb()
    hist["current"] = {"text": text, "kb": kb, "photo": None}
    final_kb = InlineKeyboardMarkup(inline_keyboard=list(kb.inline_keyboard) + _nav_row(hist))
    await message.answer(text, parse_mode="HTML", reply_markup=final_kb)


@dp.message(QuizState.in_quiz, F.photo)
async def handle_photo_in_state(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode")
    if mode not in ("compare", "specs"):
        return

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    img_bytes = buf.read()

    photos = data.get("photos", [])
    photos.append(img_bytes)
    prompt_msg_id = data.get("prompt_msg_id")

    if mode == "specs" or len(photos) >= 2:
        await _finish_ai_flow(message, state, mode, photos, prompt_msg_id)
        return

    # "compare": перше фото отримано, чекаємо на друге
    await state.update_data(photos=photos)
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Не вдалося видалити фото користувача: {e}")
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=prompt_msg_id,
            text=f"⚖️ Порівняння товарів (ШІ)\n{DIVIDER}\n\nПерше фото отримано ✅\n\nНадішліть друге фото 📷",
            reply_markup=cancel_kb(),
        )
    except Exception as e:
        logger.warning(f"Не вдалося оновити запит на друге фото: {e}")


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
    await refresh_all_data(force=True)
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
    data = get_handbook_data()
    services_data = get_services_data()
    all_items = []
    for feed in FEEDS.values():
        all_items.extend(get_feed_data(feed["sheet"], feed["cache_key"]))
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
    # Чекаємо, поки кеш повністю прогріється, і лише потім починаємо приймати
    # повідомлення — перший-ліпший користувач одразу отримує миттєву відповідь
    # замість очікування на перше (найповільніше) звернення до Google Таблиць.
    await preload_cache()
    asyncio.create_task(background_refresh_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
