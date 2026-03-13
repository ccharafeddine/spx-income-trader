"""
Regression tests for dashboard layout changes.

Verifies panel placement after layout reorganization:
- Reconciliation panel removed from Overview
- Signal Log moved to Logs tab
- Trade History moved to Trade Journal tab
- Chart CSS heights updated
- No orphaned bottom-row div
"""

import re
from pathlib import Path

TEMPLATE = (Path(__file__).parent.parent / "dashboard" / "templates" / "index.html").read_text(
    encoding="utf-8"
)


def _extract_tab(tab_id: str) -> str:
    """Extract content between tab opening and closing tags."""
    pattern = rf'<div id="{tab_id}"[^>]*>(.*?)</div><!-- /{tab_id} -->'
    m = re.search(pattern, TEMPLATE, re.DOTALL)
    assert m, f"Could not find tab {tab_id}"
    return m.group(1)


class TestOverviewTab:
    overview = _extract_tab("tab-overview")

    def test_no_recon_panel(self):
        assert 'id="recon-panel"' not in self.overview

    def test_no_recon_body(self):
        assert 'id="recon-body"' not in self.overview

    def test_no_bottom_row(self):
        assert "bottom-row" not in self.overview

    def test_no_signal_log_in_overview(self):
        assert 'id="signal-log-body"' not in self.overview

    def test_no_trade_history_in_overview(self):
        assert 'id="history-table-body"' not in self.overview
        assert 'id="agg-stats"' not in self.overview

    def test_positions_panel_present(self):
        assert 'id="positions-panel-wrap"' in self.overview


class TestJournalTab:
    journal = _extract_tab("tab-journal")

    def test_agg_stats_present(self):
        assert 'id="agg-stats"' in self.journal

    def test_equity_chart_present(self):
        assert 'id="equity-chart"' in self.journal

    def test_history_table_body_present(self):
        assert 'id="history-table-body"' in self.journal

    def test_title_includes_mtd(self):
        assert "MTD" in self.journal


class TestLogsTab:
    logs = _extract_tab("tab-logs")

    def test_signal_log_body_present(self):
        assert 'id="signal-log-body"' in self.logs


class TestChartCSS:
    def test_chart_panel_min_height(self):
        assert "min-height: 380px" in TEMPLATE

    def test_chart_container_min_height(self):
        assert "min-height: 340px" in TEMPLATE


class TestUniqueIDs:
    """Each critical ID must appear exactly once in the HTML."""

    def test_agg_stats_unique(self):
        assert TEMPLATE.count('id="agg-stats"') == 1

    def test_equity_chart_unique(self):
        assert TEMPLATE.count('id="equity-chart"') == 1

    def test_history_table_body_unique(self):
        assert TEMPLATE.count('id="history-table-body"') == 1

    def test_signal_log_body_unique(self):
        assert TEMPLATE.count('id="signal-log-body"') == 1

    def test_recon_panel_gone(self):
        assert TEMPLATE.count('id="recon-panel"') == 0

    def test_recon_body_gone_from_html(self):
        # recon-body only exists in JS getElementById calls, not as an HTML element
        assert 'id="recon-body"' not in TEMPLATE
