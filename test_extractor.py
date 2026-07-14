import json
from scraper.bulk_downloader import _public_s3_client
from scraper.config import PUBLIC_S3_BUCKET
from processor.pdf_extractor import extract_pdf

def test_extract():
    key = "data/pdf/year=2024/english/2024_11_1504_1524_EN.pdf"
    print(f"Downloading {key}...")
    s3 = _public_s3_client()
    resp = s3.get_object(Bucket=PUBLIC_S3_BUCKET, Key=key)
    pdf_bytes = resp['Body'].read()
    
    print("Extracting...")
    res = extract_pdf(pdf_bytes=pdf_bytes, pdf_name=key)
    
    print("--- TITLE ---")
    print(res.get("title"))
    print("\n--- TAGS ---")
    print(res.get("tags"))
    print("\n--- SUMMARY ---")
    print(res.get("summary"))
    print("\n--- HOLDING ---")
    print(res.get("holding"))
    print("\n--- APPLICABILITY ---")
    print(res.get("applicability"))

if __name__ == "__main__":
    test_extract()
