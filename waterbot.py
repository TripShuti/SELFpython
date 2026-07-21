import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import JobLookupError

TOKEN = "------"

bot = Bot(token=TOKEN)
dp = Dispatcher()

jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///waterbot_jobs.sqlite')
}

scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="Europe/Kyiv")

kb_wake = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🌅 Доброго ранку")]],
    resize_keyboard=True
)

kb_active = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💧 Водіческі попіт")],
        [KeyboardButton(text="😴 Надобраніч")]
    ],
    resize_keyboard=True
)

def get_user_name(user: types.User) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name

async def nag_user(user_id: int):
    try:
        await bot.send_message(
            user_id,
            "Агов, ти ще не попив води! 😠 Зроби ковток і натисни кнопку!",
            reply_markup=kb_active
        )
        logging.info(f"[НАБРИДАННЯ] Відправлено користувачу ID:{user_id}")
    except Exception as e:
        logging.error(f"Помилка відправки 'набридання' користувачу {user_id}: {e}")

async def remind_water(user_id: int):
    try:
        await bot.send_message(
            user_id,
            "Час попити водички! 💧 Як поп'єш — обов'язково натисни кнопку.",
            reply_markup=kb_active
        )
        logging.info(f"[НАГАДУВАННЯ] Відправлено користувачу ID:{user_id}")
    except Exception as e:
        logging.error(f"Помилка відправки 'нагадування' користувачу {user_id}: {e}")
    
    scheduler.add_job(
        nag_user, 
        'interval', 
        minutes=3, 
        args=[user_id], 
        id=f"nag_{user_id}", 
        replace_existing=True,
        misfire_grace_time=3600
    )

def schedule_next_reminder(user_id: int):
    try:
        scheduler.remove_job(f"nag_{user_id}")
    except JobLookupError:
        pass 

    run_time = datetime.now() + timedelta(minutes=90)
    scheduler.add_job(
        remind_water, 
        'date', 
        run_date=run_time, 
        args=[user_id], 
        id=f"main_{user_id}", 
        replace_existing=True,
        misfire_grace_time=3600
    )
    logging.info(f"[ТАЙМЕР] Встановлено нове нагадування для ID:{user_id} на {run_time.strftime('%H:%M:%S')}")

def cancel_all_jobs(user_id: int):
    for job_id in [f"main_{user_id}", f"nag_{user_id}"]:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass
    logging.info(f"[ТАЙМЕР] Усі таймери видалено для ID:{user_id}")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_name = get_user_name(message.from_user)
    logging.info(f"Користувач {user_name} запустив бота (/start)")
    await message.answer(
        "Привіт! Я твій бот для підтримки водного балансу. 🚰\n"
        "Натисни '🌅 Доброго ранку', коли почнеш свій день!",
        reply_markup=kb_wake
    )

@dp.message(F.text == "🌅 Доброго ранку")
async def wake_up(message: types.Message):
    user_id = message.from_user.id
    user_name = get_user_name(message.from_user)
    
    logging.info(f"➡️ {user_name} ПРОКИНУВСЯ")
    schedule_next_reminder(user_id)
    
    await message.answer(
        "Доброго ранку! ☀️ Таймер запущено. Я нагадаю тобі про воду через 1.5 години.",
        reply_markup=kb_active
    )

@dp.message(F.text == "💧 Водіческі попіт")
async def drank_water(message: types.Message):
    user_id = message.from_user.id
    user_name = get_user_name(message.from_user)
    
    is_nagging = scheduler.get_job(f"nag_{user_id}") is not None
    
    logging.info(f"💧 {user_name} ПОПИВ ВОДИ")
    schedule_next_reminder(user_id)
    
    if is_nagging:
        await message.answer(
            "Молодець! 🌊 Наступне нагадування рівно через півтори години.",
            reply_markup=kb_active
        )
    else:
        await message.answer(
            "Супер! П'єш воду навіть без нагадувань. 🚀 Таймер оновлено.",
            reply_markup=kb_active
        )

@dp.message(F.text == "😴 Надобраніч")
async def go_to_sleep(message: types.Message):
    user_id = message.from_user.id
    user_name = get_user_name(message.from_user)
    
    logging.info(f"🛌 {user_name} ПІШОВ СПАТИ")
    cancel_all_jobs(user_id)
    
    await message.answer(
        "Надобраніч! 🌙 Усі нагадування вимкнено до ранку.",
        reply_markup=kb_wake
    )

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    scheduler.start()
    logging.info("🚀 БОТ ЗАПУЩЕНИЙ І ГОТОВИЙ ДО РОБОТИ!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())