# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Native settings dialog for the Daily Mix add-on.

The dialog edits the same ``config.json`` values that can be changed by hand.
All validation and clamping is delegated to :mod:`src.config` so the engine and
the UI can never disagree about what a valid configuration looks like.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequence,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    Qt,
    qconnect,
)
from aqt.theme import theme_manager
from aqt.utils import restoreGeom, saveGeom, showWarning

from . import constants
from .config import load_config, save_config

logger = logging.getLogger(__name__)

_HINT_COLORS = {"light": "#5b6172", "dark": "#a3aabd"}

_ORDER_HINTS = {
    constants.ORDER_INTERLEAVED: (
        "Cards are shuffled between decks so the session alternates between "
        "subjects instead of finishing one deck before starting the next."
    ),
    constants.ORDER_SEQUENTIAL: (
        "Card positions are assigned deck by deck, in the order shown under "
        "Decks. Anki may still intersperse new cards among reviews."
    ),
}

#: ``QKeySequence.toString()`` moved into a nested enum in Qt 6.
_NATIVE_TEXT = QKeySequence.SequenceFormat.NativeText


# ---------------------------------------------------------------------------
# Callback registration
# ---------------------------------------------------------------------------

_saved_callback: Optional[Callable[[dict], None]] = None


def set_saved_callback(callback: Optional[Callable[[dict], None]]) -> None:
    """Register a function called after the dialog saves successfully.

    The entry point uses this to apply shortcut/toolbar changes and to refresh
    an open dashboard without introducing a circular import.
    """
    global _saved_callback
    _saved_callback = callback


def _hint_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(
        "color: %s; font-size: 12px;" % theme_manager.var(_HINT_COLORS)
    )
    return label


