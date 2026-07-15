import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_html_code_runner_serves_sandboxed_preview_not_document_write():
    """runHTML now renders in a sandboxed iframe from the opaque-origin preview
    endpoint (no window.open/document.write injection). The optional
    open-in-window path must carry noopener so the run page can't reach back."""
    src = _source("static/js/codeRunner.js")
    match = re.search(
        r"export async function runHTML\(code, panel\) \{(?P<body>.*?)\n\}",
        src,
        re.S,
    )
    assert match
    body = match.group("body")
    # no legacy document.write injection anywhere in the HTML runner
    assert "document.write" not in body
    # renders inline via the staged preview URL in a scripts-only sandbox
    assert "stagePreview(code)" in body
    assert "iframe.sandbox = 'allow-scripts allow-pointer-lock'" in body
    assert "iframe.src = url" in body
    # the escape-hatch window opens with noopener
    assert "noopener" in body


def test_compare_print_popup_detaches_opener_before_document_write():
    src = _source("static/js/compare/index.js")
    match = re.search(
        r"function _exportPrint\(\) \{(?P<body>.*?)w\.document\.close\(\);",
        src,
        re.S,
    )

    assert match
    body = match.group("body")
    assert "w.opener = null" in body
    assert body.index("w.opener = null") < body.index("w.document.write(html)")
