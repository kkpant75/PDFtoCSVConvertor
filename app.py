"""
Streamlit: browse PDFs from PALANTIR_FOLDER, structured table extract + optional LLM CSV, side-by-side preview.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pandas as pd
import pdfplumber
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Shown in the Extraction instructions text area; edit in-app or replace text per run.
DEFAULT_EXTRACTION_PROMPT = """\
Extract only real table/grid data into one CSV.

Consider only 1st page.

Exclude **everything above** the main table’s **column-header row** (the row that defines Unit, Tenant, Rent, etc.). Typical preamble to drop includes: document or corporate report titles, “Rent Roll” / itemized headers, long property or GL-account lines, “As of” dates, logos or narrative bands, and any other text that is **not** part of the data grid. None of that is a data row — do not output it as CSV body rows.

Ignore the first row(s) immediately **under** the column header when they are a **US property / site banner**, not tabular data. Treat as report location only (optional title line), not as data rows.

A banner row usually:
- Contains a **US mailing address** pattern: city or area + **two-letter state** + **5-digit ZIP** (e.g. `City, ST 12345`), and/or street-style tokens (St, Ave, Rd, Dr, Ln, Blvd, Ct, Way, Pl, Hwy, Route, Unit, Suite, #).
- Names a **residential or mixed-use complex** such as: apartments, apartment homes, condos / condominiums, townhomes, townhouses, villas, residence(s), village, gardens, estates, plaza, towers, lofts, senior living, student housing, multifamily, community, campus, property ID / “805-…” style prefixes, management or owner names plus address, or similar.

**US site / address lines are never CSV data rows.** If the PDF contains a **US mailing address** (pattern like `City, ST 12345`, or `ST 12345`, street number + suffix + state + ZIP, or property / building id + street line), treat it as **non-tabular** and **do not** emit it as a body row — whether it appears **immediately under** the column header, **above** the grid, or as another standalone banner. Apply to **any** US-style address, not only one template.

Do **not** treat as a banner if that row clearly belongs in the grid (e.g. unit ID + rent + status + lease dates like normal rent-roll lines, or currency amounts and tenant fields).

Include:
- Column headers once at the top, in the same left-to-right order as the main table in the PDF. Headers must be **only** the true grid column titles — never mix in the next row’s **property / US address** text (that row is metadata, not column names).
- Data rows from the table body (one row per line item / record as in the document).

Exclude (do not put in CSV unless the user explicitly asks below):
- Cover pages, titles, executive summaries, narrative paragraphs, footnotes, disclaimers.
- Repeated page headers/footers, boilerplate, and “summary only” blocks that are not part of the data grid.
- Any text the user lists under “Exclude” or “Do not include” in their own notes.

If the same total/subtotal row is repeated on every page, output that total row only once at the very end.
Always keep legitimate **Total / Grand total / Subtotal** data rows in the CSV (numeric summary rows), not narrative summaries.

Respond with only a ```csv fenced block — no other commentary."""

MAX_CSV_REFINE_CHARS = 200_000

# AAVA Launchpad / MySpace (int-ai.aava.ai) — light shell, white cards, indigo accent
THEME = {
    "brand_primary": "#4f46e5",
    "brand_primary_rgb": "79, 70, 229",
    "bg_page": "#f3f4f6",
    "bg_page_soft": "#eceff3",
    "bg_page_hint": "#f9fafb",
    "card_backdrop": "#ffffff",
    "card_inner_bg": "rgba(255, 255, 255, 0.96)",
    "border_card": "#f0f1f2",
    "sidebar_border": "#e5e7eb",
    "text": "#111827",
    "text_muted": "#6b7280",
    "shadow": "0 1px 3px rgba(15, 23, 42, 0.06), 0 2px 12px rgba(15, 23, 42, 0.04)",
}

# Only treat a row as "total" when a cell is clearly a total *label* (not "Total" inside a phrase).
_TOTAL_LABEL_CELL = re.compile(
    r"^(grand\s+total|total|totals?|sub-?total|page\s+total)\s*[:.]?\s*$",
    re.IGNORECASE,
)

def _column_is_unit_like(name: object) -> bool:
    """
    Narrow PDF columns (UoM only). Excludes headers like 'Unit Price' / 'Unit Cost'.
    """
    if name is None:
        return False
    n = str(name).strip().lower()
    if not n:
        return False
    if n in ("unit", "units", "uom", "measure"):
        return True
    if re.match(r"^u\.?\s*o\.?\s*m\.?$", n):
        return True
    if "unit of measure" in n:
        return True
    return False


def inject_theme_css() -> None:
    t = THEME
    st.markdown(
        f"""
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
        .stApp, [data-testid="stSidebar"], .stMarkdown, label, p, h1, h2, h3, span, button, textarea {{
            font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif !important;
        }}
        .stApp {{
            background: linear-gradient(180deg, {t["bg_page_hint"]} 0%, {t["bg_page"]} 45%, {t["bg_page_soft"]} 100%);
            color: {t["text"]};
        }}
        [data-testid="stSidebar"] {{
            background: #ffffff !important;
            border-right: 1px solid {t["sidebar_border"]} !important;
        }}
        [data-testid="stSidebar"] * {{
            color: {t["text"]};
        }}
        /* Keep toolbar in DOM so sidebar expand/collapse works; avoid it covering content */
        header[data-testid="stHeader"] {{
            z-index: 2;
            background: linear-gradient(180deg, {t["bg_page_hint"]} 0%, transparent 100%);
        }}
        [data-testid="stToolbar"], div.stAppToolbar {{
            box-shadow: none !important;
            border: none !important;
            background: transparent !important;
        }}
        .block-container {{
            padding-top: 1.5rem !important;
            max-width: 1400px;
            position: relative;
            z-index: 1;
        }}
        section[data-testid="stMain"] {{
            overflow-x: auto;
        }}
        h1 {{ color: {t["brand_primary"]} !important; font-weight: 700 !important; font-size: 1.75rem !important; }}
        h2, h3 {{ color: {t["text"]} !important; font-weight: 600 !important; }}
        /* Plain div title — avoids Streamlit h2+anchor (ghosting / “shadow” look) */
        .aava-page-heading {{
            display: block;
            font-size: 1.65rem !important;
            font-weight: 700 !important;
            line-height: 1.35 !important;
            margin: 0 0 1rem 0 !important;
            padding: 0 !important;
            color: #c084fc !important;
            -webkit-text-fill-color: #c084fc !important;
            text-shadow: none !important;
            filter: none !important;
            backdrop-filter: none !important;
            background: transparent !important;
            box-shadow: none !important;
            opacity: 1 !important;
            mix-blend-mode: normal !important;
            letter-spacing: -0.02em;
        }}
        .stSelectbox label, .stTextArea label, label[data-testid="stWidgetLabel"] p {{
            color: {t["text_muted"]} !important;
        }}
        .aava-panel {{
            border-radius: 12px;
            border: 1px solid var(--myspace-border, {t["border_card"]});
            background: {t["card_backdrop"]};
            box-shadow: {t["shadow"]};
            padding: 1rem 1rem 0.75rem 1rem;
            margin-bottom: 0.5rem;
        }}
        .aava-panel-inner {{
            border-radius: 12px;
            border: 1px solid {t["border_card"]};
            background: {t["card_inner_bg"]};
            overflow: hidden;
        }}
        div[data-testid="stVerticalBlock"] > div:has(> div[data-baseweb="select"]) {{
            background: {t["card_inner_bg"]};
            border-radius: 12px;
            padding: 0.5rem 0.75rem;
            border: 1px solid {t["border_card"]};
        }}
        .stTextArea textarea {{
            background-color: #ffffff !important;
            color: {t["text"]} !important;
            border: 1px solid {t["border_card"]} !important;
            border-radius: 12px !important;
        }}
        div[data-testid="stDataFrame"] {{
            border: 1px solid {t["border_card"]};
            border-radius: 12px;
            overflow: hidden;
            background: #fff;
        }}
        .stButton > button[kind="primary"] {{
            background-color: {t["brand_primary"]} !important;
            color: #ffffff !important;
            font-weight: 600;
            border: none !important;
            border-radius: 10px !important;
            box-shadow: {t["shadow"]};
        }}
        .stButton > button[kind="primary"]:hover {{
            filter: brightness(1.06);
            box-shadow: 0 4px 16px rgba({t["brand_primary_rgb"]}, 0.35);
        }}
        .stDownloadButton > button {{
            border-radius: 10px !important;
            border: 1px solid {t["border_card"]} !important;
            background: #fff !important;
            color: {t["brand_primary"]} !important;
        }}
        /* Crisp downscaled PNG previews (high-DPI source) */
        [data-testid="stImage"] img {{
            max-width: 100%;
            height: auto;
        }}
        .aava-icon-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 0.75rem; }}
        .aava-icon-row img {{ width: 56px !important; height: auto; }}
        .aava-panel-title {{ font-size: 1.05rem; font-weight: 600; color: {t["text"]}; margin: 0; }}
        div[data-testid="stExpander"] {{
            border: 1px solid {t["border_card"]};
            border-radius: 12px;
            background: {t["card_inner_bg"]};
        }}
        /* Material icon font leak in toolbar only (keep sidebar/widget icons working) */
        [data-testid="stToolbar"] span[data-testid="stIconMaterial"],
        div.stAppToolbar span[data-testid="stIconMaterial"] {{
            font-size: 0 !important;
            line-height: 0 !important;
            overflow: hidden !important;
            max-width: 1.25rem !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_icon_path() -> Path:
    """Sidebar + browser tab logo: Ascendion JPEG if present, else legacy SVG."""
    assets = Path(__file__).resolve().parent / "assets"
    jpeg = assets / "ascendion.jpeg"
    if jpeg.is_file():
        return jpeg
    return assets / "aava-icon-svg.svg"


def list_pdf_files(folder: str) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        return []
    # All PDFs under folder (nested OneDrive / project trees)
    found = [p for p in root.rglob("*.pdf") if p.is_file()]
    return sorted(found, key=lambda p: (str(p).lower(), p.name.lower()))


def _pdf_choice_label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def pdf_to_text(pdf_bytes: bytes, max_chars: int = 120_000) -> str:
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            parts.append(t)
    full = "\n\n".join(parts)
    if len(full) > max_chars:
        return full[:max_chars] + "\n\n[... truncated ...]"
    return full


def _cell_text(val: object) -> str:
    """Single-line cell text for CSV (wraps soft line breaks like the PDF column)."""
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return " ".join(lines)


def _refine_unit_like_cell(content: object) -> str:
    """
    Unit columns are narrow; extraction often pulls the rest of the row or page as 'raw' text.
    Prefer the first text line in the cell (matches top of the PDF cell), else a short leading token.
    """
    if content is None:
        return ""
    if isinstance(content, float) and pd.isna(content):
        return ""
    raw = str(content).replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.split("\n") if ln.strip()]
    if not lines:
        return ""
    first = lines[0]
    if len(first) <= 24:
        return first
    m = re.match(r"^([A-Za-z0-9°%/.\-]{1,16})\b", first)
    if m:
        return m.group(1)
    parts = first.split()
    if parts and len(parts[0]) <= 16:
        return parts[0]
    return first[:20].strip()


def sanitize_table_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize all cells; narrow unit/UoM columns only get that column's own text."""
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if _column_is_unit_like(col):
            out[col] = out[col].map(_refine_unit_like_cell)
        else:
            out[col] = out[col].map(_cell_text)
    return out


def _header_cell_text(val: object) -> str:
    """Light cleanup for header detection and column names (keep PDF wording)."""
    if val is None:
        return ""
    s = str(val).replace("\u00a0", " ").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s


# US / site-banner detection (used by header stripes, fill, and sanitization)
_STATE_ZIP_RE = re.compile(r",\s*[A-Z]{2}\s+\d{5}\b")
_STATE_ZIP_LOOSE_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}\b")
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
# Property / complex **types** and common US **street suffixes** only — no city or project names
_SITE_KEYWORDS = re.compile(
    r"\b(residence|apartments?|apartment|village|villas?|communities|properties|property|estates?|real\s+estate|"
    r"townhomes?|townhouse|condos?|condominiums?|multifamily|campus|complex|suites?|"
    r"ln\b|lane|blvd|boulevard|avenue|ave\b|dr\b|drive|way\b|ct\b|court|circle|"
    r"hwy|highway|rte|route|pkwy|parkway|pl\b|place)\b",
    re.IGNORECASE,
)
_PROPERTY_CODE_STREET_RE = re.compile(r"^\d{2,4}-\s*[A-Za-z].{6,}", re.IGNORECASE)


def _cell_looks_like_us_address_fragment(s: str) -> bool:
    """Single cell looks like part of a US property line (not a column title)."""
    t = _header_cell_text(s)
    if not t:
        return False
    if _STATE_ZIP_RE.search(t) or _STATE_ZIP_LOOSE_RE.search(t):
        return True
    if _ZIP_RE.search(t) and _SITE_KEYWORDS.search(t) and len(t) > 18:
        return True
    if re.search(r"#\s*\d+\b", t) and re.search(
        r"\b(ave|st|ln|rd|dr|blvd|way|ct|pl|hwy)\b", t, re.IGNORECASE
    ):
        return True
    if _PROPERTY_CODE_STREET_RE.search(t):
        return True
    return False


def _header_name_looks_contaminated(name: str) -> bool:
    """True if a merged/filled header string clearly contains address or site-banner debris."""
    t = _header_cell_text(name)
    if not t:
        return False
    if len(t) > 58:
        return True
    if _STATE_ZIP_RE.search(t) or _STATE_ZIP_LOOSE_RE.search(t):
        return True
    if _cell_looks_like_us_address_fragment(t):
        return True
    if _SITE_KEYWORDS.search(t) and (len(t) > 22 or "#" in t or bool(_ZIP_RE.search(t))):
        return True
    return False


def _table_width(table: list[list]) -> int:
    return max((len(r) for r in table if r), default=0)


def _nonempty_table_rows_normalized(table: list[list], w: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for r in table:
        nr = _normalize_row(r)
        if not _row_nonempty(nr):
            continue
        rows.append(_pad_row(nr, w))
    return rows


def _find_best_header_row_index(rows: list[list[str]], w: int) -> int:
    """
    Many Palantir-style PDFs use row 0 for report title and row 1+ for real column headers.
    Pick the row that looks most like a header (filled cells, short text, not mostly numeric).
    """
    if not rows:
        return 0
    max_scan = min(8, len(rows))
    best_i = 0
    best_score = float("-inf")
    for i in range(max_scan):
        r = _pad_row(rows[i], w)
        nonempty = sum(1 for c in r if (c or "").strip())
        if nonempty == 0:
            continue
        fill_ratio = nonempty / max(w, 1)
        if fill_ratio < 0.2 and w >= 5:
            continue
        if w >= 8:
            min_nonempty = max(4, int(w * 0.18))
            if nonempty < min_nonempty:
                continue
        lens = [len((c or "").strip()) for c in r if (c or "").strip()]
        avg_len = sum(lens) / len(lens) if lens else 100.0
        max_cell = max(lens) if lens else 0
        title_like = nonempty <= 2 and max_cell > 50 and fill_ratio < 0.45
        digit_cells = 0
        for c in r:
            t = (c or "").strip()
            if t and re.fullmatch(r"[\d,.\-$€£%\s]+", t):
                digit_cells += 1
        numeric_ratio = digit_cells / max(nonempty, 1)
        score = fill_ratio * 40.0 + nonempty * 1.2 - min(avg_len, 45) * 0.12
        score -= 30.0 if title_like else 0.0
        score -= numeric_ratio * 25.0
        if score > best_score:
            best_score = score
            best_i = i
    return best_i


def _join_header_column_fragments(parts: list[str]) -> str:
    """Combine vertically stacked PDF header fragments for one column (newlines already flattened in cells)."""
    cleaned = [_header_cell_text(p) for p in parts if _header_cell_text(p)]
    if not cleaned:
        return ""
    s = " ".join(cleaned)
    s = re.sub(r"([A-Za-z]{1,3}/)\s+([A-Za-z]{1,4})\b", r"\1\2", s)
    s = re.sub(r"(Sq\.?)\s+(Ft\.?)", r"\1 \2", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _row_is_header_stripe(r: list[str], w: int) -> bool:
    """True if this row is likely part of a multi-row table header (not body data)."""
    r = _pad_row(r, w)
    texts = [(c or "").strip() for c in r if (c or "").strip()]
    if not texts:
        return False
    if any(len(t) > 85 for t in texts):
        return False
    if len(texts) <= 3 and any(len(t) > 55 for t in texts):
        return False
    digit_cells = sum(1 for t in texts if re.fullmatch(r"[\d,.\-$€£%\s]+", t))
    if digit_cells / len(texts) > 0.52:
        return False
    first = texts[0]
    if len(texts) >= 4 and re.match(r"^[A-Za-z]?\d{2,}[A-Za-z]?$", first):
        if digit_cells >= max(3, len(texts) // 3):
            return False
    blob = " ".join(texts)
    # US "City, ST 12345" or "ST 12345" — never a header stripe
    if _STATE_ZIP_RE.search(blob) or _STATE_ZIP_LOOSE_RE.search(blob):
        return False
    if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\s*,\s*[A-Z]{2}\s+\d{5}\b", blob):
        return False
    # Property / site banner rows must not merge into column names
    if _row_is_property_or_site_banner(r, w):
        return False
    return True


def _merge_stacked_header_rows(rows: list[list[str]], hi: int, w: int) -> tuple[list[str], int]:
    """
    Merge consecutive header stripes above/below the scored header row into one name per column.
    Returns (merged_header_cells, first_body_row_index).
    """
    if not rows:
        return _pad_row([], w), 0
    lo = hi
    for _ in range(2):
        if lo <= 0:
            break
        if _row_is_header_stripe(rows[lo - 1], w):
            lo -= 1
        else:
            break
    hi2 = hi
    for _ in range(2):
        if hi2 + 1 >= len(rows):
            break
        if _row_is_header_stripe(rows[hi2 + 1], w):
            hi2 += 1
        else:
            break
    merged: list[str] = []
    for j in range(w):
        parts: list[str] = []
        for ridx in range(lo, hi2 + 1):
            row = rows[ridx]
            cell = row[j] if j < len(row) else ""
            if cell is not None and str(cell).strip():
                parts.append(str(cell))
        merged.append(_join_header_column_fragments(parts))
    return merged, hi2 + 1


def _fill_blank_header_cells(names: list[str], sample_data_row: list[str] | None) -> list[str]:
    """Use first data row for missing header labels when PDF merged cells leave blanks."""
    out: list[str] = []
    for i, n in enumerate(names):
        t = _header_cell_text(n)
        if t and not _header_name_looks_contaminated(t):
            out.append(t)
            continue
        if sample_data_row and i < len(sample_data_row):
            st = _header_cell_text(sample_data_row[i])
            if (
                st
                and len(st) < 80
                and not re.fullmatch(r"[\d,.\-$€£%\s]+", st)
                and not _cell_looks_like_us_address_fragment(st)
            ):
                out.append(st)
                continue
        out.append(f"Column_{i + 1}")
    return out


def _first_non_banner_body_index(body: list[list[str]], w: int) -> int:
    """Skip site/address rows so we never use them to fill or sanitize real headers."""
    i = 0
    while i < len(body) and _row_is_property_or_site_banner(body[i], w):
        i += 1
    return i


def _sanitize_table_header_names(
    names: list[str], sample_row: list[str] | None, ncols: int
) -> list[str]:
    """Replace header cells that picked up address/banner text with clean sample or Column_N."""
    out: list[str] = []
    for i, raw in enumerate(names):
        t = _header_cell_text(raw)
        if not _header_name_looks_contaminated(t):
            out.append(t if t else f"Column_{i + 1}")
            continue
        fix = ""
        if sample_row and i < len(sample_row):
            fix = _header_cell_text(sample_row[i])
        if (
            fix
            and not _header_name_looks_contaminated(fix)
            and not _cell_looks_like_us_address_fragment(fix)
            and len(fix) < 48
        ):
            out.append(fix)
        else:
            out.append(f"Column_{i + 1}")
    return out


def _make_unique_column_names(raw: list[str]) -> list[str]:
    """
    PDF tables often yield blank or repeated header cells; pandas requires unique, non-empty names.
    """
    base: list[str] = []
    for i, name in enumerate(raw):
        s = _header_cell_text(str(name) if name is not None else "")
        if not s:
            s = f"Column_{i + 1}"
        base.append(s)
    counts: dict[str, int] = {}
    out: list[str] = []
    for s in base:
        n = counts.get(s, 0)
        counts[s] = n + 1
        out.append(s if n == 0 else f"{s}_{n + 1}")
    return out


def _normalize_row(row: list) -> list[str]:
    return [_cell_text(c if c is not None else "") for c in row]


def _row_nonempty(row: list[str]) -> bool:
    return any(c.strip() for c in row)


def _pad_row(row: list[str], n: int) -> list[str]:
    r = list(row) + [""] * n
    return r[:n]


def _rows_match_header(a: list[str], b: list[str]) -> bool:
    if len(a) != len(b):
        return False
    return all((x or "").strip().lower() == (y or "").strip().lower() for x, y in zip(a, b))


def _is_total_row(row: list[str]) -> bool:
    if not row:
        return False
    for c in row:
        s = (c or "").strip()
        if not s:
            continue
        compact = re.sub(r"\s+", " ", s)
        if _TOTAL_LABEL_CELL.match(s) or _TOTAL_LABEL_CELL.match(compact):
            return True
    return False


def _row_join_non_empty_cells(row: list[str], ncols: int) -> str:
    r = _pad_row(row, ncols)
    parts = [(c or "").strip() for c in r if (c or "").strip()]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _row_blob_looks_like_tabular_data_not_address(blob: str) -> bool:
    """True when joined row text looks like a rent-roll data row, not a property/address banner."""
    if re.search(r"\d{1,3},\d{3}\.\d{2}", blob):
        return True
    if re.search(r"\b(?:current|notice|vacant|eviction|past\s+due)\b", blob, re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,3}\.\d+%\b", blob):
        return True
    return False


def _row_is_property_or_site_banner(row: list[str], ncols: int) -> bool:
    """
    Rent rolls often place the site name / address in the first row after column headers.
    That row should become CSV preamble, not a data row.
    PDFs split one address across many cells — do not require low column fill when City, ST ZIP is present.
    """
    r = _pad_row(row, ncols)
    blob = _row_join_non_empty_cells(r, ncols)
    if len(blob) < 22:
        return False
    nonempty = sum(1 for c in r if (c or "").strip())
    fill = nonempty / max(ncols, 1)
    # Strong signal: US mailing line (City, ST ZIP) — banner even if high fill from cell splits
    if _STATE_ZIP_RE.search(blob) and len(blob) >= 28:
        if not _row_blob_looks_like_tabular_data_not_address(blob):
            return True
    if _ZIP_RE.search(blob) and _SITE_KEYWORDS.search(blob) and len(blob) >= 35:
        if fill <= 0.72 and not _row_blob_looks_like_tabular_data_not_address(blob):
            return True
    if _SITE_KEYWORDS.search(blob) and len(blob) >= 42 and fill <= 0.58:
        if not _row_blob_looks_like_tabular_data_not_address(blob):
            return True
    if len(blob) >= 55 and fill <= 0.28 and nonempty <= max(6, ncols // 3):
        return True
    return False


def _pop_leading_report_banners(rows: list[list[str]], ncols: int) -> tuple[list[list[str]], str]:
    titles: list[str] = []
    while rows and _row_is_property_or_site_banner(rows[0], ncols):
        titles.append(_row_join_non_empty_cells(rows[0], ncols))
        rows.pop(0)
    title = re.sub(r"\s+", " ", " ".join(titles)).strip()
    return rows, title


def _merge_date_cells(top: str, bottom: str) -> str:
    """Join mm/dd/ + newline row year (e.g. 04/01/ + 2019 -> 04/01/2019)."""
    ta = (top or "").strip()
    tb = (bottom or "").strip()
    if not tb:
        return ta
    if not ta:
        return tb
    if re.fullmatch(r"\d{4}", tb) and re.search(r"\d{1,2}/\d{1,2}/\s*$", ta):
        return ta.rstrip() + tb
    if re.fullmatch(r"\d{4}", tb) and re.search(r"\d{1,2}/\d{1,2}\s*$", ta) and "/" in ta and not ta.endswith("/"):
        return ta.rstrip() + "/" + tb
    return ta


def _merge_row_pair_date_continuation(top: list[str], bottom: list[str], ncols: int) -> list[str]:
    top, bottom = _pad_row(top, ncols), _pad_row(bottom, ncols)
    out: list[str] = []
    for j in range(ncols):
        ta, tb = (top[j] or "").strip(), (bottom[j] or "").strip()
        merged = _merge_date_cells(ta, tb)
        if merged != ta:
            out.append(merged)
        elif not ta and tb:
            out.append(tb)
        else:
            out.append(ta)
    return out


def _row_looks_like_date_continuation(bottom: list[str], top: list[str], ncols: int) -> bool:
    if _is_total_row(bottom):
        return False
    top, bottom = _pad_row(top, ncols), _pad_row(bottom, ncols)
    date_merges = 0
    other_nonempty = 0
    for j in range(ncols):
        ta, tb = (top[j] or "").strip(), (bottom[j] or "").strip()
        if not tb:
            continue
        if _merge_date_cells(ta, tb) != ta:
            date_merges += 1
        else:
            other_nonempty += 1
    if date_merges == 0:
        return False
    if other_nonempty >= 3:
        return False
    nonempty_bottom = sum(1 for c in bottom if (c or "").strip())
    if date_merges >= 2:
        return True
    if date_merges == 1 and nonempty_bottom <= 4:
        return True
    return False


def _merge_split_date_rows(rows: list[list[str]], ncols: int) -> None:
    """In-place: merge PDF rows where date month/day and year landed on separate lines."""
    i = 0
    while i < len(rows) - 1:
        if _row_looks_like_date_continuation(rows[i + 1], rows[i], ncols):
            rows[i] = _merge_row_pair_date_continuation(rows[i], rows[i + 1], ncols)
            del rows[i + 1]
            continue
        i += 1


def csv_with_report_title_line(df: pd.DataFrame, title: str) -> str:
    """First CSV line = report location; then standard header + rows (UTF-8)."""
    body = df.to_csv(index=False)
    t = (title or "").strip()
    if not t:
        return body
    esc = t.replace('"', '""')
    return f'Report location,"{esc}"\n{body}'


def _fallback_tables_from_page(page: pdfplumber.page.Page) -> list[list[list[str]]]:
    """Borderless / scanned-style PDFs: infer columns from spaced layout text."""
    try:
        raw = page.extract_text(layout=True)
    except (TypeError, ValueError, Exception):
        raw = None
    if not raw or not str(raw).strip():
        try:
            raw = page.extract_text()
        except (TypeError, ValueError, Exception):
            raw = None
    if not raw:
        return []
    rows: list[list[str]] = []
    for ln in str(raw).split("\n"):
        if not ln.strip():
            continue
        parts = [p.strip() for p in re.split(r"\s{2,}", ln) if p.strip()]
        if len(parts) >= 2:
            rows.append(parts)
    if len(rows) < 2:
        return []
    widths = [len(r) for r in rows]
    mode_w = max(set(widths), key=widths.count)
    if mode_w < 2:
        return []
    norm = [r + [""] * (mode_w - len(r)) for r in rows if len(r) <= mode_w]
    norm = [r[:mode_w] for r in norm if len(r) == mode_w]
    if len(norm) < 2:
        return []
    return [norm]


def _table_extraction_score(tables: list) -> int:
    score = 0
    for table in tables or []:
        for row in table or []:
            for cell in row or []:
                if cell is not None and str(cell).strip():
                    score += 1
    return score


def _extract_tables_on_page(page: pdfplumber.page.Page) -> list[list[list[str]]]:
    """
    Run every pdfplumber strategy and keep the result with the most non-empty cells
    (avoids returning a sparse partial table and missing rows).
    """
    strategies: tuple[dict, ...] = (
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 4,
            "snap_tolerance": 2,
            "join_tolerance": 2,
            "text_x_tolerance": 2,
            "text_y_tolerance": 2,
        },
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 6,
            "text_x_tolerance": 3,
            "text_y_tolerance": 3,
        },
        {
            "vertical_strategy": "lines_strict",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 4,
            "text_x_tolerance": 3,
            "text_y_tolerance": 3,
        },
        {"vertical_strategy": "text", "horizontal_strategy": "text"},
        {"vertical_strategy": "lines", "horizontal_strategy": "text"},
        {"vertical_strategy": "text", "horizontal_strategy": "lines"},
    )
    best: list = []
    best_score = -1
    for settings in strategies:
        try:
            tables = page.extract_tables(table_settings=settings) or []
        except (TypeError, ValueError, Exception):
            continue
        sc = _table_extraction_score(tables)
        if sc > best_score:
            best_score = sc
            best = tables
    if best:
        return best
    try:
        tables = page.extract_tables() or []
        if tables:
            return tables
    except (TypeError, ValueError, Exception):
        pass
    fb = _fallback_tables_from_page(page)
    return fb if fb else []


def extract_pdf_tables_to_dataframe(pdf_bytes: bytes) -> tuple[pd.DataFrame | None, str]:
    """
    Merge tables across pages; detect real header row; preserve PDF column names; totals at end.
    Returns (dataframe, report_location_title for first CSV line) or (None, "").
    """
    header_raw: list[str] | None = None  # logical headers (before uniquify) for repeat detection
    ncols = 0
    detail_rows: list[list[str]] = []
    total_rows: list[list[str]] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in _extract_tables_on_page(page):
                w = _table_width(table)
                if w < 2:
                    continue
                raw_rows = _nonempty_table_rows_normalized(table, w)
                if not raw_rows:
                    continue

                if header_raw is None:
                    hi = _find_best_header_row_index(raw_rows, w)
                    hdr_cells, body_start = _merge_stacked_header_rows(raw_rows, hi, w)
                    body = raw_rows[body_start:]
                    sample_i = _first_non_banner_body_index(body, w)
                    sample = body[sample_i] if sample_i < len(body) else None
                    hdr = [_header_cell_text(c) for c in _pad_row(hdr_cells, w)]
                    hdr = _fill_blank_header_cells(hdr, sample)
                    hdr = _sanitize_table_header_names(hdr, sample, w)
                    header_raw = hdr
                    ncols = w
                else:
                    if _rows_match_header(
                        [_header_cell_text(c) for c in raw_rows[0]],
                        [_header_cell_text(c) for c in _pad_row(header_raw, ncols)],
                    ):
                        body = raw_rows[1:]
                    else:
                        body = raw_rows

                for row in body:
                    row = _pad_row(row, ncols)
                    if _is_total_row(row):
                        total_rows.append(row)
                    else:
                        detail_rows.append(row)

    if not header_raw or ncols == 0:
        return None, ""

    detail_rows, report_title = _pop_leading_report_banners(detail_rows, ncols)
    _merge_split_date_rows(detail_rows, ncols)

    seen_total: set[tuple[str, ...]] = set()
    unique_totals: list[list[str]] = []
    for r in total_rows:
        key = tuple(r)
        if key not in seen_total:
            seen_total.add(key)
            unique_totals.append(r)

    all_rows = detail_rows + unique_totals
    if not all_rows:
        return None, report_title
    col_names = _make_unique_column_names([str(h) for h in _pad_row(header_raw, ncols)])
    df = pd.DataFrame(all_rows, columns=col_names)
    return sanitize_table_dataframe(df), report_title


def strip_code_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:csv)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def parse_csv_from_llm(raw: str) -> pd.DataFrame:
    cleaned = strip_code_fences(raw)
    df = pd.read_csv(io.StringIO(cleaned))
    df.columns = pd.Index(_make_unique_column_names([str(c) for c in df.columns]))
    return sanitize_table_dataframe(df)


def call_llm_chat(user_content: str, system: str, api_key: str) -> str:
    url = os.getenv("OLLAMA_URL", "").strip()
    key = (api_key or "").strip()
    model = os.getenv("OLLAMA_MODEL", "").strip()
    if not url or not key:
        raise RuntimeError("Conversion service URL or API key is missing. Set URL/model in .env and paste the API key in the sidebar.")
    if not model:
        raise RuntimeError("OLLAMA_MODEL is not set in .env.")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=300)
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Unexpected API response: {data}")
    return (choices[0].get("message") or {}).get("content") or ""


def _llm_available(api_key: str) -> bool:
    return bool(os.getenv("OLLAMA_URL", "").strip() and (api_key or "").strip())


def refine_dataframe_per_user_instructions(
    df: pd.DataFrame, extract_prompt: str, api_key: str
) -> pd.DataFrame:
    """
    After pdfplumber table extraction, apply on-screen instructions via the cloud model:
    drop summaries, narrative in cells, and other non-tabular noise the user excludes.
    """
    if df is None or df.empty or not _llm_available(api_key):
        return df
    csv_in = df.to_csv(index=False)
    if len(csv_in) > MAX_CSV_REFINE_CHARS:
        csv_in = (
            csv_in[:MAX_CSV_REFINE_CHARS]
            + "\n# ... [CSV truncated for size; apply the same inclusion/exclusion rules to the full table.]\n"
        )
    system = (
        "You clean CSV that was extracted from PDF table cells. The USER INSTRUCTIONS are authoritative. "
        "Remove rows that are cover blurbs, section titles (non-header), long narrative in cells, footnotes, "
        "or anything the user says to omit. "
        "Never remove numeric data rows or rows whose first column is Total / Grand total / Subtotal unless the user explicitly asks to drop totals. "
        "Keep all body rows; keep total/subtotal summary rows at the end. "
        "Keep a single header row when possible; preserve column order and names unless the user says otherwise. "
        "Output exactly one ```csv code block. No explanation outside the block."
    )
    user_msg = f"USER INSTRUCTIONS (follow strictly):\n{extract_prompt}\n\n---\nINPUT CSV:\n{csv_in}"
    try:
        raw = call_llm_chat(user_msg, system, api_key)
        out = parse_csv_from_llm(raw)
        if out is not None and not out.empty:
            return out
    except Exception:
        pass
    return df


