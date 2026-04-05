"""
Desktop web browser whose UI and logic are written in Python (PyQt6).

The page renderer is Qt WebEngine (Chromium-based, bundled with PyQt6-WebEngine).
Sites that expect stock Chrome may look for QtWebEngine in the user agent, missing
Client Hints, or WebGPU — we configure profile + hints to better match Chrome.

Epic and similar properties may still block embedded engines; if signup fails, use
Edge or Chrome for that step.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from PyQt6.QtCore import (
    QEvent,
    QEventLoop,
    QRectF,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    QUrlQuery,
)
from PyQt6.QtGui import (
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWebEngineCore import (
    QWebEngineCookieStore,
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

# Default search engines: id -> (display name, query URL template with {q})
SEARCH_ENGINES: dict[str, tuple[str, str]] = {
    "google": ("Google", "https://www.google.com/search?q={q}"),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q={q}"),
    "bing": ("Bing", "https://www.bing.com/search?q={q}"),
    "brave": ("Brave", "https://search.brave.com/search?q={q}"),
    "ecosia": ("Ecosia", "https://www.ecosia.org/search?q={q}"),
}

ASSETS_HOME = Path(__file__).resolve().parent / "assets" / "home"

# User-visible product name (title bar, about, QSettings org key prefix)
APP_NAME = "Secret Browser"


def _chrome_version_from_ua(ua: str) -> str:
    m = re.search(r"Chrome/([\d.]+(?:\.\d+)*)", ua)
    return m.group(1) if m else "140.0.0.0"


def chrome_like_user_agent() -> str:
    """
    Qt's default UA contains 'QtWebEngine/…', which many sites treat as non‑Chrome.
    Keep the same Chromium major as the embedded engine, but use a standard Chrome UA.
    """
    raw = QWebEngineProfile.defaultProfile().httpUserAgent()
    ver = _chrome_version_from_ua(raw)
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{ver} Safari/537.36"
    )


def apply_chrome_client_hints(profile: QWebEngineProfile) -> None:
    """Align Sec-CH-UA style metadata with a typical desktop Chrome on Windows."""
    hints = profile.clientHints()
    hints.setAllClientHintsEnabled(True)
    fv = _chrome_version_from_ua(profile.httpUserAgent())
    hints.setFullVersion(fv)
    hints.setPlatform("Windows")
    hints.setPlatformVersion("10.0.0")
    hints.setArch("x86")
    hints.setBitness("64")
    hints.setModel("")
    hints.setIsMobile(False)
    hints.setIsWow64(False)
    hints.setFullVersionList(
        {
            "Google Chrome": fv,
            "Chromium": fv,
            "Not_A Brand": "24.0.0.0",
        }
    )
    if hasattr(hints, "setFormFactors"):
        hints.setFormFactors(["Desktop"])


# Chromium profile on disk (cookies, cache, site storage). Used for wipe on exit.
WEBENGINE_DATA_ROOT = Path.home() / ".python_browser_webengine"


def configure_profile(profile: QWebEngineProfile) -> None:
    WEBENGINE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    profile.setPersistentStoragePath(str(WEBENGINE_DATA_ROOT / "storage"))
    profile.setCachePath(str(WEBENGINE_DATA_ROOT / "cache"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
    )
    profile.setHttpUserAgent(chrome_like_user_agent())
    profile.setHttpAcceptLanguage("en-US,en;q=0.9")
    apply_chrome_client_hints(profile)


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def load_bookmarks_list(settings: QSettings) -> list[dict[str, str]]:
    raw = settings.value("bookmarks_json", "[]")
    if not isinstance(raw, str):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            title = str(item.get("title", url)).strip() or url
            if url.startswith(("http://", "https://")):
                out.append({"url": url, "title": title})
    return out


def save_bookmarks_list(settings: QSettings, items: list[dict[str, str]]) -> None:
    settings.setValue("bookmarks_json", json.dumps(items, ensure_ascii=False))


def bookmark_menu_label(b: dict[str, str], max_len: int = 56) -> str:
    """Label shown in the bookmark menu (title or URL, truncated)."""
    t = (b.get("title") or b.get("url") or "").strip()
    if not t:
        t = str(b.get("url", ""))
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


def clear_profile_browsing_data(profile: QWebEngineProfile) -> None:
    """
    Clear HTTP cache, cookies, visited-link hints, and on-disk storage/cache dirs.
    """
    profile.clearAllVisitedLinks()
    store = profile.cookieStore()
    if store is not None:
        store.deleteSessionCookies()
        store.deleteAllCookies()

    loop = QEventLoop()
    done = False

    def finish_cache() -> None:
        nonlocal done
        if done:
            return
        done = True
        loop.quit()

    profile.clearHttpCacheCompleted.connect(finish_cache)
    QTimer.singleShot(8000, finish_cache)
    profile.clearHttpCache()
    loop.exec()
    try:
        profile.clearHttpCacheCompleted.disconnect(finish_cache)
    except TypeError:
        pass

    flush = QEventLoop()
    QTimer.singleShot(400, flush.quit)
    flush.exec()

    storage = Path(profile.persistentStoragePath())
    cache = Path(profile.cachePath())
    _safe_rmtree(storage)
    _safe_rmtree(cache)
    WEBENGINE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)


def apply_third_party_cookie_filter(profile: QWebEngineProfile, block: bool) -> None:
    """When True, reject third-party cookies via the cookie store filter."""
    cs = profile.cookieStore()
    if cs is None:
        return

    def allow_all(req: QWebEngineCookieStore.FilterRequest) -> bool:
        return True

    def block_3p(req: QWebEngineCookieStore.FilterRequest) -> bool:
        return not req.thirdParty

    cs.setCookieFilter(block_3p if block else allow_all)


def _search_url_for_query(query: str, settings: QSettings) -> QUrl:
    """Build search URL for the user’s chosen engine (see SEARCH_ENGINES)."""
    engine = settings.value("search_engine", "google")
    if not isinstance(engine, str) or engine not in SEARCH_ENGINES:
        engine = "google"
    _, tpl = SEARCH_ENGINES[engine]
    return QUrl(tpl.format(q=quote(query, safe="")))


def _should_open_url_not_search(text: str) -> bool:
    """
    True if the input looks like a host/URL to open directly (e.g. rovix.life),
    False if it should be sent to the search engine (e.g. what is a cow).
    """
    t = text.strip()
    if not t:
        return False
    if any(c in t for c in " \t\n\r"):
        return False
    if t.startswith(("http://", "https://")):
        q = QUrl(t)
        return q.isValid() and q.scheme() in ("http", "https") and bool(q.host())

    host_only = t.split("/")[0].split("?")[0]
    if host_only.count(":") > 1 and not host_only.startswith("["):
        try:
            ipaddress.ip_address(host_only)
            return True
        except ValueError:
            pass

    q = QUrl(f"https://{t}")
    if not q.isValid():
        return False
    host = q.host()
    if not host:
        return False
    hl = host.lower()
    if hl == "localhost":
        return True
    try:
        ipaddress.ip_address(hl.strip("[]"))
        return True
    except ValueError:
        pass
    return "." in hl


def _https_url_for_typed_host(text: str) -> QUrl:
    """Build https URL for a bare host; bracket IPv6 literals when needed."""
    t = text.strip()
    slash = t.find("/")
    if slash >= 0:
        host_part, tail = t[:slash], t[slash:]
    else:
        host_part, tail = t, ""
    if host_part.count(":") > 1 and not host_part.startswith("["):
        try:
            ipaddress.ip_address(host_part)
            return QUrl(f"https://[{host_part}]{tail}")
        except ValueError:
            pass
    return QUrl(f"https://{t}")


def _unique_path_in_folder(folder: Path, filename: str) -> Path:
    """Return a save path under folder that does not overwrite an existing file."""
    raw = (filename or "download").strip() or "download"
    base_name = Path(raw).name
    if not base_name or base_name in (".", ".."):
        base_name = "download"
    target = folder / base_name
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for i in range(1, 10000):
        cand = folder / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    return folder / f"{stem}_download{suffix}"


def resolve_omnibox_input(text: str, settings: QSettings) -> QUrl | None:
    """
    Map omnibox text to a destination URL: explicit http(s), host-like strings →
    open that site; phrases / questions → search with the chosen engine.
    """
    t = text.strip()
    if not t:
        return None
    if t.startswith(("http://", "https://")):
        q = QUrl(t)
        if q.isValid() and q.scheme() in ("http", "https"):
            return q
        return None
    if _should_open_url_not_search(t):
        return _https_url_for_typed_host(t)
    return _search_url_for_query(t, settings)


def _short_tab_title(title: str, max_len: int = 24) -> str:
    t = title.strip() or "New tab"
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


# Lucide SVGs in assets/icons (ISC license — https://lucide.dev)
_ICONS_DIR = Path(__file__).resolve().parent / "assets" / "icons"


def _svg_qicon(filename: str, logical: int = 24, dpr: float = 2.0) -> QIcon:
    """Render SVG to a HiDPI pixmap so toolbar icons stay sharp."""
    path = _ICONS_DIR / filename
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return QIcon()
    side = int(logical * dpr)
    pm = QPixmap(side, side)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(p, QRectF(0, 0, float(logical), float(logical)))
    p.end()
    return QIcon(pm)


class ClosableTabBar(QTabBar):
    """
    Closable tabs with a visible ×. Fusion + QSS often ignore `::close-button`
    `image`, so we replace the default close control with a QToolButton that uses
    the same SVG icon path as the toolbar.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTabsClosable(True)
        self._close_icon = _svg_qicon("tab-close.svg", logical=18)
        self._close_icon_size = QSize(16, 16)

    def tabInserted(self, index: int) -> None:
        super().tabInserted(index)
        self._refresh_close_buttons()

    def tabRemoved(self, index: int) -> None:
        super().tabRemoved(index)
        self._refresh_close_buttons()

    def _refresh_close_buttons(self) -> None:
        for i in range(self.count()):
            btn = QToolButton(self)
            btn.setObjectName("tabCloseButton")
            btn.setIcon(self._close_icon)
            btn.setIconSize(self._close_icon_size)
            btn.setAutoRaise(True)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("Close tab (Ctrl+W)")
            btn.setStyleSheet(
                """
                QToolButton#tabCloseButton {
                    background: transparent;
                    border: none;
                    border-radius: 8px;
                    padding: 1px;
                    min-width: 18px;
                    max-width: 18px;
                    min-height: 18px;
                    max-height: 18px;
                }
                QToolButton#tabCloseButton:hover {
                    background-color: rgba(255, 255, 255, 0.12);
                }
                QToolButton#tabCloseButton:pressed {
                    background-color: rgba(255, 255, 255, 0.18);
                }
                """
            )
            btn.clicked.connect(
                lambda checked=False, idx=i: self.tabCloseRequested.emit(idx)
            )
            self.setTabButton(i, QTabBar.ButtonPosition.RightSide, btn)


