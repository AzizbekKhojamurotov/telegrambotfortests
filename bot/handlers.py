from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, MessageEntity)

from . import ai, branding, config, export
from .extract import UnsupportedFile, extract, tag
from .models import Extracted, ParseResult, Question
from .service import NeedsAI, analyze

log = logging.getLogger(__name__)
router = Router()

TEXT_DEBOUNCE = 1.5
LETTERS = "ABCDEFGH"


@dataclass
class Source:
    name: str
    ex: Extracted
    result: ParseResult


@dataclass
class Session:
    sources: list[Source] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    text_source: Source | None = None
    time_limit: int = 20
    shuffle: bool = False
    summary: tuple[int, int] | None = None  # chat_id, message_id
    debounce: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ai_solved: int = 0

    def all_sources(self) -> list[Source]:
        return self.sources + ([self.text_source] if self.text_source else [])

    @property
    def questions(self) -> list[Question]:
        return [q for s in self.all_sources() for q in s.result.questions]

    @property
    def dropped(self) -> list[str]:
        return [d for s in self.all_sources() for d in s.result.dropped]

    @property
    def base_name(self) -> str:
        if self.sources:
            stem = Path(self.sources[0].name).stem
            stem = re.sub(r"[^\w\-]+", "_", stem).strip("_")
            return stem[:40] or "test"
        return "test"


SESSIONS: dict[int, Session] = {}


def session_for(user_id: int) -> Session:
    return SESSIONS.setdefault(user_id, Session())


# ---------------------------------------------------------------- texts

def start_text(first_name: str | None) -> str:
    name = html.escape(first_name or "do'stim")
    return (
        f"👋 <b>Assalomu alaykum, {name}!</b>\n\n"
        "Men test savollaringizni <b>Kahoot</b> va <b>Blooket</b> uchun tayyor import fayliga aylantiraman.\n\n"
        "<b>Qanday ishlaydi?</b>\n"
        "1️⃣ Testni yuboring: fayl, rasm yoki matn\n"
        "2️⃣ Savollarni tekshiring, vaqtni tanlang\n"
        "3️⃣ <b>Kahoot</b> yoki <b>Blooket</b> tugmasini bosing, fayl tayyor!\n\n"
        "📎 Word · PDF · Excel · PowerPoint · CSV · TXT · rasm\n"
        "🤖 ChatGPT, Gemini yoki Claude'dan nusxalangan matn ham bo'ladi.\n\n"
        "👇 Testingizni yuboring yoki namunani sinab ko'ring."
    )


def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Namunani sinab ko'rish", callback_data="go:demo")],
        [InlineKeyboardButton(text="📖 Formatlar", callback_data="go:help"),
         InlineKeyboardButton(text="📥 Import qilish", callback_data="go:guide")],
    ])


GUIDE_TEXT = (
    "<b>📥 Tayyor faylni platformaga yuklash</b>\n\n"
    "🟣 <b>Kahoot</b> (.xlsx)\n"
    "1. kahoot.com → <b>Create</b> → <b>Kahoot</b>\n"
    "2. <b>Add question</b> → <b>Import spreadsheet</b>\n"
    "3. Botdan kelgan <code>.xlsx</code> faylni tanlang\n\n"
    "📘 <b>Blooket</b> (.csv)\n"
    "1. blooket.com → <b>Create</b> → <b>Spreadsheet Import</b>\n"
    "2. <b>Upload CSV</b> tugmasini bosing\n"
    "3. Botdan kelgan <code>.csv</code> faylni tanlang\n\n"
    "💡 Faylni Excel'da ochib saqlamang: Excel CSV'ni boshqa ko'rinishda saqlab qo'yishi mumkin."
)

DEMO_TEXT = """1. O'zbekiston poytaxti qaysi shahar?
A) Samarqand
B) Toshkent
C) Buxoro
D) Xiva
Javob: B

2. Quyosh tizimidagi eng katta sayyora qaysi?
A) Yer
B) Mars
C) Yupiter
D) Saturn
Javob: C

3. 7 × 8 nechaga teng?
A) 54
B) 56
C) 64
D) 48
Javob: B"""

