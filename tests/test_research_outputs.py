"""Gates for research deliverables produced by agy jobs.

The agy wrapper only accepts `verify` commands in a small allowlist (pytest, ruff,
npm test, cargo test, go test), so research-only jobs are verified with these cheap
file-level assertions instead of shell `test`/`grep`.
"""

from pathlib import Path

PROBE_DOC = Path("docs/research/agy-probe-android-clipboard.md")
OPTIONS_DOC = Path("docs/research/agy-options-landscape.md")


def test_probe_doc_exists_with_verified_claims() -> None:
    assert PROBE_DOC.is_file(), f"missing {PROBE_DOC}"
    text = PROBE_DOC.read_text(encoding="utf-8")
    assert "VERIFIED" in text, "probe doc must mark claims VERIFIED"
    assert "https://" in text, "probe doc must cite at least one source URL"
    assert len(text) > 500, "probe doc looks too short to be useful"


def test_options_doc_covers_all_ten_options() -> None:
    assert OPTIONS_DOC.is_file(), f"missing {OPTIONS_DOC}"
    text = OPTIONS_DOC.read_text(encoding="utf-8").lower()
    required = [
        "kde connect",
        "gsconnect",
        "syncthing",
        "ntfy",
        "pushbullet",
        "tailscale",
        "self-hosted",
        "termux",
        "firebase",
        "supabase",
        "websocket",
    ]
    missing = [name for name in required if name not in text]
    assert not missing, f"options doc is missing sections: {missing}"
    assert "verified" in text and "https://" in text
    assert len(text) > 3000, "options doc looks too short to be a real survey"
