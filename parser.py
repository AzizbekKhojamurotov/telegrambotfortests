"""Rule-based extraction of multiple-choice questions from plain text and tables."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extract import FMT_CLOSE, FMT_OPEN, TABLE_CLOSE, TABLE_OPEN, tag
from .models import ParseResult, Question

LATIN = "ABCDEFGH"
CYRILLIC = "АБВГДЕЖЗ"
# Cyrillic letters that look like Latin ones (people mix keyboard layouts)
LOOKALIKE = {"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H"}

LETTER = r"[A-Ha-hА-Жа-ж]"
MARK_CHARS = "*+#✓✔✅☑√"
APOS = r"[\'`ʻʼ‘’]?"

TAG_RE = re.compile(f"^{FMT_OPEN}([a-z]*){FMT_CLOSE}")
ANY_TAG_RE = re.compile(f"{FMT_OPEN}[a-z]*{FMT_CLOSE}")

OPTION_RE = re.compile(
    rf"^(?P<pre>[{re.escape(MARK_CHARS)}]+\s*)?[\(\[]?(?P<letter>{LETTER})\s*(?P<sep>[\)\.\]:])(?P<rest>.*)$"
)
TRAIL_MARK_RE = re.compile(
    rf"(?:\s+\+|\s*(?:\*+|✓|✔|✅|☑|\(\+\)|\[\+\]))\s*$|\s*(?:\((?:to{APOS}g{APOS}ri(?:\s+javob)?|correct|true|right|"
    rf"правильн\w*|верно|верный)\))\s*$",
    re.I,
)
LEAD_BODY_MARK_RE = re.compile(r"^\s*(?:\*+|✓|✔|✅|☑)\s*")

Q_KEYWORD = r"(?:savol|вопрос|question|задание|topshiriq|test|q)"
Q_PATTERNS = [
    re.compile(rf"^{Q_KEYWORD}\s*(?:№|#|n)?\s*(?P<num>\d{{1,4}})\s*(?P<sep>[\.\):\-–—]?)\s*(?P<body>.*)$", re.I),
    re.compile(r"^(?:№\s*)?(?P<num>\d{1,4})\s*-?\s*(?:savol|вопрос|question|topshiriq)\s*(?P<sep>[\.\):\-–—]?)\s*(?P<body>.*)$", re.I),
    re.compile(r"^(?:№\s*|#\s*)?(?P<num>\d{1,4})\s*(?P<sep>[\.\)\]:\-–—])(?!\d)\s*(?P<body>.*)$"),
]
Q_MARKER_RE = re.compile(r"^(?:\?|S\s*:|(?:savol|вопрос|question)\s*:)\s*(?P<body>.+)$", re.I)
PLUS_RE = re.compile(r"^\+\s*:?\s*(?P<body>.+)$")
MINUS_RE = re.compile(r"^(?:-|=|~|–)\s*:?\s*(?P<body>.+)$")
HASH_OPT_RE = re.compile(r"^#\s*(?P<body>\D.*)$")

KEY_INLINE_RE = re.compile(
    rf"^(?:[^\w\s]+\s*)?(?:to{APOS}g{APOS}ri\s+)?(?:javob(?:i|lar)?|answer|ответ|правильный\s+ответ|correct(?:\s+answer)?|"
    rf"right\s+answer|kalit|key)\s*[:\-=–]\s*(?P<value>.+)$",
    re.I,
)
KEY_HEADER_RE = re.compile(
    rf"^(?:to{APOS}g{APOS}ri\s+)?(?:javoblar(?:\s+kaliti)?|kalitlar?|test\s+kaliti|answers?(?:\s+key)?|"
    rf"answer\s+key|ответы|ключи?(?:\s+к\s+тесту)?|keys?)\s*[:\-–]?\s*(?P<rest>.*)$",
    re.I,
)
# explanation lines AI chats add after the answer ("Izoh: ...")
EXPLAIN_RE = re.compile(
    r"^(?:[^\w\s]+\s*)?(?:izoh|tushuntirish|sharh|explanation|reason|объяснение|пояснение|комментарий)\s*[:\-–]",
    re.I,
)
PAIR_RE = re.compile(
    rf"(?P<num>\d{{1,4}})\s*[\-\.\):=–]?\s*(?P<ans>{LETTER}(?:\s*[,/&]\s*{LETTER})*)(?![A-Za-zА-Яа-яЁё0-9])"
)
NUM_PAIR_RE = re.compile(r"(?P<num>\d{1,4})\s*[\-\.\):=–]\s*(?P<ans>\d(?:\s*[,/&]\s*\d)*)(?!\d)")
SPLIT_KEY_RE = re.compile(
    rf"\s(?=(?:(?:to{APOS}g{APOS}ri|правильный|correct)\s+)?(?:javob|answer|ответ)\s*[:\-–])", re.I
)
# what may stand before "javob:" on its own line ("✅ To'g'ri javob: B") — never split there
KEY_PREFIX_RE = re.compile(rf"^(?:[^\w\s]+\s*)?(?:to{APOS}g{APOS}ri|правильный|correct)?$", re.I)
INLINE_OPT_RE = re.compile(
    rf"(?:(?<=\s)|^)(?P<mark>[{re.escape(MARK_CHARS)}]?)[\(\[]?(?P<letter>{LETTER})(?:\)|\]|\.(?=\s))"
)


def norm_letter(ch: str) -> str:
    ch = ch.upper()
    return LOOKALIKE.get(ch, ch)


def letter_positions(ch: str) -> list[int]:
    """Possible 0-based positions of an option letter (Latin and Cyrillic orders)."""
    up = ch.upper()
    out = []
    n = norm_letter(up)
    if n in LATIN:
        out.append(LATIN.index(n))
    if up in CYRILLIC:
        out.append(CYRILLIC.index(up))
    return out or [-1]


def clean(s: str) -> str:
    s = ANY_TAG_RE.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


@dataclass
class _Opt:
    text: str
    letter: str | None = None  # normalised letter, or None for +/- style
    style: str = "letter"  # letter | num | marker
    correct: bool = False
    flags: str = ""


@dataclass
class _Q:
    lines: list[str] = field(default_factory=list)
    number: int | None = None
    sep: str = ""
    options: list[_Opt] = field(default_factory=list)
    inline_key: str | None = None
    closed: bool = False  # answer / explanation already seen: further plain text is commentary

    def add_text(self, s: str):
        if self.options:
            self.options[-1].text = f"{self.options[-1].text} {s}".strip()
        else:
            self.lines.append(s)


# ---------------------------------------------------------------- public API

def parse_text(text: str) -> ParseResult:
    text = _strip_markdown(_normalize(text))
    for special in (_parse_hemis, _parse_gift):
        res = special(text)
        if res is not None and res.questions:
            return res
    return _parse_generic(text)


def parse_any(text: str, tables: list[list[list[str]]]) -> ParseResult:
    """Parses text and tables together: a table that reads as a question table is used
    as such, any other table stays part of the surrounding text."""
    text, md_tables = _markdown_tables(_normalize(text), len(tables))
    tables = list(tables) + md_tables
    candidates = [parse_text(strip_table_marks(text))] if text.strip() else []
    if tables:
        table_results = {i: _parse_table(t) for i, t in enumerate(tables)}
        consumed = {i: r for i, r in table_results.items() if r.questions}
        if consumed:
            candidates.append(_parse_segments(text, consumed))
    if not candidates:
        return ParseResult()
    return max(candidates, key=_score)


TABLE_MARK_RE = re.compile(f"^[{TABLE_OPEN}{TABLE_CLOSE}]\\d+$", re.M)


def strip_table_marks(text: str) -> str:
    return TABLE_MARK_RE.sub("", text)


def _parse_segments(text: str, consumed: dict[int, ParseResult]) -> ParseResult:
    result = ParseResult()
    buffer: list[str] = []
    skipping: int | None = None

    def flush():
        chunk = "\n".join(buffer)
        buffer.clear()
        if chunk.strip():
            r = parse_text(chunk)
            result.questions.extend(r.questions)
            result.dropped.extend(r.dropped)

    for line in text.split("\n"):
        if line.startswith(TABLE_OPEN) and line[1:].isdigit():
            idx = int(line[1:])
            if idx in consumed:
                flush()
                result.questions.extend(consumed[idx].questions)
                result.dropped.extend(consumed[idx].dropped)
                skipping = idx
            continue
        if line.startswith(TABLE_CLOSE) and line[1:].isdigit():
            if skipping == int(line[1:]):
                skipping = None
            continue
        if skipping is None:
            buffer.append(line)
    flush()
    return result


def _score(res: ParseResult):
    with_correct = sum(q.has_correct for q in res.questions)
    return (with_correct, len(res.questions), -len(res.dropped))


# ---------------------------------------------------------------- helpers

def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace("\t", " ")
    text = re.sub(r"[​‌‍﻿️]", "", text)
    return text


# ---------------------------------------------------------------- markdown (text copied from AI chats)

MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
MD_HEADING_RE = re.compile(rf"^#{{2,6}}\s+|^#\s+(?=\d|{Q_KEYWORD}\b)", re.I)
MD_BULLET_RE = re.compile(rf"^\s*[-*•]\s+(?=(?:\*\*|__)?[\(\[]?{LETTER}\s*[\)\.\]:])")
MD_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _md_unbold(s: str) -> str:
    return MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2), s).replace("`", "")


def _strip_markdown(text: str) -> str:
    """Removes the Markdown ChatGPT / Gemini / Claude put into copied answers.
    A line that is (mostly) **bold** gets the bold flag, like a bold Telegram entity."""
    if len(re.findall(r"^\s*={3,}\s*$", text, re.M)) >= 2:
        return text  # HEMIS uses ==== and # itself
    lines = text.split("\n")
    bullets = sum(bool(MD_BULLET_RE.match(_split_line(l)[0])) for l in lines) >= 2
    out = []
    for raw in lines:
        m = TAG_RE.match(raw)
        flags, line = (m.group(1), raw[m.end():]) if m else ("", raw)
        line = line.strip()
        if line.startswith("```") or MD_RULE_RE.match(line):
            out.append("")
            continue
        line = MD_HEADING_RE.sub("", line)
        if bullets:
            line = MD_BULLET_RE.sub("", line)
        bold = sum(len(re.sub(r"\s", "", a or b)) for a, b in MD_BOLD_RE.findall(line))
        line = _md_unbold(line)
        visible = len(re.sub(r"\s", "", line))
        if visible and bold / visible >= 0.6 and "b" not in flags:
            flags += "b"
        out.append(tag(line, flags))
    return "\n".join(out)


def _markdown_tables(text: str, first_index: int) -> tuple[str, list[list[list[str]]]]:
    """Turns '| a | b |' Markdown tables into real tables, wrapped in table marks like docx tables."""
    if "|" not in text:
        return text, []
    lines = text.split("\n")
    out, tables, i = [], [], 0
    while i < len(lines):
        if MD_TABLE_ROW_RE.match(lines[i]) and i + 1 < len(lines) and MD_TABLE_SEP_RE.match(lines[i + 1]):
            block = [lines[i]]
            j = i + 2
            while j < len(lines) and MD_TABLE_ROW_RE.match(lines[j]):
                block.append(lines[j])
                j += 1
            idx = first_index + len(tables)
            rows = [[_md_unbold(c).strip() for c in r.strip().strip("|").split("|")] for r in block]
            tables.append(rows)
            out.append(f"{TABLE_OPEN}{idx}")
            out.extend(" ".join(c for c in r if c) for r in rows)
            out.append(f"{TABLE_CLOSE}{idx}")
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), tables


def _split_line(raw: str) -> tuple[str, str]:
    m = TAG_RE.match(raw)
    if m:
        return raw[m.end():].strip(), m.group(1)
    return raw.strip(), ""


def _split_inline_options(line: str) -> list[str]:
    """'Savol? A) x B) y C) z' -> ['Savol?', 'A) x', 'B) y', 'C) z']."""
    matches = list(INLINE_OPT_RE.finditer(line))
    if len(matches) < 2:
        return [line]
    chosen = []
    for m in matches:
        pos = letter_positions(m.group("letter"))
        if len(chosen) in pos:
            chosen.append(m)
    if len(chosen) < 2:
        return [line]
    pieces = []
    head = line[: chosen[0].start()].strip()
    if head:
        pieces.append(head)
    for i, m in enumerate(chosen):
        end = chosen[i + 1].start() if i + 1 < len(chosen) else len(line)
        pieces.append(line[m.start():end].strip())
    return pieces


def _prepare_lines(text: str) -> list[tuple[str, str]]:
    """Returns (line, flags); blank lines are ('', '')."""
    out: list[tuple[str, str]] = []
    for raw in text.split("\n"):
        line, flags = _split_line(raw)
        if not line:
            out.append(("", ""))
            continue
        pieces: list[str] = []
        for piece in SPLIT_KEY_RE.split(line):
            if pieces and KEY_PREFIX_RE.match(pieces[-1].strip()):
                pieces[-1] = f"{pieces[-1]} {piece}"
            else:
                pieces.append(piece)
        for piece in pieces:
            for sub in _split_inline_options(piece.strip()):
                if sub:
                    out.append((sub, flags))
    return out


def _strip_marks(body: str) -> tuple[str, bool]:
    correct = False
    m = TRAIL_MARK_RE.search(body)
    if m:
        body, correct = body[: m.start()], True
    m = LEAD_BODY_MARK_RE.match(body)
    if m:
        body, correct = body[m.end():], True
    return body.strip(), correct


def _match_option(line: str, q: _Q | None) -> _Opt | None:
    if q is None:
        return None
    m = OPTION_RE.match(line)
    if not m:
        return None
    letter, sep, rest = m.group("letter"), m.group("sep"), m.group("rest")
    lettered = [o for o in q.options if o.style == "letter"]
    expected = len(lettered)
    sequential = expected in letter_positions(letter)
    if sep == "." and rest and not rest[0].isspace() and not sequential:
        return None  # "A.Navoiy" style text, not an option
    if sep in ".:" and not sequential:
        return None
    if not sequential and not lettered:
        # first option must be A / А
        return None
    body, correct = _strip_marks(rest)
    pre = m.group("pre")
    if pre and pre.strip():
        correct = True
    return _Opt(text=body, letter=norm_letter(letter), style="letter", correct=correct)


def _parse_answer_value(value: str, options: list[_Opt]) -> list[int]:
    """'B', 'A, C', '2', 'Toshkent' -> option indexes."""
    value = clean(value).rstrip(".;")
    if not value or not options:
        return []
    for i, o in enumerate(options):
        if clean(o.text).lower() == value.lower():
            return [i]
    # "B) Toshkent", "2. Toshkent", "B - Toshkent": the label decides
    m = re.match(rf"^[\(\[]?(?P<label>{LETTER}|\d{{1,2}})\s*(?:[\)\]\.:]|\s[\-–—])\s*\S", value)
    if m:
        return _parse_answer_value(m.group("label"), options)
    tokens = [t for t in re.split(r"[\s,;/&]+|\s+va\s+|\s+и\s+|\s+and\s+", value) if t]
    out: list[int] = []
    for t in tokens:
        t = t.strip("()[].:")
        if re.fullmatch(r"\d{1,2}", t):
            idx = int(t) - 1
            if 0 <= idx < len(options):
                out.append(idx)
            else:
                return []
        elif re.fullmatch(LETTER, t):
            idx = _letter_to_index(t, options)
            if idx is None:
                return []
            out.append(idx)
        else:
            return []
    return sorted(set(out))


def _letter_to_index(letter: str, options: list[_Opt]) -> int | None:
    n = norm_letter(letter)
    for i, o in enumerate(options):
        if o.letter == n:
            return i
    for pos in letter_positions(letter):
        if 0 <= pos < len(options):
            return pos
    return None


def _key_pairs(line: str, allow_numeric: bool) -> dict[int, str] | None:
    """Parses '1-A 2-B, 3.C' answer-key lines. Returns None when the line is not a key line."""
    pairs = {}
    rest = line
    for regex in (PAIR_RE, NUM_PAIR_RE) if allow_numeric else (PAIR_RE,):
        for m in regex.finditer(line):
            pairs[int(m.group("num"))] = m.group("ans")
        rest = regex.sub("", rest)
    if not pairs:
        return None
    if re.sub(r"[\s,;|.]+", "", rest):
        return None
    return pairs


def _peek_starts_question(lines: list[tuple[str, str]], start: int) -> bool:
    """After a plain line: do the following lines begin a fresh option list (A / + / 1)?"""
    for j in range(start, min(start + 4, len(lines))):
        line = lines[j][0]
        if not line:
            continue
        m = OPTION_RE.match(line)
        if m:
            return 0 in letter_positions(m.group("letter"))
        if PLUS_RE.match(line) or re.match(r"^1\s*\)", line):
            return True
        if any(p.match(line) for p in Q_PATTERNS):
            return False
    return False


def _next_is_first_option(lines: list[tuple[str, str]], start: int) -> bool:
    """'1) 5 * 6 = ?' followed by 'A) 30': the numbered line is a question, not option 1."""
    for j in range(start, min(start + 3, len(lines))):
        line = lines[j][0]
        if not line:
            continue
        m = OPTION_RE.match(line)
        return bool(m) and 0 in letter_positions(m.group("letter"))
    return False


# ---------------------------------------------------------------- generic parser

def _parse_generic(text: str) -> ParseResult:
    lines = _prepare_lines(text)
    qs: list[_Q] = []
    q: _Q | None = None
    keys: dict[int, str] = {}
    key_mode = False
    last_blank = True

    def new_q(number=None, sep="", body=""):
        nonlocal q
        q = _Q(number=number, sep=sep)
        if body:
            q.lines.append(body)
        qs.append(q)

    for i, (line, flags) in enumerate(lines):
        if not line:
            last_blank = True
            continue
        was_blank, last_blank = last_blank, False

        if key_mode:
            pairs = _key_pairs(line, allow_numeric=True)
            if pairs:
                keys.update(pairs)
                continue
            key_mode = False

        m = KEY_HEADER_RE.match(line)
        if m and (not m.group("rest") or _key_pairs(m.group("rest"), True)):
            key_mode = True
            if m.group("rest"):
                keys.update(_key_pairs(m.group("rest"), True))
            continue

        pairs = _key_pairs(line, allow_numeric=False)
        if pairs and (len(pairs) >= 2 or keys or (q and len(q.options) >= 2)):
            keys.update(pairs)
            continue

        m = KEY_INLINE_RE.match(line)
        if m and q is not None and q.options:
            q.inline_key = m.group("value")
            q.closed = True
            continue

        if q is not None and q.options and EXPLAIN_RE.match(line):
            q.closed = True
            continue

        opt = _match_option(line, q)
        if opt:
            opt.flags = flags
            q.options.append(opt)
            continue

        if q is not None and not any(o.style == "letter" for o in q.options):
            m = PLUS_RE.match(line)
            if m and (q.lines or q.options):
                body, _ = _strip_marks(m.group("body"))
                q.options.append(_Opt(body, style="marker", correct=True, flags=flags))
                continue
            m = MINUS_RE.match(line) or HASH_OPT_RE.match(line)
            if m and (q.lines or q.options) and not line.startswith("--"):
                body, marked = _strip_marks(m.group("body"))
                q.options.append(_Opt(body, style="marker", correct=line.startswith("#") or marked, flags=flags))
                continue

        qm = None
        for pat in Q_PATTERNS:
            qm = pat.match(line)
            if qm:
                break
        if qm:
            k, sep, body = int(qm.group("num")), qm.group("sep"), qm.group("body").strip()
            if q is not None and _is_numeric_option(q, k, sep, body) and not _next_is_first_option(lines, i + 1):
                text_, correct = _strip_marks(body)
                q.options.append(_Opt(text_, letter=str(k), style="num", correct=correct, flags=flags))
                continue
            new_q(k, sep, body)
            continue

        m = Q_MARKER_RE.match(line)
        if m:
            new_q(body=m.group("body").strip())
            continue

        # plain text line
        if q is None:
            new_q(body=line)
        elif not q.options:
            q.lines.append(line)
        elif was_blank or line.rstrip().endswith(("?", ":")) or _peek_starts_question(lines, i + 1):
            new_q(body=line)
        elif not q.closed:
            q.add_text(line)

    return _finish(qs, keys)


def _is_numeric_option(q: _Q, k: int, sep: str, body: str) -> bool:
    nums = [o for o in q.options if o.style == "num"]
    if any(o.style != "num" for o in q.options):
        return False
    expected_q = q.number + 1 if q.number is not None else None
    if nums:
        if k != len(nums) + 1:
            return False
        if k == expected_q:
            # "4." after three numeric options: decide by separator / question mark
            if sep == q.sep and sep != ")":
                return False
            return not body.rstrip().endswith("?")
        return True
    if k != 1:
        return False
    if expected_q is not None:
        return expected_q != 1
    return sep == ")"


def _finish(qs: list[_Q], keys: dict[int, str]) -> ParseResult:
    result = ParseResult()
    built: list[tuple[_Q, Question]] = []
    for raw in qs:
        text = clean(" ".join(raw.lines))
        text, _ = _strip_marks(text) if text.endswith("*") else (text, False)
        options = [o for o in raw.options if clean(o.text)]
        if len(options) < 2:
            if raw.number is not None or options:
                result.dropped.append(text[:60] or f"#{raw.number}")
            continue
        correct = [i for i, o in enumerate(options) if o.correct]
        if not correct and raw.inline_key:
            correct = _parse_answer_value(raw.inline_key, options)
        q = Question(text=text, answers=[clean(o.text) for o in options], correct=correct, number=raw.number)
        built.append((raw, q))

    if keys:
        numbers = [q.number for _, q in built]
        by_number = all(n is not None for n in numbers) and len(set(numbers)) == len(numbers)
        for pos, (raw, q) in enumerate(built, start=1):
            if q.correct:
                continue
            value = keys.get(q.number if by_number else pos)
            if value:
                opts = [o for o in raw.options if clean(o.text)]
                q.correct = _parse_answer_value(value, opts)

    for raw, q in built:
        if not q.correct:
            q.correct = _correct_from_flags([o.flags for o in raw.options if clean(o.text)])
        result.questions.append(q)
    return result


def _correct_from_flags(flags: list[str]) -> list[int]:
    n = len(flags)
    for f in "hcusb":
        marked = [i for i, fl in enumerate(flags) if f in fl]
        if 0 < len(marked) < n and (len(marked) == 1 or len(marked) <= n // 2):
            return marked
    return []


# ---------------------------------------------------------------- HEMIS (++++ / ==== / #)

def _parse_hemis(text: str) -> ParseResult | None:
    if len(re.findall(r"^\s*={3,}\s*$", text, re.M)) < 2:
        return None
    result = ParseResult()
    for block in re.split(r"^\s*\+{3,}\s*$", text, flags=re.M):
        parts = [p for p in re.split(r"^\s*={3,}\s*$", block, flags=re.M)]
        if not "".join(parts).strip():
            continue
        qtext = clean(" ".join(l for l in parts[0].split("\n")))
        qtext = re.sub(r"^(?:\d{1,4}\s*[\.\)]\s*)", "", qtext)
        answers, correct = [], []
        for part in parts[1:]:
            body = clean(" ".join(part.split("\n")))
            if not body:
                continue
            is_correct = body.startswith("#")
            body = body.lstrip("#").strip()
            body = re.sub(rf"^{LETTER}\s*[\)\.]\s+", "", body)
            body, marked = _strip_marks(body)
            if is_correct or marked:
                correct.append(len(answers))
            answers.append(body)
        if len(answers) < 2 or not qtext:
            if qtext or answers:
                result.dropped.append(qtext[:60])
            continue
        result.questions.append(Question(qtext, answers, correct))
    return result


# ---------------------------------------------------------------- GIFT (Moodle)

GIFT_RE = re.compile(r"(?:::(?P<title>[^:]*)::)?(?P<pre>[^{}]*)\{(?P<body>[^{}]*)\}(?P<post>[^{}\n]*)", re.S)


def _parse_gift(text: str) -> ParseResult | None:
    if not re.search(r"\{[^{}]*~[^{}]*\}", text):
        return None
    result = ParseResult()
    plain = ANY_TAG_RE.sub("", text)
    plain = "\n".join(l for l in plain.split("\n") if not l.strip().startswith("//"))
    for m in GIFT_RE.finditer(plain):
        qtext = clean(" ".join((m.group("pre") + " " + m.group("post")).split()))
        body = m.group("body").strip()
        answers, correct = [], []
        if body.upper() in ("T", "TRUE", "F", "FALSE"):
            answers = ["True", "False"]
            correct = [0 if body.upper().startswith("T") else 1]
        else:
            for tok in re.findall(r"([=~])((?:\\.|[^=~])*)", body):
                mark, ans = tok
                ans = re.sub(r"#.*$", "", ans, flags=re.S)
                ans = re.sub(r"^%-?\d+(?:\.\d+)?%", "", ans.strip()).replace("\\", "").strip()
                if not ans:
                    continue
                if mark == "=" or tok[1].strip().startswith("%100%"):
                    correct.append(len(answers))
                answers.append(ans)
        if len(answers) >= 2 and qtext:
            result.questions.append(Question(qtext, answers, correct))
        elif qtext:
            result.dropped.append(qtext[:60])
    return result


# ---------------------------------------------------------------- tables

def _cell(raw: str) -> tuple[str, str]:
    raw = raw or ""
    m = TAG_RE.match(raw)
    flags = m.group(1) if m else ""
    return clean(raw), flags


H_QUESTION = re.compile(r"question|savol|вопрос|topshiriq|задани|matn", re.I)
H_CORRECT = re.compile(rf"correct|to{APOS}g{APOS}ri|правильн|kalit|key|right|верн", re.I)
H_ANSWER = re.compile(r"answer|javob|variant|ответ|option|вариант|^[A-HА-Г]\)?$", re.I)
H_TIME = re.compile(r"time|vaqt|время", re.I)
H_NUMBER = re.compile(r"^(?:#|№|n|no\.?|question\s*#|t/?r|savol\s*№)$", re.I)


def parse_tables(tables: list[list[list[str]]]) -> ParseResult:
    result = ParseResult()
    for rows in tables:
        res = _parse_table(rows)
        result.questions.extend(res.questions)
        result.dropped.extend(res.dropped)
    return result


def _parse_table(rows: list[list[str]]) -> ParseResult:
    result = ParseResult()
    header_idx, cols = _find_header(rows)
    if cols is None:
        header_idx, cols = _guess_columns(rows)
    if cols is None:
        return result
    q_col, a_cols, c_col = cols["q"], cols["a"], cols.get("c")
    for row in rows[header_idx + 1:]:
        cells = [_cell(c) for c in row]

        def get(i):
            return cells[i] if i is not None and i < len(cells) else ("", "")

        qtext, _ = get(q_col)
        answers, flags, marked = [], [], []
        for ci in a_cols:
            text, fl = get(ci)
            if not text:
                continue
            body, is_marked = _strip_marks(text)
            if not body:
                continue
            if is_marked:
                marked.append(len(answers))
            answers.append(body)
            flags.append(fl)
        if not qtext and not answers:
            continue
        if len(answers) < 2 or not qtext:
            result.dropped.append(qtext[:60])
            continue
        correct = marked
        if not correct and c_col is not None:
            correct = _parse_answer_value(get(c_col)[0], [_Opt(a, letter=LATIN[i] if i < 8 else None)
                                                          for i, a in enumerate(answers)])
        if not correct:
            correct = _correct_from_flags(flags)
        result.questions.append(Question(qtext, answers, correct))
    return result


def _find_header(rows):
    for idx, row in enumerate(rows[:15]):
        cells = [_cell(c)[0] for c in row]
        q_col, a_cols, c_col = None, [], None
        for ci, c in enumerate(cells):
            if not c:
                continue
            if H_CORRECT.search(c) and c_col is None:
                c_col = ci
            elif H_TIME.search(c) or H_NUMBER.match(c):
                continue
            elif H_QUESTION.search(c) and q_col is None:
                q_col = ci
            elif H_ANSWER.search(c):
                a_cols.append(ci)
        if c_col is None and len(a_cols) >= 3:
            # "Savol | A | B | C | Javob": a bare "Javob" column holds the correct answer
            bare = [ci for ci in a_cols if re.fullmatch(r"(?:javob|ответ|answer|kalit)", cells[ci], re.I)]
            if len(bare) == 1:
                c_col = bare[0]
                a_cols.remove(c_col)
        if q_col is not None and len(a_cols) >= 2:
            return idx, {"q": q_col, "a": a_cols, "c": c_col}
    return -1, None


def _guess_columns(rows):
    data = [[_cell(c)[0] for c in r] for r in rows]
    data = [r for r in data if sum(bool(c) for c in r) >= 3]
    if len(data) < 1:
        return -1, None
    width = max(len(r) for r in data)

    def column(i):
        return [r[i] if i < len(r) else "" for r in data]

    def ratio(i, pred):
        vals = [v for v in column(i) if v]
        return sum(pred(v) for v in vals) / len(vals) if vals else 0

    first = 0
    if ratio(0, lambda v: bool(re.fullmatch(r"\d{1,4}[\.\)]?", v))) > 0.8:
        first = 1
    last = width - 1
    c_col = None
    is_key = lambda v: bool(re.fullmatch(rf"(?:{LETTER}|\d)(?:\s*[,/]\s*(?:{LETTER}|\d))*", v))
    if last > first + 2 and ratio(last, is_key) > 0.8:
        c_col = last
        last -= 1
    a_cols = list(range(first + 1, last + 1))
    if len(a_cols) < 2:
        return -1, None
    # header row was not recognised, so start from the very top
    return -1, {"q": first, "a": a_cols, "c": c_col}
