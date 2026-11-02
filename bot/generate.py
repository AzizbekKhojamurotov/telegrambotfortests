"""AI quiz generation: from a topic or from uploaded material, with per-user daily limits."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import ai, config
from .extract import FMT_CLOSE, FMT_OPEN, UnsupportedFile, extract
from .handlers import SESSIONS, TEXT_DEBOUNCE, Session, Source, session_for, show_summary
from .models import Extracted, GenRequest, ParseResult
from .parser import strip_table_marks

log = logging.getLogger(__name__)
router = Router()

TASHKENT = timezone(timedelta(hours=5))
COUNTS = (5, 10, 15, 20, 30)
LEVEL_NAMES = {"easy": "🟢 Oson", "medium": "🟡 O'rta", "hard": "🔴 Qiyin"}
LANG_NAMES = {"uz": "🇺🇿 O'zbek", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
TOPIC_MAX = 300  # longer text is treated as material
TAG_RE = re.compile(f"{FMT_OPEN}[a-z]*{FMT_CLOSE}")

# user_id -> (day, tests generated that day). In memory: a restart resets the counters.
USAGE: dict[int, tuple[object, int]] = {}


# ---------------------------------------------------------------- limits

def _today():
    return datetime.now(TASHKENT).date()


def used_today(user_id: int) -> int:
    day, n = USAGE.get(user_id, (None, 0))
    return n if day == _today() else 0


def left_today(user_id: int) -> int:
    return max(0, config.GEN_DAILY_LIMIT - used_today(user_id))


def _spend(user_id: int):
    USAGE[user_id] = (_today(), used_today(user_id) + 1)


LIMIT_TEXT = (
    f"⛔️ Bugun {config.GEN_DAILY_LIMIT} ta test tuzish limiti tugadi.\n"
    "Limit Toshkent vaqti bilan soat 00:00 da yangilanadi. Tayyor testlarni fayl yoki matn qilib "
    "yuborishingiz mumkin, ular uchun limit yo'q."
)


# ---------------------------------------------------------------- state helpers

def _gen(user_id: int, *stages: str) -> GenRequest | None:
    s = SESSIONS.get(user_id)
    if s is None or s.gen is None:
        return None
    return s.gen if not stages or s.gen.stage in stages else None


def awaiting_source(message: Message) -> bool:
    return message.from_user is not None and _gen(message.from_user.id, "source", "options") is not None


def running(message: Message) -> bool:
    return message.from_user is not None and _gen(message.from_user.id, "running") is not None


def _guess_lang(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if letters and sum("а" <= c.lower() <= "я" or c in "ёЁўЎқҚғҒҳҲ" for c in letters) / len(letters) > 0.5:
        return "ru"
    return "uz"


def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def _clean_material(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", TAG_RE.sub("", strip_table_marks(text))).strip()


# ---------------------------------------------------------------- texts / keyboards

SOURCE_TEXT = (
    "✨ <b>AI bilan test tuzish</b>\n\n"
    "Quyidagilardan birini yuboring:\n"
    "📝 <b>Mavzu</b>, masalan: <i>7-sinf biologiya: hujayra tuzilishi</i>\n"
    "📎 <b>Material</b>: dars matni, konspekt yoki fayl (Word, PDF, PowerPoint, TXT, rasm)\n\n"
    f"Cheklovlar: fayl {config.GEN_MAX_FILE_BYTES // (1024 * 1024)} MB gacha, matn "
    f"{_n(config.GEN_MAX_CHARS)} belgigacha (≈10 bet), test {config.GEN_MAX_QUESTIONS} savolgacha.\n"
    "Bugun yana <b>{left}</b> ta test tuzishingiz mumkin."
)


def _source_label(g: GenRequest) -> str:
    if g.topic:
        return f"📝 Mavzu: <i>{html.escape(g.topic[:120])}</i>"
    if g.media:
        return f"📎 Material: <i>{html.escape(g.source_name)}</i> (AI o'qiydi)"
    return f"📎 Material: <i>{html.escape(g.source_name)}</i> · {_n(len(g.material))} belgi"


def options_text(g: GenRequest, left: int) -> str:
    return (
        "✨ <b>AI bilan test tuzish</b>\n\n"
        f"{_source_label(g)}\n\n"
        f"🔢 Savollar: <b>{g.count}</b> · 🎚 Qiyinlik: <b>{LEVEL_NAMES[g.level]}</b> · 🌐 Til: <b>{LANG_NAMES[g.lang]}</b>\n\n"
        "Sozlamalarni tanlang va <b>✨ Tuzish</b> tugmasini bosing.\n"
        f"Bugun qolgan: <b>{left}</b> ta test. Boshqa mavzu yoki fayl yuborsangiz, manba almashadi."
    )


def options_kb(g: GenRequest) -> InlineKeyboardMarkup:
    def mark(on: bool, text: str) -> str:
        return f"✅ {text}" if on else text

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=mark(g.count == n, str(n)), callback_data=f"g:n:{n}") for n in COUNTS],
        [InlineKeyboardButton(text=mark(g.level == k, v), callback_data=f"g:l:{k}") for k, v in LEVEL_NAMES.items()],
        [InlineKeyboardButton(text=mark(g.lang == k, v), callback_data=f"g:t:{k}") for k, v in LANG_NAMES.items()],
        [InlineKeyboardButton(text="✨ Tuzish", callback_data="g:go"),
         InlineKeyboardButton(text="✖️ Bekor qilish", callback_data="g:x")],
    ])


CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Bekor qilish", callback_data="g:x")]])


# ---------------------------------------------------------------- entry

async def _start(message: Message, user_id: int):
    if not ai.available():
        await message.answer("🤖 Test tuzish uchun botda GEMINI_API_KEY sozlanmagan.")
        return
    left = left_today(user_id)
    if left == 0:
        await message.answer(LIMIT_TEXT)
        return
    SESSIONS.pop(user_id, None)  # a generated quiz starts a fresh test
    s = session_for(user_id)
    s.gen = GenRequest()
    await message.answer(SOURCE_TEXT.replace("{left}", str(left)), reply_markup=CANCEL_KB)


@router.message(Command("generate"))
async def cmd_generate(message: Message):
    await _start(message, message.from_user.id)


@router.callback_query(F.data == "gen:start")
async def cb_generate(cb: CallbackQuery):
    await cb.answer()
    await _start(cb.message, cb.from_user.id)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    s = SESSIONS.get(message.from_user.id)
    if s and s.gen and s.gen.stage != "running":
        s.gen = None
        await message.answer("✖️ Test tuzish bekor qilindi.")
    else:
        await message.answer("Bekor qilinadigan jarayon yo'q.")


@router.callback_query(F.data == "g:x")
async def cb_cancel(cb: CallbackQuery):
    s = SESSIONS.get(cb.from_user.id)
    if s and s.gen and s.gen.stage == "running":
        await cb.answer("Test tuzilmoqda, biroz kuting.", show_alert=True)
        return
    if s:
        s.gen = None
    await cb.answer("Bekor qilindi")
    try:
        await cb.message.edit_text("✖️ Test tuzish bekor qilindi. Qayta boshlash: /generate")
    except Exception:
        pass


# ---------------------------------------------------------------- source input

async def _show_options(message: Message, s: Session):
    s.gen.stage = "options"
    await message.answer(options_text(s.gen, left_today(message.from_user.id)), reply_markup=options_kb(s.gen))


@router.message(F.text & ~F.text.startswith("/"), awaiting_source)
async def gen_text(message: Message):
    s = session_for(message.from_user.id)
    s.gen.parts.append(message.text)
    if s.debounce and not s.debounce.done():
        s.debounce.cancel()
    # long pasted material arrives as several messages: wait until they stop
    s.debounce = asyncio.create_task(_text_after_pause(message, s))


async def _text_after_pause(message: Message, s: Session):
    try:
        await asyncio.sleep(TEXT_DEBOUNCE)
    except asyncio.CancelledError:
        return
    if s.debounce is asyncio.current_task():
        s.debounce = None
    g = s.gen
    if g is None:
        return
    text = "\n".join(g.parts).strip()
    g.parts.clear()
    g.topic = g.material = ""
    g.media = None
    if len(text) <= TOPIC_MAX and "\n" not in text:
        g.topic = text
        g.source_name = "mavzu"
    else:
        if len(text) > config.GEN_MAX_CHARS:
            await message.answer(
                f"❌ Matn juda uzun: {_n(len(text))} belgi. Ko'pi bilan {_n(config.GEN_MAX_CHARS)} belgi "
                "(≈10 bet) yuboring."
            )
            return
        g.material = text
        g.source_name = "matn"
    g.lang = _guess_lang(text)
    await _show_options(message, s)


@router.message(F.document, awaiting_source)
async def gen_document(message: Message):
    s = session_for(message.from_user.id)
    doc = message.document
    name = doc.file_name or "fayl"
    if doc.file_size and doc.file_size > config.GEN_MAX_FILE_BYTES:
        await message.reply(
            f"❌ Material fayli {config.GEN_MAX_FILE_BYTES // (1024 * 1024)} MB dan katta "
            f"({doc.file_size / 1024 / 1024:.1f} MB). Kichikroq fayl yoki faqat kerakli qismini yuboring."
        )
        return
    status = await message.reply(f"⏳ <b>{html.escape(name)}</b> o'qilmoqda…")
    try:
        buf = await message.bot.download(doc)
        ex = await asyncio.to_thread(extract, buf.read(), name)
    except UnsupportedFile as e:
        await status.edit_text(f"❌ {html.escape(str(e))}")
        return
    except Exception:
        log.exception("material extract failed for %s", name)
        await status.edit_text("❌ Faylni o'qib bo'lmadi. Fayl buzilmaganini tekshiring yoki boshqa formatda yuboring.")
        return
    await _set_material(status, s, message.from_user.id, name, ex)


@router.message(F.photo, awaiting_source)
async def gen_photo(message: Message):
    s = session_for(message.from_user.id)
    status = await message.reply("⏳ Rasm qabul qilindi…")
    buf = await message.bot.download(message.photo[-1])
    await _set_material(status, s, message.from_user.id, "rasm",
                        Extracted(media=(buf.read(), "image/jpeg"), note="image"))


async def _set_material(status: Message, s: Session, user_id: int, name: str, ex: Extracted):
    g = s.gen
    if g is None:
        return
    text = _clean_material(ex.text)
    g.topic = ""
    g.source_name = name
    if ex.media is not None and (ex.note == "image" or len(text) < 200):
        g.material, g.media = "", ex.media  # photo or scanned PDF: Gemini reads it itself
    else:
        if len(text) < 100:
            await status.edit_text("😕 Fayldan deyarli matn topilmadi. Boshqa fayl yoki mavzu yuboring.")
            return
        if len(text) > config.GEN_MAX_CHARS:
            await status.edit_text(
                f"❌ Material juda uzun: {_n(len(text))} belgi. Ko'pi bilan {_n(config.GEN_MAX_CHARS)} belgi "
                "(≈10 bet) bo'lishi kerak. Faylning kerakli qismini alohida yuboring."
            )
            return
        g.material, g.media = text, None
        g.lang = _guess_lang(text)
    g.stage = "options"
    await status.edit_text(options_text(g, left_today(user_id)), reply_markup=options_kb(g))


@router.message(running)
async def gen_busy(message: Message):
    await message.reply("⏳ Test tuzilmoqda, biroz kuting…")


# ---------------------------------------------------------------- options

@router.callback_query(F.data.regexp(r"^g:[nlt]:"))
async def cb_option(cb: CallbackQuery):
    g = _gen(cb.from_user.id, "options")
    if g is None:
        await cb.answer("Bu sozlama eskirgan. Qayta boshlash: /generate", show_alert=True)
        return
    _, kind, value = cb.data.split(":")
    if kind == "n" and value.isdigit() and int(value) in COUNTS:
        g.count = int(value)
    elif kind == "l" and value in LEVEL_NAMES:
        g.level = value
    elif kind == "t" and value in LANG_NAMES:
        g.lang = value
    await cb.answer()
    try:
        await cb.message.edit_text(options_text(g, left_today(cb.from_user.id)), reply_markup=options_kb(g))
    except Exception:
        pass  # same option pressed twice: "message is not modified"


@router.callback_query(F.data == "g:go")
async def cb_go(cb: CallbackQuery):
    user_id = cb.from_user.id
    s = SESSIONS.get(user_id)
    g = _gen(user_id)
    if s is None or g is None or g.stage == "source":
        await cb.answer("Avval mavzu yoki material yuboring. Qayta boshlash: /generate", show_alert=True)
        return
    if g.stage == "running":
        await cb.answer("Test tuzilmoqda, biroz kuting.")
        return
    if left_today(user_id) == 0:
        s.gen = None
        await cb.answer()
        await cb.message.edit_text(LIMIT_TEXT)
        return
    g.stage = "running"
    await cb.answer()
    await cb.message.edit_text(
        f"⏳ <b>{g.count} ta savol tuzilmoqda…</b>\n{_source_label(g)}\n\nBu 20–60 soniya davom etishi mumkin."
    )
    try:
        questions = await ai.generate(g.count, g.level, g.lang, topic=g.topic, material=g.material, media=g.media)
    except ai.AIError as e:
        log.warning("generate failed: %s", e)
        questions, error = [], str(e)
    except Exception:
        log.exception("generate crashed")
        questions, error = [], "kutilmagan xato"
    else:
        error = ""
    if not questions:
        g.stage = "options"
        reason = "AI hozir band" if "429" in error or "503" in error else "test tuzib bo'lmadi"
        await cb.message.edit_text(
            f"❌ Kechirasiz, {reason}. Limit sarflanmadi — birozdan keyin qayta urinib ko'ring.\n\n"
            + options_text(g, left_today(user_id)),
            reply_markup=options_kb(g),
        )
        return

    _spend(user_id)
    title = g.topic[:40] if g.topic else g.source_name
    s.sources.append(Source(f"✨ AI test: {title}", Extracted(text=g.topic or g.material),
                            ParseResult(questions=questions, used_ai=True, generated=True)))
    requested = g.count
    s.gen = None
    await show_summary(cb.message.bot, cb.message.chat.id, s, cb.message)
    note = f"✨ {len(questions)} ta savol tuzildi"
    if len(questions) < requested:
        note += f" ({requested} tasi so'ralgan edi, takror yoki noto'g'ri chiqqanlari olib tashlandi)"
    await cb.message.answer(
        f"{note}.\n⚠️ Javoblarni AI tanladi: eksportdan oldin <b>👁 Savollarni ko'rish</b> orqali tekshirib chiqing.\n"
        f"Bugun yana <b>{left_today(user_id)}</b> ta test tuzishingiz mumkin."
    )
