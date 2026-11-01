"""Turn uploaded files into plain text (plus tables / raw media for AI)."""
from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser
from pathlib import Path

from .models import Extracted

# A line may start with FMT_OPEN + flags + FMT_CLOSE, telling the parser how the
# line was formatted in the source: b=bold, h=highlight/fill, c=coloured text,
# u=underline, s=spoiler. The parser uses it to find correct answers that are
# marked only by formatting.
FMT_OPEN, FMT_CLOSE = "\ue000", "\ue001"
# Lines TABLE_OPEN + n / TABLE_CLOSE + n surround the flattened text of tables[n], so the
# parser can swap a table it understood for its own result.
TABLE_OPEN, TABLE_CLOSE = "\ue002", "\ue003"

IMAGE_MIMES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
}
TEXT_EXTS = {".txt", ".md", ".gift", ".json", ".xml", ".text", ".log", ""}


class UnsupportedFile(Exception):
    pass


def tag(line: str, flags: str) -> str:
    return f"{FMT_OPEN}{flags}{FMT_CLOSE}{line}" if flags and line.strip() else line


def decode_bytes(data: bytes) -> str:
    encodings = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ()
    for enc in encodings + ("utf-8-sig", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def extract(data: bytes, filename: str) -> Extracted:
    ext = Path(filename or "").suffix.lower()
    if ext in IMAGE_MIMES:
        return Extracted(media=(data, IMAGE_MIMES[ext]), note="image")
    if ext == ".docx":
        return _docx(data)
    if ext == ".pdf":
        return _pdf(data)
    if ext in (".xlsx", ".xlsm"):
        return _xlsx(data)
    if ext == ".xls":
        return _xls(data)
    if ext in (".csv", ".tsv"):
        return _csv(decode_bytes(data))
    if ext == ".pptx":
        return _pptx(data)
    if ext in (".html", ".htm"):
        return Extracted(text=_html_to_text(decode_bytes(data)))
    if ext == ".rtf":
        return Extracted(text=_rtf_to_text(decode_bytes(data)))
    if ext in (".doc", ".ppt", ".odt", ".ods", ".odp", ".pages"):
        raise UnsupportedFile(
            f"{ext} formati to'g'ridan-to'g'ri o'qilmaydi. Faylni .docx / .xlsx / .pptx / .pdf "
            f"qilib saqlab qayta yuboring yoki matnini nusxalab tashlang."
        )
    if ext in TEXT_EXTS or _looks_textual(data):
        return Extracted(text=decode_bytes(data))
    raise UnsupportedFile(f"{ext or 'Bu'} faylni o'qib bo'lmadi.")


def _looks_textual(data: bytes) -> bool:
    sample = data[:4096]
    if not sample or b"\x00" in sample:
        return False
    text = decode_bytes(sample)
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return printable / max(len(text), 1) > 0.95


# ---------------------------------------------------------------- DOCX

def _docx(data: bytes) -> Extracted:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    numbering = _DocxNumbering(document)
    lines: list[str] = []
    tables: list[list[list[str]]] = []

    def para_line(p: Paragraph) -> str:
        text = p.text.strip()
        prefix = numbering.prefix(p)
        if not text:
            return ""
        return tag(f"{prefix}{text}" if prefix else text, _docx_flags(p))

    for child in document.element.body.iterchildren():
        name = child.tag.rsplit("}", 1)[-1]
        if name == "p":
            lines.append(para_line(Paragraph(child, document)))
        elif name == "tbl":
            table = Table(child, document)
            rows: list[list[str]] = []
            for row in table.rows:
                cells: list[str] = []
                seen = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:  # merged cells repeat
                        continue
                    seen.add(id(cell._tc))
                    cell_lines = [para_line(p) for p in cell.paragraphs]
                    cells.append("\n".join(l for l in cell_lines if l))
                rows.append(cells)
            tables.append(rows)
            lines.extend(_flatten_rows(rows, len(tables) - 1))
    return Extracted(text="\n".join(lines), tables=tables)


def _flatten_rows(rows: list[list[str]], index: int) -> list[str]:
    out: list[str] = ["", f"{TABLE_OPEN}{index}"]
    for cells in rows:
        cells = [c for c in cells if c.strip()]
        if not cells:
            continue
        if len(cells) == 2 and "\n" not in cells[0] and "\n" not in cells[1]:
            out.append(f"{_strip_tag(cells[0])} - {_strip_tag(cells[1])}")
        else:
            for c in cells:
                out.extend(c.split("\n"))
    out += [f"{TABLE_CLOSE}{index}", ""]
    return out


def _strip_tag(s: str) -> str:
    return re.sub(f"{FMT_OPEN}[a-z]*{FMT_CLOSE}", "", s)


def _docx_flags(p) -> str:
    from docx.oxml.ns import qn

    counts = {"b": 0, "h": 0, "c": 0, "u": 0}
    total = 0
    for run in p.runs:
        n = len(run.text.strip())
        if not n:
            continue
        total += n
        font = run.font
        if font.bold or (font.bold is None and p.style is not None and p.style.font.bold):
            counts["b"] += n
        rpr = run._r.rPr
        shd = rpr.find(qn("w:shd")) if rpr is not None else None
        shd_fill = shd.get(qn("w:fill")) if shd is not None else None
        if font.highlight_color or (shd_fill and shd_fill.upper() not in ("AUTO", "FFFFFF")):
            counts["h"] += n
        try:
            rgb = font.color.rgb if font.color is not None and font.color.type is not None else None
        except Exception:
            rgb = None
        if rgb is not None and str(rgb).upper() not in ("000000", "FFFFFF"):
            counts["c"] += n
        if font.underline:
            counts["u"] += n
    if not total:
        return ""
    return "".join(k for k, v in counts.items() if v / total >= 0.6)


class _DocxNumbering:
    """Re-creates Word's automatic list numbers ("1.", "A)") that are not part of the text."""

    def __init__(self, document):
        from docx.oxml.ns import qn

        self.qn = qn
        self.levels: dict[str, dict[int, tuple[str, str, int]]] = {}
        self.counters: dict[str, dict[int, int]] = {}
        try:
            root = document.part.numbering_part.element
        except Exception:
            return
        abstract: dict[str, dict[int, tuple[str, str, int]]] = {}
        for an in root.findall(qn("w:abstractNum")):
            lvls = {}
            for lvl in an.findall(qn("w:lvl")):
                ilvl = int(lvl.get(qn("w:ilvl"), "0"))
                fmt = self._val(lvl, "w:numFmt") or "decimal"
                text = self._val(lvl, "w:lvlText") or ""
                start = int(self._val(lvl, "w:start") or "1")
                lvls[ilvl] = (fmt, text, start)
            abstract[an.get(qn("w:abstractNumId"))] = lvls
        for num in root.findall(qn("w:num")):
            num_id = num.get(qn("w:numId"))
            abs_id = self._val(num, "w:abstractNumId")
            lvls = dict(abstract.get(abs_id, {}))
            for ov in num.findall(qn("w:lvlOverride")):
                ilvl = int(ov.get(qn("w:ilvl"), "0"))
                start = self._val(ov, "w:startOverride")
                if start and ilvl in lvls:
                    fmt, text, _ = lvls[ilvl]
                    lvls[ilvl] = (fmt, text, int(start))
            self.levels[num_id] = lvls

    def _val(self, el, tag_name):
        child = el.find(self.qn(tag_name))
        return child.get(self.qn("w:val")) if child is not None else None

    def _num_pr(self, p):
        ppr = p._p.pPr
        num_pr = ppr.numPr if ppr is not None else None
        style = p.style
        while num_pr is None and style is not None:
            sppr = style.element.pPr
            num_pr = sppr.numPr if sppr is not None else None
            style = style.base_style
        if num_pr is None or num_pr.numId is None:
            return None
        ilvl = num_pr.ilvl.val if num_pr.ilvl is not None else 0
        return str(num_pr.numId.val), int(ilvl)

    def prefix(self, p) -> str:
        if not self.levels:
            return ""
        found = self._num_pr(p)
        if not found:
            return ""
        num_id, ilvl = found
        lvls = self.levels.get(num_id)
        if not lvls or ilvl not in lvls or num_id == "0":
            return ""
        counters = self.counters.setdefault(num_id, {})
        fmt, text, start = lvls[ilvl]
        counters[ilvl] = counters[ilvl] + 1 if ilvl in counters else start
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]
        if fmt in ("bullet", "none"):
            return ""

        def repl(m):
            lv = int(m.group(1)) - 1
            if lv not in lvls:
                return ""
            value = counters.get(lv, lvls[lv][2])
            return _format_number(value, lvls[lv][0])

        rendered = re.sub(r"%(\d)", repl, text).strip()
        return f"{rendered} " if rendered else ""