HELP_TEXT = (
    "<b>Tushuniladigan formatlar</b>\n\n"
    "<b>1. Raqam + harfli variantlar</b>\n"
    "<code>1. O'zbekiston poytaxti?\nA) Samarqand\n*B) Toshkent\nC) Buxoro</code>\n\n"
    "To'g'ri javobni shunday belgilash mumkin: <code>*B)</code>, <code>+B)</code>, <code>#B)</code>, "
    "<code>B) Toshkent *</code>, <code>(to'g'ri)</code>, alohida <code>Javob: B</code> qatori, "
    "oxirida kalit (<code>Javoblar: 1-B 2-A 3-C</code>) yoki Word/Excel'da qalin, rangli yoki "
    "bo'yalgan variant. Telegram matnida qalin yoki spoiler qilingan variant ham to'g'ri deb olinadi.\n\n"
    "<b>2. HEMIS formati</b>\n"
    "<code>Savol matni\n====\n#To'g'ri javob\n====\nNoto'g'ri\n++++</code>\n\n"
    "<b>3. +/- formati</b>\n"
    "<code>? Savol matni\n+ To'g'ri javob\n- Noto'g'ri javob</code>\n\n"
    "<b>4. Jadval</b> (Excel/Word/CSV): <code>Savol | A | B | C | D | Javob</code>\n\n"
    "<b>5. Moodle GIFT</b>: <code>Savol {=to'g'ri ~xato ~xato}</code>\n\n"
    "Bir qatorda yozilgan variantlar ham ajratiladi: <code>1. Savol? A) x B) y C) z</code>\n\n"
    "<b>Cheklovlar</b>\n"
    "• Har bir savolda 2–4 ta variant bo'ladi (4 tadan ko'p bo'lsa, to'g'ri javob saqlanib, ortiqchasi olib tashlanadi)\n"
    "• Kahoot: savol 120 belgigacha, javob 75 belgigacha (oshgani qisqartiriladi)\n"
    "• Vaqt: 5, 10, 20, 30, 60, 90, 120 yoki 240 soniya\n"
    "• Har bir faylga 100 tagacha savol sig'adi, ko'p bo'lsa qismlarga bo'linadi"
)


def _nums(nums: list[int], limit: int = 15) -> str:
    shown = ", ".join(map(str, nums[:limit]))
    return shown + (f" … (+{len(nums) - limit})" if len(nums) > limit else "")


def summary_text(s: Session) -> str:
    qs = s.questions
    missing = [i + 1 for i, q in enumerate(qs) if not q.has_correct]
    names = ", ".join(html.escape(src.name) for src in s.all_sources())
    lines = [f"📋 <b>{len(qs)} ta savol topildi</b>"]
    if names:
        lines.append(f"Manba: {names}")
    if any(src.result.used_ai for src in s.all_sources()):
        lines.append("🤖 Savollar Gemini AI yordamida ajratildi — ko'rib chiqishni tavsiya qilaman.")
    multi = sum(len(q.correct) > 1 for q in qs)
    if multi:
        lines.append(f"✔️ {multi} ta savolda bir nechta to'g'ri javob bor.")
    if s.ai_solved:
        lines.append(f"🤖 {s.ai_solved} ta savolning javobini AI aniqladi — albatta tekshiring!")
    if missing:
        lines.append(f"\n⚠️ <b>{len(missing)} ta savolda to'g'ri javob topilmadi:</b> {_nums(missing)}\n"
                     "Fayl yaratishdan oldin quyidagi tugmalardan birini tanlang.")
    if s.dropped:
        sample = "; ".join(html.escape(d) for d in s.dropped[:3] if d)
        lines.append(f"\n🚫 {len(s.dropped)} ta savol tashlab ketildi (2 tadan kam variant)"
                     + (f": <i>{sample}</i>" if sample else ""))
    long_q = sum(len(q.text) > export.KAHOOT_Q_MAX for q in qs)
    long_a = sum(any(len(a) > export.KAHOOT_A_MAX for a in q.answers) for q in qs)
    many = sum(len(q.answers) > export.MAX_ANSWERS for q in qs)
    if long_q or long_a:
        lines.append(f"✂️ Kahoot uchun qisqartiriladi: {long_q} ta savol, {long_a} ta savolning javoblari.")
    if many:
        lines.append(f"✂️ {many} ta savolda 4 tadan ko'p variant bor, ortiqchasi olib tashlanadi.")
    if len(qs) > 100:
        lines.append(f"📂 Savollar 100 tadan ko'p, fayl {(len(qs) + 99) // 100} qismga bo'linadi.")
    lines.append(f"\n⏱ Vaqt: <b>{s.time_limit} s</b> · 🔀 Aralashtirish: <b>{'yoqiq' if s.shuffle else 'o‘chiq'}</b>")
    lines.append("Qaysi platforma uchun fayl kerak?")
    return "\n".join(lines)


