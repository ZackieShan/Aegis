#!/usr/bin/env python3
"""Package a folder of PDFs as a Kiwix collection (ZIM) for the offline library.

    venv/bin/python scripts/library/build-manuals-zim.py                 # ~/library/manuals
    venv/bin/python scripts/library/build-manuals-zim.py --source ~/pdfs --name my-guides_en_all \
        --title "My field guides"

Why a ZIM: everything kiwix-serve serves is browsable from a phone and is
indexed page by page by `aegis-library index`, so wrapping loose PDFs keeps one
code path and one link scheme for all manuals.

Layout expected under --source:

    <category>/<file>.pdf      first-level folders become sections of the index page
    sources.tsv                optional; columns category, filename, title, publisher,
                               year (extra columns are ignored) give the index page
                               proper titles instead of file names

Each PDF is screened before packing: a file that is not a PDF, or whose first
pages carry a restricted-distribution statement (FOUO, "Distribution authorized
to U.S. Government agencies…") without "approved for public release", is left
out and listed. Then: zimwriterfs → <library>/zim/<name>_<YYYY-MM>.zim (older
editions of the same name are removed), rebuild-library.sh, and the page index.
"""

from __future__ import annotations

import argparse
import csv
import json
import datetime
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESTRICTED = re.compile(
    r"for official use only|\bFOUO\b|distribution (is )?authorized to (the )?(u\.?\s?s\.?|dod|department of defense)"
    r"|distribution statement [B-F]\b|destruction notice|export[- ]controlled|controlled unclassified",
    re.I,
)
PUBLIC = re.compile(r"approved for public release|distribution (is )?unlimited", re.I)


def library_dir() -> Path:
    return Path(os.environ.get("AEGIS_LIBRARY_DIR") or "~/library").expanduser()


def screen(pdf: Path) -> str:
    """Return '' when the file is fine to pack, else the reason it is not."""
    try:
        with open(pdf, "rb") as handle:
            if b"%PDF" not in handle.read(1024):
                return "not a PDF"
    except OSError as exc:
        return f"unreadable: {exc}"
    try:
        import fitz  # PyMuPDF

        fitz.TOOLS.mupdf_display_errors(False)
        with fitz.open(pdf) as doc:
            if doc.needs_pass:
                return "encrypted"
            text = " ".join(doc[i].get_text("text") for i in range(min(4, doc.page_count)))
    except Exception as exc:
        return f"unreadable PDF: {type(exc).__name__}"
    text = re.sub(r"\s+", " ", text)
    hit = RESTRICTED.search(text)
    if hit and not PUBLIC.search(text):
        return f"restricted-distribution marking: “{hit.group(0)}”"
    return ""


def pretty(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\.pdf$", "", name, flags=re.I).replace("_", " ")).strip()


def read_sources(source: Path) -> dict:
    table = {}
    path = source / "sources.tsv"
    if not path.exists():
        return table
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename((row.get("filename") or "").strip()))
            if name and not name.lower().endswith(".pdf"):
                name += ".pdf"
            table[((row.get("category") or "").strip(), name[:150])] = row
    return table


def write_icon(path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (48, 48), (38, 70, 58))
    draw = ImageDraw.Draw(image)
    draw.rectangle((9, 7, 38, 41), fill=(236, 232, 214))          # a manual…
    draw.rectangle((9, 7, 14, 41), fill=(176, 142, 76))           # …with a spine
    for y in (16, 22, 28, 34):
        draw.line((19, y, 33, y), fill=(70, 70, 60), width=1)
    image.save(path, format="PNG")


