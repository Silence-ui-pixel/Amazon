"""Windows Agent 打包规格的回归约束。"""

from pathlib import Path


def test_agent_build_bundles_status_window_html() -> None:
    spec = Path(__file__).resolve().parents[1] / "build" / "KeywordLibraryAgentFull.spec"
    source = spec.read_text(encoding="utf-8")

    assert 'os.path.join(ROOT, "agent", "status_window.html")' in source
    assert "datas=pw_datas + [status_window_data]" in source
