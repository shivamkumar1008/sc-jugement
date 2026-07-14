"""
pdf_extractor.py — Extract text from SC India judgment PDFs
Handles both text-based and scanned (OCR) PDFs.
"""
import hashlib
import re
from pathlib import Path
from loguru import logger

try:
    import fitz  # PyMuPDF
    PYMUPDF_OK = True
except ImportError:
    PYMUPDF_OK = False
    logger.warning("PyMuPDF not installed — PDF extraction disabled")

try:
    import pytesseract
    from PIL import Image
    import io
    OCR_OK = True
except ImportError:
    OCR_OK = False
    logger.warning("pytesseract/Pillow not installed — OCR disabled")


# ─── Regex patterns (compiled to match on collapsed whitespace text) ───

CASE_NUM_RE = re.compile(
    r'\b(Civil Appeal|Writ Petition|Criminal Appeal|SLP|Special Leave Petition|Transfer Petition|'
    r'Review Petition|Curative Petition|Original Suit|Case|Reference|Appeal|Petition)\s*(?:\([^)]*\))?\s*'
    r'(?:Nos?\.?|No\(s\)\.?)\s*([A-Za-z0-9lI\-\/,\s\&]+?)\s*of\s*(\d{4})\b',
    re.IGNORECASE
)

# Date patterns (applied on collapsed space text)
DATE_PATTERNS = [
    # 30 September 2024 or 3. September 2024 or 30-September-2024
    re.compile(rf'\b(\d{{1,2}}[\s\.,\-]+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s\.,\-]+\d{{4}})\b', re.IGNORECASE),
    # September 30, 2024
    re.compile(rf'\b((?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s\.,\-]*\d{{1,2}}[\s\.,\-]*\d{{4}})\b', re.IGNORECASE),
    # 1950. December 21
    re.compile(rf'\b(\d{{4}}[\s\.,\-]*(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s\.,\-]*\d{{1,2}})\b', re.IGNORECASE),
    # 30-09-2024 or 30/09/2024
    re.compile(r'\b(\d{1,2}[\-\/]\d{1,2}[\-\/]\d{4})\b'),
]

def _clean_bench(raw_bench: str) -> list:
    if not raw_bench:
        return []
    
    # Replace newlines/tabs with spaces
    raw_bench = re.sub(r'\s+', ' ', raw_bench)
    
    # Split by common delimiters (comma, "and", "&", semicolon) case-insensitively
    segments = re.split(r'(?i),|\band\b|&|;', raw_bench)
    
    cleaned_judges = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
            
        # Clean title suffixes at the very end of segment (e.g. CJI, CJ, JJ, J, CJI.)
        seg_clean = re.sub(r'(?i)\b(?:JJ\.?|J\.?|CJI\.?|C\.?J\.?|C\.?J\.?I\.?)\s*$', '', seg)
        
        # Clean title prefixes at the start of segment (e.g. Hon'ble, Justice, Dr, Mr, Mrs, Shri, Smt)
        seg_clean = re.sub(r'(?i)^\s*\b(?:Hon\'ble|Justice|Mr\.?|Mrs\.?|Shri|Smt\.?|Dr\.?)\b', '', seg_clean)
        
        seg_clean = seg_clean.replace('*', '')
        
        # Remove extra leading/trailing punctuation
        seg_clean = re.sub(r'^[^\w]+|[^\w]+$', '', seg_clean).strip()
        seg_clean = re.sub(r'\s+', ' ', seg_clean)
        
        # Lowercase check for false positive words
        seg_lower = seg_clean.lower()
        forbidden_words = {
            'held', 'court', 'appeal', 'petition', 'plaintiff', 'defendant', 
            'non-judice', 'nonjudice', 'decree', 'embargo', 'judgement', 
            'judgment', 'embargo', 'industrial', 'companies', 'provisions', 
            'arising', 'suit', 'interim', 'order', 'application'
        }
        
        # If the name is too short, or contains any forbidden word, skip it
        if len(seg_clean) > 4 and not any(w in seg_lower for w in forbidden_words):
            cleaned_judges.append(seg_clean)
            
    return cleaned_judges

