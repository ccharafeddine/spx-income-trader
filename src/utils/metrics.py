"""Prometheus metrics for production monitoring.

prometheus_client is an OPTIONAL dependency. When not installed, every metric
object becomes a silent no-op so the rest of the codebase can call
``metrics.trades_total.labels(...).inc()`` unconditionally.

Install for real metrics::

    pip install prometheus_client

Or install the monitoring extras::

    pip install -r requirements-monitoring.txt
"""

import time

# ---------------------------------------------------------------------------
# Try to import prometheus_client; build no-ops if missing
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    # ------ No-op stubs ------

    class _NoOpMetric:
        """Silent stub that swallows any call chain."""
        def __getattr__(self, _):
            return self
        def __call__(self, *_a, **_kw):
            return self

    def _noop_factory(*_a, **_kw):
        return _NoOpMetric()

    Counter = _noop_factory
    Gauge = _noop_factory
    Histogram = _noop_factory

    def generate_latest(_registry=None):
        return b''

    CONTENT_TYPE_LATEST = 'text/plain; charset=utf-8'


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

# Counters
trades_total = Counter(
    'spx_trades_total',
    'Total trades executed',
    ['strategy', 'direction', 'result'],
)

signals_total = Counter(
    'spx_signals_total',
    'Total signals detected',
    ['strategy', 'direction'],
)

rejections_total = Counter(
    'spx_rejections_total',
    'Total trade rejections',
    ['reason'],
)

# Gauges
daily_pnl_dollars = Gauge(
    'spx_daily_pnl_dollars',
    'Current daily realized P&L in dollars',
)

open_positions_count = Gauge(
    'spx_open_positions_count',
    'Number of currently open positions',
)

spx_price = Gauge(
    'spx_spx_price',
    'Latest SPX price',
)

vix_level = Gauge(
    'spx_vix_level',
    'Latest VIX level',
)

circuit_breaker_active = Gauge(
    'spx_circuit_breaker_active',
    'Whether a circuit breaker is currently triggered (1=active, 0=inactive)',
)

bot_uptime_seconds = Gauge(
    'spx_bot_uptime_seconds',
    'Seconds since bot started',
)

# Histograms
order_fill_latency_seconds = Histogram(
    'spx_order_fill_latency_seconds',
    'Time from order submission to fill confirmation',
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

trade_pnl_dollars = Histogram(
    'spx_trade_pnl_dollars',
    'P&L per closed trade in dollars',
    buckets=[-500, -200, -100, -50, 0, 50, 100, 200, 500, 1000],
)


# ---------------------------------------------------------------------------
# Helper: timed context manager for latency measurement
# ---------------------------------------------------------------------------

class track_latency:
    """Context manager that observes elapsed time on a Histogram.

    Usage::

        with track_latency(order_fill_latency_seconds):
            broker.place_order(...)
    """

    def __init__(self, histogram):
        self._histogram = histogram
        self._start = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        elapsed = time.monotonic() - self._start
        self._histogram.observe(elapsed)
        return False