def build(args) -> int:
    source = Path(args.source).expanduser()
    if not source.is_dir():
        sys.stderr.write(f"error: no folder at {source}\n")
        return 2
    for tool in ("zimwriterfs", "kiwix-manage"):
        if not shutil.which(tool):
            sys.stderr.write(f"error: {tool} not found (packages zim-tools, kiwix-tools)\n")
            return 2

    known = read_sources(source)
    sections: dict = {}
    left_out = []
    for pdf in sorted(source.rglob("*.pdf")):
        relative = pdf.relative_to(source)
        category = relative.parts[0] if len(relative.parts) > 1 else "general"
        reason = screen(pdf)
        if reason:
            left_out.append((str(relative), reason))
            continue
        info = known.get((category, pdf.name), {})
        sections.setdefault(category, []).append({
            "path": pdf,
            "href": f"{category}/{pdf.name}",
            "title": (info.get("title") or "").strip() or pretty(pdf.name),
            "publisher": (info.get("publisher") or "").strip(),
            "year": (info.get("year") or "").strip(),
            "size_mb": pdf.stat().st_size / 1e6,
        })
    total = sum(len(items) for items in sections.values())
    if not total:
        sys.stderr.write("error: no usable PDFs found\n")
        return 2

    stamp = datetime.date.today().strftime("%Y-%m")
    zim_dir = library_dir() / "zim"
    zim_dir.mkdir(parents=True, exist_ok=True)
    target = zim_dir / f"{args.name}_{stamp}.zim"

    with tempfile.TemporaryDirectory(prefix="manuals-zim-", dir=str(library_dir())) as staging:
        stage = Path(staging)
        for category, items in sections.items():
            (stage / category).mkdir(parents=True, exist_ok=True)
            for item in items:
                destination = stage / item["href"]
                try:
                    os.link(item["path"], destination)             # same disk: no copy
                except OSError:
                    shutil.copyfile(item["path"], destination)
        write_icon(stage / "icon.png")

        parts = [
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
            f"<title>{html.escape(args.title)}</title>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;"
            "color:#222;background:#fbfaf6}h1{font-size:1.5em}h2{margin-top:1.6em;border-bottom:1px solid #ccc;"
            "text-transform:capitalize}li{margin:.35em 0}small{color:#666}nav a{margin-right:.8em;"
            "text-transform:capitalize}@media (prefers-color-scheme:dark){body{background:#1b1d1c;color:#ddd}"
            "a{color:#8ec5a4}small{color:#999}h2{border-color:#444}}</style></head><body>",
            f"<h1>{html.escape(args.title)}</h1>",
            f"<p>{total} documents in {len(sections)} sections, packed {stamp}. Each opens as a PDF. "
            "The assistant searches these page by page.</p><nav>",
        ]
        for category in sorted(sections):
            parts.append(f"<a href='#{html.escape(category)}'>{html.escape(category.replace('-', ' '))}</a>")
        parts.append("</nav>")
        for category in sorted(sections):
            parts.append(f"<h2 id='{html.escape(category)}'>{html.escape(category.replace('-', ' '))}</h2><ul>")
            for item in sorted(sections[category], key=lambda entry: entry["title"].lower()):
                detail = ", ".join(part for part in (item["publisher"], item["year"]) if part)
                detail = f"{detail} · " if detail else ""
                parts.append(
                    f"<li><a href='{html.escape(item['href'], quote=True)}'>{html.escape(item['title'])}</a> "
                    f"<small>{html.escape(detail)}{item['size_mb']:.1f} MB</small></li>"
                )
            parts.append("</ul>")
        parts.append("</body></html>")
        (stage / "index.html").write_text("\n".join(parts), encoding="utf-8")
        # The page index reads this to name documents properly: a PDF's own
        # metadata title is often junk, and scanned covers OCR badly.
        catalogue = {}
        for items in sections.values():
            for item in items:
                detail = ", ".join(part for part in (item["publisher"], item["year"]) if part)
                catalogue[item["href"]] = f"{item['title']} ({detail})" if detail else item["title"]
        (stage / "titles.json").write_text(json.dumps(catalogue, ensure_ascii=False, indent=1), encoding="utf-8")

        scratch = target.with_suffix(".zim.building")
        scratch.unlink(missing_ok=True)
        command = [
            "zimwriterfs", "--welcome=index.html", "--illustration=icon.png", "--language=eng",
            f"--name={args.name}", f"--title={args.title[:30]}", f"--description={args.description[:80]}",
            "--creator=Various", "--publisher=Aegis", "--tags=manuals;preppers;_ftindex:yes",
            "--threads=8", str(stage), str(scratch),
        ]
        print(f"packing {total} PDFs ({sum(i['size_mb'] for s in sections.values() for i in s) / 1e3:.2f} GB)…", flush=True)
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode != 0 or not scratch.exists():
            sys.stderr.write(done.stdout[-1500:] + done.stderr[-1500:] + "\nerror: zimwriterfs failed\n")
            scratch.unlink(missing_ok=True)
            return 1
        for old in zim_dir.glob(f"{args.name}_*.zim"):
            if old != target:
                old.unlink()
        scratch.replace(target)

    print(f"wrote {target} ({target.stat().st_size / 1e9:.2f} GB), {total} documents")
    for category in sorted(sections):
        print(f"  {category}: {len(sections[category])}")
    if left_out:
        print(f"left out ({len(left_out)}):")
        for name, reason in left_out:
            print(f"  {name}: {reason}")

    if not args.no_refresh:
        subprocess.run([str(REPO / "scripts" / "library" / "rebuild-library.sh")], check=False)
        subprocess.run([sys.executable, str(REPO / "scripts" / "aegis-library"), "index", "--only", args.name],
                       check=False, cwd=str(REPO))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a folder of PDFs as a Kiwix collection.")
    parser.add_argument("--source", default=str(library_dir() / "manuals"))
    parser.add_argument("--name", default="aegis-manuals_en_all",
                        help="Collection id; keep the _en_all style so it sorts with the others")
    parser.add_argument("--title", default="Field manuals & guides")
    parser.add_argument("--description", default="Survival, medical, hunting, plant, mushroom and field manuals (PDF)")
    parser.add_argument("--no-refresh", action="store_true", help="Do not rebuild the catalog and the page index")
    return build(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
