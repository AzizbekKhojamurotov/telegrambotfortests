"""Glue between extraction, the rule-based parser and the AI fallback."""
from __future__ import annotations

import asyncio
import logging

from . import ai
from .extract import Extracted
from .models import ParseResult
from .parser import parse_any

log = logging.getLogger(__name__)


class NeedsAI(Exception):
    """Content can only be read by AI, but no Gemini key is configured."""


def _is_poor(res: ParseResult) -> bool:
    n = len(res.questions)
    if n == 0:
        return True
    if len(res.dropped) > n * 0.3:
        return True
    return len(res.missing_correct) > n * 0.5


def _score(res: ParseResult):
    return (sum(q.has_correct for q in res.questions), len(res.questions))


async def analyze(ex: Extracted, force_ai: bool = False) -> ParseResult:
    rule = await asyncio.to_thread(parse_any, ex.text, ex.tables)

    media_only = ex.media is not None and (ex.note == "image" or not rule.questions)
    if media_only or force_ai or _is_poor(rule):
        if not ai.available():
            if media_only:
                raise NeedsAI()
            return rule
        try:
            if ex.media is not None and (media_only or not ex.text.strip()):
                questions = await ai.parse(media=ex.media)
            else:
                questions = await ai.parse(text=ex.text)
        except ai.AIError as e:
            log.warning("AI parse failed: %s", e)
            if media_only:
                raise
            return rule
        ai_res = ParseResult(questions=questions, used_ai=True)
        if force_ai or media_only or _score(ai_res) > _score(rule):
            return ai_res
    return rule