CLOUD_EXTRACT_SYSTEM = (
    "You output CSV only from PDF text. Follow USER INSTRUCTIONS strictly for what to include and exclude. "
    "Omit narrative summaries, cover blurbs, footnotes, and non-table boilerplate unless the user wants them. "
    "Extract all tabular rows: one header row then every data row; preserve column order and names from the document. "
    "Include Total / Grand total / Subtotal rows; if the same total row repeats each page, include it once at the end. "
    "Single ```csv ... ``` block only; no commentary."
)


def pdf_first_page_png_bytes(pdf_bytes: bytes, resolution: int = 200) -> tuple[bytes | None, str | None]:
    """
    Rasterize page 1 with pdfplumber/pypdfium2 — higher resolution = sharper on screen when scaled.
    """
    last_err: str | None = None
    for res in (resolution, 120):
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                if not pdf.pages:
                    return None, "PDF has no pages"
                page_img = pdf.pages[0].to_image(resolution=res, antialias=True)
                buf = io.BytesIO()
                page_img.original.save(buf, format="PNG", compress_level=2)
                return buf.getvalue(), None
        except Exception as e:
            last_err = str(e)
            continue
    return None, last_err


def _panel_container():
    """Bordered container on Streamlit ≥1.33; plain container on older versions."""
    try:
        return st.container(border=True)
    except TypeError:
        return st.container()


