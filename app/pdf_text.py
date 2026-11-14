from io import BytesIO

from pypdf import PdfReader

MAX_PAGES = 12
MAX_CHARS = 18_000


def extract_resume_text(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("That PDF is encrypted. Export an unlocked copy and try again.")
    parts: list[str] = []
    for page in reader.pages[:MAX_PAGES]:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts)
    text = " ".join(text.split())
    if len(text) < 40:
        raise ValueError(
            "No readable text came out of that PDF. If it is a scan, export a text-based resume."
        )
    return text[:MAX_CHARS]
