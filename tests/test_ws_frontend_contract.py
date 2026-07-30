from pathlib import Path


def test_connect_ws_clears_previous_heartbeat_timer() -> None:
    """Regression: reconnect must not stack setInterval heartbeats."""
    source = (
        Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"
    ).read_text(encoding="utf-8")
    assert "_wsHeartbeat" in source
    assert "_clearWsTimers" in source
    assert "clearInterval(this._wsHeartbeat)" in source
    assert "this._wsHeartbeat = setInterval" in source
