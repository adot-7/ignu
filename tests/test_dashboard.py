from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "app" / "static"


def test_dashboard_keeps_only_the_supported_demo_surface() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert index.count('class="stage-card"') == 5
    for stage in ("ingest", "github", "profile", "graph", "rank"):
        assert f'data-stage="{stage}"' in index
    for removed in ("Teams", "Memory", "How we rank", "chat-panel", "reset-demo", "person-drawer"):
        assert removed not in index

    assert 'fetchJson("/api/state")' in script
    assert 'new EventSource("/events")' in script
    for removed in ("/ask", "/api/reset", "/api/person/", "renderTeams", "openDrawer", "renderRanking", "scoring", "chat"):
        assert removed not in script
    for removed in ("chat-panel", "team-card", "person-drawer", "drawer-backdrop", "ranking-panel", "ranking-content", "weight-row", "thresholds", "rank-footnote"):
        assert removed not in styles
