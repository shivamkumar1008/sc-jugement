import psycopg2
from scraper.config import NEON_DB_URL
from processor.pdf_extractor import _parse_metadata

conn = psycopg2.connect(NEON_DB_URL)
cur = conn.cursor()

cur.execute("SELECT full_text, pdf_s3_key FROM judgments WHERE pdf_s3_key LIKE '%2024_10_126_149_EN.pdf'")
r = cur.fetchone()
if r:
    text = r[0]
    res = {"bench": [], "tags": [], "full_text": text, "pdf_s3_key": r[1]}
    _parse_metadata(text, res)
    print('Title:', res.get('title'))
    print('Tags:', res.get('tags'))
    print('Summary:', res.get('summary'))
    holding = res.get('holding')
    print('Holding:', holding[:100] + '...' if holding else None)
    print('Applicability:', res.get('applicability'))
else:
    print("Not found in DB")

conn.close()
