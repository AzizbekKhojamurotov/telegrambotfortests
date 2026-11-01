"""Fill the official Blooket / Kahoot import templates."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from . import config
from .models import Question

MAX_ANSWERS = 4
KAHOOT_TIMES = (5, 10, 20, 30, 60, 90, 120, 240)
KAHOOT_Q_MAX, KAHOOT_A_MAX = 120, 75
BLOOKET_TIME_MAX = 300


@dataclass
class Platform:
    name: str
    template: str
    first_row: int
    rows: int  # data rows prepared in the template
    q_limit: int | None = None
    a_limit: int | None = None


BLOOKET = Platform("blooket", str(config.BLOOKET_TEMPLATE), first_row=3, rows=100)
KAHOOT = Platform("kahoot", str(config.KAHOOT_TEMPLATE), first_row=9, rows=100,
                  q_limit=KAHOOT_Q_MAX, a_limit=KAHOOT_A_MAX)
PLATFORMS = {"blooket": BLOOKET, "kahoot": KAHOOT}


@dataclass
class ExportReport:
    files: list[tuple[str, bytes]] = field(default_factory=list)
    truncated_questions: list[int] = field(default_factory=list)
    truncated_answers: list[int] = field(default_factory=list)
    trimmed_options: list[int] = field(default_factory=list)


def _truncate(text: str, limit: int | None) -> tuple[str, bool]:
    if limit is None or len(text) <= limit:
        return text, False
    return text[: limit - 1].rstrip() + "…", True


def _fit_answers(q: Question) -> tuple[list[str], list[int], bool]:
    """Keeps at most 4 options; correct ones are never dropped."""
    if len(q.answers) <= MAX_ANSWERS:
        return list(q.answers), list(q.correct), False
    keep = list(q.correct[:MAX_ANSWERS])
    for i in range(len(q.answers)):
        if len(keep) >= MAX_ANSWERS:
            break
        if i not in keep:
            keep.append(i)
    keep.sort()
    answers = [q.answers[i] for i in keep]
    correct = [keep.index(i) for i in q.correct if i in keep]
    return answers, correct, True


def _put(ws, row: int, col: int, value):
    cell = ws.cell(row=row, column=col)
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell.value = value
        cell.data_type = "s"  # never let "=..." become a formula
    else:
        cell.value = value


def _correct_value(correct: list[int]):
    nums = [i + 1 for i in sorted(correct)]
    return nums[0] if len(nums) == 1 else ",".join(map(str, nums))


INLINE_CELL_RE = re.compile(r'<c ([^>]*?)t="inlineStr"([^>]*)><is>(.*?)</is></c>', re.S)
SST_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SST_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
SST_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"


def _to_shared_strings(data: bytes) -> bytes:
    """openpyxl writes every string inline; the official templates (and the importers
    built around them) use a shared-strings table, so convert to that layout."""
    src = zipfile.ZipFile(io.BytesIO(data))
    if "xl/sharedStrings.xml" in src.namelist():
        return data
    strings: dict[str, int] = {}

    def repl(m):
        inner = m.group(3)
        idx = strings.setdefault(inner, len(strings))
        return f'<c {m.group(1)}t="s"{m.group(2)}><v>{idx}</v></c>'

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet") and item.filename.endswith(".xml"):
                content = INLINE_CELL_RE.sub(repl, content.decode("utf-8")).encode("utf-8")
            elif item.filename == "[Content_Types].xml":
                content = content.decode("utf-8").replace(
                    "</Types>", f'<Override PartName="/xl/sharedStrings.xml" ContentType="{SST_CT}"/></Types>'
                ).encode("utf-8")
            elif item.filename == "xl/_rels/workbook.xml.rels":
                content = content.decode("utf-8").replace(
                    "</Relationships>",
                    f'<Relationship Type="{SST_REL}" Target="sharedStrings.xml" Id="rIdSst1"/></Relationships>',
                ).encode("utf-8")
            dst.writestr(item, content)
        sst = "".join(f"<si>{s}</si>" for s in strings)
        dst.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<sst xmlns="{SST_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">{sst}</sst>',
        )
    return out.getvalue()


def build(platform_name: str, questions: list[Question], time_limit: int, base_name: str) -> ExportReport:
    p = PLATFORMS[platform_name]
    if p is KAHOOT and time_limit not in KAHOOT_TIMES:
        time_limit = min(KAHOOT_TIMES, key=lambda t: abs(t - time_limit))
    time_limit = max(1, min(time_limit, BLOOKET_TIME_MAX))

    report = ExportReport()
    chunks = [questions[i:i + p.rows] for i in range(0, len(questions), p.rows)] or [[]]
    for part, chunk in enumerate(chunks, start=1):
        wb = openpyxl.load_workbook(p.template)
        ws = wb.worksheets[0]
        # wipe the template's example rows, keep the numbering in column A
        for r in range(p.first_row, p.first_row + p.rows):
            for c in range(2, 9):
                ws.cell(row=r, column=c).value = None
        offset = (part - 1) * p.rows
        for i, q in enumerate(chunk):
            n = offset + i + 1
            row = p.first_row + i
            answers, correct, trimmed = _fit_answers(q)
            if trimmed:
                report.trimmed_options.append(n)
            text, cut = _truncate(q.text, p.q_limit)
            if cut:
                report.truncated_questions.append(n)
            _put(ws, row, 2, text)
            any_cut = False
            for j in range(MAX_ANSWERS):
                if j < len(answers):
                    value, cut = _truncate(answers[j], p.a_limit)
                    any_cut |= cut
                    _put(ws, row, 3 + j, value)
            if any_cut:
                report.truncated_answers.append(n)
            _put(ws, row, 7, time_limit)
            _put(ws, row, 8, _correct_value(correct))
        suffix = f"_{part}-qism" if len(chunks) > 1 else ""
        if p is BLOOKET:
            # Blooket's importer only takes the template exported as CSV
            report.files.append((f"{base_name}_{p.name}{suffix}.csv", _sheet_to_csv(ws, p.first_row + len(chunk))))
            continue
        buf = io.BytesIO()
        wb.save(buf)
        report.files.append((f"{base_name}_{p.name}{suffix}.xlsx", _to_shared_strings(buf.getvalue())))
    return report


def _sheet_to_csv(ws, end_row: int) -> bytes:
    """Rows 1..end_row-1, columns A-H, the way Google Sheets / Excel export the template:
    row 1 (logo) empty, row 2 headers, then the filled rows."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for r in range(1, end_row):
        writer.writerow(["" if (v := ws.cell(row=r, column=c).value) is None else v for c in range(1, 9)])
    return out.getvalue().encode("utf-8")