def _locate_bench_span(text: str):
    """
    Find the judge-bench block in the header. Tolerant of both [...] and (...)
    delimiters, and of OCR-mangled closing delimiters (scanned SCR volumes often
    misread "]" as a stray letter like "J"/"j", or drop it entirely).
    Returns (raw_bench_text, end_index) — end_index is where the bench block ends
    in `text`, used elsewhere (e.g. summary extraction) to know where the
    post-bench headnote/catchline starts. Returns (None, None) if nothing found.
    """
    window = text[:3000]

    # 1. Bracketed/parenthesised list of judges with an INTACT closing delimiter,
    # e.g. [Pankaj Mithal and R. Mahadevan,* JJ.] or the older
    # (B. P. SINHA, ... and K. N. WANCHOO, JJ.) form. The mandatory closing
    # delimiter anchors the lazy match, so an embedded "CJ"/"C.J." for a Chief
    # Justice listed first (e.g. "[SABYASACHI MUKHARJI, CJ AND M.M. PUNCHHI, J.]")
    # doesn't cause it to stop early — it's forced to extend to the true end.
    # Tried before the paren fallback so a modern doc's case-number parenthetical,
    # e.g. "(Civil Appeal No. 10854 of 2016)", never gets a chance to shadow it.
    for open_c, close_c in (("[", "]"), ("(", ")")):
        pattern = re.compile(
            r'\%s([^%s%s]+?(?:JJ|CJI|J\.?))\s*\%s' % (open_c, re.escape(open_c), re.escape(close_c), close_c),
            re.IGNORECASE,
        )
        m = pattern.search(window)
        if m:
            return m.group(1).strip(), m.end()

    # 2. OCR-damaged fallback: closing delimiter was misread as a stray letter
    # (e.g. "JJ.J" / "JJ.j") or dropped entirely, so no clean pass-1 match exists.
    # Scan each opening "[" / "(" and take the LAST JJ/CJI/C.J marker within a
    # capped window (last, not first, for the same Chief-Justice-first reason as
    # above). Require a comma or "and" before the marker so a plain parenthetical
    # like a case number — which has no judge list — can't be mistaken for one.
    for open_match in re.finditer(r'[\[\(]\s*(?=[A-Z])', window):
        span = window[open_match.end():open_match.end() + 400]
        span = span.split('\n\n')[0]

        last_marker = None
        for m in re.finditer(r'(?:JJ\.?|CJI\.?|C\.?J\.?)', span, re.IGNORECASE):
            last_marker = m

        if last_marker and re.search(r',|\band\b', span[:last_marker.start()], re.IGNORECASE):
            raw_bench = span[:last_marker.end()].strip()
            return raw_bench, open_match.end() + last_marker.end()

    return None, None


def _extract_bench(text: str) -> list:
    raw_bench, _ = _locate_bench_span(text)
    if raw_bench:
        cleaned = _clean_bench(raw_bench)
        if cleaned:
            return cleaned

    # Fallback to CORAM:
    coram_match = re.search(r'\bCORAM\b\s*[:\-]?\s*(.*?)(?=\n\n|\Z)', text[:3000], re.DOTALL | re.IGNORECASE)
    if coram_match:
        raw_bench = coram_match.group(1).strip()
        if len(raw_bench) < 200:
            return _clean_bench(raw_bench)

    return []

def _extract_citations(text: str) -> list:
    cites = set()
    # 1. SCC: (2024) 10 SCC 126
    scc_re = re.compile(r'\((\d{4})\)\s*(\d+)\s*S\.?C\.?C\.?\s*(\d+)', re.IGNORECASE)
    for y, v, p in scc_re.findall(text):
        cites.add(f"({y}) {v} SCC {p}")
        
    # 2. AIR: AIR 2024 SC 123
    air_re = re.compile(r'\bA\.?I\.?R\.?\s*(\d{4})\s*S\.?C\.?\s*(\d+)\b', re.IGNORECASE)
    for y, p in air_re.findall(text):
        cites.add(f"AIR {y} SC {p}")
        
    # 3. SCR: [2024] 10 SCR 126
    scr_re = re.compile(r'\[(\d{4})\]\s*(\d+)\s*S\.?C\.?R\.?\s*(\d+)', re.IGNORECASE)
    for y, v, p in scr_re.findall(text):
        cites.add(f"[{y}] {v} SCR {p}")
        
    # 4. INSC: 2024 INSC 748
    insc_re = re.compile(r'\b(\d{4})\s*INSC\s*(\d+)\b', re.IGNORECASE)
    for y, n in insc_re.findall(text):
        cites.add(f"{y} INSC {n}")
        
    return sorted(list(cites))

