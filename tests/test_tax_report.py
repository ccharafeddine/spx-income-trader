"""
Tests for tax report CSV export and trade data audit.

Validates:
1. Tax CSV includes all required columns
2. Proceeds and cost basis are correctly assigned for credit spreads
3. Section 1256 flag is present
4. Summary totals are accurate
5. Expired trades show settlement values
6. All financial data traces back to Schwab-sourced fields
7. Fill timestamps are extracted from Schwab execution legs
8. Settlement P&L reconciliation from Schwab transactions
"""

import pytest
import csv
import io
import json
from datetime import datetime, date
from unittest.mock import MagicMock, patch, PropertyMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Tax CSV column and format tests (client-side logic mirrored in Python)
# ---------------------------------------------------------------------------

TAX_COLUMNS = [
    'Date Opened', 'Date Closed', 'Description', 'Underlying Symbol',
    'Option Type', 'Spread Type', 'Short Strike', 'Long Strike',
    'Quantity', 'Proceeds', 'Cost Basis', 'Commissions & Fees',
    'Net Gain/Loss', 'Holding Period', 'Term', 'Section 1256 Contract',
    'Schwab Order ID', 'Account Number',
]


def _build_journal_trade(
    direction_raw='bullish',
    short_strike=6795,
    long_strike=6790,
    credit_received=2.50,
    exit_price=0.50,
    quantity=1,
    commissions=1.22,
    entry_time='2026-03-12T10:30:00',
    exit_time='2026-03-12T15:50:00',
    status='closed',
    entry_order_id='12345',
    exit_order_id='12346',
    exit_reason='profit_target',
    pnl_gross=None,
):
    """Build a journal trade dict as returned by /api/journal."""
    credit = credit_received
    qty = quantity
    ep = exit_price
    gross_pnl = pnl_gross if pnl_gross is not None else (credit - ep) * 100 * qty
    return {
        'id': 'test-trade-001',
        'direction_raw': direction_raw,
        'direction': 'PUT CREDIT' if direction_raw == 'bullish' else 'CALL CREDIT',
        'short_strike': short_strike,
        'long_strike': long_strike,
        'credit_received': credit,
        'exit_price': ep,
        'quantity': qty,
        'commissions': commissions,
        'entry_time': entry_time,
        'exit_time': exit_time,
        'status': status,
        'entry_order_id': entry_order_id,
        'exit_order_id': exit_order_id,
        'exit_reason': exit_reason,
        'pnl_gross': gross_pnl,
        'net_pnl': round(gross_pnl - commissions, 2),
    }


