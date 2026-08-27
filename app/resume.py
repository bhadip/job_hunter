import re
from pathlib import Path

from docx import Document
from fpdf import FPDF

_PDF_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "•": "-", "…": "...", " ": " ",
}

# Tokens longer than this get zero-width break hints so fpdf2's multi_cell()
# can always find somewhere to wrap (long URLs/paths otherwise trigger
# "Not enough horizontal space to render a single character").
_MAX_TOKEN = 60
_BREAK_EVERY = 30
_ZWSP = "​"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", name)
    cleaned = re.sub(r"[\s]+", "_", cleaned.strip())
    return cleaned[:80] or "untitled"


def _split_bold(text: str):
    """Yield (chunk, is_bold) pairs for **bold** markdown inline markup."""
    parts = text.split("**")
    for i, part in enumerate(parts):
        if part:
            yield part, i % 2 == 1


def _pdf_safe(text: str) -> str:
    for src, dst in _PDF_REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "replace").decode("latin-1")


def _wrap_long_tokens(text: str) -> str:
    """Insert zero-width spaces into very long unbreakable tokens so that
    fpdf2's multi_cell() always has a legal break point."""
    out = []
    for token in text.split(" "):
        if len(token) > _MAX_TOKEN:
            token = _ZWSP.join(
                token[i:i + _BREAK_EVERY] for i in range(0, len(token), _BREAK_EVERY)
            )
        out.append(token)
    return " ".join(out)


def md_to_docx(md_text: str, path: Path):
    doc = Document()
    for line in md_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            doc.add_heading(stripped[2:], level=1)
        elif stripped.startswith(("- ", "* ")):
            para = doc.add_paragraph(style="List Bullet")
            for chunk, bold in _split_bold(stripped[2:]):
                run = para.add_run(chunk)
                run.bold = bold
        else:
            para = doc.add_paragraph()
            for chunk, bold in _split_bold(stripped):
                run = para.add_run(chunk)
                run.bold = bold
    doc.save(str(path))


def _pdf_line(pdf: FPDF, text: str, height: float):
    """Render one wrapped line, never raising. Falls back to ASCII-only, then
    to skipping the line entirely."""
    text = _wrap_long_tokens(text)
    try:
        pdf.multi_cell(0, height, _pdf_safe(text))
    except Exception:
        try:
            ascii_only = text.encode("ascii", "ignore").decode("ascii")
            pdf.multi_cell(0, height, _pdf_safe(ascii_only))
        except Exception:
            pass  # skip the line rather than kill the whole document


def md_to_pdf(md_text: str, path: Path):
    pdf = FPDF()
    pdf.set_margin(15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    for line in md_text.splitlines():
        stripped = line.strip()
        if not stripped:
            pdf.ln(4)
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            pdf.set_font("helvetica", "B", size={1: 16, 2: 13}.get(level, 11))
            _pdf_line(pdf, text, 7)
            pdf.ln(1)
        elif stripped.startswith(("- ", "* ")):
            pdf.set_font("helvetica", size=10)
            _pdf_line(pdf, "- " + stripped[2:].replace("**", ""), 5)
        else:
            pdf.set_font("helvetica", size=10)
            _pdf_line(pdf, stripped.replace("**", ""), 5)
    pdf.output(str(path))


def save_documents(resume_md: str, cover_md: str, formats: list, out_dir: Path, base_name: str) -> dict:
    """Save resume + cover letter in each requested format.

    Returns {"resume": [paths...], "cover_letter": [paths...]}.
    A failure in one format is logged and skipped so the other formats
    (and the rest of the pipeline) still succeed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"resume": [], "cover_letter": []}
    generators = {"md": None, "docx": md_to_docx, "pdf": md_to_pdf}

    for fmt in formats:
        if fmt not in generators:
            continue
        for kind, content in (("resume", resume_md), ("cover_letter", cover_md)):
            path = out_dir / f"{base_name}_{kind}.{fmt}"
            try:
                if fmt == "md":
                    path.write_text(content, encoding="utf-8")
                else:
                    generators[fmt](content, path)
                result[kind].append(str(path))
            except Exception as exc:
                print(f"[RESUME] Failed to write {path}: {exc}")
    return result
