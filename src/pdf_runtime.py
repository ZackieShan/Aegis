"""Small helpers for optional PDF runtime dependencies."""

PDF_VIEWER_PYMUPDF_MISSING = (
    "PDF viewer requires PyMuPDF. Reinstall dependencies with "
    "`pip install -r requirements.txt` (PyMuPDF is a core dependency; "
    "AGPL-3.0, same as Aegis)."
)


def load_pymupdf_for_pdf_viewer():
    """Return the PyMuPDF module, or raise a user-facing setup hint."""
    try:
        import fitz  # PyMuPDF, optional
    except ImportError as exc:
        raise RuntimeError(PDF_VIEWER_PYMUPDF_MISSING) from exc
    return fitz
