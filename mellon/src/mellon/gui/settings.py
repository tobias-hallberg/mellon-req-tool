"""Centralised `QSettings` factory.

Every GUI module that persists a user preference (theme, hidden columns,
…) goes through `make_settings()`. Production reads and writes the
per-user "mellon / mellon" group; tests monkey-patch `ORG` / `APP` to
a per-test unique suffix so Qt's internal settings cache can't leak state
across tests even when the redirected INI path changes.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings


ORG = "mellon"
APP = "mellon"


def make_settings() -> QSettings:
    # `QSettings(ORG, APP)` would always use the native store (the registry
    # on Windows), ignoring `setDefaultFormat`; this honours it, so tests can
    # redirect to INI files. In production the default is the native store.
    return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, ORG, APP)
