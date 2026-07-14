"""
config.py — Load all settings from .env
"""
import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ─── Neon DB ──────────────────────────────────────────────────
NEON_DB_URL     = os.environ["NEON_DB_URL"]
NEON_DB_HOST    = os.environ["NEON_DB_HOST"]
NEON_DB_PORT    = int(os.getenv("NEON_DB_PORT", 5432))
NEON_DB_NAME    = os.environ["NEON_DB_NAME"]
NEON_DB_USER    = os.environ["NEON_DB_USER"]
NEON_DB_PASSWORD= os.environ["NEON_DB_PASSWORD"]
NEON_DB_SSLMODE = os.getenv("NEON_DB_SSLMODE", "require")

# ─── Scraper ──────────────────────────────────────────────────
DELAY_MIN       = float(os.getenv("SCRAPER_REQUEST_DELAY_MIN", 3))
DELAY_MAX       = float(os.getenv("SCRAPER_REQUEST_DELAY_MAX", 7))
CONCURRENCY     = int(os.getenv("SCRAPER_CONCURRENCY", 2))
HEADLESS        = os.getenv("SCRAPER_HEADLESS", "true").lower() == "true"
START_YEAR      = int(os.getenv("SCRAPER_START_YEAR", 1950))
END_YEAR        = int(os.getenv("SCRAPER_END_YEAR", 2025))

# ─── Data Paths ───────────────────────────────────────────────
RAW_PDF_DIR        = os.getenv("RAW_PDF_DIR", "data/raw/pdfs")
EXTRACTED_TEXT_DIR = os.getenv("EXTRACTED_TEXT_DIR", "data/extracted")
CHECKPOINT_DIR     = os.getenv("CHECKPOINT_DIR", "data/checkpoints")

# ─── DataImpulse Proxy ────────────────────────────────────────
PROXY_ENABLED   = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_HOST      = os.getenv("PROXY_HOST", "gw.dataimpulse.com")
PROXY_PORT      = os.getenv("PROXY_PORT", "823")
PROXY_USER      = os.getenv("PROXY_USER", "")
PROXY_PASS      = os.getenv("PROXY_PASS", "")
PROXY_URL       = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}" if PROXY_ENABLED else None

# ─── Object Storage (Cloudflare R2 & Public S3) ──────────────────
PUBLIC_S3_BUCKET = os.getenv("PUBLIC_S3_BUCKET", "indian-supreme-court-judgments")
PUBLIC_S3_REGION = "ap-south-1"

R2_ENDPOINT_URL = os.getenv("CLOUDFLARE_R2_ENDPOINT_URL", "")
R2_BUCKET_NAME  = os.getenv("CLOUDFLARE_R2_BUCKET_NAME", "indian-supreme-court-judgments-1")
R2_ACCESS_KEY   = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY   = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")

# ─── CAPTCHA ──────────────────────────────────────────────────
CAPTCHA_SERVICE = os.getenv("CAPTCHA_SERVICE", "none")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")