def _format_number(n: int, fmt: str) -> str:
    if fmt in ("upperLetter", "lowerLetter"):
        s = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s if fmt == "upperLetter" else s.lower()
    if fmt in ("russianUpper", "russianLower"):
        letters = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ"
        s = letters[(n - 1) % len(letters)]
        return s if fmt == "russianUpper" else s.lower()
    if fmt in ("upperRoman", "lowerRoman"):
        vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
        s = ""
        for v, sym in vals:
            while n >= v:
                s += sym
                n -= v
        return s if fmt == "upperRoman" else s.lower()
    return str(n)


# ---------------------------------------------------------------- PDF

def _pdf(data: bytes) -> Extracted:
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    lines: list[str] = []
    plain_chars = 0
    for page in doc:
        page_dict = page.get_text("dict", sort=True)
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                text = "".join(s["text"] for s in line.get("spans", [])).strip()
                if not text:
                    continue
                plain_chars += len(text)
                total = sum(len(s["text"].strip()) for s in spans) or 1
                bold = sum(len(s["text"].strip()) for s in spans
                           if s.get("flags", 0) & 16 or "bold" in s.get("font", "").lower())
                colored = sum(len(s["text"].strip()) for s in spans
                              if s.get("color", 0) not in (0, 0xFFFFFF))
                flags = ("b" if bold / total >= 0.6 else "") + ("c" if colored / total >= 0.6 else "")
                lines.append(tag(text, flags))
            lines.append("")
    pages = max(len(doc), 1)
    if plain_chars < 40 * pages:
        # scanned PDF: no text layer, let the AI read the pages
        return Extracted(text="\n".join(lines), media=(data, "application/pdf"), note="scanned_pdf")
    return Extracted(text="\n".join(lines))