def summary_kb(s: Session) -> InlineKeyboardMarkup:
    qs = s.questions
    missing = any(not q.has_correct for q in qs)
    rows = [[
        InlineKeyboardButton(text=f"⏱ Vaqt: {s.time_limit} s", callback_data="t:menu"),
        InlineKeyboardButton(text=f"🔀 Aralashtirish: {'✅' if s.shuffle else '❌'}", callback_data="sh"),
    ], [InlineKeyboardButton(text="👁 Savollarni ko'rish", callback_data="pv")]]
    if missing:
        fix = [InlineKeyboardButton(text="1️⃣ Birinchi variant to'g'ri", callback_data="fix:first")]
        if ai.available():
            fix.append(InlineKeyboardButton(text="🤖 AI aniqlasin", callback_data="fix:ai"))
        rows.append(fix)
        rows.append([InlineKeyboardButton(text="🗑 Javobsizlarni olib tashlash", callback_data="fix:drop")])
    if qs:
        rows.append([
            InlineKeyboardButton(text="📘 Blooket", callback_data="ex:blooket"),
            InlineKeyboardButton(text="🟣 Kahoot", callback_data="ex:kahoot"),
        ])
        rows.append([InlineKeyboardButton(text="📦 Ikkalasi", callback_data="ex:both")])
    extra = []
    if ai.available() and not all(src.result.used_ai for src in s.all_sources()):
        extra.append(InlineKeyboardButton(text="🔁 AI bilan qayta tahlil", callback_data="ai:reparse"))
    extra.append(InlineKeyboardButton(text="🆕 Yangi test", callback_data="reset"))
    rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def time_kb() -> InlineKeyboardMarkup:
    times = [InlineKeyboardButton(text=f"{t} s", callback_data=f"t:{t}") for t in export.KAHOOT_TIMES]
    return InlineKeyboardMarkup(inline_keyboard=[times[:4], times[4:],
                                                 [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="t:back")]])


# ---------------------------------------------------------------- summary message

async def show_summary(bot: Bot, chat_id: int, s: Session, status: Message | None = None):
    if s.summary and (not status or status.message_id != s.summary[1]):
        try:
            await bot.edit_message_reply_markup(chat_id=s.summary[0], message_id=s.summary[1], reply_markup=None)
        except Exception:
            pass
    text, kb = summary_text(s), summary_kb(s)
    if status is not None:
        try:
            await status.edit_text(text, reply_markup=kb)
            s.summary = (chat_id, status.message_id)
            return
        except Exception:
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    s.summary = (chat_id, msg.message_id)


async def refresh_summary(cb: CallbackQuery, s: Session):
    try:
        await cb.message.edit_text(summary_text(s), reply_markup=summary_kb(s))
        s.summary = (cb.message.chat.id, cb.message.message_id)
    except Exception:
        pass


# ---------------------------------------------------------------- commands

@router.message(CommandStart())
async def cmd_start(message: Message):
    SESSIONS.pop(message.from_user.id, None)
    text, kb = start_text(message.from_user.first_name), start_kb()
    try:
        sent = await message.answer_photo(branding.banner(), caption=text, reply_markup=kb)
        branding.remember_banner(sent.photo[-1].file_id)
    except Exception:
        log.exception("welcome banner failed")
        await message.answer(text, reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("guide"))
async def cmd_guide(message: Message):
    await message.answer(GUIDE_TEXT)


@router.callback_query(F.data == "go:help")
async def cb_help(cb: CallbackQuery):
    await cb.message.answer(HELP_TEXT)
    await cb.answer()


@router.callback_query(F.data == "go:guide")
async def cb_guide(cb: CallbackQuery):
    await cb.message.answer(GUIDE_TEXT)
    await cb.answer()


@router.callback_query(F.data == "go:demo")
async def cb_demo(cb: CallbackQuery):
    await cb.answer()
    SESSIONS.pop(cb.from_user.id, None)
    s = session_for(cb.from_user.id)
    await cb.message.answer(
        "🧪 <b>Namuna test.</b> O'zingiz ham shunday matn yuborishingiz mumkin:\n\n"
        f"<code>{html.escape(DEMO_TEXT)}</code>"
    )
    s.text_parts.append(DEMO_TEXT)
    await _analyze_text(cb.message, s)


