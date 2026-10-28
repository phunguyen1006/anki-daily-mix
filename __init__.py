# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Anki Daily Mix - a daily, deck-spanning study session.

Entry point wiring:

* a **Daily Mix** item in the Tools menu and a link in Anki's top toolbar;
* an application-level shortcut (configurable in Settings);
* Anki's Add-ons "Config" button opens the native settings dialog;
* web assets are exported so the dashboard can load its CSS and JS;
* profile hooks clean up a stale filtered deck after an abrupt close.

The actual card selection lives in :mod:`src.engine` and is intentionally free
of Qt imports so it can be unit tested.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from aqt import gui_hooks, mw
from aqt.qt import QAction, QIcon, QKeySequence, Qt
from aqt.utils import showWarning, tooltip

from .src import constants
from .src.config import load_config
from .src.engine import cleanup_mix_deck
from .src.ui_dashboard import DailyMixDashboard
from .src.ui_settings import open_settings, set_saved_callback

logger = logging.getLogger(__name__)

_dashboard: Optional[DailyMixDashboard] = None
_open_action: Optional[QAction] = None
_settings_action: Optional[QAction] = None
_toolbar_visible: Optional[bool] = None
_initialized = False
_hooks_installed = False


# ---------------------------------------------------------------------------
# Dashboard window management
# ---------------------------------------------------------------------------


def _forget_dashboard(*_args: Any) -> None:
    global _dashboard
    _dashboard = None


def _close_dashboard(delete: bool = False) -> None:
    global _dashboard
    dashboard, _dashboard = _dashboard, None
    if dashboard is None:
        return
    try:
        dashboard.prepare_for_close()
        dashboard.close()
        if delete:
            dashboard.deleteLater()
    except RuntimeError:
        # The C++ object was already destroyed by Anki.
        pass
    except Exception:
        logger.exception("Could not close the Daily Mix dashboard")


def open_dashboard() -> None:
    """Show the dashboard, reusing the existing window when possible."""
    global _dashboard
    if mw is None or mw.col is None:
        showWarning("Open an Anki profile before using Daily Mix.")
        return

    if _dashboard is not None:
        try:
            _dashboard.refresh()
            _dashboard.show()
            _dashboard.raise_()
            _dashboard.activateWindow()
            return
        except RuntimeError:
            _dashboard = None

    try:
        dashboard = DailyMixDashboard(mw)
    except Exception as exc:
        logger.exception("Could not create the Daily Mix dashboard")
        showWarning(
            "Daily Mix could not open its dashboard:\n\n%s" % exc,
            parent=mw,
        )
        return

    _dashboard = dashboard
    dashboard.destroyed.connect(_forget_dashboard)
    dashboard.show()
    dashboard.raise_()
    dashboard.activateWindow()


# ---------------------------------------------------------------------------
# Actions, menu, toolbar and shortcut
# ---------------------------------------------------------------------------


def _set_shortcut() -> None:
    if _open_action is None:
        return
    shortcut = str(load_config().get("shortcut") or "").strip()
    if shortcut:
        _open_action.setShortcut(QKeySequence(shortcut))
        _open_action.setToolTip("Open the Daily Mix dashboard (%s)" % shortcut)
        _open_action.setStatusTip(
            "Build today's mix from your selected decks (%s)" % shortcut
        )
    else:
        _open_action.setShortcut(QKeySequence())
        _open_action.setToolTip("Open the Daily Mix dashboard")
        _open_action.setStatusTip("Build today's mix from your selected decks")


def _apply_toolbar_setting(config: dict) -> None:
    global _toolbar_visible
    visible = bool(config.get("show_toolbar_button", True))
    if visible == _toolbar_visible:
        return
    _toolbar_visible = visible
    try:
        if mw is not None and getattr(mw, "toolbar", None) is not None:
            # redraw() only refreshes sync status; draw() rebuilds the links and
            # therefore re-runs the top_toolbar_did_init_links hook.
            mw.toolbar.draw()
    except Exception:
        logger.exception("Could not redraw Anki's toolbar")