def _full_app_stylesheet() -> str:
    """Application QSS (tab × is drawn by ClosableTabBar, not stylesheet image)."""
    return APP_STYLESHEET


class EdgeGrip(QWidget):
    """Transparent strip that starts a native resize via QWindow.startSystemResize."""

    BORDER = 4

    def __init__(self, edges: Qt.Edge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edges = edges
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")
        e = edges
        tl = Qt.Edge.LeftEdge | Qt.Edge.TopEdge
        tr = Qt.Edge.RightEdge | Qt.Edge.TopEdge
        bl = Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
        br = Qt.Edge.RightEdge | Qt.Edge.BottomEdge
        if e == tl or e == br:
            self.setFixedSize(self.BORDER, self.BORDER)
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif e == tr or e == bl:
            self.setFixedSize(self.BORDER, self.BORDER)
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif e == Qt.Edge.TopEdge or e == Qt.Edge.BottomEdge:
            self.setFixedHeight(self.BORDER)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setFixedWidth(self.BORDER)
            self.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Expanding,
            )
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            wh = self.window().windowHandle()
            if wh is not None and wh.startSystemResize(self._edges):
                event.accept()
                return
        super().mousePressEvent(event)


class TitleBar(QWidget):
    """Client-side caption: title, minimize / maximize / close, drag and double-click."""

    def __init__(self, main: QMainWindow, parent: QWidget) -> None:
        super().__init__(parent)
        self._main = main
        self.setObjectName("titleBar")
        self.setFixedHeight(38)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._full_title = ""
        self._title = QLabel(self)
        self._title.setObjectName("titleBarLabel")
        self._title.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._title.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._title.setMinimumWidth(0)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 4, 8, 4)
        row.setSpacing(0)
        row.addWidget(self._title, 1)

        self._min_btn = self._make_ctl_button(
            "\u2212", "Minimize", self._minimize, "windowMinBtn"
        )
        self._max_btn = self._make_ctl_button(
            "\u25a1", "Maximize", self._toggle_maximize, "windowMaxBtn"
        )
        self._close_btn = self._make_ctl_button(
            "\u00d7", "Close", self._close_window, "windowCloseBtn"
        )

        row.addWidget(self._min_btn, 0)
        row.addWidget(self._max_btn, 0)
        row.addWidget(self._close_btn, 0)

        main.windowTitleChanged.connect(self._sync_title)
        self._sync_title(main.windowTitle())
        self.update_max_button()

    def _make_ctl_button(
        self,
        text: str,
        tip: str,
        slot: Callable[..., None],
        obj_name: str,
    ) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName(obj_name)
        btn.setText(text)
        btn.setAutoRaise(True)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(44, 30)
        bf = QFont()
        bf.setPointSize(13)
        bf.setWeight(QFont.Weight.Light)
        btn.setFont(bf)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.clicked.connect(slot)
        return btn

    def _sync_title(self, title: str) -> None:
        self._full_title = title.strip() if title.strip() else APP_NAME
        self._apply_elided_title()

    def _apply_elided_title(self) -> None:
        w = max(0, self._title.width())
        if w <= 0:
            self._title.setText(self._full_title)
            return
        fm = self._title.fontMetrics()
        self._title.setText(
            fm.elidedText(self._full_title, Qt.TextElideMode.ElideRight, w)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elided_title()

    def update_max_button(self) -> None:
        if self._main.isMaximized():
            self._max_btn.setText("\u29c9")
            self._max_btn.setToolTip("Restore down")
        else:
            self._max_btn.setText("\u25a1")
            self._max_btn.setToolTip("Maximize")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if isinstance(child, QToolButton):
                super().mousePressEvent(event)
                return
            wh = self.window().windowHandle()
            if wh is not None:
                event.accept()
                wh.startSystemMove()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not isinstance(child, QToolButton):
                self._toggle_maximize()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _minimize(self) -> None:
        self._main.showMinimized()

    def _toggle_maximize(self) -> None:
        if self._main.isMaximized():
            self._main.showNormal()
        else:
            self._main.showMaximized()
        self.update_max_button()

    def _close_window(self) -> None:
        self._main.close()


# Chrome-like toolbar icon size (logical px); SVGs scale cleanly at any size
NAV_ICON_SIZE = QSize(22, 22)


def render_newtab_html(settings: QSettings) -> str:
    engine = settings.value("search_engine", "google")
    if not isinstance(engine, str) or engine not in SEARCH_ENGINES:
        engine = "google"
    name, tpl = SEARCH_ENGINES[engine]
    raw = (ASSETS_HOME / "newtab.html").read_text(encoding="utf-8")
    raw = raw.replace("__SEARCH_TEMPLATE__", json.dumps(tpl))
    raw = raw.replace("__ENGINE_NAME__", json.dumps(name))
    return raw


def home_page_base_url() -> QUrl:
    return QUrl.fromLocalFile(str(ASSETS_HOME.resolve()).replace("\\", "/") + "/")


def _merge_chromium_privacy_flags() -> None:
    """
    Enable Chromium secure DNS (DNS-over-HTTPS) when supported. That way the
    router usually does not see plaintext DNS queries to your ISP/resolver.

    HTTPS already encrypts page content to websites; this does not hide which
    servers you connect to from the router (IP addresses and packet timing are
    still visible). For that, use a VPN or Tor — not something a browser alone
    can do for all traffic.
    """
    cur = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if "dns-over-https-mode" in cur:
        return
    flag = "--dns-over-https-mode=automatic"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{cur} {flag}".strip() if cur else flag


class SecureWebEnginePage(QWebEnginePage):
    """
    Prefer HTTPS for top-level navigations so the connection is TLS-encrypted.
    Local HTTP (loopback, private LAN IPs) is left alone for dev/router pages.
    """

    _LOCAL_HTTP = frozenset({"localhost", "127.0.0.1", "[::1]"})

    @staticmethod
    def _is_non_public_http_host(host: str) -> bool:
        h = host.lower().strip(".")
        if h.endswith(".local"):
            return True
        if h in SecureWebEnginePage._LOCAL_HTTP:
            return True
        try:
            ip = ipaddress.ip_address(h.strip("[]"))
            return bool(
                ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            )
        except ValueError:
            return False

    def acceptNavigationRequest(
        self,
        url: QUrl,
        nav_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        if is_main_frame and url.scheme().lower() == "http":
            host = url.host().lower()
            if self._is_non_public_http_host(host):
                return super().acceptNavigationRequest(url, nav_type, is_main_frame)
            u = QUrl(url)
            u.setScheme("https")
            if u.port() == 80:
                u.setPort(-1)
            self.load(u)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class SetupPage(QWebEnginePage):
    """Intercepts setup.html?finish=1&… so we can save choices without leaving the page."""

    def __init__(
        self,
        profile: QWebEngineProfile,
        parent: QWidget,
        on_pick: Callable[[dict[str, object]], None],
    ) -> None:
        super().__init__(profile, parent)
        self._on_pick = on_pick

    def acceptNavigationRequest(
        self,
        url: QUrl,
        _type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        if is_main_frame and url.isLocalFile():
            q = QUrlQuery(url)
            if q.queryItemValue("finish") == "1":
                eng = q.queryItemValue("engine")
                if eng not in SEARCH_ENGINES:
                    eng = "google"
                privacy = q.queryItemValue("privacy").lower()
                exit_cleanse = privacy == "cleanse"
                block3p = q.queryItemValue("block3p") != "0"
                self._on_pick(
                    {
                        "search_engine": eng,
                        "exit_cleanse": exit_cleanse,
                        "block_third_party_cookies": block3p,
                    }
                )
                return False
        return super().acceptNavigationRequest(url, _type, is_main_frame)


class SetupDialog(QDialog):
    """First-run full-screen style picker (HTML/CSS)."""

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle(f"{APP_NAME} — Welcome")
        self.setModal(True)
        self.resize(960, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        view = QWebEngineView(self)
        layout.addWidget(view)

        profile = QWebEngineProfile.defaultProfile()

        def on_pick(choices: dict[str, object]) -> None:
            self._settings.setValue("search_engine", choices["search_engine"])
            self._settings.setValue(
                "exit_cleanse", bool(choices.get("exit_cleanse", False))
            )
            self._settings.setValue(
                "block_third_party_cookies",
                bool(choices.get("block_third_party_cookies", True)),
            )
            self.accept()

        page = SetupPage(profile, view, on_pick)
        view.setPage(page)

        setup_path = ASSETS_HOME / "setup.html"
        view.load(QUrl.fromLocalFile(str(setup_path.resolve())))


@dataclass
class DownloadRecord:
    """One finished download for the history list."""

    path: Path
    source_url: str
    file_name: str
    ok: bool
    error: str = ""
    referrer_url: str = ""
    when: datetime = field(default_factory=datetime.now)


DOWNLOADS_DIALOG_QSS = """
QDialog {
    background-color: #1a1a1d;
    color: #eceeef;
}
QLabel#downloadsTitle {
    font-size: 16px;
    font-weight: 600;
    color: #f1f3f4;
}
QTableWidget {
    background-color: #141416;
    color: #eceeef;
    gridline-color: #2e3036;
    border: 1px solid #2e3036;
    border-radius: 8px;
}
QTableWidget::item {
    padding: 6px;
}
QTableWidget::item:selected {
    background-color: #303438;
}
QHeaderView::section {
    background-color: #252528;
    color: #9aa0a8;
    padding: 8px;
    border: none;
    border-bottom: 1px solid #2e3036;
}
QPushButton {
    background-color: #303438;
    color: #eceeef;
    border: 1px solid #3a3d45;
    border-radius: 8px;
    padding: 8px 16px;
}
QPushButton:hover {
    background-color: #3a3d47;
}
QPushButton:pressed {
    background-color: #252528;
}
QPushButton#openFolderBtn {
    background-color: transparent;
    border: 1px solid #5e9eff;
    color: #8ab4f8;
}
QPushButton#openFolderBtn:hover {
    background-color: rgba(94, 158, 255, 0.12);
}
"""


class DownloadsDialog(QDialog):
    """Shows download history with open file and open folder actions."""

    def __init__(
        self,
        parent: QWidget,
        records: list[DownloadRecord],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Downloads — {APP_NAME}")
        self.setMinimumSize(560, 380)
        self.resize(640, 420)
        self.setStyleSheet(DOWNLOADS_DIALOG_QSS)
        self._records_chrono: list[DownloadRecord] = list(records)

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Recent downloads")
        title.setObjectName("downloadsTitle")
        root.addWidget(title)

        if not records:
            tip = QLabel(
                "No downloads yet. When you save a file from the web, it appears here "
                "with the page it came from."
            )
            tip.setWordWrap(True)
            tip.setStyleSheet("color: #9aa0a8; padding: 12px 0;")
            root.addWidget(tip)
        else:
            self._table = QTableWidget(0, 3)
            self._table.setObjectName("downloadsTable")
            self._table.setHorizontalHeaderLabels(["File", "From (page or link)", ""])
            self._table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            self._table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self._table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.Fixed
            )
            self._table.setColumnWidth(2, 100)
            self._table.verticalHeader().setVisible(False)
            self._table.setShowGrid(True)
            self._table.setAlternatingRowColors(False)
            self._table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows
            )
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self._table.doubleClicked.connect(self._on_double_click)

            for rec in reversed(records):
                row = self._table.rowCount()
                self._table.insertRow(row)
                name_item = QTableWidgetItem(rec.file_name)
                if not rec.ok:
                    name_item.setToolTip(rec.error or "Download failed")
                self._table.setItem(row, 0, name_item)
                from_label = rec.referrer_url or rec.source_url
                url_item = QTableWidgetItem(from_label)
                if rec.referrer_url and rec.source_url != rec.referrer_url:
                    url_item.setToolTip(
                        f"Page: {rec.referrer_url}\nDownload link: {rec.source_url}"
                    )
                else:
                    url_item.setToolTip(rec.source_url)
                self._table.setItem(row, 1, url_item)
                ob = QPushButton("Open")
                ob.setEnabled(rec.ok and rec.path.exists())
                ob.clicked.connect(lambda checked=False, p=rec.path: self._open_file(p))
                self._table.setCellWidget(row, 2, ob)

            root.addWidget(self._table, 1)

        row_btns = QHBoxLayout()
        row_btns.addStretch(1)
        folder_btn = QPushButton("Open downloads folder")
        folder_btn.setObjectName("openFolderBtn")
        folder_btn.clicked.connect(self._open_downloads_folder)
        row_btns.addWidget(folder_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row_btns.addWidget(close_btn)
        root.addLayout(row_btns)

    def _on_double_click(self, index) -> None:
        row = index.row()
        recs_newest_first = list(reversed(self._records_chrono))
        if 0 <= row < len(recs_newest_first) and recs_newest_first[row].ok:
            self._open_file(recs_newest_first[row].path)

    def _open_file(self, path: Path) -> None:
        p = path.resolve()
        if p.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _open_downloads_folder(self) -> None:
        loc = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        folder = Path(loc) if loc else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))


class OptionsDialog(QDialog):
    """Privacy and data options (stored in QSettings)."""

    def __init__(
        self,
        parent: QWidget,
        settings: QSettings,
        profile: QWebEngineProfile,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._profile = profile
        self.setWindowTitle(f"Options — {APP_NAME}")
        self.setMinimumWidth(440)
        self.setStyleSheet(DOWNLOADS_DIALOG_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        intro = QLabel(
            "Privacy and browsing data. You can change these anytime from the menu."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #9aa0a8;")
        layout.addWidget(intro)

        self._cleanse = QCheckBox(
            "Delete cookies, cache, and site storage when I quit (no trace on this device)"
        )
        self._cleanse.setChecked(
            settings.value("exit_cleanse", False, type=bool)
        )
        layout.addWidget(self._cleanse)

        self._block3p = QCheckBox(
            "Block third-party cookies (reduces cross-site tracking)"
        )
        self._block3p.setChecked(
            settings.value("block_third_party_cookies", True, type=bool)
        )
        layout.addWidget(self._block3p)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _save(self) -> None:
        self._settings.setValue("exit_cleanse", self._cleanse.isChecked())
        self._settings.setValue(
            "block_third_party_cookies", self._block3p.isChecked()
        )
        apply_third_party_cookie_filter(
            self._profile, self._block3p.isChecked()
        )
        self.accept()


# Modern dark chrome — single surface + soft contrast (no harsh Fusion chrome)
APP_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #141416;
    color: #eceeef;
    font-size: 13px;
}

#clientShell {
    background-color: #141416;
}

#pageStack {
    background-color: #141416;
}

#titleBar {
    background-color: #1a1a1d;
    border: none;
    border-bottom: 1px solid #2e3036;
}

#titleBarLabel {
    color: #c4c8ce;
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.02em;
    background: transparent;
}

