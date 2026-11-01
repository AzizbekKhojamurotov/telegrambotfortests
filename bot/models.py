from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Question:
    text: str
    answers: list[str] = field(default_factory=list)
    # 0-based indexes into `answers`
    correct: list[int] = field(default_factory=list)
    # number written in the source ("12. ..."), used to match answer keys
    number: int | None = None
    # True when correct answers were guessed by AI, not taken from the source
    ai_guessed: bool = False

    def copy(self) -> "Question":
        return Question(self.text, list(self.answers), list(self.correct), self.number, self.ai_guessed)

    @property
    def is_valid(self) -> bool:
        return bool(self.text.strip()) and len(self.answers) >= 2

    @property
    def has_correct(self) -> bool:
        return bool(self.correct) and all(0 <= i < len(self.answers) for i in self.correct)


@dataclass
class ParseResult:
    questions: list[Question] = field(default_factory=list)
    # questions dropped because they had fewer than two answers
    dropped: list[str] = field(default_factory=list)
    used_ai: bool = False

    @property
    def missing_correct(self) -> list[int]:
        """1-based positions of questions without a correct answer."""
        return [i + 1 for i, q in enumerate(self.questions) if not q.has_correct]


@dataclass
class Extracted:
    """Content pulled out of an uploaded file or message."""
    text: str = ""
    # each table is a list of rows, each row a list of cell strings
    tables: list[list[list[str]]] = field(default_factory=list)
    # raw media for AI (scanned PDFs, photos): (bytes, mime type)
    media: tuple[bytes, str] | None = None
    note: str = ""
