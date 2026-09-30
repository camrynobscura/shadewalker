"""Tests for pipeline/scoring/trees.py -- the per-tree value formula.

    dead trees are excluded
    dbh is capped at DBH_CAP_IN
    garbage dbh values are rejected
    evergreen genera split from deciduous
    unknown condition falls back to the midpoint
    multiple trees on one edge sum

Plus the cases the real citywide data contains:

    dbh arrives as JSON null -> float(None) raises TypeError, not
    ValueError. 77 of 898,643 rows.
    dbh of 2427 inches -- 61 metres of trunk -- is really in the feed, so
    the cap is load-bearing rather than defensive.
    genusspecies missing entirely (21 rows) must fall through to deciduous.
"""

from pipeline import config
from pipeline.scoring import trees


def tree(dbh="10", condition="Good", species="Acer rubrum - red maple"):
    """A Forestry row, shaped like the real feed: dbh is a string."""
    return {"dbh": dbh, "tpcondition": condition, "genusspecies": species}


# --- dead trees are excluded -------------------------------------------

def test_dead_trees_contribute_nothing():
    assert trees.tree_value(tree(condition="Dead")) is None


def test_a_dead_tree_with_a_huge_trunk_still_contributes_nothing():
    # size must not rescue a dead tree
    assert trees.tree_value(tree(dbh="40", condition="Dead")) is None


# --- dbh is capped -----------------------------------------------------

def test_dbh_is_capped_so_a_giant_scores_the_same_as_the_cap():
    at_cap = trees.tree_value(tree(dbh=str(config.DBH_CAP_IN)))
    over_cap = trees.tree_value(tree(dbh=str(config.DBH_CAP_IN * 3)))
    assert at_cap.deciduous == over_cap.deciduous


def test_the_real_worlds_2427_inch_trunk_is_capped_not_dominant():
    """2427 is the actual maximum in the live citywide feed."""
    absurd = trees.tree_value(tree(dbh="2427"))
    at_cap = trees.tree_value(tree(dbh=str(config.DBH_CAP_IN)))
    assert absurd.deciduous == at_cap.deciduous


def test_value_scales_with_dbh_below_the_cap():
    small = trees.tree_value(tree(dbh="5")).deciduous
    large = trees.tree_value(tree(dbh="20")).deciduous
    assert large > small
    # linear in dbh below the cap
    assert large == small * 4


# --- garbage dbh is rejected ------------------------------------------

def test_missing_dbh_key_is_rejected():
    row = {"tpcondition": "Good", "genusspecies": "Acer rubrum - red maple"}
    assert trees.tree_value(row) is None


def test_null_dbh_is_rejected_without_raising():
    """JSON null: float(None) raises TypeError, not ValueError. 77 real rows."""
    assert trees.tree_value(tree(dbh=None)) is None


def test_non_numeric_dbh_is_rejected():
    assert trees.tree_value(tree(dbh="not a number")) is None
    assert trees.tree_value(tree(dbh="")) is None


def test_zero_and_negative_dbh_are_rejected():
    assert trees.tree_value(tree(dbh="0")) is None
    # none in today's feed, but a live feed must not be able to subtract shade
    assert trees.tree_value(tree(dbh="-12")) is None


# --- evergreen split --------------------------------------------------

def test_evergreen_genus_scores_as_evergreen():
    value = trees.tree_value(tree(species="Pinus strobus - eastern white pine"))
    assert value.evergreen > 0
    assert value.deciduous == 0


def test_unlisted_genus_defaults_to_deciduous():
    value = trees.tree_value(tree(species="Quercus rubra - northern red oak"))
    assert value.deciduous > 0
    assert value.evergreen == 0


def test_missing_species_defaults_to_deciduous():
    """21 real rows have no genusspecies at all."""
    value = trees.tree_value(tree(species=None))
    assert value.deciduous > 0
    assert value.evergreen == 0


def test_the_split_is_exclusive_and_preserves_the_value():
    """An evergreen and a deciduous of identical size/condition carry the
    same total value -- the split routes it, it does not change it."""
    ever = trees.tree_value(tree(species="Picea abies - Norway spruce"))
    deci = trees.tree_value(tree(species="Acer rubrum - red maple"))
    assert ever.evergreen == deci.deciduous


# --- condition ---------------------------------------------------------

def test_unknown_condition_falls_back_to_the_midpoint():
    unknown = trees.tree_value(tree(condition="Unknown")).deciduous
    excellent = trees.tree_value(tree(condition="Excellent")).deciduous
    assert unknown == excellent * config.CONDITION_DEFAULT


def test_an_unrecognised_condition_also_falls_back():
    weird = trees.tree_value(tree(condition="Sideways")).deciduous
    unknown = trees.tree_value(tree(condition="Unknown")).deciduous
    assert weird == unknown


def test_blank_condition_falls_back():
    assert (trees.tree_value(tree(condition="")).deciduous
            == trees.tree_value(tree(condition="Unknown")).deciduous)


def test_healthier_condition_scores_higher():
    order = ["Excellent", "Good", "Fair", "Poor", "Critical"]
    scores = [trees.tree_value(tree(condition=c)).deciduous for c in order]
    assert scores == sorted(scores, reverse=True)


# --- multiple trees sum ------------------------------------------------

def test_multiple_trees_sum_into_totals():
    totals = trees.Totals()
    for row in (tree(dbh="10"), tree(dbh="20"),
                tree(dbh="15", species="Pinus strobus - white pine")):
        value = trees.tree_value(row)
        totals.add(value)
    assert totals.count == 3
    assert totals.deciduous == (trees.tree_value(tree(dbh="10")).deciduous
                                + trees.tree_value(tree(dbh="20")).deciduous)
    assert totals.evergreen > 0


def test_a_skipped_tree_is_not_counted():
    """count feeds the UI's tree_count; counting a dead tree would show a
    number the walker cannot see."""
    totals = trees.Totals()
    for row in (tree(), tree(condition="Dead"), tree(dbh=None)):
        value = trees.tree_value(row)
        if value is not None:
            totals.add(value)
    assert totals.count == 1


# --- the batch summary -------------------------------------------------

def test_summarise_separates_dead_from_unusable():
    tally = trees.summarise([
        tree(), tree(species="Pinus strobus - white pine"),
        tree(condition="Dead"), tree(condition="Dead"),
        tree(dbh=None), tree(dbh="junk"),
    ])
    assert tally == {"scored": 2, "dead": 2, "unusable": 2, "evergreen": 1}
