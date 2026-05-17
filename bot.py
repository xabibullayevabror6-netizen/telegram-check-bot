import asyncio
import cv2
import os
import requests

from bs4 import BeautifulSoup
from pyzbar.pyzbar import decode
from rapidfuzz import fuzz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

# ====================================
# TOKEN
# ====================================
TOKEN = "8608586763:AAH17Il2eE3jUTlICLgQmTe97LyW7OLCBkI"

# ====================================
# BOT
# ====================================
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ====================================
# FUZZY MATCH
# ====================================
def similar(a, b):

    return fuzz.partial_ratio(a, b) >= 70

# ====================================
# QR O'QISH
# ====================================
def read_qr(filename):

    image = cv2.imread(filename)

    # kattalashtirish
    image = cv2.resize(
        image,
        None,
        fx=3,
        fy=3,
        interpolation=cv2.INTER_CUBIC
    )

    decoded = decode(image)

    qr_text = ""

    for obj in decoded:

        qr_text += obj.data.decode("utf-8")

    return qr_text

# ====================================
# OFD PAGE O'QISH
# ====================================
def get_check_type(url):

    try:

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        html = response.text.lower()

        soup = BeautifulSoup(html, "html.parser")

        text = soup.get_text(" ")

        print(text)

        # ====================================
        # SHAXSIY
        # ====================================
        shaxsiy_words = [
            "shaxsiy",
            "личный",
            "fiz",
        ]

        # ====================================
        # KORPORATIV
        # ====================================
        korporativ_words = [
            "korporativ",
            "корпоратив",
            "юр",
            "yuridik",
        ]

        for word in shaxsiy_words:

            if similar(text, word):
                return "shaxsiy"

        for word in korporativ_words:

            if similar(text, word):
                return "korporativ"

        return "unknown"

    except Exception as e:

        print(e)

        return "error"

# ====================================
# PHOTO HANDLER
# ====================================
@dp.message(F.photo | F.document)
async def handle_photo(message: Message):

    print("FILE KELDI ✅")

    try:

        # ====================================
        # PHOTO
        # ====================================
        if message.photo:

            photo = message.photo[-1]

            file = await bot.get_file(photo.file_id)

            filename = f"{message.message_id}.jpg"

            await bot.download(file, destination=filename)

        # ====================================
        # DOCUMENT
        # ====================================
        elif message.document:

            file = await bot.get_file(message.document.file_id)

            filename = message.document.file_name

            await bot.download(file, destination=filename)

        else:
            return

        # ====================================
        # QR
        # ====================================
        qr_text = read_qr(filename)

        print("QR:")
        print(qr_text)

        # ====================================
        # QR TOPILMADI
        # ====================================
        if not qr_text:

            await message.reply(
                "❌ QR kod topilmadi"
            )

            os.remove(filename)

            return

        # ====================================
        # CHECK TYPE
        # ====================================
        check_type = get_check_type(qr_text)

        print("TYPE:", check_type)

        # ====================================
        # SHAXSIY
        # ====================================
        if check_type == "shaxsiy":

            await message.reply(
                "⚠️ Shaxsiy chek aniqlandi"
            )

        # ====================================
        # KORPORATIV
        # ====================================
        elif check_type == "korporativ":

            await message.reply(
                "✅ Korporativ chek"
            )

        # ====================================
        # ANIQLANMADI
        # ====================================
        elif check_type == "unknown":

            await message.reply(
                "❓ Chek turi aniqlanmadi"
            )

        # ====================================
        # ERROR
        # ====================================
        else:

            await message.reply(
                "❌ OFD saytini o‘qib bo‘lmadi"
            )

        # ====================================
        # DELETE FILE
        # ====================================
        os.remove(filename)

    except Exception as e:

        print(e)

        await message.reply(
            f"❌ Error:\n{e}"
        )

# ====================================
# START
# ====================================
async def main():

    print("BOT ISHLADI ✅")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