@router.message(Command("new"))
async def cmd_new(message: Message):
    SESSIONS.pop(message.from_user.id, None)
    await message.answer("🆕 Yangi test boshlandi. Savollarni fayl yoki matn ko'rinishida yuboring.")


# ---------------------------------------------------------------- input

def entities_to_tagged(text: str, entities: list[MessageEntity] | None) -> str:
    """Marks lines that are fully bold / underlined / spoilered (a common way to show the right answer)."""
    if not entities:
        return text
    # Telegram offsets are in UTF-16 code units
    unit_to_index, units = [], 0
    for i, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        unit_to_index.extend([i] * width)
        units += width
    unit_to_index.append(len(text))
    masks = {f: [False] * len(text) for f in "bus"}
    kinds = {"bold": "b", "underline": "u", "spoiler": "s"}
    for e in entities:
        f = kinds.get(e.type)
        if not f:
            continue
        start = unit_to_index[min(e.offset, units)]
        end = unit_to_index[min(e.offset + e.length, units)]
        for i in range(start, end):
            masks[f][i] = True
    out, pos = [], 0
    for line in text.split("\n"):
        idx = [pos + i for i, ch in enumerate(line) if not ch.isspace()]
        flags = ""
        if idx:
            for f, mask in masks.items():
                if sum(mask[i] for i in idx) / len(idx) >= 0.6:
                    flags += f
        out.append(tag(line, flags))
        pos += len(line) + 1
    return "\n".join(out)


async def _process(message: Message, s: Session, name: str, ex: Extracted, status: Message):
    try:
        result = await analyze(ex)
    except NeedsAI:
        await status.edit_text(
            "🖼 Bu fayl rasm yoki skanerlangan PDF — uni faqat AI o'qiy oladi, "
            "lekin botda GEMINI_API_KEY sozlanmagan. Matnli fayl yoki matn yuboring."
        )
        return
    except ai.AIError as e:
        await status.edit_text(f"❌ AI faylni o'qiy olmadi: {html.escape(str(e))}")
        return
    if not result.questions:
        await status.edit_text(
            f"😕 <b>{html.escape(name)}</b> ichidan test savollari topilmadi.\n"
            "Savollar va variantlar qanday yozilishi kerakligini /help orqali ko'ring."
        )
        if s.questions:
            await show_summary(message.bot, message.chat.id, s)
        return
    s.sources.append(Source(name, ex, result))
    await show_summary(message.bot, message.chat.id, s, status)


@router.message(F.document)
async def on_document(message: Message):
    doc = message.document
    s = session_for(message.from_user.id)
    name = doc.file_name or "fayl"
    if doc.file_size and doc.file_size > config.MAX_FILE_BYTES:
        await message.reply("❌ Fayl 20 MB dan katta. Telegram botlari bundan katta faylni yuklab ololmaydi.")
        return
    async with s.lock:
        status = await message.reply(f"⏳ <b>{html.escape(name)}</b> tahlil qilinmoqda…")
        try:
            buf = await message.bot.download(doc)
            data = buf.read()
            ex = await asyncio.to_thread(extract, data, name)
        except UnsupportedFile as e:
            await status.edit_text(f"❌ {html.escape(str(e))}")
            return
        except Exception:
            log.exception("extract failed for %s", name)
            await status.edit_text("❌ Faylni o'qib bo'lmadi. Fayl buzilmaganini tekshiring yoki boshqa formatda yuboring.")
            return
        await _process(message, s, name, ex, status)


@router.message(F.photo)
async def on_photo(message: Message):
    s = session_for(message.from_user.id)
    async with s.lock:
        status = await message.reply("⏳ Rasm tahlil qilinmoqda…")
        buf = await message.bot.download(message.photo[-1])
        ex = Extracted(text=message.caption or "", media=(buf.read(), "image/jpeg"), note="image")
        await _process(message, s, f"rasm {len(s.sources) + 1}", ex, status)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    s = session_for(message.from_user.id)
    s.text_parts.append(entities_to_tagged(message.text, message.entities))
    if s.debounce and not s.debounce.done():
        s.debounce.cancel()
    # long texts arrive split into several messages: wait until they stop coming
    s.debounce = asyncio.create_task(_text_after_pause(message, s))


