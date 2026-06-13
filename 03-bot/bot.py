"""
bot.py — aiogram 3.x setup: создание Bot, Dispatcher, роутеры.
"""

import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

load_dotenv()

bot = Bot(
    token=os.environ["CONTENT_BRAIN_BOT_TOKEN"],
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()
