"""Right-pane container: tabbed content.

Hosts a `QTabWidget` for the per-tab content. Show/hide is driven from the
main window's toolbar pane-toggle buttons; this widget itself is a thin
shell.

Tabs (in order):
    0. Inspector — attributes, description, links for the selected object.
    1. History   — git log for the selected object's file.
"""

from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from ..controller import RepoController
from .history_tab import HistoryTab
from .inspector import Inspector


class RightPane(QWidget):
    def __init__(self, controller: RepoController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Mirror the grid pane: child labels' minimumSizeHint would otherwise
        # pin this pane wider than the user can drag it.
        self.setMinimumWidth(120)

        self.inspector = Inspector(controller, self)
        self.history = HistoryTab(controller, self)

        self._tabs = QTabWidget()
        self._tabs.addTab(self.inspector, "Inspector")
        self._tabs.addTab(self.history, "History")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._tabs)

    # --- read-only forwarding ------------------------------------------

    def set_read_only(self, on: bool) -> None:
        """Forward read-only state to the Inspector tab.

        The History tab is always read-only by nature, so it doesn't need
        the flag.
        """
        self.inspector.set_read_only(on)