def _extract_acts(text: str) -> list:
    acts = set()
    act_names_pat = (
        r'(?:Indian Penal Code|IPC|I\.?P\.?C\.?|Code of Criminal Procedure|CrPC|Cr\.?P\.?C\.?|'
        r'Code of Civil Procedure|CPC|C\.?P\.?C\.?|Constitution of India|Constitution|BNS|B\.?N\.?S\.?|'
        r'Income Tax Act|GST Act|Companies Act|Arbitration.*?Act|NDPS Act|N\.?D\.?P\.?S\.?\s+Act|'
        r'Prevention of Corruption Act|P\.?C\.?\s+Act|Specific Relief Act|Evidence Act|Indian Evidence Act|'
        r'Limitation Act|Contract Act|Indian Contract Act|Motor Vehicles Act|M\.?V\.?\s+Act|'
        r'Negotiable Instruments Act|N\.?I\.?\s+Act|Industrial Disputes Act|I\.?D\.?\s+Act|'
        r'SARFAESI Act|SARFAESI|Transfer of Property Act|T\.?P\.?\s+Act)'
    )
    sec_num_pat = r'\d+[\w\(\)\-\/,]*'
    pattern = re.compile(
        rf'\b(Section|Sec\.?|Article|Art\.?|Order|Rule)\s+({sec_num_pat})\s+(?:of\s+)?(?:the\s+)?({act_names_pat})\b',
        re.IGNORECASE
    )
    for match in pattern.finditer(text):
        m_type = match.group(1).strip().title()
        m_sec = match.group(2).strip().rstrip(',')
        m_act = match.group(3).strip()
        
        m_act_clean = re.sub(r'\s+', ' ', m_act)
        m_act_clean = re.sub(r'\bI\.?P\.?C\.?\b', 'IPC', m_act_clean, flags=re.I)
        m_act_clean = re.sub(r'\bCr\.?P\.?C\.?\b|\bCode of Criminal Procedure\b', 'CrPC', m_act_clean, flags=re.I)
        m_act_clean = re.sub(r'\bC\.?P\.?C\.?\b|\bCode of Civil Procedure\b', 'CPC', m_act_clean, flags=re.I)
        m_act_clean = re.sub(r'\bN\.?I\.?\s+Act\b', 'Negotiable Instruments Act', m_act_clean, flags=re.I)
        m_act_clean = re.sub(r'\bM\.?V\.?\s+Act\b', 'Motor Vehicles Act', m_act_clean, flags=re.I)
        m_act_clean = re.sub(r'\bConstitution of India\b', 'Constitution', m_act_clean, flags=re.I)
        
        acts.add(f"{m_type} {m_sec} of {m_act_clean.strip()}")
        
    return sorted(list(acts))


# ─── Core extractor ───────────────────────────────────────────

def extract_pdf(pdf_path: str | Path = None, pdf_bytes: bytes = None, pdf_name: str = "unknown.pdf") -> dict:
    """
    Extract text + metadata from a PDF (either from file path or raw bytes).
    Returns a dict ready for DB insertion.
    """
    if pdf_path:
        pdf_path = Path(pdf_path)
        pdf_name = pdf_path.name
        key_name = str(pdf_path)
    else:
        key_name = pdf_name

    result = {
        "title": None,
        "full_text": "",
        "case_number": None,
        "case_type": None,
        "year": None,
        "judgment_date": None,
        "bench": [],
        "petitioner": None,
        "respondent": None,
        "result": None,
        "acts_cited": [],
        "cases_cited": [],
        "tags": [],
        "summary": None,
        "holding": None,
        "applicability": None,
        "content_hash": None,
        "quality_score": 0.0,
        "pdf_s3_key": key_name,
    }

    if not PYMUPDF_OK:
        logger.error("PyMuPDF not available")
        return result

    try:
        full_text = _extract_text_pymupdf(pdf_path=pdf_path, pdf_bytes=pdf_bytes, pdf_name=pdf_name)
        result["full_text"] = full_text
        result["content_hash"] = hashlib.sha256(full_text.encode()).hexdigest()
        result["quality_score"] = _quality_score(full_text)

        # Parse metadata from text
        _parse_metadata(full_text, result)

    except Exception as e:
        logger.error(f"Failed to extract {pdf_name}: {e}")

    return result


