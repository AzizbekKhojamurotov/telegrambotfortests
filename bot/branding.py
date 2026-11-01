"""Bot profile: descriptions, command menu, profile photo and the welcome banner."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, FSInputFile, InputProfilePhotoStatic

from . import config

log = logging.getLogger(__name__)

AVATAR = config.ROOT / "assets" / "avatar.jpg"
BANNER = config.ROOT / "assets" / "banner.jpg"

# shown in an empty chat before the user presses Start (max 512 chars)
DESCRIPTION = (
    "📝 Test yasovchi bot test savollaringizni bir zumda Kahoot va Blooket uchun tayyor import "
    "fayliga aylantiradi.\n\n"
    "📥 Word, PDF, Excel, PowerPoint, rasm, oddiy matn, hatto ChatGPT yoki Gemini'dan nusxalangan "
    "testni ham qabul qiladi.\n"
    "🎯 To'g'ri javoblarni belgilar, qalin yozuv yoki javoblar kalitidan o'zi topadi.\n"
    "⚡️ Natija: Kahoot (.xlsx) va Blooket (.csv) fayli.\n\n"
    "Boshlash uchun «Start» tugmasini bosing 👇"
)

# profile "bio" and the text shown when the bot is shared (max 120 chars)
SHORT_DESCRIPTION = "Testlaringizni Kahoot (.xlsx) va Blooket (.csv) fayliga aylantiraman. Word, PDF, Excel, rasm, matn."

COMMANDS = [
    BotCommand(command="start", description="🏠 Bosh sahifa"),
    BotCommand(command="new", description="🆕 Yangi test boshlash"),
    BotCommand(command="guide", description="📥 Faylni Kahoot / Blooket'ga yuklash"),
    BotCommand(command="help", description="📖 Qo'llab-quvvatlanadigan formatlar"),
]

_banner_file_id: str | None = None


def banner():
    """The welcome banner: uploaded once, then re-sent by file_id."""
    return _banner_file_id or FSInputFile(BANNER)


def remember_banner(file_id: str):
    global _banner_file_id
    _banner_file_id = file_id


async def apply(bot: Bot):
    """Brings the bot's public profile in line with the texts above. Failures are logged, never fatal."""
    steps = [
        ("commands", bot.set_my_commands(COMMANDS)),
        ("description", bot.set_my_description(DESCRIPTION)),
        ("short description", bot.set_my_short_description(SHORT_DESCRIPTION)),
    ]
    for name, call in steps:
        try:
            await call
        except Exception as e:
            log.warning("Could not set %s: %s", name, e)
    try:
        me = await bot.get_me()
        photos = await bot.get_user_profile_photos(me.id, limit=1)
        # only when the bot has no photo: every upload would add another one to the profile history
        if photos.total_count == 0 and AVATAR.exists():
            await bot.set_my_profile_photo(InputProfilePhotoStatic(photo=FSInputFile(AVATAR)))
            log.info("Profile photo set")
    except Exception as e:
        log.warning("Could not set profile photo: %s", e)