async def _text_after_pause(message: Message, s: Session):
    try:
        await asyncio.sleep(TEXT_DEBOUNCE)
    except asyncio.CancelledError:
        return
    # from here on a newer message must not cancel us; it starts its own run instead
    if s.debounce is asyncio.current_task():
        s.debounce = None
    await _analyze_text(message, s)


async def _analyze_text(message: Message, s: Session):
    async with s.lock:
        ex = Extracted(text="\n".join(s.text_parts))
        status = await message.answer("⏳ Matn tahlil qilinmoqda…")
        try:
            result = await analyze(ex)
        except ai.AIError as e:
            await status.edit_text(f"❌ AI xatosi: {html.escape(str(e))}")
            return
        if not result.questions:
            await status.edit_text(
                "😕 Matndan test savollari topilmadi. Har bir savol kamida 2 ta variantga ega bo'lishi kerak. "
                "Namunalar: /help"
            )
            if s.text_source is None:
                s.text_parts.clear()
            elif s.questions:
                await show_summary(message.bot, message.chat.id, s)
            return
        s.text_source = Source("matn", ex, result)
        await show_summary(message.bot, message.chat.id, s, status)


@router.message()
async def on_other(message: Message):
    await message.reply("Menga test savollari yozilgan fayl, rasm yoki matn yuboring. Formatlar: /help · Import: /guide")


# ---------------------------------------------------------------- callbacks

def _session_or_none(cb: CallbackQuery) -> Session | None:
    s = SESSIONS.get(cb.from_user.id)
    if not s or not s.questions:
        return None
    return s


@router.callback_query(F.data == "t:menu")
async def cb_time_menu(cb: CallbackQuery):
    await cb.message.edit_reply_markup(reply_markup=time_kb())
    await cb.answer("Har bir savol uchun vaqtni tanlang")


@router.callback_query(F.data.startswith("t:"))
async def cb_time(cb: CallbackQuery):
    s = _session_or_none(cb)
    if not s:
        await cb.answer("Sessiya tugagan. Savollarni qayta yuboring.", show_alert=True)
        return
    value = cb.data[2:]
    if value.isdigit() and int(value) in export.KAHOOT_TIMES:
        s.time_limit = int(value)
    await refresh_summary(cb, s)
    await cb.answer()


@router.callback_query(F.data == "sh")
async def cb_shuffle(cb: CallbackQuery):
    s = _session_or_none(cb)
    if not s:
        await cb.answer("Sessiya tugagan. Savollarni qayta yuboring.", show_alert=True)
        return
    s.shuffle = not s.shuffle
    await refresh_summary(cb, s)
    await cb.answer("Variantlar har bir savolda aralashtiriladi" if s.shuffle else "Variantlar tartibi saqlanadi")


@router.callback_query(F.data == "pv")
async def cb_preview(cb: CallbackQuery):
    s = _session_or_none(cb)
    if not s:
        await cb.answer("Sessiya tugagan.", show_alert=True)
        return
    await cb.answer()
    chunks, cur = [], ""
    for i, q in enumerate(s.questions, start=1):
        block = f"<b>{i}. {html.escape(q.text)}</b>"
        if not q.has_correct:
            block += " ⚠️ <i>javob yo'q</i>"
        elif q.ai_guessed:
            block += " 🤖"
        for j, a in enumerate(q.answers):
            mark = "✅" if j in q.correct else "▫️"
            block += f"\n{mark} {LETTERS[j] if j < len(LETTERS) else j + 1}) {html.escape(a)}"
        block += "\n\n"
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += block
    if cur:
        chunks.append(cur)
    for c in chunks:
        await cb.message.answer(c)
    await show_summary(cb.bot, cb.message.chat.id, s)