def _small_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class SettingsDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent or mw)
        self.mw = mw
        self._config = load_config()
        self.saved_config: Optional[dict] = None
        self._loading = True
        self._tree_items: dict[str, QTreeWidgetItem] = {}
        self._weight_spins: dict[str, QDoubleSpinBox] = {}

        self.setWindowTitle("Daily Mix Settings")
        self.setObjectName("dailyMixSettings")
        self.setSizeGripEnabled(True)
        self.setMinimumSize(500, 420)
        self.resize(600, 560)

        self._build_ui()
        self._load_values()
        self._loading = False
        self._sync_selection_mode()
        restoreGeom(self, "dailyMixSettings")
        self._clamp_to_screen()

    def _clamp_to_screen(self) -> None:
        screen = self.screen() or (self.mw.screen() if self.mw else None)
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(self.width(), max(500, avail.width() - 40))
            h = min(self.height(), max(420, avail.height() - 60))
            if h > 800:
                h = 750
            if self.width() != w or self.height() != h:
                self.resize(w, h)
            geo = self.geometry()
            x = max(avail.left() + 10, min(geo.x(), avail.right() - geo.width() - 10))
            y = max(avail.top() + 10, min(geo.y(), avail.bottom() - geo.height() - 10))
            self.move(x, y)

    # -- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        intro = QLabel(
            "Choose how much to study each day and which decks feed the mix. "
            "Changes apply the next time you start a session."
        )
        intro.setWordWrap(True)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(14, 14, 14, 12)
        outer_layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        body_layout = QVBoxLayout(container)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(12)
        body_layout.addWidget(intro)
        body_layout.addWidget(self._build_quota_group())
        body_layout.addWidget(self._build_deck_group(), 1)
        body_layout.addWidget(self._build_session_group())

        scroll.setWidget(container)
        outer_layout.addWidget(scroll, 1)
        outer_layout.addLayout(self._build_buttons())
        self._refresh_spin_states()

    def _build_quota_group(self) -> QWidget:
        group = QGroupBox("Daily quota")
        form = QFormLayout(group)
        form.setContentsMargins(14, 16, 14, 12)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.target_spin = QSpinBox()
        self.target_spin.setRange(constants.MIN_DAILY_TARGET, constants.MAX_DAILY_TARGET)
        self.target_spin.setSingleStep(10)
        self.target_spin.setSuffix(" cards")
        self.target_spin.setToolTip(
            "Cards answered per day, including cards studied outside the mix."
        )
        form.addRow("Daily target", self.target_spin)

        self.review_spin = QSpinBox()
        self.review_spin.setRange(0, 100)
        self.review_spin.setSingleStep(5)
        self.review_spin.setSuffix("%")
        self.review_spin.setToolTip("Share of the remaining quota reserved for reviews.")
        self.new_label = QLabel("20% new")
        ratio_row = QWidget()
        ratio_layout = QHBoxLayout(ratio_row)
        ratio_layout.setContentsMargins(0, 0, 0, 0)
        ratio_layout.setSpacing(10)
        ratio_layout.addWidget(self.review_spin)
        ratio_layout.addWidget(self.new_label, 1)
        form.addRow("Review / new", ratio_row)
        qconnect(
            self.review_spin.valueChanged,
            lambda value: self.new_label.setText(f"{100 - int(value)}% new"),
        )

        self.order_combo = QComboBox()
        for value in constants.ORDER_STRATEGIES:
            self.order_combo.addItem(constants.ORDER_LABELS[value], value)
        form.addRow("Order", self.order_combo)

        self.order_hint = _hint_label()
        form.addRow(self.order_hint)
        qconnect(self.order_combo.currentIndexChanged, lambda _: self._update_order_hint())

        return group

    def _build_deck_group(self) -> QWidget:
        group = QGroupBox("Decks")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 16, 14, 12)
        layout.setSpacing(8)

        self.all_radio = QRadioButton("All decks")
        self.selected_radio = QRadioButton("Choose decks")
        self.all_radio.setToolTip(
            "Include every deck. New decks are picked up automatically."
        )
        self.selected_radio.setToolTip("Include only the decks checked below.")
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(self.all_radio)
        mode_row.addWidget(self.selected_radio)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.deck_mode_hint = _hint_label()
        layout.addWidget(self.deck_mode_hint)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter decks…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setToolTip("Type to narrow down the deck list.")
        layout.addWidget(self.filter_edit)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Deck", "Weight"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(160)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 100)
        layout.addWidget(self.tree, 1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        self.select_all_button = _small_button("Select all")
        self.select_none_button = _small_button("Select none")
        self.reset_weights_button = _small_button("Reset weights")
        button_row.addWidget(self.select_all_button)
        button_row.addWidget(self.select_none_button)
        button_row.addWidget(self.reset_weights_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.include_subdecks = QCheckBox("Include subdecks when a parent is selected")
        self.include_subdecks.setToolTip(
            "A separately selected subdeck keeps its own weight and is resolved first."
        )
        layout.addWidget(self.include_subdecks)
        self.subdeck_hint = _hint_label()
        layout.addWidget(self.subdeck_hint)

        qconnect(self.filter_edit.textChanged, self._apply_filter)
        qconnect(self.all_radio.toggled, lambda _: self._sync_selection_mode())
        qconnect(self.selected_radio.toggled, lambda _: self._sync_selection_mode())
        qconnect(self.tree.itemChanged, self._on_item_changed)
        qconnect(self.select_all_button.clicked, self._select_all)
        qconnect(self.select_none_button.clicked, self._select_none)
        qconnect(self.reset_weights_button.clicked, self._reset_weights)
        qconnect(
            self.include_subdecks.toggled, lambda _: self._update_subdeck_hint()
        )

        self._build_deck_tree()
        return group

    def _build_session_group(self) -> QWidget:
        group = QGroupBox("Session and cleanup")
        form = QFormLayout(group)
        form.setContentsMargins(14, 16, 14, 12)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(80)
        self.name_edit.setToolTip(
            "Name of the temporary filtered deck. Use a different name if a normal "
            "deck already owns this one."
        )
        form.addRow("Mix deck name", self.name_edit)

        self.shortcut_edit = QKeySequenceEdit()
        self.shortcut_edit.setToolTip("Shortcut that opens the Daily Mix dashboard.")
        self.clear_shortcut_button = _small_button("Clear")
        self.clear_shortcut_button.setToolTip("Disable the keyboard shortcut.")
        shortcut_row = QWidget()
        shortcut_layout = QHBoxLayout(shortcut_row)
        shortcut_layout.setContentsMargins(0, 0, 0, 0)
        shortcut_layout.setSpacing(8)
        shortcut_layout.addWidget(self.shortcut_edit, 1)
        shortcut_layout.addWidget(self.clear_shortcut_button)
        form.addRow("Shortcut", shortcut_row)
        form.addRow(
            _hint_label(
                "Leave the shortcut empty to disable it. The dashboard also opens "
                "from Tools and from the toolbar."
            )
        )

        self.backfill_check = QCheckBox(
            "Top up from the other category when one side runs short"
        )
        self.cleanup_start_check = QCheckBox(
            "Return stale mix cards when Anki opens"
        )
        self.cleanup_close_check = QCheckBox(
            "Return remaining mix cards when Anki closes"
        )
        self.toolbar_check = QCheckBox("Show Daily Mix in the toolbar")
        form.addRow(self.backfill_check)
        form.addRow(self.cleanup_start_check)
        form.addRow(self.cleanup_close_check)
        form.addRow(self.toolbar_check)

        qconnect(self.clear_shortcut_button.clicked, self.shortcut_edit.clear)
        return group

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.cancel_button = _small_button("Cancel")
        self.save_button = _small_button("Save")
        self.save_button.setDefault(True)
        self.save_button.setAutoDefault(True)
        row.addWidget(self.cancel_button)
        row.addWidget(self.save_button)
        qconnect(self.cancel_button.clicked, self.reject)
        qconnect(self.save_button.clicked, self._save)
        return row

    # -- deck tree ----------------------------------------------------------

    def _build_deck_tree(self) -> None:
        self.tree.clear()
        self._tree_items.clear()
        self._weight_spins.clear()
        if self.mw is None or self.mw.col is None:
            return

        self._loading = True
        try:
            entries = sorted(
                self.mw.col.decks.all_names_and_ids(include_filtered=False),
                key=lambda entry: entry.name,
            )
            for entry in entries:
                name = entry.name
                parent_name = name.rsplit("::", 1)[0] if "::" in name else None
                parent_item = self._tree_items.get(parent_name or "")
                if parent_item is not None:
                    item = QTreeWidgetItem(parent_item)
                else:
                    item = QTreeWidgetItem(self.tree)

                item.setText(0, name.rsplit("::", 1)[-1])
                item.setToolTip(0, name)
                item.setData(0, Qt.ItemDataRole.UserRole, name)
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                )
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._tree_items[name] = item

                spin = QDoubleSpinBox()
                spin.setRange(constants.MIN_DECK_WEIGHT, constants.MAX_DECK_WEIGHT)
                spin.setDecimals(1)
                spin.setSingleStep(0.1)
                spin.setValue(1.0)
                spin.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
                spin.setFixedWidth(88)
                spin.setToolTip(
                    "Relative share of the quota. 2.0 receives twice as many cards "
                    "as 1.0 when both decks have enough cards."
                )
                self.tree.setItemWidget(item, 1, spin)
                self._weight_spins[name] = spin

            self.tree.expandAll()
        finally:
            self._loading = False

    def _load_values(self) -> None:
        cfg = self._config
        self.target_spin.setValue(int(cfg["daily_target"]))
        self.review_spin.setValue(int(cfg["review_percent"]))
        self.new_label.setText(f"{100 - int(cfg['review_percent'])}% new")

        index = self.order_combo.findData(cfg["order_strategy"])
        self.order_combo.setCurrentIndex(index if index >= 0 else 0)
        self._update_order_hint()

        self.name_edit.setText(str(cfg["mix_deck_name"]))
        self.shortcut_edit.setKeySequence(QKeySequence(str(cfg["shortcut"])))
        self.include_subdecks.setChecked(bool(cfg["include_subdecks"]))
        self.backfill_check.setChecked(bool(cfg["backfill"]))
        self.cleanup_start_check.setChecked(bool(cfg["cleanup_on_start"]))
        self.cleanup_close_check.setChecked(bool(cfg["cleanup_on_close"]))
        self.toolbar_check.setChecked(bool(cfg["show_toolbar_button"]))

        configured = {
            str(entry["name"]): float(entry["weight"])
            for entry in cfg["decks"]
            if entry.get("name")
        }
        has_selection = bool(configured)
        self.selected_radio.setChecked(has_selection)
        self.all_radio.setChecked(not has_selection)

        self._loading = True
        try:
            for name, item in self._tree_items.items():
                if name in configured:
                    item.setCheckState(0, Qt.CheckState.Checked)
                    self._weight_spins[name].setValue(configured[name])
        finally:
            self._loading = False

        if not self._tree_items:
            self.selected_radio.setEnabled(False)
            self.all_radio.setChecked(True)
        self._update_subdeck_hint()
        self._refresh_spin_states()

    # -- dynamic hints / state ---------------------------------------------

    def _update_order_hint(self) -> None:
        value = self.order_combo.currentData()
        self.order_hint.setText(_ORDER_HINTS.get(value, ""))

    def _update_subdeck_hint(self) -> None:
        if self.include_subdecks.isChecked():
            self.subdeck_hint.setText(
                "A parent deck covers its whole subtree. Selecting a subdeck as "
                "well gives that subdeck its own weight and takes it out of the "
                "parent's share."
            )
        else:
            self.subdeck_hint.setText(
                "Only the exact decks you check are included. Subdecks keep their "
                "own cards and can be selected independently."
            )

    def _sync_selection_mode(self) -> None:
        choosing = self.selected_radio.isChecked()
        for widget in (
            self.tree,
            self.filter_edit,
            self.include_subdecks,
            self.select_all_button,
            self.select_none_button,
            self.reset_weights_button,
        ):
            widget.setEnabled(choosing)

        if not self._tree_items:
            self.deck_mode_hint.setText(
                "This collection has no normal decks yet. Create a deck in Anki "
                "first."
            )
        elif choosing:
            self.deck_mode_hint.setText(
                "Check the decks to include. Weights control how much of the "
                "daily quota each deck receives."
            )
        else:
            self.deck_mode_hint.setText(
                "Every current deck is included and weighted by how much work it "
                "has available. New decks are picked up automatically."
            )
        self._refresh_spin_states()

    def _refresh_spin_states(self) -> None:
        choosing = self.selected_radio.isChecked()
        for name, item in self._tree_items.items():
            spin = self._weight_spins.get(name)
            if spin is not None:
                spin.setEnabled(
                    choosing and item.checkState(0) == Qt.CheckState.Checked
                )

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._loading or column != 0:
            return
        self._refresh_spin_states()

    def _select_all(self) -> None:
        self._loading = True
        try:
            for name, item in self._tree_items.items():
                # With subdecks enabled, checking every top-level deck already
                # covers the whole collection; checking children too would split
                # the weights in an unexpected way.
                if self.include_subdecks.isChecked() and "::" in name:
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                else:
                    item.setCheckState(0, Qt.CheckState.Checked)
        finally:
            self._loading = False
        self._refresh_spin_states()

    def _select_none(self) -> None:
        self._loading = True
        try:
            for item in self._tree_items.values():
                item.setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            self._loading = False
        self._refresh_spin_states()

    def _reset_weights(self) -> None:
        for spin in self._weight_spins.values():
            spin.setValue(1.0)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            self._filter_item(self.tree.topLevelItem(index), needle)

    def _filter_item(self, item: QTreeWidgetItem, needle: str) -> bool:
        name = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
        child_visible = False
        for index in range(item.childCount()):
            if self._filter_item(item.child(index), needle):
                child_visible = True
        visible = not needle or needle in name or child_visible
        item.setHidden(not visible)
        if needle and visible:
            item.setExpanded(True)
        return visible

    # -- saving -------------------------------------------------------------

    def _save(self) -> None:
        choosing = self.selected_radio.isChecked()
        decks: list[dict] = []
        if choosing:
            for name, item in self._tree_items.items():
                if item.checkState(0) != Qt.CheckState.Checked:
                    continue
                spin = self._weight_spins[name]
                decks.append(
                    {"name": name, "weight": round(float(spin.value()), 2)}
                )
            if not decks:
                showWarning(
                    "Select at least one deck, or choose “All decks”.",
                    parent=self,
                )
                return

        sequence = self.shortcut_edit.keySequence()
        shortcut = "" if sequence.isEmpty() else sequence.toString(_NATIVE_TEXT).strip()
        name = self.name_edit.text().strip() or constants.MIX_DECK_NAME

        updated = dict(self._config)
        updated.update(
            {
                "daily_target": int(self.target_spin.value()),
                "review_percent": int(self.review_spin.value()),
                "order_strategy": self.order_combo.currentData(),
                "decks": decks,
                "include_subdecks": self.include_subdecks.isChecked(),
                "backfill": self.backfill_check.isChecked(),
                "cleanup_on_start": self.cleanup_start_check.isChecked(),
                "cleanup_on_close": self.cleanup_close_check.isChecked(),
                "mix_deck_name": name,
                "show_toolbar_button": self.toolbar_check.isChecked(),
                "shortcut": shortcut,
            }
        )

        try:
            self.saved_config = save_config(updated)
        except Exception as exc:
            logger.exception("Could not save Daily Mix settings")
            showWarning(
                "Daily Mix could not save the settings:\n\n%s" % exc,
                parent=self,
            )
            return

        self.accept()

    def done(self, result: int) -> None:  # type: ignore[override]
        try:
            saveGeom(self, "dailyMixSettings")
        except Exception:
            logger.debug("Could not save settings window geometry")
        super().done(result)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def open_settings(parent: Optional[QWidget] = None) -> bool:
    """Show the modal settings dialog and return whether it was saved."""
    if mw is None or mw.col is None:
        showWarning("Open an Anki profile before changing Daily Mix settings.")
        return False

    dialog = SettingsDialog(parent if isinstance(parent, QWidget) else mw)
    if not dialog.exec():
        return False

    if dialog.saved_config is not None and _saved_callback is not None:
        try:
            _saved_callback(dialog.saved_config)
        except Exception:
            logger.exception("Daily Mix post-save callback failed")

    return True