def _on_toolbar_links(links: list, toolbar: Any) -> None:
    try:
        config = load_config()
    except Exception:
        logger.exception("Could not read config while drawing the toolbar")
        return
    if not config.get("show_toolbar_button", True):
        return
    links.append(
        toolbar.create_link(
            "dailyMix",
            "Daily Mix",
            open_dashboard,
            tip="Open today's Daily Mix",
            id="daily-mix-link",
        )
    )


def _build_actions() -> None:
    global _open_action, _settings_action, _initialized
    if _initialized:
        return
    _initialized = True

    _open_action = QAction(constants.ADDON_NAME, mw)
    _open_action.setObjectName("dailyMixOpenAction")
    _open_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
    _open_action.triggered.connect(open_dashboard)

    icon_path = Path(__file__).resolve().parent / "web" / "icon.svg"
    if icon_path.exists():
        _open_action.setIcon(QIcon(str(icon_path)))

    _settings_action = QAction("Daily Mix Settings…", mw)
    _settings_action.setObjectName("dailyMixSettingsAction")
    _settings_action.triggered.connect(open_settings)

    menu = getattr(getattr(mw, "form", None), "menuTools", None)
    if menu is not None:
        menu.addSeparator()
        menu.addAction(_open_action)
        menu.addAction(_settings_action)
    else:
        logger.warning("Anki's Tools menu was not available; using shortcut only")

    _set_shortcut()


def _apply_settings(config: Optional[dict] = None) -> None:
    cfg = config or load_config()
    _set_shortcut()
    _apply_toolbar_setting(cfg)


def _on_settings_saved(config: dict) -> None:
    _apply_settings(config)
    if _dashboard is not None:
        try:
            _dashboard.refresh()
        except RuntimeError:
            pass
    tooltip("Daily Mix settings saved.", parent=mw)


# ---------------------------------------------------------------------------
# Profile lifecycle
# ---------------------------------------------------------------------------


def _cleanup_stale(config: dict) -> None:
    if mw is None or mw.col is None:
        return
    try:
        returned = cleanup_mix_deck(mw.col, config)
    except Exception:
        logger.exception("Could not clean up a stale Daily Mix deck")
        return
    if returned and mw.state == "deckBrowser":
        try:
            mw.deckBrowser.refresh()
        except Exception:
            logger.debug("Could not refresh the deck browser", exc_info=True)


def _on_profile_did_open() -> None:
    _close_dashboard(delete=True)
    config = load_config()
    if config.get("cleanup_on_start", True):
        _cleanup_stale(config)
    _apply_settings(config)


def _on_profile_will_close() -> None:
    _close_dashboard(delete=True)
    if mw is None or mw.col is None:
        return
    try:
        config = load_config()
    except Exception:
        logger.exception("Could not read config while closing the profile")
        return
    if config.get("cleanup_on_close", True):
        try:
            cleanup_mix_deck(mw.col, config)
        except Exception:
            logger.exception("Could not return mix cards on profile close")


def _install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    gui_hooks.profile_did_open.append(_on_profile_did_open)
    gui_hooks.profile_will_close.append(_on_profile_will_close)
    gui_hooks.top_toolbar_did_init_links.append(_on_toolbar_links)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _setup() -> None:
    if mw is None:
        logger.warning("Daily Mix loaded without a main window; skipping setup")
        return

    # The dashboard loads its assets through Anki's media server.
    try:
        mw.addonManager.setWebExports(__name__, r"web/.*")
    except Exception:
        logger.exception("Could not export Daily Mix web assets")

    try:
        mw.addonManager.setConfigAction(__name__, open_settings)
    except Exception:
        logger.exception("Could not register the Daily Mix config action")

    _build_actions()
    _install_hooks()
    set_saved_callback(_on_settings_saved)


try:
    _setup()
except Exception:
    logger.exception("Daily Mix failed to initialize")
    try:
        showWarning(
            "Daily Mix failed to start. Open Anki's console for details, then "
            "restart Anki."
        )
    except Exception:
        pass
