"""Theme tokens and a `ThemeManager` singleton.

Token palettes lifted verbatim from the `:root` blocks of the UI design
reference. Two variants: dark (default) and light. Themes apply a
`QPalette` to the live `QApplication` plus a small `QSS` for widgets palette
doesn't cover (splitter handles, alternating rows, suspect banner).

Theme choices persist via `QSettings("mellon", "mellon")`. "Follow system"
resolves at apply-time from `QApplication.styleHints().colorScheme()`.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QPalette
from PySide6.QtWidgets import QApplication, QMenu

from . import settings as _settings_mod


ThemeName = Literal["dark", "light", "system"]
ResolvedTheme = Literal["dark", "light"]


# --- Tokens ---------------------------------------------------------------


TOKENS: dict[ResolvedTheme, dict[str, str]] = {
    "dark": {
        "bg_0": "#15181c",
        "bg_1": "#1a1e23",
        "bg_2": "#20252b",
        "bg_3": "#272d34",
        "bg_hover": "#2a3139",
        "bg_selected": "#1e3a44",
        "bg_selected_strong": "#245662",
        "border_0": "#0d1014",
        "border_1": "#2a3139",
        "border_2": "#363e47",
        "fg_0": "#e6e8ea",
        "fg_1": "#b4b8bd",
        "fg_2": "#7f858c",
        "fg_3": "#555b63",
        "accent": "#4fb3b8",
        "accent_dim": "#2d6e72",
        "accent_bg": "#1b3638",
        "warn_bg": "#4a380f",
        "info_bg": "#0f2d4a",
        "ok": "#8bc48a",
        "warn": "#e0b86a",
        "err": "#e06c6c",
        "purple": "#b48ee0",
        "blue": "#6aa9e0",
        "suspect_mark": "#e0b86a",
    },
    "light": {
        "bg_0": "#e4e6e8",
        "bg_1": "#f3f4f6",
        "bg_2": "#fafbfc",
        "bg_3": "#eceef1",
        "bg_hover": "#e2e5e9",
        "bg_selected": "#d7ebee",
        "bg_selected_strong": "#bcdbe0",
        "border_0": "#c2c7cd",
        "border_1": "#d8dce1",
        "border_2": "#ccd1d7",
        "fg_0": "#1c2024",
        "fg_1": "#4a5058",
        "fg_2": "#6c7279",
        "fg_3": "#9ba1a8",
        "accent": "#1e8085",
        "accent_dim": "#4fb3b8",
        "accent_bg": "#d7ebee",
        "warn_bg": "#fef3cd",
        "info_bg": "#cfe1f5",
        "ok": "#3a8b3d",
        "warn": "#a6751a",
        "err": "#b23939",
        "purple": "#6b4aa0",
        "blue": "#2a6bb8",
        "suspect_mark": "#c98e26",
    },
}


# --- Resolution -----------------------------------------------------------


def resolve(choice: ThemeName) -> ResolvedTheme:
    """Collapse `"system"` to an actual `"dark"` or `"light"` palette."""
    if choice in ("dark", "light"):
        return choice  # type: ignore[return-value]
    app = QApplication.instance()
    try:
        scheme = app.styleHints().colorScheme() if app is not None else None
    except AttributeError:
        scheme = None
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return "dark"


def tokens_for(choice: ThemeName) -> dict[str, str]:
    return TOKENS[resolve(choice)]


# --- Qt palette + stylesheet ---------------------------------------------


def _palette_for(tokens: dict[str, str]) -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(tokens["bg_1"]))
    p.setColor(QPalette.ColorRole.WindowText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.Base, QColor(tokens["bg_2"]))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["bg_1"]))
    p.setColor(QPalette.ColorRole.Text, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.Button, QColor(tokens["bg_3"]))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.BrightText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens["fg_2"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(tokens["bg_selected_strong"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens["bg_3"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.Mid, QColor(tokens["fg_2"]))
    p.setColor(QPalette.ColorRole.Light, QColor(tokens["bg_3"]))
    p.setColor(QPalette.ColorRole.Midlight, QColor(tokens["bg_2"]))
    p.setColor(QPalette.ColorRole.Dark, QColor(tokens["bg_0"]))
    p.setColor(QPalette.ColorRole.Shadow, QColor(tokens["border_0"]))
    p.setColor(QPalette.ColorRole.Link, QColor(tokens["accent"]))
    p.setColor(QPalette.ColorRole.LinkVisited, QColor(tokens["accent_dim"]))
    # Qt 6.6+ adds an Accent role; tolerate older Qt.
    try:
        p.setColor(QPalette.ColorRole.Accent, QColor(tokens["accent"]))
    except AttributeError:
        pass
    # Disabled variants — dim the primary roles.
    disabled = QPalette.ColorGroup.Disabled
    p.setColor(disabled, QPalette.ColorRole.Text, QColor(tokens["fg_3"]))
    p.setColor(disabled, QPalette.ColorRole.WindowText, QColor(tokens["fg_3"]))
    p.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(tokens["fg_3"]))
    return p


_QSS_TEMPLATE = """
QMainWindow, QDialog {{ background: {bg_1}; color: {fg_0}; }}
QToolBar {{
    background: {bg_1};
    border: 0;
    border-bottom: 1px solid {border_1};
    padding: 2px;
    spacing: 4px;
}}
QToolButton {{
    background: transparent;
    color: {fg_0};
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 4px 8px;
}}
QToolButton:hover {{ background: {bg_hover}; border-color: {border_2}; }}
QToolButton:pressed {{ background: {bg_selected}; }}
QToolButton:checked {{
    background: {bg_selected_strong};
    color: {fg_0};
    border-color: {accent};
}}
QToolButton#PaneToggleButton:checked {{
    background: transparent;
    border-color: transparent;
}}
QToolButton#PaneToggleButton:checked:hover {{
    background: {bg_hover};
    border-color: {border_2};
}}
QToolButton:disabled {{ color: {fg_3}; }}
QStatusBar {{
    background: {bg_0};
    color: {fg_2};
    min-height: 22px;
    font-size: 8pt;
}}
QStatusBar::item {{ border: 0; }}
QSplitter::handle {{ background: {bg_0}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}

QTreeView, QTableView, QListView {{
    background: {bg_2};
    alternate-background-color: {bg_1};
    color: {fg_0};
    gridline-color: {border_1};
    selection-background-color: {bg_selected};
    selection-color: {fg_0};
    border: 1px solid {border_1};
}}
QTableView::item {{ padding: 4px 8px; }}
QTreeView::item {{ padding: 2px 4px; }}
QHeaderView::section {{
    background: {bg_3};
    color: {fg_1};
    border: 0;
    border-right: 1px solid {border_1};
    border-bottom: 1px solid {border_1};
    padding: 6px 10px;
    font-weight: 600;
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background: {bg_selected_strong};
    color: {fg_0};
}}

QLabel#PaneHeader {{
    background: {bg_1};
    color: {fg_1};
    border-bottom: 1px solid {border_1};
    padding: 6px 10px;
    font-size: 8pt;
    font-weight: 500;
    letter-spacing: 1px;
}}

