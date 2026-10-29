"""Tests for `mellon.gui.dpi` — DPR-aware logical→physical pixel conversion."""

from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_dpi_scale_at_1x(monkeypatch):
    from mellon.gui import dpi

    monkeypatch.setattr(dpi, "device_pixel_ratio", lambda widget=None: 1.0)
    assert dpi.dpi_scale(10) == 10
    assert dpi.dpi_scale(0) == 0


def test_dpi_scale_at_1_5x(monkeypatch):
    from mellon.gui import dpi

    monkeypatch.setattr(dpi, "device_pixel_ratio", lambda widget=None: 1.5)
    assert dpi.dpi_scale(10) == 15
    assert dpi.dpi_scale(24) == 36


def test_dpi_scale_at_2x(monkeypatch):
    from mellon.gui import dpi

    monkeypatch.setattr(dpi, "device_pixel_ratio", lambda widget=None: 2.0)
    assert dpi.dpi_scale(10) == 20
    assert dpi.dpi_scale(0.5) == 1


def test_dpi_scale_rounds_half_to_even(monkeypatch):
    """Python's `round()` is banker's rounding — 7 * 1.5 = 10.5 → 10, not 11.

    The exact rounding behaviour matters less than knowing which one we get;
    documenting it here so a future reader doesn't have to re-derive it.
    """
    from mellon.gui import dpi

    monkeypatch.setattr(dpi, "device_pixel_ratio", lambda widget=None: 1.5)
    assert dpi.dpi_scale(7) == 10  # 10.5 → 10 (banker's)
    assert dpi.dpi_scale(9) == 14  # 13.5 → 14 (banker's)


def test_device_pixel_ratio_with_no_screen(monkeypatch):
    """Falls back to 1.0 when QGuiApplication has no primary screen."""
    from mellon.gui import dpi

    monkeypatch.setattr(
        dpi.QGuiApplication, "primaryScreen", staticmethod(lambda: None)
    )
    assert dpi.device_pixel_ratio() == 1.0


def test_device_pixel_ratio_uses_widget_screen(qapp, monkeypatch):
    """If a widget is given, use its screen's DPR rather than the primary."""
    from mellon.gui import dpi

    class FakeScreen:
        @staticmethod
        def devicePixelRatio() -> float:
            return 2.5

    class FakeWidget:
        @staticmethod
        def screen():
            return FakeScreen()

    assert dpi.device_pixel_ratio(FakeWidget()) == 2.5
