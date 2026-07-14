"""
bulk_downloader.py — Download SC India judgments from AWS S3.
KEY FIX: Streams S3 pages — does NOT list all keys before starting.
Starts downloading immediately from first page, stops at max_keys.
"""
import os
import sys
import time
import random
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice

import boto3
from botocore.exceptions import ClientError
from botocore.client import Config
from botocore import UNSIGNED
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scraper.config import (
    PUBLIC_S3_BUCKET, PUBLIC_S3_REGION, R2_ENDPOINT_URL, 
    R2_BUCKET_NAME, R2_ACCESS_KEY, R2_SECRET_KEY, RAW_PDF_DIR,
    START_YEAR, END_YEAR
)
from storage.db import log_scrape, already_scraped, bulk_insert_judgments, init_db
from processor.pdf_extractor import extract_pdf


# ─── S3/R2 Clients (Global & Thread-Safe Caching) ───────────────

_public_client_instance = None
_public_client_lock = threading.Lock()

_r2_client_instance = None
_r2_client_lock = threading.Lock()


def _public_s3_client():
    """Returns a thread-safe global public S3 client"""
    global _public_client_instance
    if _public_client_instance is None:
        with _public_client_lock:
            if _public_client_instance is None:
                _public_client_instance = boto3.client(
                    "s3",
                    region_name=PUBLIC_S3_REGION,
                    config=Config(
                        signature_version=UNSIGNED,
                        connect_timeout=15,
                        read_timeout=90,
                        max_pool_connections=100,
                        retries={"max_attempts": 5, "mode": "adaptive"},
                    ),
                )
    return _public_client_instance


def _my_s3_client():
    """Returns a thread-safe global Cloudflare R2 client"""
    global _r2_client_instance
    if _r2_client_instance is None:
        with _r2_client_lock:
            if _r2_client_instance is None:
                _r2_client_instance = boto3.client(
                    "s3",
                    endpoint_url=R2_ENDPOINT_URL,
                    aws_access_key_id=R2_ACCESS_KEY,
                    aws_secret_access_key=R2_SECRET_KEY,
                    config=Config(
                        connect_timeout=15,
                        read_timeout=90,
                        max_pool_connections=100,
                        retries={"max_attempts": 5, "mode": "adaptive"},
                    )
                )
    return _r2_client_instance


# ─── Stream PDF keys (generator — no upfront full listing) ────

def stream_s3_pdf_keys(prefix: str = "data/pdf/", max_keys: int = None, years: list = None):
    """
    Generator: yields S3 keys one by one, page by page.
    Stops after max_keys if specified.
    """
    s3 = _public_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    prefixes = []
    if years:
        for yr in years:
            prefixes.append(f"data/pdf/year={yr}/english/")
    else:
        prefixes.append(prefix)

    yielded = 0
    page_num = 0

    for pref in prefixes:
        logger.info(f"Streaming keys from s3://{PUBLIC_S3_BUCKET}/{pref}")
        try:
            for page in paginator.paginate(Bucket=PUBLIC_S3_BUCKET, Prefix=pref):
                page_num += 1
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.lower().endswith(".pdf"):
                        yield key
                        yielded += 1
                        if max_keys and yielded >= max_keys:
                            logger.info(f"Reached max_keys={max_keys}, stopping listing.")
                            return

                logger.debug(f"  S3 page {page_num}: got {page.get('KeyCount', 0)} objects (total yielded: {yielded})")
        except Exception as e:
            logger.error(f"Failed to list prefix {pref}: {e}")

    logger.info(f"Listed {yielded:,} PDF keys from S3")


# ─── Transfer & Process PDF (In-Memory) ───────────────────────

