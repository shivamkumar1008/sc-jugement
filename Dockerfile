FROM python:3.10-slim

# tesseract-ocr: required by pytesseract for the scanned-page OCR fallback in
# processor/pdf_extractor.py. Nothing else here needs compiling — psycopg2-binary,
# PyMuPDF and Pillow all ship prebuilt manylinux x86_64 wheels, which is what a
# Hostinger VPS runs, so no build-essential/libpq-dev layer is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data/raw/pdfs data/extracted data/checkpoints

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs scraper/bulk_downloader.py's own __main__ block, which calls init_db()
# then streams+downloads+extracts+stores. Args are the default CMD below and can
# be overridden per `docker run <image> --years 2020-2024 --workers 8` or via
# docker-compose's `command:`.
ENTRYPOINT ["python", "-m", "scraper.bulk_downloader"]
CMD ["--years", "1950-2025", "--workers", "4"]
