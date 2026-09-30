"""What one tree is worth, as shade.

This module is deliberately geometry-free. It answers "how much shade is
this tree worth, and is it evergreen or deciduous" and nothing else -- no
distances, no buffers, no streets. Where a tree's shade lands is
pipeline/graph/blockface.py's problem. How it becomes a route statistic is
server/graph_store.py's.

THE FORMULA
-----------
    value = condition_score x min(dbh, DBH_CAP_IN) / DBH_CAP_IN

`dbh` is trunk diameter at breast height, in inches, and it is the only
size information Forestry publishes -- there is no canopy-radius field.
Across 898,643 living trees: median 9in, p25 4, p75 16, p90 24, p99 38.

The cap is not decoration. The largest `dbh` in the live citywide data is
2427 inches -- 61 metres of trunk -- so without it one mistyped record
would dominate an entire block's score.

Seasonality is applied later, not here: the server multiplies the deciduous
share by config.CANOPY_BY_MONTH, blended by day for the date being routed
(graph_store._tree_fraction), so one scored export serves every day.
Evergreens are held out of that multiplication, which is the only reason
the split exists.
"""

import logging
from typing import NamedTuple

from pipeline import config

logger = logging.getLogger(__name__)

# tpcondition values that contribute no shade at all. Standing dead trees
# are still physically present -- the fetch filters on tpstructure='Full',
# which means "standing" and includes them -- so they must be dropped here.
# 10,635 of the 898,643 living-structure trees are Dead (1.2%).
DEAD = "Dead"


class TreeValue(NamedTuple):
    """One tree's shade contribution, split by whether it drops its leaves.

    Exactly one of the two is non-zero. Both are kept as separate fields
    rather than a value plus a flag because every consumer sums many trees
    and needs the two totals, never the individual flags.
    """
    evergreen: float
    deciduous: float


def tree_value(row: dict) -> TreeValue | None:
    """A tree's shade value, or None if it contributes nothing.

    None (rather than a zero TreeValue) for the two cases where the tree
    should not be counted at all: it is Dead, or it has no usable trunk
    diameter. A caller summing values wants those skipped, and a caller
    counting trees must not count them either -- a block reporting "3 trees"
    that are all dead would be a lie in the UI.
    """
    if (row.get("tpcondition") or "").strip() == DEAD:
        return None

    # dbh arrives as a string ("22") from Socrata, and 77 of the 898,643
    # citywide rows carry JSON null. float(None) raises TypeError, not
    # ValueError, so both are caught.
    try:
        dbh = float(row.get("dbh") or 0)
    except (TypeError, ValueError):
        dbh = 0.0
    # A negative diameter is meaningless and would subtract shade. None are
    # present in today's data; the guard is here because the feed is live
    # and a sign error upstream must not silently make a block sunnier.
    if dbh <= 0:
        return None

    condition = (row.get("tpcondition") or "").strip()
    # Unknown and blank both land on CONDITION_DEFAULT via the fallback --
    # "Unknown" is an explicit key with the same 0.5, so both paths agree.
    condition_score = config.CONDITION_SCORES.get(
        condition, config.CONDITION_DEFAULT)

    value = condition_score * min(dbh, config.DBH_CAP_IN) / config.DBH_CAP_IN
    if value <= 0:
        return None

    # genusspecies reads "Quercus bicolor - swamp white oak"; the genus is
    # the first word. 21 citywide rows have none, which yields "" and falls
    # through to deciduous -- the overwhelmingly right default for NYC's
    # street forest.
    genus = (row.get("genusspecies") or "").split(" ")[0]
    if genus in config.EVERGREEN_GENERA:
        return TreeValue(evergreen=value, deciduous=0.0)
    return TreeValue(evergreen=0.0, deciduous=value)


class Totals:
    """Running sum of tree value for one block face."""

    __slots__ = ("evergreen", "deciduous", "count")

    def __init__(self):
        self.evergreen = 0.0
        self.deciduous = 0.0
        self.count = 0

    def add(self, value: TreeValue) -> None:
        self.evergreen += value.evergreen
        self.deciduous += value.deciduous
        self.count += 1

    def __repr__(self):
        return (f"Totals(evergreen={self.evergreen:.3f}, "
                f"deciduous={self.deciduous:.3f}, count={self.count})")


def summarise(rows) -> dict:
    """Count how a batch of raw tree rows broke down, for logging.

    Worth its own function because the counts are the only warning that a
    feed change has quietly gutted the scoring -- a jump in `unusable`
    means Forestry altered a field, not that the trees died.
    """
    tally = {"scored": 0, "dead": 0, "unusable": 0, "evergreen": 0}
    for row in rows:
        if (row.get("tpcondition") or "").strip() == DEAD:
            tally["dead"] += 1
            continue
        value = tree_value(row)
        if value is None:
            tally["unusable"] += 1
            continue
        tally["scored"] += 1
        if value.evergreen > 0:
            tally["evergreen"] += 1
    return tally