def _generate_tax_csv(trades, account_masked='****1234'):
    """Mirror the exportTaxReport() JS logic in Python for testing."""
    closed = [t for t in trades if t['status'] in ('closed', 'expired')]
    closed.sort(key=lambda t: t.get('entry_time', ''))

    first_date = (closed[0]['entry_time'] or '')[:10] if closed else ''
    last_date = (closed[-1]['entry_time'] or '')[:10] if closed else ''

    lines = []
    lines.append('# SPX Options Tax Report')
    lines.append(f'# Account: {account_masked}')
    lines.append(f'# Date Range: {first_date} to {last_date}')
    lines.append('# Section 1256 Contracts — 60% Long-Term / 40% Short-Term (IRC §1256)')
    lines.append('# All financial data sourced from Charles Schwab execution records')
    lines.append('#')

    lines.append(','.join(TAX_COLUMNS))

    total_proceeds = 0
    total_cost_basis = 0
    total_commissions = 0
    total_net = 0

    rows = []
    for t in closed:
        entry_date = (t.get('entry_time') or '')[:10]
        exit_date = (t.get('exit_time') or '')[:10]

        dir_raw = (t.get('direction_raw') or '').lower()
        if dir_raw == 'bullish':
            option_type = 'Put'
            desc = f"SPX 0DTE Put Credit Spread {t['short_strike']}/{t['long_strike']}"
        else:
            option_type = 'Call'
            desc = f"SPX 0DTE Call Credit Spread {t['short_strike']}/{t['long_strike']}"

        qty = t.get('quantity') or 1
        credit = t.get('credit_received') or 0
        proceeds = round(credit * 100 * qty, 2)
        ep = t.get('exit_price') if t.get('exit_price') is not None else 0
        cost_basis = round(ep * 100 * qty, 2)
        commissions = round(t.get('commissions') or 0, 2)
        net_gain = round(proceeds - cost_basis - commissions, 2)

        d1 = datetime.strptime(entry_date, '%Y-%m-%d') if entry_date else None
        d2 = datetime.strptime(exit_date, '%Y-%m-%d') if exit_date else None
        term_days = (d2 - d1).days if d1 and d2 else 0

        total_proceeds += proceeds
        total_cost_basis += cost_basis
        total_commissions += commissions
        total_net += net_gain

        row = {
            'Date Opened': entry_date,
            'Date Closed': exit_date,
            'Description': desc,
            'Underlying Symbol': 'SPX',
            'Option Type': option_type,
            'Spread Type': 'Vertical Credit Spread',
            'Short Strike': str(t['short_strike']),
            'Long Strike': str(t['long_strike']),
            'Quantity': str(qty),
            'Proceeds': f"{proceeds:.2f}",
            'Cost Basis': f"{cost_basis:.2f}",
            'Commissions & Fees': f"{commissions:.2f}",
            'Net Gain/Loss': f"{net_gain:.2f}",
            'Holding Period': 'Short-Term',
            'Term': str(term_days),
            'Section 1256 Contract': 'Yes',
            'Schwab Order ID': t.get('entry_order_id') or '',
            'Account Number': account_masked,
        }
        rows.append(row)
        lines.append(','.join(row.values()))

    # Summary
    lines.append('')
    lines.append(
        f',,,,,,,Total,{len(closed)},{total_proceeds:.2f},{total_cost_basis:.2f},'
        f'{total_commissions:.2f},{total_net:.2f},,,,,'
    )
    lines.append('')
    lines.append('# Section 1256 Tax Treatment:')
    lines.append('# 60% of net gain/loss is treated as long-term capital gain/loss')
    lines.append('# 40% of net gain/loss is treated as short-term capital gain/loss')
    lines.append(f'# 60% Long-Term: ,{total_net * 0.6:.2f}')
    lines.append(f'# 40% Short-Term: ,{total_net * 0.4:.2f}')

    return '\n'.join(lines), rows, {
        'total_proceeds': round(total_proceeds, 2),
        'total_cost_basis': round(total_cost_basis, 2),
        'total_commissions': round(total_commissions, 2),
        'total_net': round(total_net, 2),
    }


# ---------------------------------------------------------------------------
# Test 1: Tax CSV includes all required columns
# ---------------------------------------------------------------------------

class TestTaxCSVColumns:
    def test_all_required_columns_present(self):
        trade = _build_journal_trade()
        csv_text, rows, _ = _generate_tax_csv([trade])
        # Parse column header (first non-comment line)
        for line in csv_text.split('\n'):
            if not line.startswith('#'):
                headers = line.split(',')
                break
        for col in TAX_COLUMNS:
            assert col in headers, f"Missing column: {col}"

    def test_column_count_matches(self):
        trade = _build_journal_trade()
        _, rows, _ = _generate_tax_csv([trade])
        assert len(rows[0]) == len(TAX_COLUMNS)

    def test_column_order(self):
        trade = _build_journal_trade()
        csv_text, _, _ = _generate_tax_csv([trade])
        for line in csv_text.split('\n'):
            if not line.startswith('#'):
                headers = line.split(',')
                assert headers == TAX_COLUMNS
                break


# ---------------------------------------------------------------------------
# Test 2: Proceeds and cost basis for credit spreads
# ---------------------------------------------------------------------------