# ---------------------------------------------------------------- spreadsheets

def _xlsx(data: bytes) -> Extracted:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    tables: list[list[list[str]]] = []
    for ws in wb.worksheets:
        rows: list[list[str]] = []
        for row in ws.iter_rows():
            cells = []
            for c in row:
                value = "" if c.value is None else _cell_str(c.value)
                cells.append(tag(value, _xlsx_flags(c)) if value else "")
            while cells and not cells[-1]:
                cells.pop()
            rows.append(cells)
        while rows and not any(rows[-1]):
            rows.pop()
        if rows:
            tables.append(rows)
    return _tables_result(tables)


def _xlsx_flags(c) -> str:
    flags = ""
    font, fill = c.font, c.fill
    if font is not None and font.b:
        flags += "b"
    try:
        if fill is not None and fill.fill_type == "solid" and fill.fgColor.type == "rgb" \
                and fill.fgColor.rgb not in ("00000000", "FFFFFFFF", "FF000000"):
            flags += "h"
    except Exception:
        pass
    try:
        if font is not None and font.color is not None and font.color.type == "rgb" \
                and font.color.rgb not in ("FF000000", "00000000", "FFFFFFFF"):
            flags += "c"
    except Exception:
        pass
    return flags


def _cell_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _xls(data: bytes) -> Extracted:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    tables = []
    for sheet in book.sheets():
        rows = [[_cell_str(v) if v != "" else "" for v in sheet.row_values(r)] for r in range(sheet.nrows)]
        if rows:
            tables.append(rows)
    return _tables_result(tables)


def _csv(text: str) -> Extracted:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
    return _tables_result([rows])


def _tables_result(tables: list[list[list[str]]]) -> Extracted:
    lines: list[str] = []
    for i, rows in enumerate(tables):
        lines.extend(_flatten_rows(rows, i))
    return Extracted(text="\n".join(lines), tables=tables)


# ---------------------------------------------------------------- PPTX

def _pptx(data: bytes) -> Extracted:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    lines: list[str] = []
    tables: list[list[list[str]]] = []

    def walk(shapes):
        for shape in shapes:
            if shape.shape_type == 6 and hasattr(shape, "shapes"):  # group
                walk(shape.shapes)
            elif getattr(shape, "has_table", False) and shape.has_table:
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                tables.append(rows)
                lines.extend(_flatten_rows(rows, len(tables) - 1))
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(r.text for r in para.runs).strip()
                    if not text:
                        continue
                    total = sum(len(r.text.strip()) for r in para.runs) or 1
                    bold = sum(len(r.text.strip()) for r in para.runs if r.font.bold)
                    lines.append(tag(text, "b" if bold / total >= 0.6 else ""))

    for slide in prs.slides:
        walk(slide.shapes)
        lines.append("")
    return Extracted(text="\n".join(lines), tables=tables)


# ---------------------------------------------------------------- HTML / RTF

class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, t, attrs):
        if t in ("script", "style"):
            self.skip += 1
        elif t in self.BLOCK:
            self.parts.append("\n")
        elif t in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, t):
        if t in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif t in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.parts.append(d)


def _html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts))


def _rtf_to_text(rtf: str) -> str:
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), rtf)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1251"), text)
    text = re.sub(r"\\(par|line)\b ?", "\n", text)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return text.replace("{", "").replace("}", "")