QToolButton#windowMinBtn,
QToolButton#windowMaxBtn,
QToolButton#windowCloseBtn {
    background: transparent;
    color: #a8adb5;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 300;
    padding: 0;
}

QToolButton#windowMinBtn:hover,
QToolButton#windowMaxBtn:hover {
    background-color: rgba(255, 255, 255, 0.08);
    color: #f1f3f4;
}

QToolButton#windowCloseBtn:hover {
    background-color: #c42b1c;
    color: #ffffff;
}

QToolButton#windowCloseBtn:pressed {
    background-color: #a32618;
}

#tabStrip {
    background-color: #1a1a1d;
    border: none;
    border-bottom: 1px solid #2e3036;
}

QTabBar#tabBar {
    background: transparent;
}

QTabBar#tabBar::tab {
    background-color: #2e3038;
    color: #9aa0a8;
    border: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    min-width: 112px;
    max-width: 220px;
    padding: 8px 14px 9px 14px;
    margin-right: 4px;
    margin-top: 5px;
    font-weight: 500;
}

QTabBar#tabBar::tab:selected {
    background-color: #141416;
    color: #f1f3f4;
    font-weight: 600;
}

QTabBar#tabBar::tab:hover:!selected {
    background-color: #3a3d47;
    color: #dfe0e4;
}