class TestCreditSpreadTaxTreatment:
    def test_proceeds_is_credit_received(self):
        """For credit spreads, proceeds = credit received (sale)."""
        trade = _build_journal_trade(credit_received=2.50, quantity=1)
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Proceeds'] == '250.00'  # 2.50 * 100 * 1

    def test_cost_basis_is_exit_debit(self):
        """Cost basis = debit paid to close."""
        trade = _build_journal_trade(exit_price=0.50, quantity=1)
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Cost Basis'] == '50.00'  # 0.50 * 100 * 1

    def test_proceeds_before_cost_basis_chronologically(self):
        """Credit spread: proceeds (credit) comes first, cost basis (debit) second."""
        trade = _build_journal_trade(credit_received=3.00, exit_price=0.80)
        _, rows, _ = _generate_tax_csv([trade])
        proceeds = float(rows[0]['Proceeds'])
        cost_basis = float(rows[0]['Cost Basis'])
        # Proceeds should be larger for a winning trade
        assert proceeds > cost_basis

    def test_multi_contract_scaling(self):
        """Proceeds and cost basis scale with quantity."""
        trade = _build_journal_trade(credit_received=2.00, exit_price=0.40, quantity=3)
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Proceeds'] == '600.00'    # 2.00 * 100 * 3
        assert rows[0]['Cost Basis'] == '120.00'   # 0.40 * 100 * 3

    def test_net_gain_loss_formula(self):
        """Net = proceeds - cost_basis - commissions."""
        trade = _build_journal_trade(
            credit_received=2.50, exit_price=0.50, commissions=1.22, quantity=1
        )
        _, rows, _ = _generate_tax_csv([trade])
        proceeds = float(rows[0]['Proceeds'])
        cost_basis = float(rows[0]['Cost Basis'])
        commissions = float(rows[0]['Commissions & Fees'])
        net = float(rows[0]['Net Gain/Loss'])
        assert net == pytest.approx(proceeds - cost_basis - commissions, abs=0.01)

    def test_losing_trade_negative_net(self):
        """Max loss trade should show negative net gain/loss."""
        trade = _build_journal_trade(
            credit_received=2.50, exit_price=5.00, commissions=1.22,
            short_strike=6795, long_strike=6790,
        )
        _, rows, _ = _generate_tax_csv([trade])
        net = float(rows[0]['Net Gain/Loss'])
        assert net < 0

    def test_put_credit_spread_description(self):
        trade = _build_journal_trade(direction_raw='bullish', short_strike=6795, long_strike=6790)
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Option Type'] == 'Put'
        assert 'Put Credit Spread' in rows[0]['Description']
        assert '6795/6790' in rows[0]['Description']

    def test_call_credit_spread_description(self):
        trade = _build_journal_trade(direction_raw='bearish', short_strike=6690, long_strike=6695)
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Option Type'] == 'Call'
        assert 'Call Credit Spread' in rows[0]['Description']
        assert '6690/6695' in rows[0]['Description']


# ---------------------------------------------------------------------------
# Test 3: Section 1256 flag
# ---------------------------------------------------------------------------

class TestSection1256:
    def test_section_1256_flag_yes(self):
        trade = _build_journal_trade()
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Section 1256 Contract'] == 'Yes'

    def test_underlying_symbol_spx(self):
        trade = _build_journal_trade()
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Underlying Symbol'] == 'SPX'

    def test_holding_period_short_term(self):
        trade = _build_journal_trade()
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Holding Period'] == 'Short-Term'

    def test_spread_type_vertical(self):
        trade = _build_journal_trade()
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Spread Type'] == 'Vertical Credit Spread'

    def test_header_mentions_section_1256(self):
        trade = _build_journal_trade()
        csv_text, _, _ = _generate_tax_csv([trade])
        assert 'Section 1256' in csv_text
        assert '60%' in csv_text
        assert '40%' in csv_text

    def test_sixty_forty_split_in_footer(self):
        trade = _build_journal_trade(credit_received=2.50, exit_price=0.50, commissions=1.22)
        csv_text, _, totals = _generate_tax_csv([trade])
        total_net = totals['total_net']
        lt = total_net * 0.6
        st = total_net * 0.4
        assert f'{lt:.2f}' in csv_text
        assert f'{st:.2f}' in csv_text


