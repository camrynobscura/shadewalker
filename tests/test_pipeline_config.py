"""Tests for pipeline/config.py's get_tile_bbox()."""

import pytest

from pipeline import config


def test_get_tile_bbox_returns_the_pilot_bbox_for_pilot():
    assert config.get_tile_bbox("pilot") == config.PILOT_BBOX


def test_get_tile_bbox_rejects_unknown_tile_ids():
    with pytest.raises(ValueError, match="Unknown tile id"):
        config.get_tile_bbox("brooklyn")
