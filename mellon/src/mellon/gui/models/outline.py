"""Outline model: hierarchical tree of the current document's outline.

Mirrors `Document.outline` one-to-one. Each item stores its object id in
`UserRole + 1`. Heading rows (objects with `type == "Heading"`) are labeled with
a dotted chapter number derived from the outline tree (e.g. "1", "1.2"); other
rows are labeled `<ref_id>: <first-line-of-description>`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication

from ...schema import Document, OutlineNode, RequirementObject
from ..controller import RepoController


OBJ_ID_ROLE = Qt.ItemDataRole.UserRole + 1


def _is_heading(ref_id: str, objects: dict[str, RequirementObject]) -> bool:
    obj = objects.get(ref_id)
    return obj is not None and obj.attrs.get("type") == "Heading"


def _chapter_numbers(
    outline: tuple[OutlineNode, ...],
    objects: dict[str, RequirementObject],
) -> dict[str, str]:
    """Map each heading ref_id in `outline` to its dotted chapter number.

    Only heading nodes consume a counter at each level. Non-heading nodes are
    transparent: their children are numbered using the surrounding heading
    prefix.
    """
    numbers: dict[str, str] = {}

    def walk(nodes: tuple[OutlineNode, ...], prefix: tuple[int, ...]) -> None:
        idx = 0
        for node in nodes:
            if _is_heading(node.ref_id, objects):
                idx += 1
                number = prefix + (idx,)
                numbers[node.ref_id] = ".".join(str(n) for n in number)
                walk(node.children, number)
            else:
                walk(node.children, prefix)

    walk(outline, ())
    return numbers


class OutlineModel(QStandardItemModel):
    def __init__(self, controller: RepoController) -> None:
        super().__init__()
        self._controller = controller
        self._chapter_map: dict[str, str] = {}
        self.setHorizontalHeaderLabels(["Outline"])
        controller.document_selected.connect(self._rebuild)

    def _rebuild(self, doc_id: str) -> None:
        self.removeRows(0, self.rowCount())
        doc = self._controller.document(doc_id)
        self._chapter_map = _chapter_numbers(doc.outline, doc.objects)
        for node in doc.outline:
            self.appendRow(self._item_for(node, doc))

    def _item_for(self, node: OutlineNode, doc: Document) -> QStandardItem:
        obj = doc.objects.get(node.ref_id)
        chapter_no = self._chapter_map.get(node.ref_id)
        if chapter_no is not None:
            title = obj.description.strip() if obj and obj.description else ""
            label = f"{chapter_no}  {title}" if title else chapter_no
            item = QStandardItem(label)
            # QStandardItem.font() on a fresh item returns a default QFont
            # with pointSize=-1, which trips Qt's setPointSize warning when
            # the view repaints. Inherit from the application font instead.
            font = QFont(QApplication.font())
            font.setBold(True)
            item.setFont(font)
        else:
            first_line = ""
            if obj and obj.description:
                first_line = obj.description.splitlines()[0]
            label = f"{node.ref_id}" + (f": {first_line}" if first_line else "")
            item = QStandardItem(label)
        item.setData(node.ref_id, OBJ_ID_ROLE)
        item.setEditable(False)
        for child in node.children:
            item.appendRow(self._item_for(child, doc))
        return item

    @staticmethod
    def obj_id_from_index(index) -> str | None:
        if not index.isValid():
            return None
        return index.data(OBJ_ID_ROLE)
