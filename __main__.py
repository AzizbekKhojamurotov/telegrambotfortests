import asyncio
import hashlib
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from . import ai, config
from .handlers import router

log = logging.getLogger("bot")


def _secret() -> str:
    # Telegram allows A-Z a-z 0-9 _ - in the secret token; derive one from the bot token if none is set
    return config.WEBHOOK_SECRET or hashlib.sha256(config.BOT_TOKEN.encode()).hexdigest()


async def _set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="new", description="Yangi test"),
        BotCommand(command="help", description="Qo'llab-quvvatlanadigan formatlar"),
    ])


async def run_polling(bot: Bot, dp: Dispatcher):
    info = await bot.get_webhook_info()
    if info.url and os.getenv("FORCE_POLLING") != "1":
        # the bot is deployed (Render): polling here would take its updates away
        sys.exit(
            f"Bot serverda ishlayapti (webhook: {info.url}).\n"
            "Lokal ishga tushirish uchun FORCE_POLLING=1 qo'ying — lekin bu serverdagi botni to'xtatadi."
        )
    if info.url:
        await bot.delete_webhook()
    await _set_commands(bot)
    await dp.start_polling(bot)


def run_webhook(bot: Bot, dp: Dispatcher):
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    # requests are authenticated by the X-Telegram-Bot-Api-Secret-Token header, not by the path
    path = "/webhook"
    url = config.WEBHOOK_BASE + path

    async def on_startup(bot: Bot):
        await bot.set_webhook(url, secret_token=_secret(), allowed_updates=dp.resolve_used_update_types())
        await _set_commands(bot)
        log.info("Webhook set: %s", url)

    async def health(_request):
        return web.Response(text="ok")

    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=_secret()).register(app, path=path)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=config.PORT, print=None)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        sys.exit("BOT_TOKEN topilmadi: .env faylga BOT_TOKEN=... yozing (.env.example'ga qarang).")
    if not ai.available():
        logging.warning("GEMINI_API_KEY yo'q: rasm va skan PDF o'qilmaydi, AI zaxira o'chiq.")
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    if config.WEBHOOK_BASE:
        run_webhook(bot, dp)
    else:
        asyncio.run(run_polling(bot, dp))


if __name__ == "__main__":
    main()