def _extract_text_pymupdf(pdf_path: Path = None, pdf_bytes: bytes = None, pdf_name: str = "") -> str:
    """Extract text page by page; fall back to OCR for image-only pages."""
    pages_text = []

    # Open from bytes or file path
    if pdf_bytes:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    else:
        doc = fitz.open(str(pdf_path))
    
    with doc:
        for page_num, page in enumerate(doc):
            text = page.get_text("text").strip()

            if len(text) < 50 and OCR_OK:
                # Scanned page — render at 150 DPI (instead of 300) for MUCH faster OCR
                pix = page.get_pixmap(dpi=150)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text = pytesseract.image_to_string(img, lang="eng")
                logger.debug(f"  OCR used on page {page_num + 1} of {pdf_name}")

            pages_text.append(text)

    raw = "\n".join(pages_text)
    return _clean_text(raw)


def _clean_text(text: str) -> str:
    """Normalise whitespace, remove boilerplate headers/footers."""
    # Remove common SC header boilerplate
    text = re.sub(r'(?i)in\s+the\s+supreme\s+court\s+of\s+india\s*', '', text)
    text = re.sub(r'(?i)not\s+reportable\s*|reportable\s*', '', text)
    # Strip page numbers (lone digits on a line)
    text = re.sub(r'\n\s*\d{1,4}\s*\n', '\n', text)
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Collapse spaces
    text = re.sub(r'[ \t]{2,}', ' ', text)
    # Fix form-feed characters
    text = re.sub(r'[\x0c\x0b]', '\n\n', text)
    return text.strip()

def _clean_party_name(name: str) -> str:
    if not name:
        return ""
    # Remove margin letter prefixes (case-insensitive, like "a c e g", "b d f h", "a b c d")
    name = re.sub(r'^(?:\b[a-zA-Z]\b\s*)+', '', name).strip()
    
    # Remove case details prefix
    name = re.sub(r'(?i)^\s*CASE\s+DETAILS\s*', '', name).strip()
    
    # Remove headers like "SUPREME COURT REPORTS", "DIGITAL SUPREME COURT REPORTS", "SUPREME COURT OF INDIA" at the start
    name = re.sub(r'(?i)^\s*(?:DIGITAL\s+)?SUPREME\s+COURT\s+(?:REPORTS|OF\s+INDIA)\s*', '', name).strip()
    
    # Remove any S.C.R. citation prefix (case-insensitive, e.g. "[2017] 12 S.C.R. 674" or "(20 I 7] 1 S.C.R. 25")
    name = re.sub(r'(?i)^.*?\bS\.?C\.?R\.?(?:\s*\d+)?\s*', '', name).strip()
    
    # Remove standard INSC citations e.g. "2024 INSC 789"
    name = re.sub(r'(?i)^\s*\d{4}\s*INSC\s*\d+\s*', '', name).strip()
    
    # Remove leading/trailing non-word characters except parentheses or quotes
    name = re.sub(r'^[^\w\(\"\'\.\s]+|[^\w\)\"\'\.\s]+$', '', name).strip()
    
    # Remove extra spaces
    name = re.sub(r'\s+', ' ', name)
    return name

