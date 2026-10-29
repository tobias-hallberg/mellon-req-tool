"""Tests for the `QSettings` factory (`gui/settings.py`)."""

from __future__ import annotations

import os
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_make_settings_honours_the_default_format(qtbot):
    """`QSettings(org, app)` ignores `setDefaultFormat` and always uses the
    native store (the registry on Windows), so the test isolation's INI
    redirect did nothing and every test run wrote to the real user store."""
    from PySide6.QtCore import QSettings

    from mellon.gui import settings as gui_settings

    s = gui_settings.make_settings()

    # conftest's autouse fixture set IniFormat + a temp folder for this test.
    assert QSettings.defaultFormat() == QSettings.Format.IniFormat
    assert s.format() == QSettings.Format.IniFormat
    assert s.scope() == QSettings.Scope.UserScope
    assert (s.organizationName(), s.applicationName()) == (
        gui_settings.ORG,
        gui_settings.APP,
    )
    s.setValue("probe", "x")
    s.sync()
    assert s.status() == QSettings.Status.NoError
    assert Path(s.fileName()).is_file()