QWidget#PaneHeaderBar {{
    background: {bg_1};
    border-bottom: 1px solid {border_1};
}}
QLabel#PaneHeaderTitle {{
    background: transparent;
    color: {fg_1};
    padding: 6px 0 6px 10px;
    font-size: 8pt;
    font-weight: 500;
    letter-spacing: 1px;
}}
QTabWidget::pane {{
    background: {bg_1};
    border: 1px solid {border_1};
    top: -1px;
}}
QTabWidget::tab-bar {{
    left: 0;
}}
QTabBar {{
    background: {bg_1};
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: {bg_2};
    color: {fg_2};
    border: 1px solid {border_1};
    border-bottom: none;
    padding: 6px 14px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {bg_1};
    color: {fg_0};
}}
QTabBar::tab:hover:!selected {{
    background: {bg_hover};
    color: {fg_0};
}}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
    background: {bg_2};
    color: {fg_0};
    border: 1px solid {border_2};
    border-radius: 3px;
    padding: 3px 6px;
    selection-background-color: {bg_selected_strong};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit:read-only, QTextEdit:read-only, QPlainTextEdit:read-only,
QLineEdit:read-only:focus, QTextEdit:read-only:focus, QPlainTextEdit:read-only:focus {{
    border: 1px solid transparent;
    background: transparent;
}}

QPushButton {{
    background: {bg_3};
    color: {fg_0};
    border: 1px solid {border_2};
    border-radius: 3px;
    padding: 4px 12px;
}}
QPushButton:hover {{ background: {bg_hover}; }}
QPushButton:pressed {{ background: {bg_selected_strong}; }}
QPushButton:disabled {{ color: {fg_3}; background: {bg_2}; }}
QPushButton:default {{ border: 1px solid {accent}; }}

QMenu {{
    background: {bg_3};
    color: {fg_0};
    border: 1px solid {border_2};
}}
QMenu::item {{ padding: 4px 24px 4px 20px; }}
QMenu::item:selected {{ background: {bg_hover}; }}
QMenu::item:disabled {{ color: {fg_3}; }}
QMenu::item:selected:disabled {{ background: transparent; color: {fg_3}; }}
QMenuBar {{ background: {bg_1}; color: {fg_0}; border-bottom: 1px solid {border_1}; }}
QMenuBar::item:selected {{ background: {bg_hover}; }}
QMenuBar::item:disabled {{ color: {fg_3}; }}

