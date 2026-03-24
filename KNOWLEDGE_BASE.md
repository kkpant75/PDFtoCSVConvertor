# PalantairFoundary · PDF → CSV app — knowledge base

Reference for maintaining and extending the Streamlit app (`app.py`).

## Environment

| Variable | Role |
|----------|------|
| `PALANTIR_FOLDER` | Root folder; all `*.pdf` files are discovered recursively (`rglob`). Example: `Palantir Foundry Data Validation (Sean)` trees under OneDrive. |
| `OLLAMA_URL`, `OLLAMA_MODEL` | Chat API base URL and model id (`.env` only). **API key is not stored in the repo** — users paste it in the sidebar (**Cloud extraction** → API key). |

## UI layout

- **Sidebar:** AAVA logo (`assets/aava-icon-svg.svg`), **Configured folder** path, **`st.file_uploader`** (“Browse / upload PDF”) so users can pick any PDF from the OS file dialog even when the folder list is empty or they want a file outside the tree.
- **Main:** Page title **Palantir File Conversion(PDF->CSV)** (plain `h2`, anchor link styled inert; chain SVG hidden), **`st.selectbox`** “Select PDF from Palantir folder” when the folder contains PDFs, extraction instructions, Convert, two columns (PDF iframe | CSV).
- **Chrome:** Top toolbar stays **visible** (minimal mode) so the **sidebar expand/collapse** control works; toolbar shadow is reduced via CSS. Previously hiding the whole toolbar removed access to the sidebar on narrow layouts.

## PDF header formats (Palantir validation folder)

Rent rolls and similar exports often use **multi-row headers** (pdfplumber returns separate rows). The app merges stripes **above and below** the best-scored row into **one CSV column name per column**, then normalizes spaces and newlines inside each cell.

### Typical rent-roll style (CVII / masked samples)

Observed logical columns (order left-to-right may vary by template):

| CSV-style name | Notes |
|----------------|--------|
| Unit | Sometimes `UUnit` in OCR-ish text; normalized from PDF. |
| BD/BA | Often split as `BD/` + `BA` in two rows → merged to **BD/BA**. |
| Sq. Ft. | May appear as `Sq.` + `Ft.` across rows or lines → **Sq. Ft.** |
| Tenant | |
| Status | |
| Market Rent | Two-row: `Market` / `Rent`. |
| Rent | Second “Rent” may be contract vs market depending on template. |
| Monthly Charges | |
| Lease From | Split headers like `Lease` / `From` stack in the same column. |
| Lease To | |
| Move-in / Move-out | Line breaks inside cells (`Move-in Pa` + `out`) → collapsed to spaces in `_header_cell_text`. |
| Past Due | Split `Pa` + `st Due` across rows in some PDFs. |
| NSF Count | |
| Late Count | Sometimes paired with **Last** / **Rent** / **Increase** on continuation rows (Willow-style). |

### Corporate / Yardi-style (e.g. Oak Creek Village)

- Long **title bands** (property, GL accounts, “As of” date) occupy the first rows; **not** used as the data header.
- True table headers often include: **Unit**, **BD/BA**, **Tenant**, **Status**, **Sqft**, charge buckets (**MTM Fees**, **Pet Fees**, **Utility**, **Parking/Garage**, **Total**, **Deposit**, **Lease From**, **Lease To**, **Move-in**).
- Header row index varies; scoring **rejects very sparse rows** (e.g. a single `Rent` in a wide table) so they are not chosen as the only header line.

### Newlines and spaces in headers

- Per-cell: `_header_cell_text` replaces `\r`, `\n`, `\u00a0` with spaces and collapses whitespace so CSV names stay single-line.
- Multi-row: `_merge_stacked_header_rows` walks up to **two** stripes above and below the detected header row and **concatenates** fragments per column; `_join_header_column_fragments` applies light joins such as **BD/ BA → BD/BA** and **Sq. + Ft. → Sq. Ft.**

## Extraction pipeline (structured)

1. **Per page, pick the best pdfplumber strategy** — Run lines / looser lines / `lines_strict` / text / mixed; choose the result with the **largest count of non-empty cells**.
2. **Default `extract_tables()`** if all custom strategies fail; then **layout-text fallback** (split lines on 2+ spaces).
3. **Do not pass `text_layout` / `layout` in `table_settings`** — Can break `WordExtractor` on some pdfplumber versions.
4. **Header row index** (`_find_best_header_row_index`) — Scans the first up to 8 rows; for wide tables (**w ≥ 8**), requires at least **`max(4, int(w * 0.18))`** non-empty cells so a lone label row is not chosen as the header.
5. **Stacked header merge** (`_merge_stacked_header_rows`, `_row_is_header_stripe`) — Merges consecutive header-looking rows before body data; **first body row** starts after the merged block.
6. **Blank header cells** — `_fill_blank_header_cells` uses the **first data row** when a header cell is still empty (merged columns in PDFs).
7. **Column names** — `_make_unique_column_names` enforces uniqueness for pandas.
8. **Totals** — `_is_total_row` uses whole-cell labels; deduped and appended at the end.
9. **Repeated headers** on later pages — Skipped when matching established header (`_rows_match_header`).
10. **LLM refine** — Optional; system prompt preserves totals and column names unless the user overrides.

## Cloud fallback

If structured extraction returns nothing, full-page text plus **Extraction instructions** go to the chat API. System prompt asks for document-faithful column names and order.

## Unit / UoM columns

Strict header match (`unit`, `uom`, `measure`, …, not “Unit Price”): cells refined to reduce neighbor-column bleed.

## Tuning against your PDF folder

```text
python tools/analyze_pdf_headers.py "C:\Users\Admin\Downloads\OneDrive_2026-03-24\Palantir Foundry Data Validation (Sean)"
```

Prints table width, header index, and first rows per PDF (subset of files/pages). Use it to validate `_find_best_header_row_index` and stacked merge behavior.

## Common failures

| Symptom | Likely cause | Direction |
|---------|----------------|-----------|
| `WordExtractor` unexpected keyword | `text_layout` in table settings | Remove it. |
| Duplicate pandas columns | Empty / repeated header cells | `_make_unique_column_names`. |
| `Column_1`, `Column_2` only | Wrong header row / empty merges | Header scoring, stacked merge, `_fill_blank_header_cells`. |
| Cannot pick PDF / no folder list | Sidebar collapsed with toolbar hidden | Toolbar visible again; use **Browse / upload PDF** or main **selectbox**. |
| Missing rows | Weak table strategy returned first | Best-strategy scoring by cell count. |

## Theming

- Light gray shell, white cards, indigo accent, **Inter** font; see `THEME` in `app.py` and `.streamlit/config.toml`.
