"""Utilities for chart annotation layout, overlap resolution, and indicators."""

from collections import defaultdict
from typing import List, Optional, Set


def resolve_label_overlaps(
    annotations: List[dict],
    pulse_x_set: Optional[Set[str]] = None,
) -> None:
    """Offset annotations sharing the same x-coordinate to prevent overlap.

    Only considers data-anchored arrow annotations (xref='x', yref='y',
    showarrow=True).  Each annotation must carry a ``_direction`` key
    ('bullish' or 'bearish') so the nudge goes the right way:

    * bearish / call-credit  -> ay += +15  (push label down)
    * bullish / put-credit   -> ay += -15  (push label up)

    When *pulse_x_set* is provided, a lone annotation sitting on a
    pulse-bar x position also receives the directional offset.

    Mutates *annotations* in place; returns nothing.
    """
    by_x: dict[str, list[int]] = defaultdict(list)
    for i, ann in enumerate(annotations):
        if (
            ann.get("xref") == "x"
            and ann.get("yref") == "y"
            and ann.get("showarrow")
        ):
            by_x[str(ann["x"])].append(i)

    for x_key, indices in by_x.items():
        # Multiple labels at the same candle -> offset later ones
        if len(indices) > 1:
            for rank in range(1, len(indices)):
                ann = annotations[indices[rank]]
                is_bearish = ann.get("_direction") == "bearish"
                ann["ay"] += 15 if is_bearish else -15

        # Single label coinciding with a pulse-bar highlight -> offset
        if pulse_x_set and x_key in pulse_x_set and len(indices) == 1:
            ann = annotations[indices[0]]
            is_bearish = ann.get("_direction") == "bearish"
            ann["ay"] += 15 if is_bearish else -15


def compute_bollinger_series(
    closes: List[float],
    period: int = 20,
    num_std: float = 2.0,
) -> List[Optional[dict]]:
    """Compute rolling Bollinger Band values for chart overlay.

    Returns a list the same length as *closes*. Each element is either
    None (insufficient data, fewer than *period* bars) or a dict with
    keys ``upper``, ``middle``, ``lower``.

    Uses standard 20-period, 2-std BB (industry default for chart
    overlays). Independent from the strategy's BollingerFilter.
    """
    result: List[Optional[dict]] = []
    for i in range(len(closes)):
        if i + 1 < period:
            result.append(None)
            continue
        window = closes[i + 1 - period : i + 1]
        middle = sum(window) / period
        variance = sum((x - middle) ** 2 for x in window) / period
        std = variance ** 0.5
        result.append({
            'upper': round(middle + num_std * std, 2),
            'middle': round(middle, 2),
            'lower': round(middle - num_std * std, 2),
        })
    return result