#chromeBar {
    background-color: #141416;
    border: none;
    border-bottom: 1px solid #2e3036;
}

QToolButton#navBtn {
    background: transparent;
    border: none;
    border-radius: 10px;
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    padding: 0px;
}

QToolButton#navBtn:hover {
    background-color: rgba(255, 255, 255, 0.08);
}

QToolButton#navBtn:pressed {
    background-color: rgba(255, 255, 255, 0.12);
}

QToolButton#newTabButton {
    background: transparent;
    border: none;
    border-radius: 10px;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0px;
    margin-bottom: 3px;
}

QToolButton#newTabButton:hover {
    background-color: rgba(255, 255, 255, 0.08);
}

QToolButton#newTabButton:pressed {
    background-color: rgba(255, 255, 255, 0.12);
}

QToolButton#downloadBtn {
    background: transparent;
    border: none;
    border-radius: 10px;
    min-width: 108px;
    min-height: 34px;
    max-height: 34px;
    padding: 0px 10px;
    color: #c4c8ce;
    font-size: 12px;
    font-weight: 500;
}

QToolButton#downloadBtn:hover {
    background-color: rgba(255, 255, 255, 0.08);
}

QToolButton#downloadBtn:pressed {
    background-color: rgba(255, 255, 255, 0.12);
}

