"""
Regression tests for chart update decoupling from the main refresh loop.

Verifies:
- updateChart() is called on an independent 10-second timer
- refreshAll() does NOT call updateChart()
- /api/chart/bars?tf=5m uses period='1d' instead of date range
- Plotly layout includes datarevision for forced re-renders
"""

import re
from pathlib import Path

TEMPLATE = (Path(__file__).parent.parent / "dashboard" / "templates" / "index.html").read_text(
    encoding="utf-8"
)
APP_SRC = (Path(__file__).parent.parent / "dashboard" / "app.py").read_text(encoding="utf-8")


class TestChartPollingIndependence:
    """Chart updates on its own 10-second timer, not inside refreshAll()."""

    def test_set_interval_for_update_chart(self):
        assert re.search(r"setInterval\(\s*\(\)\s*=>\s*\{\s*updateChart\(\)", TEMPLATE), \
            "Expected setInterval(() => { updateChart(); }, ...) in template"

    def test_interval_is_10_seconds(self):
        m = re.search(r"setInterval\(\s*\(\)\s*=>\s*\{\s*updateChart\(\);\s*\},\s*(\d+)", TEMPLATE)
        assert m, "Could not find setInterval for updateChart"
        assert int(m.group(1)) == 10000, f"Expected 10000ms interval, got {m.group(1)}"

    def test_initial_update_chart_call(self):
        # updateChart() should be called once on page load (outside setInterval)
        m = re.search(r"updateChart\(\);\s*//.*first chart", TEMPLATE)
        assert m, "Expected initial updateChart() call on page load"

    def test_refresh_all_does_not_call_update_chart(self):
        # Extract refreshAll function body
        m = re.search(r"async function refreshAll\(\)\s*\{(.*?)\n\}", TEMPLATE, re.DOTALL)
        assert m, "Could not find refreshAll function"
        body = m.group(1)
        assert "updateChart" not in body, "refreshAll() should not call updateChart()"

    def test_update_chart_function_exists(self):
        assert "async function updateChart()" in TEMPLATE, \
            "Expected standalone updateChart() function"


class TestFiveMinuteBars:
    """5m bars use period='5d' for multi-day history (date-range fails for <1h)."""

    def test_5m_uses_period_5d(self):
        # The 5m branch should use period='5d' for multi-day history
        assert re.search(r"if\s+tf\s*==\s*'5m'.*?period='5d'", APP_SRC, re.DOTALL), \
            "5m bars should use period='5d'"

    def test_5m_does_not_use_start_end(self):
        # Extract the 5m-specific download block
        m = re.search(r"if tf == '5m':(.*?)else:", APP_SRC, re.DOTALL)
        assert m, "Could not find 5m conditional branch"
        block = m.group(1)
        assert "start=" not in block, "5m branch should not use start= parameter"
        assert "end=" not in block, "5m branch should not use end= parameter"

    def test_5m_empty_data_warning(self):
        assert re.search(r"if tf == '5m'.*?logger\.warning.*5m", APP_SRC, re.DOTALL), \
            "Empty 5m data should log a warning"


class TestPlotlyDataRevision:
    """Plotly layout must include datarevision for forced re-renders."""

    def test_datarevision_in_layout(self):
        assert "datarevision:" in TEMPLATE, \
            "Plotly layout should include datarevision property"

    def test_datarevision_uses_timestamp(self):
        assert re.search(r"datarevision:\s*Date\.now\(\)", TEMPLATE), \
            "datarevision should use Date.now() for unique values"