# ---------------------------------------------------------------------------
# Test 4: Summary totals accuracy
# ---------------------------------------------------------------------------

class TestSummaryTotals:
    def test_single_trade_totals(self):
        trade = _build_journal_trade(
            credit_received=2.50, exit_price=0.50, commissions=1.22, quantity=1
        )
        _, _, totals = _generate_tax_csv([trade])
        assert totals['total_proceeds'] == 250.00
        assert totals['total_cost_basis'] == 50.00
        assert totals['total_commissions'] == 1.22
        assert totals['total_net'] == pytest.approx(250.00 - 50.00 - 1.22, abs=0.01)

    def test_multiple_trade_totals(self):
        trades = [
            _build_journal_trade(credit_received=2.50, exit_price=0.50, commissions=1.22),
            _build_journal_trade(
                direction_raw='bearish', short_strike=6690, long_strike=6695,
                credit_received=3.00, exit_price=5.00, commissions=1.30,
                entry_time='2026-03-11T10:00:00', exit_time='2026-03-11T16:00:00',
                entry_order_id='12347', exit_order_id='12348',
            ),
        ]
        _, _, totals = _generate_tax_csv(trades)
        # Trade 1: proceeds=250, cost=50, comm=1.22, net=198.78
        # Trade 2: proceeds=300, cost=500, comm=1.30, net=-201.30
        assert totals['total_proceeds'] == pytest.approx(550.00, abs=0.01)
        assert totals['total_cost_basis'] == pytest.approx(550.00, abs=0.01)
        assert totals['total_commissions'] == pytest.approx(2.52, abs=0.01)
        assert totals['total_net'] == pytest.approx(-2.52, abs=0.01)

    def test_totals_two_decimal_places(self):
        trade = _build_journal_trade(credit_received=2.55, exit_price=0.33, commissions=1.15)
        _, rows, totals = _generate_tax_csv([trade])
        # Verify all dollar amounts have exactly 2 decimal places
        assert '.' in rows[0]['Proceeds']
        assert len(rows[0]['Proceeds'].split('.')[-1]) == 2
        assert len(rows[0]['Cost Basis'].split('.')[-1]) == 2
        assert len(rows[0]['Net Gain/Loss'].split('.')[-1]) == 2

    def test_account_number_masked(self):
        trade = _build_journal_trade()
        _, rows, _ = _generate_tax_csv([trade], account_masked='****5678')
        assert rows[0]['Account Number'] == '****5678'

    def test_schwab_order_id_included(self):
        trade = _build_journal_trade(entry_order_id='99887766')
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Schwab Order ID'] == '99887766'


# ---------------------------------------------------------------------------
# Test 5: Expired trades show settlement values
# ---------------------------------------------------------------------------

class TestExpiredTradeSettlement:
    def test_expired_otm_cost_basis_zero(self):
        """Expired OTM: worthless expiration, cost basis = $0."""
        trade = _build_journal_trade(
            exit_price=0.0, status='closed',
            exit_reason='Expired (OTM)',
            commissions=0.61,
        )
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Cost Basis'] == '0.00'
        # Full credit as proceeds
        assert float(rows[0]['Proceeds']) == 250.00

    def test_expired_itm_cost_basis_settlement(self):
        """Expired ITM: settlement debit shows as cost basis."""
        # ITM put credit spread: SPX dropped below short strike
        # Settlement value = intrinsic loss amount
        trade = _build_journal_trade(
            credit_received=2.50,
            exit_price=3.50,  # Settlement debit from Schwab
            status='closed',
            exit_reason='Expired (settled)',
            commissions=0.61,
        )
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Cost Basis'] == '350.00'  # 3.50 * 100
        net = float(rows[0]['Net Gain/Loss'])
        # 250 - 350 - 0.61 = -100.61
        assert net == pytest.approx(-100.61, abs=0.01)

    def test_only_closed_trades_exported(self):
        """Active/pending trades should not appear in tax report."""
        trades = [
            _build_journal_trade(status='closed'),
            _build_journal_trade(status='active',
                                 entry_time='2026-03-13T10:00:00'),
            _build_journal_trade(status='expired',
                                 entry_time='2026-03-14T10:00:00',
                                 exit_reason='Expired'),
        ]
        _, rows, _ = _generate_tax_csv(trades)
        # Only closed and expired should be included
        assert len(rows) == 2

    def test_term_days_zero_for_0dte(self):
        """0DTE trades: entry and exit on same day = 0 term days."""
        trade = _build_journal_trade(
            entry_time='2026-03-12T10:30:00',
            exit_time='2026-03-12T15:50:00',
        )
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Term'] == '0'