#urlBar {
    background-color: #1e2024;
    border: 1px solid #3a3d45;
    border-radius: 22px;
    padding: 8px 18px;
    min-height: 22px;
    selection-background-color: #8ab4f8;
    selection-color: #141416;
    color: #eceeef;
}

#urlBar:focus {
    border: 1px solid #5e9eff;
    background-color: #1e2024;
}

QStatusBar {
    background-color: #141416;
    color: #8b9099;
    border-top: 1px solid #2e3036;
    padding: 3px 10px;
    font-size: 11px;
}
"""


class BrowserWindow(QMainWindow):
    """Chrome-like: tabs on top, then compact nav + omnibox, then page stack."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 900)
        self.setMinimumSize(720, 480)
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )

        self._profile = QWebEngineProfile("secret_browser_main", self)
        configure_profile(self._profile)
        self._profile.downloadRequested.connect(self._on_download_requested)

        self._settings = QSettings("SecretBrowser", "Browser")
        apply_third_party_cookie_filter(
            self._profile,
            self._settings.value("block_third_party_cookies", True, type=bool),
        )

        self._edge_widgets: list[QWidget] = []

        shell = QWidget()
        shell_v = QVBoxLayout(shell)
        shell_v.setContentsMargins(0, 0, 0, 0)
        shell_v.setSpacing(0)

        top_row = QHBoxLayout()
        top_row.setSpacing(0)
        top_row.setContentsMargins(0, 0, 0, 0)
        _tl = EdgeGrip(Qt.Edge.LeftEdge | Qt.Edge.TopEdge)
        _top = EdgeGrip(Qt.Edge.TopEdge)
        _tr = EdgeGrip(Qt.Edge.RightEdge | Qt.Edge.TopEdge)
        self._edge_widgets.extend((_tl, _top, _tr))
        top_row.addWidget(_tl)
        top_row.addWidget(_top, 1)
        top_row.addWidget(_tr)

        mid = QHBoxLayout()
        mid.setSpacing(0)
        mid.setContentsMargins(0, 0, 0, 0)
        _left = EdgeGrip(Qt.Edge.LeftEdge)
        _right = EdgeGrip(Qt.Edge.RightEdge)
        self._edge_widgets.extend((_left, _right))

        inner = QWidget()
        inner.setObjectName("clientShell")
        inner_l = QVBoxLayout(inner)
        inner_l.setContentsMargins(0, 0, 0, 0)
        inner_l.setSpacing(0)

        self._title_bar = TitleBar(self, inner)
        inner_l.addWidget(self._title_bar)

        # —— Row 1: tabs + new tab (adjacent, + not at far window edge) ——
        tab_strip = QWidget()
        tab_strip.setObjectName("tabStrip")
        tab_row = QHBoxLayout(tab_strip)
        tab_row.setContentsMargins(12, 5, 10, 2)
        tab_row.setSpacing(2)

        self._tab_bar = ClosableTabBar()
        self._tab_bar.setObjectName("tabBar")
        self._tab_bar.setDocumentMode(True)
        self._tab_bar.setExpanding(False)
        self._tab_bar.setElideMode(Qt.TextElideMode.ElideRight)
        self._tab_bar.currentChanged.connect(self._on_tab_index_changed)
        self._tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)

        new_tab_btn = QToolButton()
        new_tab_btn.setObjectName("newTabButton")
        new_tab_btn.setIcon(_svg_qicon("plus.svg", logical=22))
        new_tab_btn.setIconSize(QSize(20, 20))
        new_tab_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        new_tab_btn.setToolTip("New tab (Ctrl+T)")
        new_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_tab_btn.clicked.connect(self._new_tab)

        tab_row.addWidget(self._tab_bar, 0, Qt.AlignmentFlag.AlignBottom)
        tab_row.addWidget(new_tab_btn, 0, Qt.AlignmentFlag.AlignBottom)
        tab_row.addStretch(1)

        # —— Row 2: back / forward / reload + omnibox (Chrome order) ——
        chrome = QFrame()
        chrome.setObjectName("chromeBar")
        row = QHBoxLayout(chrome)
        row.setContentsMargins(12, 6, 12, 8)
        row.setSpacing(2)

        for tip, icon, handler in (
            ("Back (Alt+Left)", _svg_qicon("arrow-left.svg"), self._go_back),
            ("Forward (Alt+Right)", _svg_qicon("arrow-right.svg"), self._go_forward),
            ("Reload (Ctrl+R)", _svg_qicon("rotate-cw.svg"), self._reload),
        ):
            b = QToolButton()
            b.setObjectName("navBtn")
            b.setIcon(icon)
            b.setIconSize(NAV_ICON_SIZE)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(handler)
            row.addWidget(b)

        self._download_history: list[DownloadRecord] = []
        self._downloads_btn = QToolButton()
        self._downloads_btn.setObjectName("downloadBtn")
        self._downloads_btn.setIcon(_svg_qicon("download.svg", logical=22))
        self._downloads_btn.setIconSize(NAV_ICON_SIZE)
        self._downloads_btn.setText("Downloads")
        self._downloads_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._downloads_btn.setToolTip("View downloads (Ctrl+J)")
        self._downloads_btn.setAutoRaise(True)
        self._downloads_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._downloads_btn.setVisible(True)
        self._downloads_btn.clicked.connect(self._show_downloads_dialog)
        row.addWidget(self._downloads_btn)

        self._bookmark_btn = QToolButton()
        self._bookmark_btn.setObjectName("navBtn")
        self._bookmark_btn.setIcon(_svg_qicon("star.svg", logical=22))
        self._bookmark_btn.setIconSize(NAV_ICON_SIZE)
        self._bookmark_btn.setToolTip("Bookmarks (Ctrl+D to add)")
        self._bookmark_btn.setAutoRaise(True)
        self._bookmark_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bookmark_menu = QMenu(self)
        self._bookmark_menu.setToolTipsVisible(True)
        self._bookmark_menu.aboutToShow.connect(self._populate_bookmark_menu)
        self._bookmark_btn.setMenu(self._bookmark_menu)
        self._bookmark_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        row.addWidget(self._bookmark_btn)

        self._overflow_btn = QToolButton()
        self._overflow_btn.setObjectName("navBtn")
        self._overflow_btn.setIcon(_svg_qicon("more-vertical.svg", logical=22))
        self._overflow_btn.setIconSize(NAV_ICON_SIZE)
        self._overflow_btn.setToolTip("Menu")
        self._overflow_btn.setAutoRaise(True)
        self._overflow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._overflow_menu = QMenu(self)
        self._overflow_menu.addAction("Options…", self._show_options_dialog)
        self._overflow_menu.addAction(
            "Clear browsing data…", self._clear_browsing_data_prompt
        )
        self._overflow_menu.addSeparator()
        self._overflow_menu.addAction(f"About {APP_NAME}", self._show_about_dialog)
        self._overflow_btn.setMenu(self._overflow_menu)
        self._overflow_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        row.addWidget(self._overflow_btn)

        row.addSpacing(8)

        self._urlbar = QLineEdit()
        self._urlbar.setObjectName("urlBar")
        self._update_urlbar_placeholder()
        self._urlbar.setClearButtonEnabled(True)
        self._urlbar.returnPressed.connect(self._navigate_from_bar)
        self._urlbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self._urlbar, 1)

        self._stack = QStackedWidget()
        self._stack.setObjectName("pageStack")

        inner_l.addWidget(tab_strip, 0)
        inner_l.addWidget(chrome, 0)
        inner_l.addWidget(self._stack, 1)

        mid.addWidget(_left)
        mid.addWidget(inner, 1)
        mid.addWidget(_right)

        bot_row = QHBoxLayout()
        bot_row.setSpacing(0)
        bot_row.setContentsMargins(0, 0, 0, 0)
        _bl = EdgeGrip(Qt.Edge.LeftEdge | Qt.Edge.BottomEdge)
        _bot = EdgeGrip(Qt.Edge.BottomEdge)
        _br = EdgeGrip(Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
        self._edge_widgets.extend((_bl, _bot, _br))
        bot_row.addWidget(_bl)
        bot_row.addWidget(_bot, 1)
        bot_row.addWidget(_br)

        shell_v.addLayout(top_row)
        shell_v.addLayout(mid, 1)
        shell_v.addLayout(bot_row)

        self.setCentralWidget(shell)

        self.add_tab()

        QShortcut(QKeySequence("Ctrl+T"), self, self._new_tab)
        QShortcut(QKeySequence("Ctrl+W"), self, self._close_current_tab)
        QShortcut(QKeySequence("Ctrl+R"), self, self._reload)
        QShortcut(QKeySequence("Ctrl+L"), self, self._focus_url_bar)
        QShortcut(QKeySequence("Alt+Left"), self, self._go_back)
        QShortcut(QKeySequence("Alt+Right"), self, self._go_forward)
        QShortcut(QKeySequence("Alt+Home"), self, self._go_home)
        QShortcut(QKeySequence("Ctrl+J"), self, self._show_downloads_dialog)
        QShortcut(QKeySequence("Ctrl+D"), self, self._prompt_add_bookmark)

    def prepare_exit_cleanup(self) -> None:
        """Wipe profile data on quit when exit_cleanse is enabled."""
        if not self._settings.value("exit_cleanse", False, type=bool):
            return
        save_bookmarks_list(self._settings, [])
        self._settings.sync()
        clear_profile_browsing_data(self._profile)

    def _show_downloads_dialog(self) -> None:
        dlg = DownloadsDialog(self, self._download_history)
        dlg.exec()

    def _navigate_url_in_current(self, q: QUrl) -> None:
        v = self.current_view()
        if v and q.isValid():
            v.setUrl(q)

    def _populate_bookmark_menu(self) -> None:
        self._bookmark_menu.clear()
        self._bookmark_menu.addAction("Bookmark this page…").triggered.connect(
            self._prompt_add_bookmark
        )
        self._bookmark_menu.addSeparator()
        items = load_bookmarks_list(self._settings)
        if not items:
            na = self._bookmark_menu.addAction("(No bookmarks yet)")
            na.setEnabled(False)
        else:
            for b in items:
                url = b["url"]
                act = self._bookmark_menu.addAction(bookmark_menu_label(b))
                act.setToolTip(url)
                act.triggered.connect(
                    lambda checked=False, u=url: self._navigate_url_in_current(
                        QUrl(u)
                    )
                )
            self._bookmark_menu.addSeparator()
            self._bookmark_menu.addAction("Rename bookmark…").triggered.connect(
                self._pick_rename_bookmark
            )
            self._bookmark_menu.addAction("Remove bookmark…").triggered.connect(
                self._pick_remove_bookmark
            )

    def _prompt_add_bookmark(self) -> None:
        v = self.current_view()
        if not v:
            return
        u = v.url()
        if u.scheme() not in ("http", "https") or not u.isValid():
            QMessageBox.information(
                self,
                "Bookmarks",
                "Only web pages (http/https) can be bookmarked.",
            )
            return
        url = u.toString()
        name, ok = QInputDialog.getText(
            self,
            "Bookmark",
            "Name for this bookmark (default is the page URL):",
            text=url,
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            name = url
        items = load_bookmarks_list(self._settings)
        for it in items:
            if it["url"] == url:
                it["title"] = name
                save_bookmarks_list(self._settings, items)
                self.statusBar().showMessage("Bookmark updated.", 2500)
                return
        items.append({"url": url, "title": name})
        save_bookmarks_list(self._settings, items)
        self.statusBar().showMessage("Bookmark saved.", 2500)

    def _bookmark_labels_for_picker(self, items: list[dict[str, str]]) -> list[str]:
        labels: list[str] = []
        for i, b in enumerate(items):
            disp = bookmark_menu_label(b, max_len=72)
            labels.append(f"{i + 1}. {disp}")
        return labels

    def _pick_rename_bookmark(self) -> None:
        items = load_bookmarks_list(self._settings)
        if not items:
            QMessageBox.information(self, "Bookmarks", "No bookmarks to rename.")
            return
        labels = self._bookmark_labels_for_picker(items)
        choice, ok = QInputDialog.getItem(
            self,
            "Rename bookmark",
            "Choose bookmark:",
            labels,
            0,
            False,
        )
        if not ok:
            return
        try:
            idx = labels.index(choice)
        except ValueError:
            return
        url = items[idx]["url"]
        cur = items[idx].get("title") or url
        new_name, ok2 = QInputDialog.getText(
            self,
            "Rename bookmark",
            "New name:",
            text=cur,
        )
        if not ok2:
            return
        new_name = new_name.strip()
        if not new_name:
            new_name = url
        for it in items:
            if it["url"] == url:
                it["title"] = new_name
                break
        save_bookmarks_list(self._settings, items)
        self.statusBar().showMessage("Bookmark renamed.", 2500)

    def _pick_remove_bookmark(self) -> None:
        items = load_bookmarks_list(self._settings)
        if not items:
            QMessageBox.information(self, "Bookmarks", "No bookmarks to remove.")
            return
        labels = self._bookmark_labels_for_picker(items)
        choice, ok = QInputDialog.getItem(
            self,
            "Remove bookmark",
            "Remove which bookmark?",
            labels,
            0,
            False,
        )
        if not ok:
            return
        try:
            idx = labels.index(choice)
        except ValueError:
            return
        url = items[idx]["url"]
        kept = [x for x in items if x["url"] != url]
        save_bookmarks_list(self._settings, kept)
        self.statusBar().showMessage("Bookmark removed.", 2500)

    def _show_options_dialog(self) -> None:
        dlg = OptionsDialog(self, self._settings, self._profile)
        dlg.exec()

    def _clear_browsing_data_prompt(self) -> None:
        r = QMessageBox.question(
            self,
            "Clear browsing data",
            "Clear cookies, cache, site storage, and visited links for all sites? "
            "You may need to reload open tabs.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        clear_profile_browsing_data(self._profile)
        self.statusBar().showMessage("Browsing data cleared.", 6000)

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<p><b>{APP_NAME}</b></p>"
            "<p>Desktop browser powered by Qt WebEngine with HTTPS-first navigation "
            "and optional DNS-over-HTTPS.</p>"
            "<p>Use <b>Options</b> to control cleanup on exit and third-party cookies.</p>",
        )

    def _register_download_finished(
        self,
        dest: Path,
        source_url: str,
        ok: bool,
        error: str = "",
        referrer_url: str = "",
    ) -> None:
        self._download_history.append(
            DownloadRecord(
                path=dest,
                source_url=source_url,
                file_name=dest.name,
                ok=ok,
                error=error,
                referrer_url=referrer_url,
            )
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._title_bar.update_max_button()
            hide_edges = self.isMaximized() or self.isFullScreen()
            for w in self._edge_widgets:
                w.setVisible(not hide_edges)

    def _on_download_requested(self, download: QWebEngineDownloadRequest) -> None:
        """Save linked or navigated downloads into the system Downloads folder."""
        source_url = download.url().toString()
        referrer_url = ""
        page = download.page()
        if page is not None:
            pu = page.url()
            if pu.isValid() and pu.scheme() in ("http", "https"):
                referrer_url = pu.toString()

        loc = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        folder = Path(loc) if loc else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)

        name = download.suggestedFileName()
        if not name:
            qn = download.url().fileName()
            name = qn if qn else "download"
        name = Path(name).name
        if not name or name in (".", ".."):
            name = "download"

        dest = _unique_path_in_folder(folder, name)
        download.setDownloadDirectory(str(dest.parent))
        download.setDownloadFileName(dest.name)
        download.accept()

        self.statusBar().showMessage(f"Downloading {dest.name}…", 4000)

        _terminal_done = False

        def try_finalize_download() -> None:
            nonlocal _terminal_done
            if _terminal_done or not download.isFinished():
                return
            st = download.state()
            if st == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                _terminal_done = True
                self.statusBar().showMessage(f"Saved: {dest.name}", 6000)
                self._register_download_finished(
                    dest, source_url, True, referrer_url=referrer_url
                )
            elif st == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted:
                _terminal_done = True
                reason = download.interruptReasonString()
                self.statusBar().showMessage(
                    f"Download failed: {reason}" if reason else "Download failed",
                    8000,
                )
                self._register_download_finished(
                    dest,
                    source_url,
                    False,
                    error=reason or "Download failed",
                    referrer_url=referrer_url,
                )
            elif st == QWebEngineDownloadRequest.DownloadState.DownloadCancelled:
                _terminal_done = True

        download.isFinishedChanged.connect(try_finalize_download)
        download.stateChanged.connect(try_finalize_download)

    def current_view(self) -> QWebEngineView | None:
        w = self._stack.currentWidget()
        return w if isinstance(w, QWebEngineView) else None

    def _view_index(self, view: QWebEngineView) -> int:
        return self._stack.indexOf(view)

    def _update_urlbar_placeholder(self) -> None:
        eng = self._settings.value("search_engine", "google")
        if not isinstance(eng, str) or eng not in SEARCH_ENGINES:
            eng = "google"
        name = SEARCH_ENGINES[eng][0]
        self._urlbar.setPlaceholderText(f"Search {name} or type a URL")

    def _load_home_page(self, view: QWebEngineView) -> None:
        view.setHtml(render_newtab_html(self._settings), home_page_base_url())
        view.setProperty("isHomePage", True)

    def add_tab(self, url: QUrl | None = None) -> QWebEngineView:
        view = QWebEngineView()
        view.setProperty("lastWebUrl", "")
        page = SecureWebEnginePage(self._profile, view)
        view.setPage(page)

        if url is None:
            self._load_home_page(view)
        else:
            view.setUrl(url)

        self._stack.addWidget(view)
        idx = self._tab_bar.addTab(_short_tab_title("New tab"))
        self._tab_bar.setCurrentIndex(idx)

        view.urlChanged.connect(lambda u, v=view: self._on_any_url_changed(u, v))
        view.titleChanged.connect(lambda t, v=view: self._on_any_title_changed(t, v))
        view.loadFinished.connect(lambda ok, v=view: self._on_any_load_finished(ok, v))
        view.iconChanged.connect(lambda ic, v=view: self._on_any_icon_changed(ic, v))

        return view

    def _new_tab(self) -> None:
        self.add_tab()

    def _close_current_tab(self) -> None:
        idx = self._tab_bar.currentIndex()
        if idx >= 0:
            self._on_tab_close_requested(idx)

    def _focus_url_bar(self) -> None:
        self._urlbar.setFocus()
        self._urlbar.selectAll()

    def _display_url_for_view(self, view: QWebEngineView) -> str:
        """Text for the omnibox: never show raw file:// or blob: from downloads."""
        if view.property("isHomePage"):
            return ""
        u = view.url()
        s = u.scheme().lower()
        if s in ("http", "https") and u.isValid() and u.host():
            return u.toString()
        lw = view.property("lastWebUrl")
        if lw:
            return str(lw)
        return ""

    def _on_tab_index_changed(self, index: int) -> None:
        if index < 0:
            return
        self._stack.setCurrentIndex(index)
        view = self._stack.widget(index)
        if isinstance(view, QWebEngineView):
            self._urlbar.setText(self._display_url_for_view(view))
            self._urlbar.setCursorPosition(0)
            title = view.title()
            self.setWindowTitle(
                f"{title} — {APP_NAME}" if title else APP_NAME
            )

    def _on_tab_close_requested(self, index: int) -> None:
        if self._tab_bar.count() <= 1:
            view = self._stack.widget(0)
            if isinstance(view, QWebEngineView):
                self._load_home_page(view)
            self.statusBar().showMessage("Last tab — opened home page.", 2500)
            return
        w = self._stack.widget(index)
        self._stack.removeWidget(w)
        self._tab_bar.removeTab(index)
        if w is not None:
            w.deleteLater()

    def _on_any_url_changed(self, url: QUrl, view: QWebEngineView) -> None:
        if self.current_view() is not view:
            return
        if view.property("isHomePage"):
            if url.scheme() in ("http", "https"):
                view.setProperty("isHomePage", False)
            self._urlbar.setText("")
            self._urlbar.setCursorPosition(0)
            return
        s = url.scheme().lower()
        if s in ("http", "https") and url.isValid():
            view.setProperty("lastWebUrl", url.toString())
            self._urlbar.setText(url.toString())
            self._urlbar.setCursorPosition(0)
            return
        # file://, blob:, about:blank during download, etc. — keep last real web URL
        lw = view.property("lastWebUrl")
        self._urlbar.setText(str(lw) if lw else "")
        self._urlbar.setCursorPosition(0)

    def _on_any_title_changed(self, title: str, view: QWebEngineView) -> None:
        idx = self._view_index(view)
        if idx >= 0:
            self._tab_bar.setTabText(idx, _short_tab_title(title))
        if self.current_view() is view:
            self.setWindowTitle(
                f"{title} — {APP_NAME}" if title else APP_NAME
            )

    def _on_any_load_finished(self, ok: bool, view: QWebEngineView) -> None:
        if self.current_view() is not view:
            return
        u = view.url()
        if ok and u.scheme() in ("http", "https") and u.isValid():
            view.setProperty("lastWebUrl", u.toString())
            self._urlbar.setText(u.toString())
            self._urlbar.setCursorPosition(0)
        if not ok:
            host = urlparse(view.url().toString()).hostname or "site"
            self.statusBar().showMessage(f"Load failed: {host}", 5000)
        else:
            self.statusBar().clearMessage()

    def _on_any_icon_changed(self, icon: QIcon, view: QWebEngineView) -> None:
        idx = self._view_index(view)
        if idx < 0 or icon.isNull():
            return
        self._tab_bar.setTabIcon(idx, icon)

    def _go_back(self) -> None:
        v = self.current_view()
        if v:
            v.back()

    def _go_forward(self) -> None:
        v = self.current_view()
        if v:
            v.forward()

    def _reload(self) -> None:
        v = self.current_view()
        if v:
            v.reload()

    def _go_home(self) -> None:
        v = self.current_view()
        if v:
            self._load_home_page(v)

    def _navigate_from_bar(self) -> None:
        v = self.current_view()
        if not v:
            return
        typed = self._urlbar.text().strip()
        if not typed:
            return
        q = resolve_omnibox_input(self._urlbar.text(), self._settings)
        if q is None or not q.isValid():
            QMessageBox.warning(
                self,
                "Can’t go there",
                "Type a search or a web address (e.g. rovix.life).",
            )
            return
        v.setUrl(q)


def main() -> None:
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--enable-gpu --enable-gpu-rasterization",
    )
    _merge_chromium_privacy_flags()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")

    font = app.font()
    font.setPointSize(10)
    font.setFamilies(
        [
            "Segoe UI",
            "Segoe UI Variable",
            "SF Pro Text",
            "Roboto",
            "Helvetica Neue",
            "Arial",
            "sans-serif",
        ]
    )
    app.setFont(font)

    settings = QSettings("SecretBrowser", "Browser")
    if not settings.contains("block_third_party_cookies"):
        settings.setValue("block_third_party_cookies", True)
    if not settings.contains("exit_cleanse"):
        settings.setValue("exit_cleanse", False)

    if not settings.value("setup_done", False, type=bool):
        SetupDialog(settings).exec()
        if not settings.value("search_engine"):
            settings.setValue("search_engine", "google")
        settings.setValue("setup_done", True)

    w = BrowserWindow()
    w.setStyleSheet(_full_app_stylesheet())
    w.show()
    app.aboutToQuit.connect(w.prepare_exit_cleanup)
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
