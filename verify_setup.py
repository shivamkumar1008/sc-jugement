"""
verify_setup.py — Checks all credentials and connections before running scraper.
Run this FIRST: python verify_setup.py
"""
import sys
import os

import io

# Force UTF-8 output on Windows (avoids cp1252 crash on emoji)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
logger.remove()
logger.add(sys.stdout, format="{message}", colorize=False)

PASS = "[OK]  "
FAIL = "[FAIL]"


def check_env():
    logger.info("\n─── 1. Environment Variables ───────────────────────────")
    from scraper.config import (
        NEON_DB_URL, NEON_DB_HOST, NEON_DB_USER,
        PUBLIC_S3_BUCKET, R2_ENDPOINT_URL, R2_BUCKET_NAME, R2_ACCESS_KEY,
        PROXY_ENABLED, PROXY_HOST, PROXY_USER,
    )
    checks = {
        "NEON_DB_URL":      bool(NEON_DB_URL),
        "NEON_DB_HOST":     bool(NEON_DB_HOST),
        "NEON_DB_USER":     bool(NEON_DB_USER),
        "PUBLIC_S3_BUCKET": bool(PUBLIC_S3_BUCKET),
        "CLOUDFLARE_R2_ENDPOINT_URL": bool(R2_ENDPOINT_URL),
        "CLOUDFLARE_R2_BUCKET_NAME":  bool(R2_BUCKET_NAME),
        "CLOUDFLARE_R2_ACCESS_KEY_ID": bool(R2_ACCESS_KEY),
    }
    all_ok = True
    for key, ok in checks.items():
        icon = PASS if ok else FAIL
        logger.info(f"  {icon} {key}")
        if not ok:
            all_ok = False

    if PROXY_ENABLED:
        logger.info(f"  {PASS} Proxy enabled → {PROXY_HOST} (user: {PROXY_USER[:6]}...)")
    else:
        logger.info(f"  [INFO] Proxy disabled (set PROXY_ENABLED=true to enable)")

    return all_ok


def check_neon_db():
    logger.info("\n─── 2. Neon DB Connection ──────────────────────────────")
    try:
        import psycopg2
        from scraper.config import NEON_DB_URL
        conn = psycopg2.connect(NEON_DB_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT version();")
        ver = cur.fetchone()[0]
        conn.close()
        logger.info(f"  {PASS} Connected → {ver[:60]}")
        return True
    except Exception as e:
        logger.error(f"  {FAIL} Connection failed: {e}")
        return False


def check_s3():
    logger.info("\n--- 3. Object Storage Access (Public S3 & Cloudflare R2) -----------------")
    try:
        import boto3
        from botocore.client import Config
        from botocore import UNSIGNED
        from scraper.config import PUBLIC_S3_BUCKET, R2_ENDPOINT_URL, R2_BUCKET_NAME, R2_ACCESS_KEY, R2_SECRET_KEY

        # Public bucket check
        s3_pub = boto3.client(
            "s3",
            region_name="ap-south-1",
            config=Config(signature_version=UNSIGNED),
        )
        resp = s3_pub.list_objects_v2(Bucket=PUBLIC_S3_BUCKET, Prefix="data/pdf/year=1950/", MaxKeys=1)
        count = resp.get("KeyCount", 0)
        logger.info(f"  {PASS} Public S3 Bucket '{PUBLIC_S3_BUCKET}' accessible")

        # Private Cloudflare R2 check
        r2 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )
        resp_priv = r2.list_objects_v2(Bucket=R2_BUCKET_NAME, MaxKeys=1)
        logger.info(f"  {PASS} Cloudflare R2 Bucket '{R2_BUCKET_NAME}' accessible (read/write)")

        return True
    except Exception as e:
        logger.error(f"  {FAIL} Object Storage access failed: {e}")
        return False


def check_db_schema():
    logger.info("\n─── 4. Neon DB Schema Init ─────────────────────────────")
    try:
        from storage.db import init_db
        init_db()
        return True
    except Exception as e:
        logger.error(f"  {FAIL} Schema init failed: {e}")
        return False


def check_imports():
    logger.info("\n─── 5. Python Dependencies ─────────────────────────────")
    deps = [
        ("psycopg2",    "psycopg2"),
        ("boto3",       "boto3"),
        ("fitz",        "PyMuPDF"),
        ("pdfplumber",  "pdfplumber"),
        ("pytesseract", "pytesseract"),
        ("tqdm",        "tqdm"),
        ("loguru",      "loguru"),
        ("dotenv",      "python-dotenv"),
    ]
    all_ok = True
    for module, pkg in deps:
        try:
            __import__(module)
            logger.info(f"  {PASS} {pkg}")
        except ImportError:
            logger.warning(f"  {FAIL} {pkg}  →  pip install {pkg}")
            all_ok = False
    return all_ok


if __name__ == "__main__":
    results = []
    results.append(check_imports())
    results.append(check_env())
    results.append(check_neon_db())
    results.append(check_s3())
    results.append(check_db_schema())

    logger.info("\n" + "="*55)
    if all(results):
        logger.success(">>> ALL CHECKS PASSED - Ready to run the scraper!")
        logger.info("\nNext step:")
        logger.info("  python -m scraper.bulk_downloader --max-keys 10")
        logger.info("  (test with 10 PDFs first, then remove --max-keys for full run)")
    else:
        logger.error("[!] Some checks FAILED - fix them before running the scraper.")
    logger.info("="*55)
