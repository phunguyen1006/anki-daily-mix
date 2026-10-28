# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""The Daily Mix dashboard window.

The window is a thin Qt shell around an ``AnkiWebView``.  Python owns the data
and the collection mutations; the web assets only render a payload and forward
button presses over Anki's ``pycmd`` bridge.  This keeps the UI responsive and
lets the dashboard follow Anki's light/dark theme automatically.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from aqt import mw
from aqt.qt import QDialog, QTimer, QVBoxLayout, Qt, qconnect
from aqt.utils import restoreGeom, saveGeom, showWarning, tooltip
from aqt.webview import AnkiWebView

from . import constants
from .config import load_config
from .engine import (
    DailyMixError,
    cleanup_mix_deck,
    get_dashboard_data,
    get_mix_deck_status,
    start_session,
)

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_TEMPLATE: Optional[str] = None


# ---------------------------------------------------------------------------
# Web asset helpers
# ---------------------------------------------------------------------------


def addon_package() -> str:
    """Return the add-on folder name used by Anki's media server."""
    try:
        return mw.addonManager.addonFromModule(__name__)
    except Exception:
        # ``__name__`` is ``<addon>.src.ui_dashboard`` in a normal install.
        return __name__.split(".", 1)[0]


def asset_url(relative_path: str) -> str:
    # Quote the package name as well: some Anki installations use folder names
    # with spaces or other characters that are not URL-safe.
    package = quote(addon_package(), safe="")
    path = quote(relative_path.lstrip("/"), safe="/")
    return "/_addons/%s/%s" % (package, path)


def _read_template() -> str:
    global _TEMPLATE
    if _TEMPLATE is None:
        path = _WEB_DIR / "index.html"
        try:
            _TEMPLATE = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                "Daily Mix is missing its web assets (%s). Reinstall the add-on."
                % path
            ) from exc
        if "__DAILY_MIX_DATA__" not in _TEMPLATE:
            raise RuntimeError(
                "Daily Mix's dashboard template is damaged. Reinstall the add-on."
            )
    return _TEMPLATE