@router.callback_query(F.data.startswith("fix:"))
async def cb_fix(cb: CallbackQuery):
    s = _session_or_none(cb)
    if not s:
        await cb.answer("Sessiya tugagan.", show_alert=True)
        return
    action = cb.data[4:]
    missing = [q for q in s.questions if not q.has_correct]
    if action == "first":
        for q in missing:
            q.correct = [0]
        await cb.answer(f"{len(missing)} ta savolda 1-variant to'g'ri deb belgilandi")
        if not s.shuffle:
            await cb.message.answer("💡 Hamma to'g'ri javob 1-o'rinda bo'lib qolmasligi uchun "
                                    "🔀 Aralashtirish'ni yoqishni tavsiya qilaman.")
    elif action == "drop":
        for src in s.all_sources():
            src.result.questions = [q for q in src.result.questions if q.has_correct]
        await cb.answer(f"{len(missing)} ta savol olib tashlandi")
        if not s.questions:
            SESSIONS.pop(cb.from_user.id, None)
            await cb.message.edit_text("Barcha savollar olib tashlandi. Yangi savollar yuboring.")
            return
    elif action == "ai":
        await cb.answer("🤖 AI javoblarni aniqlamoqda…")
        async with s.lock:
            try:
                solved = await ai.solve(missing)
            except ai.AIError as e:
                await cb.message.answer(f"❌ {html.escape(str(e))}")
                return
            for idx, correct in solved.items():
                missing[idx].correct = correct
                missing[idx].ai_guessed = True
            s.ai_solved += len(solved)
        if len(solved) < len(missing):
            await cb.message.answer(f"⚠️ AI {len(missing) - len(solved)} ta savolga javob bera olmadi.")
    await refresh_summary(cb, s)


@router.callback_query(F.data == "ai:reparse")
async def cb_ai_reparse(cb: CallbackQuery):
    s = _session_or_none(cb)
    if not s or not ai.available():
        await cb.answer("Sessiya tugagan.", show_alert=True)
        return
    await cb.answer("🤖 Gemini qayta tahlil qilmoqda…")
    async with s.lock:
        await cb.message.edit_text("⏳ Gemini AI qayta tahlil qilmoqda…")
        for src in s.all_sources():
            if src.result.used_ai:
                continue
            try:
                res = await analyze(src.ex, force_ai=True)
            except Exception as e:
                log.warning("AI reparse failed: %s", e)
                await cb.message.answer(f"❌ {html.escape(src.name)}: AI xatosi — {html.escape(str(e))}")
                continue
            if res.questions:
                src.result = res
        s.ai_solved = 0
        await show_summary(cb.bot, cb.message.chat.id, s, cb.message)


@router.callback_query(F.data == "reset")
async def cb_reset(cb: CallbackQuery):
    SESSIONS.pop(cb.from_user.id, None)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("🆕 Yangi test boshlandi. Savollarni fayl yoki matn ko'rinishida yuboring.")
    await cb.answer()


def _shuffled(questions: list[Question]) -> list[Question]:
    out = []
    for q in questions:
        q = q.copy()
        order = list(range(len(q.answers)))
        random.shuffle(order)
        q.answers = [q.answers[i] for i in order]
        q.correct = sorted(order.index(i) for i in q.correct)
        out.append(q)
    return out


CAPTIONS = {
    "blooket": "📘 <b>Blooket uchun tayyor!</b>\nCreate → Spreadsheet Import → Upload CSV → shu faylni tanlang.",
    "kahoot": "🟣 <b>Kahoot uchun tayyor!</b>\nCreate → Kahoot → Add question → Import spreadsheet → shu faylni tanlang.",
}


@router.callback_query(F.data.startswith("ex:"))
async def cb_export(cb: CallbackQuery):
    s = _session_or_none(cb)
    if not s:
        await cb.answer("Sessiya tugagan. Savollarni qayta yuboring.", show_alert=True)
        return
    if any(not q.has_correct for q in s.questions):
        await cb.answer("Avval to'g'ri javobi yo'q savollarni hal qiling (pastdagi tugmalar).", show_alert=True)
        return
    target = cb.data[3:]
    platforms = ["blooket", "kahoot"] if target == "both" else [target]
    await cb.answer("Fayl tayyorlanmoqda…")
    questions = _shuffled(s.questions) if s.shuffle else [q.copy() for q in s.questions]
    for p in platforms:
        report = await asyncio.to_thread(export.build, p, questions, s.time_limit, s.base_name)
        notes = [CAPTIONS[p], f"Savollar: {len(questions)} · Vaqt: {s.time_limit} s"]
        if report.truncated_questions:
            notes.append(f"✂️ Qisqartirilgan savollar: {_nums(report.truncated_questions)}")
        if report.truncated_answers:
            notes.append(f"✂️ Javobi qisqartirilgan savollar: {_nums(report.truncated_answers)}")
        if report.trimmed_options:
            notes.append(f"✂️ Varianti 4 taga tushirilgan savollar: {_nums(report.trimmed_options)}")
        for name, data in report.files:
            await cb.message.answer_document(BufferedInputFile(data, filename=name), caption="\n".join(notes)[:1024])