def _transfer_and_process(key: str) -> bool:
    """
    Downloads PDF bytes from public S3, uploads to your private S3,
    and extracts text for the DB all in-memory. No local files saved!
    Returns True if successfully processed or skipped, False on error.
    """
    if already_scraped(key):
        return False   # resume checkpoint

    try:
        # 1. Download to memory
        s3_public = _public_s3_client()
        resp = s3_public.get_object(Bucket=PUBLIC_S3_BUCKET, Key=key)
        pdf_bytes = resp['Body'].read()

        # 2. Upload to private R2
        s3_my = _my_s3_client()
        s3_my.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=pdf_bytes)

        # 3. Process from memory
        record = extract_pdf(pdf_bytes=pdf_bytes, pdf_name=key)
        record["pdf_s3_key"] = key
        record["source_url"] = f"{R2_ENDPOINT_URL}/{R2_BUCKET_NAME}/{key}"

        if record["quality_score"] < 0.25:
            logger.warning(f"Low quality ({record['quality_score']:.2f}) skipped: {key}")
            log_scrape("s3", key, "low_quality")
            return True

        # 4. Save to Neon DB
        inserted = bulk_insert_judgments([record])
        status = "done" if inserted else "duplicate"
        log_scrape("s3", key, status)
        logger.info(f"  [{status.upper()}] {Path(key).name}")
        return True

    except ClientError as e:
        logger.error(f"S3 Error [{key}]: {e}")
        log_scrape("s3", key, "error", str(e))
        return False
    except Exception as e:
        logger.error(f"Processing failed [{key}]: {e}")
        log_scrape("s3", key, "error", str(e))
        return False


# ─── Main streaming pipeline ──────────────────────────────────

def run_bulk_download(
    prefix: str = "data/pdf/",
    local_dir: str = None,
    max_workers: int = 4,
    max_keys: int = None,
    year: int = None,
    years: list = None,
):
    """
    Streaming pipeline — starts downloading immediately:
      Stream S3 key → download PDF → extract text → insert Neon DB
    """
    local_dir = local_dir or RAW_PDF_DIR
    Path(local_dir).mkdir(parents=True, exist_ok=True)

    # Override prefix/years if year specified
    if year:
        years = [year]

    target_desc = f"years={years}" if years else f"prefix={prefix}"
    logger.info(f"Starting download | {target_desc} | max_keys={max_keys} | workers={max_workers}")

    success = skip = error = 0

    # Use tqdm with unknown total (streaming — we don't know count upfront)
    with tqdm(unit="pdf", desc="Processing", dynamic_ncols=True) as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:

            # Submit jobs as we stream keys (no upfront full listing)
            futures = set()
            key_stream = stream_s3_pdf_keys(prefix=prefix, max_keys=max_keys, years=years)

            # Fill initial batch
            for key in islice(key_stream, max_workers):
                futures.add(pool.submit(_transfer_and_process, key))

            # Process as futures complete, refill from stream
            from concurrent.futures import wait, FIRST_COMPLETED
            while futures:
                # Wait for at least one future to complete
                done, not_done = wait(futures, return_when=FIRST_COMPLETED)
                
                for future in done:
                    try:
                        processed = future.result()
                        if processed:
                            success += 1
                        else:
                            skip += 1
                    except Exception as e:
                        logger.error(f"Unhandled exception processing: {e}")
                        error += 1

                    pbar.update(1)
                    pbar.set_postfix(stored=success, skip=skip, err=error)
                
                # The remaining futures that haven't finished yet
                futures = not_done
                
                # Refill the pool back up to max_workers
                while len(futures) < max_workers:
                    try:
                        next_key = next(key_stream)
                        futures.add(pool.submit(_transfer_and_process, next_key))
                    except StopIteration:
                        break  # stream exhausted

    logger.success(f"Done! Stored={success} | Skipped={skip} | Errors={error}")


# ─── CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SC India S3 Bulk Downloader")
    parser.add_argument(
        "--prefix",   default="data/pdf/",
        help="S3 key prefix (e.g. 'data/pdf/year=2020/english/')"
    )
    parser.add_argument(
        "--year",     type=int, default=None,
        help="Download one specific year, e.g. --year 2024"
    )
    parser.add_argument(
        "--years",    type=str, default=None,
        help="Download a range of years, e.g. --years 2010-2019 or list: 2010,2011,2012"
    )
    parser.add_argument("--workers",  type=int, default=4)
    parser.add_argument(
        "--max-keys", type=int, default=None,
        help="Stop after N PDFs (for testing). E.g. --max-keys 5"
    )
    args = parser.parse_args()

    logger.info("Initialising Neon DB schema...")
    init_db()

    years_list = None
    if args.years:
        if "-" in args.years:
            start, end = map(int, args.years.split("-"))
            years_list = list(range(start, end + 1))
        else:
            years_list = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    elif args.year:
        years_list = [args.year]
    elif START_YEAR and END_YEAR:
        logger.info(f"No --years provided via CLI, falling back to .env: {START_YEAR}-{END_YEAR}")
        years_list = list(range(START_YEAR, END_YEAR + 1))

    run_bulk_download(
        prefix=args.prefix,
        year=args.year,
        years=years_list,
        max_workers=args.workers,
        max_keys=args.max_keys,
    )