def _extract_title_and_parties(text: str) -> tuple:
    # 1. Clean up first 1200 characters
    header = text[:1200]
    
    # Remove margin letters (A, B, C, D, E, F, G, H on their own lines)
    header = re.sub(r'\n\s*[A-H]\s*\n', '\n', header)
    
    # Get non-empty lines
    lines = [line.strip() for line in header.split('\n') if line.strip()]
    
    # 2. Check for "In Re" cases first - scan first 15 lines due to margin offsets
    for line in lines[:15]:
        if re.match(r'^\s*(?:IN\s+RE\b|RE\b\s*[\-:]|SUO\s+MOTU\b)', line, re.I):
            title = re.sub(r'\s+', ' ', line).strip()
            parts = re.split(r'[:\-]', title, maxsplit=1)
            petitioner = "In Re"
            respondent = _clean_party_name(parts[1]) if len(parts) > 1 else _clean_party_name(title)
            return petitioner, respondent, f"{petitioner} v. {respondent}"

    # 3. Find the Case Number/Appeal line using a generic pattern to define the title block boundary
    case_num_idx = -1
    for i, line in enumerate(lines[:18]):
        if re.search(r'\(\s*[A-Za-z\s\(\)]+\s+(?:No|Nos)\b', line, re.I):
            case_num_idx = i
            break
            
    if case_num_idx != -1:
        title_lines = lines[:case_num_idx]
    else:
        title_lines = lines[:6]
        
    vs_variants = {
        'v', 'vs', 'versus', 'v.', 'vs.', 'v.p', 'v.s', "i'.", "i'", 'v.p.', 'v.r', 'v,', 'v..', 'v.', 'i\'.', 'v.p'
    }
    
    # Clean title lines
    title_lines_cleaned = []
    for line in title_lines:
        if re.search(r'\[\d{4}\].*S\.?C\.?R', line, re.I):
            continue
        if re.match(r'^\d+$', line):
            continue
        
        # Drop single characters or very short lines (margin residue) unless it is a versus variant
        val_clean = re.sub(r'^[^\w]+|[^\w]+$', '', line).strip().lower()
        if len(line.strip()) <= 2 and val_clean not in vs_variants and line.strip() not in ("v ..", "I'.", "v.p"):
            continue
            
        title_lines_cleaned.append(line)
        
    if not title_lines_cleaned:
        return None, None, None
        
    # 4. Find the "versus" line/delimiter in the title lines
    vs_idx = -1
    for i, line in enumerate(title_lines_cleaned):
        cleaned_line = re.sub(r'^[^\w]+|[^\w]+$', '', line).strip().lower()
        if cleaned_line in vs_variants or line.strip() in ("v ..", "I'.", "v.p"):
            vs_idx = i
            break
            
    if vs_idx != -1:
        petitioner = " ".join(title_lines_cleaned[:vs_idx]).strip()
        respondent = " ".join(title_lines_cleaned[vs_idx+1:]).strip()
        petitioner = _clean_party_name(petitioner)
        respondent = _clean_party_name(respondent)
        title = f"{petitioner} v. {respondent}"
        return petitioner, respondent, title
        
    # 5. Check if "v." or "vs" is inline
    full_block = " ".join(title_lines_cleaned)
    inline_vs = re.search(r'\s+\b(?:v|vs|versus|v\.p|v\.s|I\'\.)\b\.?\s+', full_block, re.I)
    if inline_vs:
        petitioner = full_block[:inline_vs.start()].strip()
        respondent = full_block[inline_vs.end():].strip()
        petitioner = _clean_party_name(petitioner)
        respondent = _clean_party_name(respondent)
        title = f"{petitioner} v. {respondent}"
        return petitioner, respondent, title
        
    # 6. Fallback: If we have at least 2 lines, assume petitioner is first, respondent is last
    if len(title_lines_cleaned) >= 2:
        petitioner = _clean_party_name(title_lines_cleaned[0])
        respondent = _clean_party_name(title_lines_cleaned[-1])
        title = f"{petitioner} v. {respondent}"
        return petitioner, respondent, title
        
    return None, None, None