# ---------------------------------------------------------------------------
# Test 6: Financial data traces back to Schwab-sourced fields
# ---------------------------------------------------------------------------

class TestSchwabSourcedFields:
    """Verify that _parse_order_response extracts fill data from Schwab."""

    def test_fill_time_extracted_from_execution_legs(self):
        """fill_time should come from executionLegs, not local clock."""
        from src.brokers.schwab_broker import SchwabBroker

        with patch('src.brokers.schwab_broker.SchwabAuth'):
            broker = SchwabBroker.__new__(SchwabBroker)

        order_data = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {
                            'legId': 1, 'price': 4.80, 'quantity': 1,
                            'commission': 0, 'fees': 0.61, 'miscFees': 0,
                            'time': '2026-03-12T14:50:23+0000',
                        },
                        {
                            'legId': 2, 'price': 2.30, 'quantity': 1,
                            'commission': 0, 'fees': 0.61, 'miscFees': 0,
                            'time': '2026-03-12T14:50:23+0000',
                        },
                    ],
                }
            ],
        }
        result = broker._parse_order_response(order_data)
        assert 'fill_time' in result
        assert result['fill_time'] == '2026-03-12T14:50:23+0000'

    def test_fill_time_absent_when_no_execution_data(self):
        """No fill_time when orderActivityCollection is empty."""
        from src.brokers.schwab_broker import SchwabBroker

        with patch('src.brokers.schwab_broker.SchwabAuth'):
            broker = SchwabBroker.__new__(SchwabBroker)

        order_data = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [],
        }
        result = broker._parse_order_response(order_data)
        assert 'fill_time' not in result

    def test_fill_price_from_execution_legs_not_limit(self):
        """Fill price must come from executionLegs, not the limit price."""
        from src.brokers.schwab_broker import SchwabBroker

        with patch('src.brokers.schwab_broker.SchwabAuth'):
            broker = SchwabBroker.__new__(SchwabBroker)

        order_data = {
            'status': 'FILLED',
            'price': 2.50,  # limit price
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 4.85, 'quantity': 1,
                         'commission': 0, 'fees': 0, 'miscFees': 0},
                        {'legId': 2, 'price': 2.30, 'quantity': 1,
                         'commission': 0, 'fees': 0, 'miscFees': 0},
                    ],
                }
            ],
        }
        result = broker._parse_order_response(order_data)
        # Actual fill: abs(4.85 - 2.30) = 2.55, NOT the 2.50 limit price
        assert result['fill_price'] == pytest.approx(2.55, abs=0.001)

    def test_fees_from_execution_legs(self):
        """Commissions come from executionLegs, not calculated."""
        from src.brokers.schwab_broker import SchwabBroker

        with patch('src.brokers.schwab_broker.SchwabAuth'):
            broker = SchwabBroker.__new__(SchwabBroker)

        order_data = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 4.80, 'quantity': 1,
                         'commission': 0.30, 'fees': 0.31, 'miscFees': 0.02},
                        {'legId': 2, 'price': 2.30, 'quantity': 1,
                         'commission': 0.30, 'fees': 0.31, 'miscFees': 0.02},
                    ],
                }
            ],
        }
        result = broker._parse_order_response(order_data)
        # Total fees: (0.30+0.31+0.02) * 2 = 1.26
        assert result['order_fees'] == pytest.approx(1.26, abs=0.01)

    def test_quantity_from_schwab_filled_quantity(self):
        """Quantity should use filledQuantity from Schwab response."""
        from src.brokers.schwab_broker import SchwabBroker

        with patch('src.brokers.schwab_broker.SchwabAuth'):
            broker = SchwabBroker.__new__(SchwabBroker)

        order_data = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 3,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 4.80, 'quantity': 3,
                         'commission': 0, 'fees': 0, 'miscFees': 0},
                        {'legId': 2, 'price': 2.30, 'quantity': 3,
                         'commission': 0, 'fees': 0, 'miscFees': 0},
                    ],
                }
            ],
        }
        result = broker._parse_order_response(order_data)
        assert result['filled_quantity'] == 3

    def test_single_leg_fill_time(self):
        """Single-leg orders also capture fill_time."""
        from src.brokers.schwab_broker import SchwabBroker

        with patch('src.brokers.schwab_broker.SchwabAuth'):
            broker = SchwabBroker.__new__(SchwabBroker)

        order_data = {
            'status': 'FILLED',
            'price': 1.20,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'BUY_TO_CLOSE'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 1.20, 'quantity': 1,
                         'time': '2026-03-12T15:30:00+0000'},
                    ],
                }
            ],
        }
        result = broker._parse_order_response(order_data)
        assert result.get('fill_time') == '2026-03-12T15:30:00+0000'


