from pathlib import Path


def test_memory_audit_uses_its_own_llm_timeout():
    source = Path("app.py").read_text(encoding="utf-8")
    start = source.index("_TIMEOUT_EXEMPT_PREFIXES =")
    end = source.index("\n)\n", start)
    timeout_exemptions = source[start:end]

    assert '"/api/memory/audit"' in timeout_exemptions


def test_memory_import_and_extract_use_their_own_llm_timeouts():
    # Import/extract await llm_call_async with a 300s budget; without the
    # exemption the 45s middleware hard-kill 504s every cold-model import
    # (seen live 2026-07-10: POST /api/memory/import -> 504 after 45s).
    source = Path("app.py").read_text(encoding="utf-8")
    start = source.index("_TIMEOUT_EXEMPT_PREFIXES =")
    end = source.index("\n)\n", start)
    timeout_exemptions = source[start:end]

    assert '"/api/memory/import"' in timeout_exemptions
    assert '"/api/memory/extract"' in timeout_exemptions
