"""
main.py — запуск content-brain Telegram-бота (aiogram 3.x polling).

Использование:
  python main.py
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("content-brain-bot")


async def main():
    from aiogram.types import BotCommand
    from bot import bot, dp
    from handlers.analyze import router as analyze_router
    from handlers.ideas import router as ideas_router
    from handlers.post_writer import router as post_router
    from handlers.channel_monitor import router as monitor_router
    from handlers.gemini_import import router as gemini_router

    dp.include_router(analyze_router)
    dp.include_router(ideas_router)
    dp.include_router(post_router)
    dp.include_router(monitor_router)
    dp.include_router(gemini_router)

    await bot.set_my_commands([
        BotCommand(command="analyze", description="🔍 Полный анализ дневника + NotebookLM → идеи"),
        BotCommand(command="analyze_fast", description="⚡️ Быстрый анализ только по дневнику → идеи"),
        BotCommand(command="ideas", description="💡 Показать готовые идеи для постов"),
    ])

    logger.info("content-brain bot starting...")

    # channel_post — получать новые посты из канала (бот должен быть admin в никbase)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "channel_post"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