def render_pdf_preview_panel(pdf_bytes: bytes, download_stem: str) -> None:
    """
    Show page 1 as PNG via st.image (works locally and on Streamlit Cloud).
    Do not wrap this in raw HTML <div> blocks — Streamlit widgets are not nested inside st.markdown HTML.
    """
    st.download_button(
        "Download PDF",
        data=pdf_bytes,
        file_name=f"{download_stem}.pdf",
        mime="application/pdf",
        key="download_original_pdf",
    )
    st.caption(
        "Preview: **page 1** at high resolution. Download the PDF for all pages and native zoom."
    )
    png, err = pdf_first_page_png_bytes(pdf_bytes)
    if png:
        st.image(png, use_container_width=True, output_format="PNG")
    else:
        st.warning("Could not render a page preview. Use **Download PDF**.")
        if err:
            with st.expander("Why preview failed"):
                st.code(err, language="text")


def main() -> None:
    icon = get_icon_path()
    page_icon = str(icon) if icon.exists() else "📄"
    st.set_page_config(
        page_title="AAVA · PDF Data Extraction",
        page_icon=page_icon,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_theme_css()

    palantir = os.getenv("PALANTIR_FOLDER", "").strip().strip('"').strip("'")
    if not palantir:
        st.error("Set **PALANTIR_FOLDER** in your `.env` file.")
        st.stop()

    palantir_path = Path(palantir)
    pdfs = list_pdf_files(str(palantir_path)) if palantir_path.is_dir() else []
    labels = [_pdf_choice_label(p, palantir_path) for p in pdfs]

    with st.sidebar:
        if icon.exists():
            st.image(str(icon), use_container_width=True)
        st.caption(
            "PDFs in **another folder**? On the main page use **Select PDF from any folder** — "
            "it opens your system file browser so you can pick any PDF on this computer."
        )
        if pdfs:
            st.caption(f"{len(pdfs)} PDF(s) available from the list on the main page.")
        st.divider()
        st.caption("Cloud extraction")
        st.text_input(
            "API key",
            type="password",
            help="Paste your chat API bearer token here. It is kept in this browser session only and is not read from .env.",
            placeholder="Paste API bearer token",
            key="extraction_api_key",
            autocomplete="new-password",
        )

    api_key = (st.session_state.get("extraction_api_key") or "").strip()

    st.markdown(
        '<div class="aava-page-heading">PDF Data Extraction</div>',
        unsafe_allow_html=True,
    )

    uploaded_pdf = st.file_uploader(
        "Select PDF from any folder",
        type=["pdf"],
        help="Opens your computer’s file dialog (Browse / Open). Navigate to any drive or folder and select a PDF. When set, this overrides the Palantir folder list.",
        key="pdf_upload",
    )
    use_upload = uploaded_pdf is not None

    if use_upload:
        pdf_bytes = uploaded_pdf.getvalue()
        choice = uploaded_pdf.name
        selected = Path(uploaded_pdf.name)
        pdf_caption = uploaded_pdf.name
        download_stem = selected.stem
    elif pdfs:
        choice = st.selectbox(
            "Or select PDF from the configured folder",
            options=labels,
            index=0,
            key="pdf_pick",
        )
        selected = pdfs[labels.index(choice)]
        pdf_bytes = selected.read_bytes()
        pdf_caption = str(selected)
        download_stem = selected.stem
    else:
        st.info(
            f"No PDFs found under the configured folder:\n`{palantir}`\n\n"
            "Use **Select PDF from any folder** above — click **Browse files** to open the dialog and go to any folder."
        )
        st.stop()

    st.session_state.setdefault("csv_df", None)
    st.session_state.setdefault("csv_raw", "")
    st.session_state.setdefault("csv_title", "")
    st.session_state.setdefault("last_pdf", "")
    if st.session_state.get("last_pdf") and st.session_state["last_pdf"] != choice:
        st.session_state["csv_df"] = None
        st.session_state["csv_raw"] = ""
        st.session_state["csv_title"] = ""

    extract_prompt = st.text_area(
        "Extraction instructions",
        value=DEFAULT_EXTRACTION_PROMPT,
        height=160,
        key="extract_prompt",
    )

    if st.button("Convert PDF → CSV", type="primary"):
        with st.spinner("Scanning PDF tables and building CSV…"):
            df_struct, report_title = extract_pdf_tables_to_dataframe(pdf_bytes)
            if df_struct is not None and not df_struct.empty:
                st.session_state["csv_title"] = report_title or ""
                if _llm_available(api_key):
                    with st.spinner("Applying your extraction instructions…"):
                        df_struct = refine_dataframe_per_user_instructions(
                            df_struct, extract_prompt, api_key
                        )
                st.session_state["csv_df"] = df_struct
                st.session_state["csv_raw"] = ""
                st.session_state["last_pdf"] = choice
                st.success("CSV ready.")
            else:
                st.session_state["csv_title"] = ""
                with st.spinner("Running extraction on document text…"):
                    if not _llm_available(api_key):
                        st.error(
                            "Structured extract found no table. Cloud extraction needs an **API key** "
                            "(sidebar → Cloud extraction) and **OLLAMA_URL** in `.env`."
                        )
                        st.stop()
                    text = pdf_to_text(pdf_bytes)
                    user_msg = f"USER INSTRUCTIONS:\n{extract_prompt}\n\n---\nDocument text:\n{text}"
                    try:
                        raw = call_llm_chat(user_msg, CLOUD_EXTRACT_SYSTEM, api_key)
                    except Exception as e:
                        st.error(f"Extraction service error: {e}")
                        st.stop()
                    try:
                        df = parse_csv_from_llm(raw)
                    except Exception as e:
                        st.error(f"Could not parse CSV from model output: {e}")
                        st.code(raw[:8000] if len(raw) > 8000 else raw)
                        st.stop()
                    st.session_state["csv_df"] = df
                    st.session_state["csv_raw"] = raw
                    st.session_state["last_pdf"] = choice
                    st.session_state["csv_title"] = ""

    left, right = st.columns(2, gap="medium")
    with left:
        with _panel_container():
            st.markdown('<p class="aava-panel-title">Original PDF</p>', unsafe_allow_html=True)
            render_pdf_preview_panel(pdf_bytes, download_stem=download_stem)
            st.caption(pdf_caption)

    with right:
        with _panel_container():
            st.markdown('<p class="aava-panel-title">Extracted CSV</p>', unsafe_allow_html=True)
            df = st.session_state.get("csv_df")
            if df is not None and isinstance(df, pd.DataFrame) and not df.empty:
                title = (st.session_state.get("csv_title") or "").strip()
                if title:
                    st.caption(f"Report location (CSV line 1): {title}")
                st.dataframe(df, use_container_width=True, height=560)
                csv_out = csv_with_report_title_line(df, title)
                st.download_button(
                    "Download CSV",
                    data=csv_out.encode("utf-8"),
                    file_name=download_stem + "_extracted.csv",
                    mime="text/csv",
                )
            else:
                st.info("Run Convert PDF → CSV to populate this panel.")


if __name__ == "__main__":
    main()