def _json_for_js(data: dict) -> str:
    """Serialise compact ASCII JSON that is safe inside a script tag."""
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def _error_payload(message: str) -> dict:
    """A complete payload for the rare case where collection access fails."""
    return {
        "date_label": datetime.now().strftime("%A, %B %d"),
        "target": 0,
        "done": 0,
        "remaining": 0,
        "percent": 0.0,
        "quota_reached": False,
        "review_percent": constants.DEFAULT_REVIEW_PERCENT,
        "review_target": 0,
        "new_target": 0,
        "planned_review": 0,
        "planned_new": 0,
        "planned_total": 0,
        "available_review": 0,
        "available_new": 0,
        "total_available": 0,
        "can_start": False,
        "status": "error",
        "error": message,
        "order_strategy": constants.DEFAULT_ORDER_STRATEGY,
        "order_label": constants.ORDER_LABELS[constants.DEFAULT_ORDER_STRATEGY],
        "uses_all_decks": True,
        "deck_count": 0,
        "decks": [],
        "mix_deck": {"name": constants.MIX_DECK_NAME, "exists": False, "card_count": 0},
        "reset_seconds": 0,
        "review_percent_label": constants.DEFAULT_REVIEW_PERCENT,
        "cleanup_on_close": True,
        "shortcut": constants.DEFAULT_SHORTCUT,
        "version": constants.ADDON_VERSION,
    }


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class DailyMixDashboard(QDialog):
    """Modeless dashboard. Reused after closing so it opens instantly."""

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent or mw)
        self.mw = mw
        self._page_loaded = False
        self._closing = False
        self._data: dict = {}

        self.setWindowTitle(constants.ADDON_NAME)
        self.setObjectName("dailyMixDashboard")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(480, 600)
        self.resize(820, 820)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.web = AnkiWebView(self, title=constants.ADDON_NAME)
        self.web.set_bridge_command(self._on_bridge, self)
        layout.addWidget(self.web)

        self._timer = QTimer(self)
        self._timer.setInterval(15000)
        qconnect(self._timer.timeout, self.refresh)

        restoreGeom(self, "dailyMixDashboard")
        self.refresh(initial=True)

    # -- lifecycle ----------------------------------------------------------

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._closing = False
        self._timer.start()
        self.refresh()
        self.web.setFocus()

    def hideEvent(self, event: Any) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event: Any) -> None:
        self._timer.stop()
        try:
            saveGeom(self, "dailyMixDashboard")
        except Exception:
            logger.debug("Could not save dashboard geometry")
        super().closeEvent(event)

    def prepare_for_close(self) -> None:
        """Permanently tear down the dialog and its web view."""
        self._closing = True
        self._timer.stop()
        try:
            saveGeom(self, "dailyMixDashboard")
        except Exception:
            logger.debug("Could not save dashboard geometry")
        try:
            self.web.cleanup()
        except Exception:
            logger.debug("Could not clean up the dashboard web view", exc_info=True)

    # -- rendering ----------------------------------------------------------

    def _body(self, data: dict) -> str:
        template = _read_template()
        return template.replace("__DAILY_MIX_DATA__", _json_for_js(data))

    def refresh(self, initial: bool = False) -> None:
        if self._closing or self.mw is None or self.mw.col is None:
            return

        try:
            data = get_dashboard_data(self.mw.col, load_config())
        except Exception:
            logger.exception("Could not assemble Daily Mix dashboard data")
            data = _error_payload(
                "Daily Mix could not read your collection. Close this window and "
                "open it again."
            )

        self._data = data
        if initial or not self._page_loaded:
            self._page_loaded = True
            self.web.stdHtml(
                self._body(data),
                css=[asset_url("web/style.css")],
                js=[asset_url("web/script.js")],
                context=self,
            )
        else:
            payload = _json_for_js(data)
            self.web.eval(
                "window.dailyMix && window.dailyMix.render(%s);" % payload
            )

    # -- bridge commands ----------------------------------------------------

    def _on_bridge(self, command: str) -> None:
        if command == constants.CMD_START:
            self._start_mix()
        elif command == constants.CMD_SETTINGS:
            self._open_settings()
        elif command == constants.CMD_REFRESH:
            self.refresh()
        elif command == constants.CMD_CLEANUP:
            self._cleanup()
        elif command == constants.CMD_CLOSE:
            self.close()

    def _open_settings(self) -> None:
        # Imported lazily so the dashboard and settings modules stay independent
        # of the root add-on entry point.
        from .ui_settings import open_settings

        open_settings(self)
        self.refresh()

    def _reviewing_mix_card(self) -> bool:
        """True when the reviewer is currently showing a Daily Mix card.

        Emptying or rebuilding the mix deck underneath the reviewer would
        invalidate the card on screen, so both actions wait until the user
        leaves the session.
        """
        if self.mw is None or self.mw.col is None or self.mw.state != "review":
            return False
        try:
            status = get_mix_deck_status(self.mw.col, load_config())
        except Exception:
            logger.debug("Could not inspect the mix deck during review", exc_info=True)
            return False
        if status.id is None:
            return False
        card = getattr(getattr(self.mw, "reviewer", None), "card", None)
        if card is None:
            return False
        try:
            return int(getattr(card, "did", 0)) == status.id
        except (TypeError, ValueError):
            return False

    def _cleanup(self) -> None:
        if self.mw is None or self.mw.col is None:
            return
        if self._reviewing_mix_card():
            showWarning(
                "Finish the current card or leave the reviewer before returning "
                "Daily Mix cards to their home decks.",
                parent=self,
            )
            return
        returned = cleanup_mix_deck(self.mw.col, load_config())
        self.refresh()
        if returned:
            tooltip(
                "Returned %d card(s) to their home decks." % returned,
                parent=self,
            )
        else:
            tooltip("There are no mix cards to return.", parent=self)

    def _start_mix(self) -> None:
        if self.mw is None or self.mw.col is None:
            return

        if self._reviewing_mix_card():
            showWarning(
                "Finish the current card or leave the reviewer before rebuilding "
                "today's mix.",
                parent=self,
            )
            return

        self.web.eval("window.dailyMix && window.dailyMix.setBusy(true);")
        try:
            self.mw.progress.start(
                label="Building your Daily Mix…", immediate=True, parent=self
            )
            try:
                result = start_session(self.mw.col, load_config())
            finally:
                self.mw.progress.finish()
        except DailyMixError as exc:
            self.web.eval("window.dailyMix && window.dailyMix.setBusy(false);")
            showWarning(str(exc), parent=self)
            self.refresh()
            return
        except Exception:
            logger.exception("Unexpected failure while starting a Daily Mix")
            self.web.eval("window.dailyMix && window.dailyMix.setBusy(false);")
            showWarning(
                "Daily Mix hit an unexpected error. Open Anki's console for "
                "details, then try again.",
                parent=self,
            )
            self.refresh()
            return

        if result.card_count <= 0:
            self.web.eval("window.dailyMix && window.dailyMix.setBusy(false);")
            showWarning(
                "Anki created the Daily Mix deck but it is empty. Try running "
                "Check Database, then start the mix again.",
                parent=self,
            )
            self.refresh()
            return

        self.close()

        # Leaving the reviewer first guarantees that Anki builds fresh study
        # queues for the filtered deck instead of reusing the previous deck's.
        if self.mw.state == "review":
            self.mw.moveToState("deckBrowser")
        self.mw.col.decks.select(result.deck_id)
        self.mw.moveToState("review")
        tooltip(
            "Daily Mix: %d card(s) from %d deck(s)."
            % (result.card_count, len(result.plan.sources)),
            parent=self.mw,
        )