def _parse_metadata(text: str, result: dict):
    """Populate metadata fields in-place from the extracted text."""
    # Build a whitespace-collapsed copy of the first 6000 characters for robust matching
    header_raw = text[:6000]
    collapsed_header = re.sub(r'\s+', ' ', header_raw).strip()

    # Case number + type + year
    m = CASE_NUM_RE.search(collapsed_header)
    case_year = None
    if m:
        result["case_type"]   = m.group(1).strip().title()
        clean_num = re.sub(r'\s+', ' ', m.group(2).strip())
        result["case_number"] = f"{result['case_type']} No. {clean_num} of {m.group(3)}"
        case_year = int(m.group(3))
        result["year"]        = case_year

    # Judgment date
    judgment_date = None
    for pat in DATE_PATTERNS:
        dm = pat.search(collapsed_header)
        if dm:
            judgment_date = dm.group(1).strip()
            result["judgment_date"] = judgment_date
            break

    # Bench (CORAM / bracketed JJ list)
    result["bench"] = _extract_bench(text)

    # Previous cases cited
    result["cases_cited"] = _extract_citations(text)

    # Acts / statutes cited
    result["acts_cited"] = _extract_acts(text)

    # Judgment result keyword detection
    result["result"] = _detect_result(text)

    # Petitioner / Respondent / Title robust extraction
    petitioner, respondent, title = _extract_title_and_parties(text)
    if petitioner:
        result["petitioner"] = petitioner[:200]
        result["respondent"] = respondent[:200] if respondent else None
        result["title"] = title[:400] if title else None

    # Parse judgment_year from date
    judgment_year = None
    if judgment_date:
        yr_m = re.search(r'\b(\d{4})\b', judgment_date)
        if yr_m:
            judgment_year = int(yr_m.group(1))

    # Fallback to key or directory year if judgment_year is still None
    if not judgment_year:
        key_year_match = re.search(r'year=(\d{4})|/(\d{4})/', result.get("pdf_s3_key", ""))
        if key_year_match:
            year_val = key_year_match.group(1) or key_year_match.group(2)
            judgment_year = int(year_val)

    # Final fallback to case year
    if not judgment_year and case_year:
        judgment_year = case_year

    if judgment_year:
        result["year"] = judgment_year

    # Extract Summary (Robust Heuristic)
    summary = None
    # 1. Try explicit markers
    match = re.search(r'(?:Issue for consideration|HEADNOTE[S]?|Summary)\s*[:\-]?\s*(.+?)(?=\n\n|\n[A-Z][a-z]+|\Z)', header_raw, re.IGNORECASE | re.DOTALL)
    if match:
        summary_text = match.group(1).strip()
        summary_text = re.sub(r'\n\s*[A-H]\s*\n', '\n', summary_text)
        if len(summary_text) > 20:
            summary = summary_text[:2000]
            
    # 2. Try Post-Bench extraction. Reuses the same tolerant [...]/(...) bench-span
    # detection as _extract_bench (handles parens and OCR-mangled closing
    # delimiters) — a bracket-only check here used to silently skip every doc whose
    # bench uses parens or has a garbled "]", even when the catchline/headnote text
    # was sitting right after it.
    if not summary:
        _, bench_end = _locate_bench_span(header_raw)
        if bench_end is not None:
            post_bench_text = header_raw[bench_end:]
            
            stop_match = re.search(r'(?i)\b(?:Case Law Reference|CIVIL ORIGINAL JURISDICTION|CRIMINAL APPELLATE JURISDICTION|JUDGMENT|O\s*R\s*D\s*E\s*R|J\s*U\s*D\s*G\s*M\s*E\s*N\s*T|For Appellant|For Respondent)\b', post_bench_text)
            
            if stop_match:
                summary_text = post_bench_text[:stop_match.start()].strip()
                summary_text = re.sub(r'\n\s*[A-H]\s*\n', '\n', summary_text)
                summary_text = re.sub(r'\s+', ' ', summary_text).strip()
                
                if len(summary_text) > 30:
                    summary_text = re.sub(r'^(?:\b[a-zA-Z]\b\s*)+', '', summary_text).strip()
                    summary = summary_text[:2000]
                    
    result["summary"] = summary

    # Extract Tags (from Headnotes / Keywords)
    # 1. Try "List of Keywords" section first
    keywords_match = re.search(r'List of Keywords\s*\n(.*?)\nCase Arising From', text, re.IGNORECASE | re.DOTALL)
    if keywords_match:
        keywords_text = keywords_match.group(1).strip()
        result["tags"] = [re.sub(r'\s+', ' ', tag).strip() for tag in keywords_text.split(';') if tag.strip()]
    else:
        # Fallback: Extract words from Headnotes title
        headnote_match = re.search(r'Headnotes.*?\n(.*?)–', text, re.IGNORECASE)
        if headnote_match:
            tags_text = headnote_match.group(1).strip()
            result["tags"] = [re.sub(r'\s+', ' ', tag).strip() for tag in tags_text.split('–') if tag.strip()]

        # Fallback 3: pre-2010ish SCR volumes have neither "List of Keywords" nor
        # an en-dash "Headnotes" line, but do carry a short hyphen-separated
        # subject catchline right after the bench block, e.g. "Industrial,
        # Dispute-Puja Bonus-Customary and traditional payment of-Test."
        if not result["tags"]:
            _, bench_end = _locate_bench_span(text)
            if bench_end is not None:
                block = re.sub(r'\s+', ' ', text[bench_end:bench_end + 400]).strip()
                catchline_match = re.match(r'(.+?\.)\s', block)
                catchline = catchline_match.group(1) if catchline_match else None
                if catchline and 15 < len(catchline) < 250 and catchline.count('-') >= 1:
                    candidate_tags = [t.strip(' .') for t in catchline.split('-') if t.strip(' .')]
                    if 1 < len(candidate_tags) <= 8:
                        result["tags"] = candidate_tags

    # Extract Holding (Held: ... or older "Held, ..." form — the comma variant
    # appears either at the start of a line, e.g. "...in that year. \nHeld, that
    # the workmen were not entitled...", or as one clause chained by hyphens into
    # a run-on catchline, e.g. "...excepted vil-lage \"-Held, validity uf the
    # notification...". Requiring "Held" to follow a newline/hyphen/string-start
    # keeps it from matching incidental mid-sentence prose like "as held, the...".
    held_matches = re.finditer(r'(?:^|[\n\-])\s*Held\s*[:,]\s*(.*?)(?=\n[A-Z][a-z]+:|\Z)', text, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    holdings = []
    for match in held_matches:
        holding = match.group(1).strip()
        if len(holding) > 20: # ignore very short matches
            # Clean up trailing bracket like [Paras 11, 12, 14]
            holding = re.sub(r'\[Para.*?\]', '', holding).strip()
            holdings.append(holding)
    
    if holdings:
        result["holding"] = "\n\n".join(holdings)

    # Applicability is usually derived from the Bench or specific keywords
    if "Constitution Bench" in str(result["bench"]):
        result["applicability"] = "Binding on all courts in India. Applied in all cases involving constitutional questions."
    elif result["bench"]:
        result["applicability"] = f"Binding on all lower courts. Decided by {len(result['bench'])}-Judge Bench."


def _detect_result(text: str) -> str:
    # 1. Search for "Result of the Case:" structured block anywhere (usually at the end of the PDF)
    m = re.search(r'Result of the\s+Case\s*:\s*([^\n\.\xa0]+)', text, re.IGNORECASE)
    if m:
        raw_res = m.group(1).strip().lower()
        if "partly allowed" in raw_res:
            return "Partly Allowed"
        elif "allowed" in raw_res:
            return "Allowed"
        elif "dismissed" in raw_res:
            return "Dismissed"
        elif "disposed" in raw_res:
            return "Disposed"

    # 2. Fallback to search in the last 2000 characters
    last_chunk = text[-2000:]
    if re.search(r'\bpartly\s+allowed\b', last_chunk, re.I):
        return "Partly Allowed"
    if re.search(r'\bappeal\s+is\s+allowed\b|\bappeals\s+are\s+allowed\b|\bappeal\s+stands\s+allowed\b', last_chunk, re.I):
        return "Allowed"
    if re.search(r'\bappeal\s+is\s+dismissed\b|\bappeals\s+are\s+dismissed\b|\bappeal\s+stands\s+dismissed\b', last_chunk, re.I):
        return "Dismissed"
    if re.search(r'\bdisposed\s+of\b', last_chunk, re.I):
        return "Disposed"
    if re.search(r'\bwrit\s+petition\s+is\s+allowed\b|\bwrit\s+petition\s+stands\s+allowed\b', last_chunk, re.I):
        return "Allowed"
    if re.search(r'\bwrit\s+petition\s+is\s+dismissed\b|\bwrit\s+petition\s+stands\s+dismissed\b', last_chunk, re.I):
        return "Dismissed"

    return "Unknown"


def _quality_score(text: str) -> float:
    """Rough 0–1 quality score based on length and legal keyword density."""
    words = text.split()
    if len(words) < 100:
        return 0.0

    alpha_ratio = sum(c.isalpha() for c in text) / max(len(text), 1)
    legal_kw = [
        'judgment', 'appeal', 'petitioner', 'respondent', 'court',
        'section', 'order', 'bench', 'coram', 'held',
    ]
    hits = sum(1 for kw in legal_kw if kw in text.lower())
    kw_score = hits / len(legal_kw)

    return round((alpha_ratio * 0.5) + (kw_score * 0.5), 3)