# ---------------------------------------------------------------------------
# Test: _parse_fill_time helper
# ---------------------------------------------------------------------------

class TestParseFillTime:
    def _make_pm(self):
        """Create a minimal PositionManager with timezone set."""
        import pytz
        pm = object.__new__(type('PM', (), {}))
        pm.tz = pytz.timezone('US/Eastern')
        # Bind the method
        from src.core.position_manager import PositionManager
        pm._parse_fill_time = PositionManager._parse_fill_time.__get__(pm)
        return pm

    def test_parses_utc_iso(self):
        pm = self._make_pm()
        result = pm._parse_fill_time('2026-03-12T14:50:23+0000')
        assert result is not None
        assert result.hour == 10  # 14:50 UTC = 10:50 ET (EDT)
        assert result.minute == 50

    def test_none_for_empty_string(self):
        pm = self._make_pm()
        assert pm._parse_fill_time('') is None
        assert pm._parse_fill_time(None) is None

    def test_none_for_invalid_format(self):
        pm = self._make_pm()
        assert pm._parse_fill_time('not-a-date') is None


# ---------------------------------------------------------------------------
# Test: Settlement P&L reconciliation (db_manager)
# ---------------------------------------------------------------------------

class TestSettlementPnlReconciliation:
    def test_update_trade_settlement_pnl(self, tmp_path):
        """update_trade_settlement_pnl updates pnl and recalculates pnl_percent."""
        from database.db_manager import DatabaseManager

        db_path = str(tmp_path / 'test.db')
        db = DatabaseManager(db_path)

        # Insert a trade manually
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                entry_time TIMESTAMP NOT NULL,
                exit_time TIMESTAMP,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                strategy_type TEXT DEFAULT 'daily_income',
                short_strike REAL NOT NULL,
                long_strike REAL NOT NULL,
                spread_width REAL NOT NULL,
                credit_received REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_order_id TEXT,
                underlying_price_at_entry REAL,
                setup_bar_time TIMESTAMP,
                spx_at_entry REAL,
                vix_at_entry REAL,
                vix_regime TEXT,
                day_open REAL,
                gap_pct REAL,
                intraday_move_at_entry REAL,
                theoretical_credit REAL,
                actual_credit REAL,
                slippage REAL,
                slippage_pct REAL,
                economic_events TEXT,
                day_of_week INTEGER,
                day_of_week_name TEXT,
                entry_time_bucket TEXT,
                sma50 REAL,
                sma200 REAL,
                spx_vs_sma50 REAL,
                spx_vs_sma50_pct REAL,
                spx_vs_sma200 REAL,
                spx_vs_sma200_pct REAL,
                prior_day_high REAL,
                prior_day_low REAL,
                spx_vs_prior_range TEXT,
                exit_price REAL,
                exit_order_id TEXT,
                exit_reason TEXT,
                spx_at_exit REAL,
                vix_at_exit REAL,
                profit_captured_pct REAL,
                time_in_trade_minutes INTEGER,
                day_type TEXT,
                daily_move_pct REAL,
                pnl REAL,
                pnl_percent REAL,
                max_profit REAL,
                max_risk REAL,
                commissions REAL DEFAULT 0.0,
                quantity INTEGER NOT NULL,
                expiration TIMESTAMP NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO trades (id, entry_time, direction, status, short_strike,
                long_strike, spread_width, credit_received, entry_price,
                quantity, expiration, pnl, pnl_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'trade-001', '2026-03-12T10:30:00', 'BULLISH', 'closed',
            6795, 6790, 5.0, 2.50, 2.50,
            1, '2026-03-12', 250.0, 100.0,
        ))
        conn.commit()
        conn.close()

        # Update settlement P&L
        db.update_trade_settlement_pnl('trade-001', 245.50)

        # Verify
        conn2 = sqlite3.connect(db_path)
        row = conn2.execute(
            "SELECT pnl, pnl_percent FROM trades WHERE id = ?",
            ('trade-001',)
        ).fetchone()
        conn2.close()

        assert row[0] == 245.50
        # pnl_percent = 245.50 / (2.50 * 100 * 1) * 100 = 98.2
        assert row[1] == pytest.approx(98.2, abs=0.1)


