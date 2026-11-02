import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()

# Webhook mode (Render web service): Render sets RENDER_EXTERNAL_URL and PORT itself.
# Without a public URL the bot falls back to long polling (local run).
WEBHOOK_BASE = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

TEMPLATES = ROOT / "templates"
BLOOKET_TEMPLATE = TEMPLATES / "blooket_template.xlsx"
KAHOOT_TEMPLATE = TEMPLATES / "kahoot_template.xlsx"

# Telegram Bot API can only download files up to 20 MB
MAX_FILE_BYTES = 20 * 1024 * 1024

# AI quiz generation limits
GEN_MAX_QUESTIONS = 30
GEN_DAILY_LIMIT = int(os.getenv("GEN_DAILY_LIMIT", "3"))  # generated tests per user per day (Tashkent time)
GEN_MAX_FILE_BYTES = 5 * 1024 * 1024  # source material file
GEN_MAX_CHARS = 20_000  # source material text, about 10 pages
