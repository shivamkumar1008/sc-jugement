"""
test_regex_by_year.py — Sample one judgment PDF per decade from the public S3
bucket, run it through processor.pdf_extractor.extract_pdf(), and report
which regex-derived fields matched vs came back empty/None.

Goal: see whether the extraction regexes (case number, date, bench,
citations, acts, title/parties, result, summary, holding) hold up across
old vs. new judgment formats, not just the 2024 PDFs used in test_extractor.py.

Usage:
    python test_regex_by_year.py                # sample years, 1 pdf each
    python test_regex_by_year.py --years 1955,1980,2005,2024
    python test_regex_by_year.py --per-year 3    # try 3 pdfs per year
    python test_regex_by_year.py --db-check      # also compare vs DB rows
"""
import argparse
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from scraper.bulk_downloader import _public_s3_client
from scraper.config import PUBLIC_S3_BUCKET
from processor.pdf_extractor import extract_pdf

DEFAULT_YEARS = [1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020, 2024]

FIELDS = [
    "title", "case_number", "case_type", "year", "judgment_date",
    "bench", "petitioner", "respondent", "result",
    "acts_cited", "cases_cited", "tags", "summary", "holding",
    "applicability", "quality_score",
]


def _is_empty(v):
    if v is None:
        return True
    if isinstance(v, (list, tuple, str)) and len(v) == 0:
        return True
    if isinstance(v, (int, float)) and v == 0:
        return True
    return False


def sample_keys_for_year(year: int, n: int) -> list:
    """List up to n english PDF keys for a given year prefix."""
    s3 = _public_s3_client()
    prefix = f"data/pdf/year={year}/english/"
    resp = s3.list_objects_v2(Bucket=PUBLIC_S3_BUCKET, Prefix=prefix, MaxKeys=n)
    keys = [obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].lower().endswith(".pdf")]
    return keys


def download(key: str) -> bytes:
    s3 = _public_s3_client()
    resp = s3.get_object(Bucket=PUBLIC_S3_BUCKET, Key=key)
    return resp["Body"].read()


def report_record(key: str, record: dict) -> dict:
    print(f"\n{'=' * 100}")
    print(f"KEY: {key}")
    print("-" * 100)

    empties = []
    for f in FIELDS:
        v = record.get(f)
        empty = _is_empty(v)
        if empty:
            empties.append(f)
        tag = "  EMPTY" if empty else "  OK   "
        display = v
        if isinstance(v, str) and len(v) > 120:
            display = v[:120] + "..."
        print(f"[{tag}] {f:16s}: {display}")

    print(f"\n  --> {len(FIELDS) - len(empties)}/{len(FIELDS)} fields populated. Missing: {empties}")
    return {"key": key, "empties": empties, "quality": record.get("quality_score", 0.0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=str, default=None, help="comma list, e.g. 1955,1980,2005")
    ap.add_argument("--per-year", type=int, default=1)
    ap.add_argument("--db-check", action="store_true", help="also cross-check against stored DB rows")
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",")] if args.years else DEFAULT_YEARS

    all_results = []
    field_empty_count = {f: 0 for f in FIELDS}
    total_docs = 0

    for year in years:
        print(f"\n########## YEAR {year} ##########")
        try:
            keys = sample_keys_for_year(year, args.per_year)
        except Exception as e:
            print(f"  [ERROR] Could not list year={year}: {e}")
            continue

        if not keys:
            print(f"  [SKIP] No PDFs found for year={year}")
            continue

        for key in keys:
            try:
                pdf_bytes = download(key)
                record = extract_pdf(pdf_bytes=pdf_bytes, pdf_name=key)
            except Exception as e:
                print(f"  [ERROR] Extraction failed for {key}: {e}")
                continue

            res = report_record(key, record)
            res["year"] = year
            all_results.append(res)
            total_docs += 1
            for f in res["empties"]:
                field_empty_count[f] += 1

    # ─── Summary ───────────────────────────────────────────────
    print(f"\n\n{'#' * 100}")
    print(f"SUMMARY across {total_docs} documents, years={years}")
    print("#" * 100)
    for f in FIELDS:
        miss = field_empty_count[f]
        pct = (miss / total_docs * 100) if total_docs else 0
        flag = "  <-- REGEX LIKELY WEAK HERE" if pct >= 30 else ""
        print(f"  {f:16s}: empty in {miss}/{total_docs} ({pct:.0f}%){flag}")

    low_quality = [r for r in all_results if r["quality"] < 0.25]
    if low_quality:
        print(f"\n  {len(low_quality)} doc(s) below quality_score 0.25 (would be SKIPPED, not stored):")
        for r in low_quality:
            print(f"    - {r['key']} (year={r['year']}, score={r['quality']:.2f})")

    if args.db_check:
        print(f"\n{'=' * 100}")
        print("DB CROSS-CHECK (rows already stored, compares live regex output vs stored values)")
        print("=" * 100)
        db_cross_check([r["key"] for r in all_results])


def db_cross_check(keys: list):
    import psycopg2
    from scraper.config import NEON_DB_URL

    conn = psycopg2.connect(NEON_DB_URL)
    cur = conn.cursor()
    try:
        for key in keys:
            cur.execute(
                "SELECT title, case_number, year, judgment_date, bench, result, quality_score "
                "FROM judgments WHERE pdf_s3_key = %s",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                print(f"  [NOT IN DB] {key}")
            else:
                title, case_number, year, jdate, bench, result, qscore = row
                print(f"  [IN DB] {key}")
                print(f"      title={title!r} case_number={case_number!r} year={year} "
                      f"date={jdate!r} bench={bench} result={result!r} q={qscore}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
