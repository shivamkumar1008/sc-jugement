import psycopg2
from scraper.config import NEON_DB_URL
import re

conn = psycopg2.connect(NEON_DB_URL)
cur = conn.cursor()

cur.execute("SELECT full_text FROM judgments WHERE pdf_s3_key LIKE '%2024_10_126_149_EN.pdf'")
r = cur.fetchone()
if r:
    header = r[0][:500]
    
    vs = re.search(r'\n([^\n]+?)\s*\n+\s*v\.\s*\n+\s*([^\n]+?)\n', header, re.IGNORECASE)
    if vs:
        print("TITLE MATCH:", f"{vs.group(1).strip()} v. {vs.group(2).strip()}")
    else:
        print("TITLE NOT MATCHED")
        
conn.close()
