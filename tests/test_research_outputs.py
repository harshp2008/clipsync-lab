"""Gates for research deliverables produced by agy jobs.

The agy wrapper only accepts `verify` commands in a small allowlist (pytest, ruff,
npm test, cargo test, go test), so research-only jobs are verified with these cheap
file-level assertions instead of shell `test`/`grep`.
"""

from pathlib import Path

PROBE_DOC = Path("docs/research/agy-probe-android-clipboard.md")


def test_probe_doc_exists_with_verified_claims() -> None:
    assert PROBE_DOC.is_file(), f"missing {PROBE_DOC}"
    text = PROBE_DOC.read_text(encoding="utf-8")
    assert "VERIFIED" in text, "probe doc must mark claims VERIFIED"
    assert "https://" in text, "probe doc must cite at least one source URL"
    assert len(text) > 500, "probe doc looks too short to be useful"
