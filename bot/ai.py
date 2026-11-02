"""Gemini fallback: reads messy text, photos and scanned PDFs."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

from pydantic import BaseModel

from . import config
from .extract import FMT_CLOSE, FMT_OPEN
from .models import Question

log = logging.getLogger(__name__)

CHUNK_CHARS = 25_000

EXTRACT_PROMPT = """You extract multiple-choice test questions from the material below (it may be Uzbek, Russian, English or mixed).

Rules:
- Return EVERY question with its answer options, in the original order.
- Copy question and option text exactly as written. Never translate, rephrase, fix spelling or invent text.
- Remove numbering and option labels such as "1.", "12)", "A)", "b.", "В)" and correct-answer markers such as "*", "+", "#", "(to'g'ri)" from the text.
- "correct" = 1-based numbers of the correct options, ONLY when the material marks them: a marker on the option (*, +, #, ✓), an answer line like "Javob: B" / "Ответ: 2", an answer key table at the end, or formatting tags like [bold]/[highlight]/[color] that single out some options. If nothing marks the answer, return an empty list. Do not solve the question yourself.
- Skip headings, instructions and anything that is not a question with at least two options.
"""

SOLVE_PROMPT = """For each multiple-choice question below choose the correct option(s).
Return a JSON list with one item per question, in the same order: {"id": <id>, "correct": [1-based option numbers]}.
Choose more than one option only when the question clearly asks for several answers.

"""


GENERATE_PROMPT = """You are an experienced teacher writing a multiple-choice quiz.

Write exactly {count} questions{source_rule}
Difficulty: {level}.
Language of questions and answers: {lang}.

Rules:
- Every question has exactly 4 answer options and exactly ONE correct option.
- The correct answer must be factually right; if you are not sure about a fact, ask about something else.
- Wrong options are plausible, of similar length and style to the correct one. No "all of the above" / "none of the above".
- Question text at most 110 characters, each option at most 70 characters (the quiz is imported into Kahoot).
- Do not number questions or label options (no "1.", "A)").
- Questions must not repeat each other; cover different parts of the {scope}.
- "correct" = list with the 1-based number of the correct option.
"""

LEVELS = {
    "easy": "easy (basic facts and definitions)",
    "medium": "medium (understanding and simple application)",
    "hard": "hard (analysis, comparison, multi-step reasoning)",
}
LANGS = {"uz": "Uzbek (Latin script)", "ru": "Russian", "en": "English"}


class AIQuestion(BaseModel):
    question: str
    answers: list[str]
    correct: list[int]


class AISolved(BaseModel):
    id: int
    correct: list[int]


class AIError(Exception):
    pass


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


FALLBACK_MODEL = "gemini-flash-latest"

_client = None
_model = config.GEMINI_MODEL


def _get_client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


async def _generate(contents, schema) -> list:
    from google.genai import errors, types

    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0,
    )
    global _model
    delay = 15
    for attempt in range(4):
        try:
            resp = await _get_client().aio.models.generate_content(
                model=_model, contents=contents, config=cfg
            )
            return json.loads(resp.text or "[]")
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 404 and _model != FALLBACK_MODEL:
                # Google retires model names; the "latest" alias always points at a live one
                log.warning("Gemini model %s unavailable, switching to %s", _model, FALLBACK_MODEL)
                _model = FALLBACK_MODEL
                continue
            if code in (429, 500, 503) and attempt < 3:
                log.warning("Gemini %s, retrying in %ss", code, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise AIError(f"Gemini xatosi ({code}): {getattr(e, 'message', e)}") from e
        except json.JSONDecodeError as e:
            raise AIError("Gemini noto'g'ri javob qaytardi") from e
    raise AIError("Gemini javob bermadi")


def _tags_to_words(text: str) -> str:
    from .parser import strip_table_marks

    names = {"b": "bold", "h": "highlight", "c": "color", "u": "underline", "s": "spoiler"}
    text = strip_table_marks(text)
    return re.sub(
        f"{FMT_OPEN}([a-z]*){FMT_CLOSE}",
        lambda m: "[" + ",".join(names[f] for f in m.group(1) if f in names) + "] ",
        text,
    )


def _chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, cur = [], ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(cur) + len(para) > CHUNK_CHARS and cur.strip():
            out.append(cur)
            cur = ""
        cur += para
    if cur.strip():
        out.append(cur)
    # a single huge paragraph still has to be cut
    final = []
    for c in out:
        while len(c) > CHUNK_CHARS * 1.5:
            cut = c.rfind("\n", 0, CHUNK_CHARS) or CHUNK_CHARS
            final.append(c[:cut])
            c = c[cut:]
        final.append(c)
    return final


def _to_questions(items: list[dict]) -> list[Question]:
    out = []
    for it in items:
        raw = [str(a).strip() for a in it.get("answers", [])]
        keep = [i for i, a in enumerate(raw) if a]
        answers = [raw[i] for i in keep]
        text = str(it.get("question", "")).strip()
        if not text or len(answers) < 2:
            continue
        # "correct" points into the raw list; re-map after dropping empty options
        correct = sorted({keep.index(int(c) - 1) for c in it.get("correct", []) if int(c) - 1 in keep})
        out.append(Question(text=text, answers=answers, correct=correct))
    return out


async def parse(text: str = "", media: tuple[bytes, str] | None = None) -> list[Question]:
    from google.genai import types

    if media:
        data, mime = media
        items = await _generate(
            [types.Part.from_bytes(data=data, mime_type=mime), EXTRACT_PROMPT], list[AIQuestion]
        )
        return _to_questions(items)
    questions: list[Question] = []
    for chunk in _chunks(_tags_to_words(text)):
        items = await _generate([EXTRACT_PROMPT + "\n--- MATERIAL ---\n" + chunk], list[AIQuestion])
        questions.extend(_to_questions(items))
    return questions


async def generate(count: int, level: str, lang: str, topic: str = "", material: str = "",
                   media: tuple[bytes, str] | None = None) -> list[Question]:
    """Writes a new quiz about `topic`, or based on `material` text / `media` (photo, scanned PDF)."""
    from google.genai import types

    if material or media:
        source_rule = (" based ONLY on the material below. Every question and correct answer must be "
                       "supported by the material; do not use outside facts.")
        scope = "material"
    else:
        source_rule = f" about this topic: {topic}"
        scope = "topic"
    prompt = GENERATE_PROMPT.format(count=count, source_rule=source_rule, level=LEVELS[level],
                                    lang=LANGS[lang], scope=scope)
    if media:
        contents = [types.Part.from_bytes(data=media[0], mime_type=media[1]), prompt]
    elif material:
        contents = [prompt + "\n--- MATERIAL ---\n" + _tags_to_words(material)]
    else:
        contents = [prompt]
    items = await _generate(contents, list[AIQuestion])

    out: list[Question] = []
    seen: set[str] = set()
    for q in _to_questions(items):
        key = q.text.casefold()
        if key in seen or len(q.correct) != 1 or not 2 <= len(q.answers) <= 4:
            continue
        seen.add(key)
        # models like to put the right answer in the same slot: spread it out
        order = list(range(len(q.answers)))
        random.shuffle(order)
        q.answers = [q.answers[i] for i in order]
        q.correct = [order.index(q.correct[0])]
        q.ai_guessed = True
        out.append(q)
    return out[:count]


async def solve(questions: list[Question]) -> dict[int, list[int]]:
    """Returns {index: [0-based correct indexes]} for the given questions."""
    result: dict[int, list[int]] = {}
    batch = 40
    for start in range(0, len(questions), batch):
        payload = [
            {"id": start + i, "question": q.text, "options": {str(n + 1): a for n, a in enumerate(q.answers)}}
            for i, q in enumerate(questions[start:start + batch])
        ]
        items = await _generate([SOLVE_PROMPT + json.dumps(payload, ensure_ascii=False)], list[AISolved])
        for it in items:
            idx = int(it.get("id", -1))
            if start <= idx < start + batch and idx < len(questions):
                n = len(questions[idx].answers)
                correct = sorted({int(c) - 1 for c in it.get("correct", []) if 1 <= int(c) <= n})
                if correct:
                    result[idx] = correct
    return result
