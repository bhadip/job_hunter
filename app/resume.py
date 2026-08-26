import re
from pathlib import Path

from docx import Document
from fpdf import FPDF

_PDF_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "•": "-", "…": "...", " ": " ",
}


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


def md_to_pdf(md_text: str, path: Path):
    pdf = FPDF()
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
            pdf.multi_cell(0, 7, _pdf_safe(text))
            pdf.ln(1)
        elif stripped.startswith(("- ", "* ")):
            pdf.set_font("helvetica", size=10)
            pdf.multi_cell(0, 5, _pdf_safe("- " + stripped[2:].replace("**", "")))
        else:
            pdf.set_font("helvetica", size=10)
            pdf.multi_cell(0, 5, _pdf_safe(stripped.replace("**", "")))
    pdf.output(str(path))


def save_documents(resume_md: str, cover_md: str, formats: list, out_dir: Path, base_name: str) -> dict:
    """Save resume + cover letter in each requested format.

    Returns {"resume": [paths...], "cover_letter": [paths...]}.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"resume": [], "cover_letter": []}
    generators = {"md": None, "docx": md_to_docx, "pdf": md_to_pdf}

    for fmt in formats:
        if fmt not in generators:
            continue
        for kind, content in (("resume", resume_md), ("cover_letter", cover_md)):
            path = out_dir / f"{base_name}_{kind}.{fmt}"
            if fmt == "md":
                path.write_text(content, encoding="utf-8")
            else:
                generators[fmt](content, path)
            result[kind].append(str(path))
    return result