QToolTip {{
    background: {bg_3};
    color: {fg_0};
    border: 1px solid {border_2};
}}

QScrollBar:vertical {{
    background: {bg_1}; width: 12px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {border_2}; min-height: 24px; border-radius: 3px; margin: 2px;
}}
QScrollBar:horizontal {{
    background: {bg_1}; height: 12px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {border_2}; min-width: 24px; border-radius: 3px; margin: 2px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ background: transparent; }}

/* Suspect banner — overridden at widget level too, but scope-safe defaults. */
QWidget#SuspectBanner {{
    background: {warn_bg};
    border: 1px solid {warn};
    border-radius: 3px;
}}
QLabel#SuspectBannerLabel {{
    background: transparent;
    border: none;
    color: {fg_0};
}}

/* Read-only banner — complementary blue companion to the suspect banner. */
QWidget#ReadOnlyBanner {{
    background: {info_bg};
    border: 1px solid {blue};
    border-radius: 3px;
}}
QLabel#ReadOnlyBannerLabel {{
    background: transparent;
    border: none;
    color: {fg_0};
}}
"""


def stylesheet_for(tokens: dict[str, str]) -> str:
    return _QSS_TEMPLATE.format(**tokens)


# --- ThemeManager singleton ----------------------------------------------


class ThemeManager(QObject):
    """Owns the current theme choice + applies it to the live QApplication.

    One instance lives on the app for the lifetime of the process. Widgets
    that paint with hardcoded colors (e.g. the suspect banner) connect to
    `theme_changed` and re-style themselves.
    """

    theme_changed = Signal(dict)  # emitted with the resolved token dict

    _instance: "ThemeManager | None" = None

    # Kept for backwards compatibility in tests that reference these names;
    # actual reads/writes go through mellon.gui.settings.make_settings().
    SETTINGS_ORG = "mellon"
    SETTINGS_APP = "mellon"
    SETTINGS_KEY = "ui/theme"

    def __init__(self) -> None:
        super().__init__()
        self._choice: ThemeName = "dark"

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._instance = None

    # --- public API ---------------------------------------------------

    def load_choice(self) -> ThemeName:
        """Read the persisted choice, defaulting to 'system' on first run.

        Following the OS theme is the friendliest default — users who prefer
        a fixed palette can lock it in via View → Theme.
        """
        settings = _settings_mod.make_settings()
        choice = settings.value(self.SETTINGS_KEY, "system")
        if choice not in ("dark", "light", "system"):
            choice = "system"
        return choice  # type: ignore[return-value]

    def current_choice(self) -> ThemeName:
        return self._choice

    def current_tokens(self) -> dict[str, str]:
        return tokens_for(self._choice)

    def apply(self, choice: ThemeName, *, persist: bool = True) -> None:
        """Apply `choice` to the live QApplication and (optionally) persist.

        Silent no-op when no QApplication exists (tests that don't construct
        one won't crash).
        """
        if choice not in ("dark", "light", "system"):
            choice = "dark"
        self._choice = choice
        if persist:
            _settings_mod.make_settings().setValue(self.SETTINGS_KEY, choice)
        app = QApplication.instance()
        tokens = tokens_for(choice)
        if app is not None:
            app.setPalette(_palette_for(tokens))
            app.setStyleSheet(stylesheet_for(tokens))
        self.theme_changed.emit(tokens)


# --- Theme menu helper ---------------------------------------------------


def build_theme_menu(parent_menu: QMenu, parent_obj: QObject) -> dict[str, QAction]:
    """Add a `&Theme` submenu under `parent_menu` and return its actions.

    Builds the canonical Dark / Light / Follow-system action triplet, wires
    each one to `ThemeManager.apply`, and pre-checks the active choice. The
    `QActionGroup` is parented to `parent_obj` so it stays alive without the
    caller having to hold a reference.
    """
    submenu = parent_menu.addMenu("&Theme")
    group = QActionGroup(parent_obj)
    group.setExclusive(True)
    actions: dict[str, QAction] = {}
    for label, choice in (
        ("&Dark", "dark"),
        ("&Light", "light"),
        ("Follow &system", "system"),
    ):
        a = QAction(label, parent_obj, checkable=True)
        a.setData(choice)
        a.triggered.connect(
            lambda _checked=False, c=choice: ThemeManager.instance().apply(c)
        )
        group.addAction(a)
        submenu.addAction(a)
        actions[choice] = a
    current = ThemeManager.instance().current_choice()
    actions.get(current, actions["dark"]).setChecked(True)
    return actions
