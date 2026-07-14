"""
db.py — Neon DB connection + schema creation
"""
import psycopg2
from psycopg2.extras import execute_values
from loguru import logger
import sys
import os

# Add parent dir so we can import config
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scraper.config import NEON_DB_URL


import threading
from psycopg2.pool import ThreadedConnectionPool

# ─── Connection Pool ──────────────────────────────────────────

_pool_lock = threading.Lock()
_db_pool = None

def get_connection():
    """Return a live psycopg2 connection from the global ThreadedConnectionPool."""
    global _db_pool
    if _db_pool is None:
        with _pool_lock:
            if _db_pool is None:
                # Create a pool that can scale up to 150 concurrent connections
                _db_pool = ThreadedConnectionPool(
                    minconn=5,
                    maxconn=150,
                    dsn=NEON_DB_URL
                )
    
    conn = _db_pool.getconn()
    # Auto-heal: If the connection is closed/dead, discard it and get a fresh one
    if conn.closed:
        try:
            _db_pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = _db_pool.getconn()
    return conn

def put_connection(conn):
    """Return a connection back to the global pool."""
    global _db_pool
    if _db_pool and conn:
        _db_pool.putconn(conn)


# ─── Schema ───────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS judgments (
    id               BIGSERIAL PRIMARY KEY,
    title            TEXT,                 -- e.g. "Kesavananda Bharati v. State of Kerala"
    case_number      TEXT,
    case_type        TEXT,
    year             INTEGER,
    judgment_date    TEXT,
    bench            TEXT[],
    petitioner       TEXT,
    respondent       TEXT,
    result           TEXT,
    acts_cited       TEXT[],
    cases_cited      TEXT[],
    tags             TEXT[],               -- e.g. ["Constitutional", "Fundamental Rights"]
    summary          TEXT,                 -- Short summary / doctrine
    holding          TEXT,                 -- The core legal holding
    applicability    TEXT,                 -- Applicability to lower courts
    full_text        TEXT,
    content_hash     TEXT UNIQUE,          -- SHA-256 of full_text for dedup
    source_url       TEXT,                 -- S3 URL to the PDF
    pdf_s3_key       TEXT,                 -- S3 object key for original PDF
    quality_score    REAL DEFAULT 0,
    scraped_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_year         ON judgments(year);
CREATE INDEX IF NOT EXISTS idx_case_type    ON judgments(case_type);
CREATE INDEX IF NOT EXISTS idx_case_number  ON judgments(case_number);
CREATE INDEX IF NOT EXISTS idx_content_hash ON judgments(content_hash);
"""

SCRAPE_LOG_SQL = """
CREATE TABLE IF NOT EXISTS scrape_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT,          -- 's3' | 'ecourts' | 'ik_api'
    key         TEXT UNIQUE,   -- S3 key or URL or case id
    status      TEXT,          -- 'pending' | 'done' | 'error'
    error_msg   TEXT,
    processed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scrape_status ON scrape_log(status);
"""


def init_db():
    """Create tables if they don't exist. Safe to run multiple times."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(SCRAPE_LOG_SQL)
        conn.commit()
        logger.success("✅ Neon DB schema ready (judgments + scrape_log)")
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ Schema creation failed: {e}")
        raise
    finally:
        put_connection(conn)


# ─── Upsert helpers ───────────────────────────────────────────

INSERT_JUDGMENT_SQL = """
INSERT INTO judgments (
    title, case_number, case_type, year, judgment_date,
    bench, petitioner, respondent, result,
    acts_cited, cases_cited, tags, summary, holding, applicability,
    full_text, content_hash, source_url, pdf_s3_key, quality_score
)
VALUES %s
ON CONFLICT (content_hash) DO NOTHING
RETURNING id;
"""

def bulk_insert_judgments(records: list[dict], conn=None) -> int:
    """
    Insert a batch of judgment dicts. Returns count of newly inserted rows.
    Each dict must have keys matching the INSERT columns above.
    """
    if not records:
        return 0

    owned = conn is None
    if owned:
        conn = get_connection()

    def _v(val):
        return "" if val is None else val

    rows = [
        (
            _v(r.get("title")),
            _v(r.get("case_number")),
            _v(r.get("case_type")),
            r.get("year"),
            _v(r.get("judgment_date")),
            r.get("bench", []),
            _v(r.get("petitioner")),
            _v(r.get("respondent")),
            _v(r.get("result")),
            r.get("acts_cited", []),
            r.get("cases_cited", []),
            r.get("tags", []),
            _v(r.get("summary")),
            _v(r.get("holding")),
            _v(r.get("applicability")),
            _v(r.get("full_text")),
            _v(r.get("content_hash")),
            _v(r.get("source_url")),
            _v(r.get("pdf_s3_key")),
            r.get("quality_score", 0.0),
        )
        for r in records
    ]

    try:
        with conn.cursor() as cur:
            results = execute_values(cur, INSERT_JUDGMENT_SQL, rows, fetch=True)
        conn.commit()
        return len(results)
    except Exception as e:
        conn.rollback()
        logger.error(f"Bulk insert failed: {e}")
        raise
    finally:
        if owned:
            put_connection(conn)


def log_scrape(source: str, key: str, status: str, error_msg: str = None):
    """Write a row to scrape_log for resume/checkpoint support."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_log (source, key, status, error_msg)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                    SET status = EXCLUDED.status,
                        error_msg = EXCLUDED.error_msg,
                        processed_at = NOW()
                """,
                (source, key, status, error_msg),
            )
        conn.commit()
    finally:
        put_connection(conn)


def already_scraped(key: str) -> bool:
    """Return True if this key has already been successfully processed."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scrape_log WHERE key = %s AND status = 'done'",
                (key,),
            )
            return cur.fetchone() is not None
    finally:
        put_connection(conn)


if __name__ == "__main__":
    init_db()
