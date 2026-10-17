"""Filesystem locations for the Daily Melt strategy review."""
from pathlib import Path
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATA = HERE / 'data'                       # downloaded market data (gitignored)
DUKA = DATA / 'duka'                       # 1-minute .bi5 files, one per UTC day
RESULTS = REPO / 'docs' / 'strategy_review' / 'results'
CHARTS = REPO / 'docs' / 'strategy_review' / 'charts'
for p in (DATA, DUKA, RESULTS, CHARTS):
    p.mkdir(parents=True, exist_ok=True)