# ---------------------------------------------------------------------------
# Test: Header info section
# ---------------------------------------------------------------------------

class TestTaxCSVHeader:
    def test_header_has_account_number(self):
        trade = _build_journal_trade()
        csv_text, _, _ = _generate_tax_csv([trade], account_masked='****1234')
        assert '****1234' in csv_text

    def test_header_has_date_range(self):
        trade = _build_journal_trade(entry_time='2026-03-12T10:30:00')
        csv_text, _, _ = _generate_tax_csv([trade])
        assert '2026-03-12' in csv_text

    def test_header_has_schwab_source_note(self):
        trade = _build_journal_trade()
        csv_text, _, _ = _generate_tax_csv([trade])
        assert 'Charles Schwab' in csv_text

    def test_header_lines_are_comments(self):
        trade = _build_journal_trade()
        csv_text, _, _ = _generate_tax_csv([trade])
        for line in csv_text.split('\n'):
            if line.startswith('#'):
                continue
            # First non-comment line should be column headers
            assert line.startswith('Date Opened')
            break


# ---------------------------------------------------------------------------
# Test: Date format
# ---------------------------------------------------------------------------

class TestDateFormat:
    def test_dates_yyyy_mm_dd(self):
        trade = _build_journal_trade(
            entry_time='2026-03-12T10:30:00',
            exit_time='2026-03-12T15:50:00',
        )
        _, rows, _ = _generate_tax_csv([trade])
        assert rows[0]['Date Opened'] == '2026-03-12'
        assert rows[0]['Date Closed'] == '2026-03-12'

    def test_sort_oldest_first(self):
        trades = [
            _build_journal_trade(entry_time='2026-03-12T10:30:00'),
            _build_journal_trade(entry_time='2026-03-10T10:30:00',
                                 entry_order_id='10001'),
        ]
        _, rows, _ = _generate_tax_csv(trades)
        assert rows[0]['Date Opened'] == '2026-03-10'
        assert rows[1]['Date Opened'] == '2026-03-12'
