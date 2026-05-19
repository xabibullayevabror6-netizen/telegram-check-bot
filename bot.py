import asyncio
import cv2
import logging
import os
import re
import sqlite3
import uuid
import numpy as np
import aiohttp

from datetime import datetime
from bs4 import BeautifulSoup
from cachetools import TTLCache
from pyzbar.pyzbar import decode
from rapidfuzz import fuzz

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# ====================================
# LOGGING
# ====================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ====================================
# TOKEN
# ====================================
TOKEN = "8925382080:AAFYBGWTM6kROggOBBR64Langvrzkhgc540"

# ====================================
# BOT
# ====================================
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ====================================
# CACHE — bir xil URL ni qayta yuklamaslik (1 soat)
# ====================================
url_cache: TTLCache = TTLCache(maxsize=500, ttl=3600)

# ====================================
# RATE LIMIT — foydalanuvchi boshiga (soniyada max 1 ta)
# ====================================
user_last_request: dict[int, float] = {}
RATE_LIMIT_SECONDS = 3

# ====================================
# DATABASE
# ====================================
DB_PATH = "checks.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            username    TEXT,
            check_type  TEXT,
            url         TEXT,
            created_at  TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_check(user_id: int, username: str, check_type: str, url: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO checks (user_id, username, check_type, url, created_at) VALUES (?,?,?,?,?)",
        (user_id, username or "", check_type, url or "", datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT check_type, COUNT(*) FROM checks GROUP BY check_type")
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM checks")
    users = cur.fetchone()[0]
    conn.close()
    result = {"shaxsiy": 0, "korporativ": 0, "unknown": 0, "users": users}
    for check_type, count in rows:
        if check_type in result:
            result[check_type] = count
    return result

# ====================================
# KEYWORDS — kengaytirilgan
# ====================================
SHAXSIY_KEYWORDS = [
    # Uzbek
    "shaxsiy", "jismoniy", "fiz shaxs", "f.i.o", "pinfl", "jshir",
    "fuqaro", "passport",
    # Russian
    "физический", "физ лицо", "физлицо", "физ.", "ф.и.о",
    "гражданин", "паспорт",
    # English
    "individual", "personal", "natural person",
]

KORPORATIV_KEYWORDS = [
    # Uzbek
    "korporativ", "yuridik", "yuridik shaxs",
    # Russian
    "корпоратив", "юридическое", "юридическое лицо", "юр лицо",
    "юрлицо", "юл", "юр.",
    # English
    "company", "corporate", "corporation",
]

# ====================================
# FUZZY MATCH — 70% chegara
# ====================================
def contains_keyword(text: str, keywords: list, threshold: int = 70) -> bool:
    text_lower = text.lower()
    words = text_lower.split()

    for keyword in keywords:
        if keyword in text_lower:
            return True
        for word in words:
            if fuzz.ratio(word, keyword) >= threshold:
                return True

    return False

# ====================================
# URL TOPISH
# ====================================
def extract_url(qr_text: str) -> str | None:
    urls = re.findall(r'https?://[^\s\"\'><]+', qr_text, flags=re.IGNORECASE)
    return urls[0] if urls else None

# ====================================
# SAHIFANI YUKLASH — Playwright (JS) + aiohttp (fallback)
# ====================================
async def fetch_with_playwright(url: str) -> str:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=20000, wait_until="networkidle")
            await page.wait_for_timeout(2000)
            text = await page.inner_text("body")
            await browser.close()
            log.info(f"[PLAYWRIGHT OK] {len(text)} chars")
            return text.lower()
    except Exception as e:
        log.warning(f"[PLAYWRIGHT ERROR]: {e}")
        return ""

async def fetch_with_aiohttp(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                ssl=False
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[HTTP {resp.status}]: {url}")
                    return ""
                html = await resp.text(errors="ignore")

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "meta", "link"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r'\s+', ' ', text).strip()
        log.info(f"[AIOHTTP OK] {len(text)} chars")
        return text.lower()

    except asyncio.TimeoutError:
        log.warning(f"[TIMEOUT]: {url}")
        return ""
    except Exception as e:
        log.warning(f"[AIOHTTP ERROR]: {e}")
        return ""

async def fetch_page_text(url: str) -> str:
    # Cache tekshirish
    if url in url_cache:
        log.info(f"[CACHE HIT]: {url}")
        return url_cache[url]

    # 1) Playwright — JS sahifalar uchun
    text = await fetch_with_playwright(url)

    # 2) aiohttp — fallback
    if not text:
        text = await fetch_with_aiohttp(url)

    if text:
        url_cache[url] = text

    return text

# ====================================
# CHEK TURINI ANIQLASH
# ====================================
def detect_type(text: str) -> str:
    if not text.strip():
        return "unknown"

    is_shaxsiy = contains_keyword(text, SHAXSIY_KEYWORDS)
    is_korporativ = contains_keyword(text, KORPORATIV_KEYWORDS)

    if is_shaxsiy and is_korporativ:
        return "korporativ"  # korporativ ustun
    if is_korporativ:
        return "korporativ"
    if is_shaxsiy:
        return "shaxsiy"

    return "unknown"

# ====================================
# RASM PREPROCESSING — 9 variant
# ====================================
def preprocess_variants(image: np.ndarray) -> list:
    variants = [image]

    up2 = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    up3 = cv2.resize(image, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    variants += [up2, up3]

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray_up = cv2.cvtColor(up2, cv2.COLOR_BGR2GRAY)
    variants.append(gray)

    adaptive = cv2.adaptiveThreshold(
        gray_up, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    variants.append(adaptive)

    _, otsu = cv2.threshold(gray_up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)

    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    variants.append(cv2.filter2D(up2, -1, kernel))

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    variants.append(clahe.apply(gray_up))

    variants.append(cv2.bitwise_not(otsu))

    return variants

# ====================================
# QR O'QISH — asyncio.to_thread orqali (bloklashmaydi)
# ====================================
def _read_qr_sync(filename: str) -> str:
    image = cv2.imread(filename)
    if image is None:
        return ""

    all_texts = []
    for variant in preprocess_variants(image):
        try:
            for obj in decode(variant):
                text = obj.data.decode("utf-8", errors="ignore").strip()
                if text:
                    all_texts.append(text)
        except Exception:
            continue

    combined = " ".join(dict.fromkeys(all_texts))
    log.info(f"[QR RAW]: {combined[:200]!r}")
    return combined

async def read_qr(filename: str) -> str:
    return await asyncio.to_thread(_read_qr_sync, filename)

# ====================================
# RATE LIMIT TEKSHIRISH
# ====================================
def is_rate_limited(user_id: int) -> bool:
    import time
    now = time.time()
    last = user_last_request.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    user_last_request[user_id] = now
    return False

# ====================================
# /start
# ====================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "👋 <b>Chek tekshirish botiga xush kelibsiz!</b>\n\n"
        "📸 Chek rasmini yuboring — QR kodni o'qib, korporativ yoki shaxsiy ekanligini aniqlayman.\n\n"
        "📌 <b>Buyruqlar:</b>\n"
        "/start — botni ishga tushirish\n"
        "/stats — statistika",
        parse_mode="HTML"
    )

# ====================================
# /stats
# ====================================
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = get_stats()
    total = stats["shaxsiy"] + stats["korporativ"] + stats["unknown"]
    await message.reply(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{stats['users']}</b>\n"
        f"📋 Jami tekshirishlar: <b>{total}</b>\n\n"
        f"✅ Korporativ: <b>{stats['korporativ']}</b>\n"
        f"❌ Shaxsiy: <b>{stats['shaxsiy']}</b>\n"
        f"❓ Aniqlanmadi: <b>{stats['unknown']}</b>",
        parse_mode="HTML"
    )

# ====================================
# PHOTO HANDLER
# ====================================
@dp.message(F.photo | F.document)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    # Rate limit
    if is_rate_limited(user_id):
        await message.reply("⏳ Iltimos, biroz kuting va qayta yuboring.")
        return

    log.info(f"[FILE] user={user_id} (@{username})")
    filename = None

    try:
        # ====================================
        # FAYLNI YUKLASH
        # ====================================
        temp_id = uuid.uuid4().hex

        if message.photo:
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            filename = f"temp_{temp_id}.jpg"
            await bot.download(file, destination=filename)

        elif message.document:
            doc = message.document
            allowed_mime = {"image/jpeg", "image/png", "image/webp", "image/bmp"}
            if doc.mime_type not in allowed_mime:
                await message.reply("❌ Faqat rasm fayllari qabul qilinadi (jpg, png, webp, bmp)")
                return
            file = await bot.get_file(doc.file_id)
            filename = f"temp_{temp_id}_{doc.file_name}"
            await bot.download(file, destination=filename)

        else:
            return

        # ====================================
        # 1-QADAM: QR O'QISH
        # ====================================
        qr_raw = await read_qr(filename)

        if not qr_raw:
            await message.reply(
                "❌ QR kod o'qilmadi.\n\n"
                "💡 Rasmni to'g'ridan, yaxshi yorug'likda qaytadan olib ko'ring."
            )
            save_check(user_id, username, "unknown", "")
            return

        # ====================================
        # 2-QADAM: URL TOPISH
        # ====================================
        url = extract_url(qr_raw)
        log.info(f"[URL]: {url}")

        # ====================================
        # 3-QADAM: SAHIFANI YUKLASH
        # ====================================
        page_text = ""

        if url:
            await message.reply(
                f"🔗 Link topildi, sahifa yuklanmoqda...\n<code>{url}</code>",
                parse_mode="HTML"
            )
            page_text = await fetch_page_text(url)

            if not page_text:
                # Sahifa yuklanmadi — QR matniga fallback
                log.warning(f"[FALLBACK to QR text]: {url}")
                page_text = qr_raw.lower()
        else:
            page_text = qr_raw.lower()

        # ====================================
        # 4-QADAM: CHEK TURINI ANIQLASH
        # ====================================
        check_type = detect_type(page_text)
        log.info(f"[RESULT] user={user_id} type={check_type}")

        # DB ga saqlash
        save_check(user_id, username, check_type, url or "")

        # ====================================
        # JAVOB
        # ====================================
        if check_type == "shaxsiy":
            await message.reply(
                "❌ <b>Shaxsiy chek aniqlandi</b>\n\n"
                "⛽ Yoqilg'i quyish shaxobchasidan korporativ turdagi chekni olishni tavsiya qilamiz.\n"
                "Korporativ chek soliq hisoboti va xarajatlarni rasmiylashtirish uchun zarur.",
                parse_mode="HTML"
            )

        elif check_type == "korporativ":
            await message.reply(
                "✅ <b>Korporativ chek aniqlandi</b>",
                parse_mode="HTML"
            )

        else:
            await message.reply(
                "❓ <b>Chek turi aniqlanmadi</b>\n\n"
                "💡 Maslahat: Rasmni yaxshiroq yorug'likda, to'g'ridan olishga harakat qiling.",
                parse_mode="HTML"
            )

    except asyncio.TimeoutError:
        log.error(f"[TIMEOUT] user={user_id}")
        await message.reply("⏱ Sayt javob bermadi. Keyinroq urinib ko'ring.")

    except Exception as e:
        log.error(f"[ERROR] user={user_id}: {e}", exc_info=True)
        await message.reply(
            "❌ Kutilmagan xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            parse_mode="HTML"
        )

    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)

# ====================================
# MATN HANDLER
# ====================================
@dp.message(F.text)
async def handle_text(message: Message):
    await message.reply(
        "📸 Iltimos, chek rasmini yuboring.\n"
        "Rasm sifatli va QR kod ko'rinib turishi kerak."
    )

# ====================================
# START
# ====================================
async def main():
    init_db()
    log.info("BOT ISHLADI ✅")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
