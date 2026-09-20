"""API Usage Monitor — compact desktop widget

Tracks remaining quota for Kimi / ChatGPT / Antigravity.
Dual rings: outer = long-period (week/month) remaining, inner = 5h remaining.
Data layer: providers.py (real APIs, using each client's local credentials).
Selected services are persisted to ~/.api-usage-monitor.json.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from providers import PROVIDERS, RingStat, UsageSnapshot

SERVICES = [
    ("Kimi", "#4d6bfe"),
    ("ChatGPT", "#10a37f"),
    ("Antigravity", "#ea4335"),
]
SERVICE_COLORS = dict(SERVICES)

CONFIG_PATH = Path.home() / ".api-usage-monitor.json"
REFRESH_INTERVAL_MS = 60_000
CARD_WIDTH = 106
WINDOW_HEIGHT = 176

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

STYLE = """
QMainWindow, QWidget {
    background-color: #1b1d23;
    color: #e6e6e6;
    font-family: "Segoe UI", sans-serif;
}
QFrame#card {
    background-color: #262932;
    border: 1px solid #353945;
    border-radius: 10px;
}
QLabel#serviceName {
    font-size: 12px;
    font-weight: 600;
}
QLabel#legend {
    color: #9aa0ab;
    font-size: 9px;
}
QLabel#hint {
    color: #7a7f8a;
    font-size: 11px;
}
QLabel#updated {
    color: #9aa0ab;
    font-size: 9px;
}
QPushButton {
    background-color: #353945;
    border: none;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 10px;
    color: #e6e6e6;
}
QPushButton:hover { background-color: #444a58; }
QPushButton:pressed { background-color: #2c303b; }
QPushButton:disabled { color: #7a7f8a; }
QPushButton#closeCard {
    background-color: transparent;
    color: #7a7f8a;
    font-size: 11px;
    font-weight: 700;
    padding: 0px 4px;
}
QPushButton#closeCard:hover { color: #e57373; }
QComboBox {
    background-color: #353945;
    border: none;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 10px;
    font-weight: 600;
    min-width: 90px;
}
QComboBox:hover { background-color: #444a58; }
QComboBox::drop-down { border: none; width: 16px; }
QComboBox QAbstractItemView {
    background-color: #262932;
    border: 1px solid #353945;
    color: #e6e6e6;
    selection-background-color: #444a58;
    outline: none;
}
"""

# ---------------------------------------------------------------------------
# Background fetching
# ---------------------------------------------------------------------------


class FetchSignals(QObject):
    result = Signal(str, object)  # service name, UsageSnapshot


class FetchWorker(QRunnable):
    def __init__(self, service: str, signals: FetchSignals) -> None:
        super().__init__()
        self._service = service
        self._signals = signals

    def run(self) -> None:
        try:
            snapshot = PROVIDERS[self._service]().fetch()
        except Exception as exc:
            snapshot = UsageSnapshot(service=self._service, ok=False, error=str(exc))
        self._signals.result.emit(self._service, snapshot)


# ---------------------------------------------------------------------------
# Dual-ring widget
# ---------------------------------------------------------------------------


class DoubleRing(QWidget):
    """Outer ring = long-period remaining, inner ring = 5h remaining.
    Center number shows inner-ring remaining percent."""

    def __init__(self, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(62, 62)
        self._inner_color = QColor(accent)
        self._outer_color = QColor("#ffffff")  # 外圈统一白色
        self._track = QColor("#3a3e4b")
        self._inner_pct: int | None = None
        self._outer_pct: int | None = None

    def set_stats(self, inner: int | None, outer: int | None) -> None:
        self._inner_pct = inner
        self._outer_pct = outer
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height())
        outer_r = side / 2 - 4
        inner_r = outer_r - 9
        cx, cy = self.width() / 2, self.height() / 2

        def draw_ring(radius: float, width: float, pct: int | None, color: QColor) -> None:
            rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            pen = QPen(self._track, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 0, 360 * 16)
            if pct is not None:
                pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawArc(rect, 90 * 16, int(-pct * 3.6 * 16))

        draw_ring(outer_r, 5.5, self._outer_pct, self._outer_color)
        draw_ring(inner_r, 4.5, self._inner_pct, self._inner_color)

        p.setPen(QColor("#e6e6e6"))
        font = p.font()
        font.setPixelSize(13)
        font.setBold(True)
        p.setFont(font)
        text = "--" if self._inner_pct is None else f"{self._inner_pct}"
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
        p.end()


# ---------------------------------------------------------------------------
# Service card
# ---------------------------------------------------------------------------


def _fmt_reset(stat: RingStat | None) -> str:
    if stat is None:
        return "—"
    pct = f"{stat.remaining_percent}%"
    if stat.reset_at is None:
        return pct
    local = stat.reset_at.astimezone()
    delta = stat.reset_at - datetime.now(local.tzinfo)
    # within 24h show clock time, otherwise just the date (keeps the line short)
    when = local.strftime("%H:%M") if delta.total_seconds() < 86400 else local.strftime("%m-%d")
    return f"{pct} ↻{when}"


class ServiceCard(QFrame):
    """One service: name + close button + dual ring + two reset legend lines."""

    closed = Signal(str)

    def __init__(self, service: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedWidth(CARD_WIDTH)
        self._service = service
        self._inner_color = accent
        self._outer_color = "#ffffff"  # 与圆环外圈一致

        name = QLabel(service)
        name.setObjectName("serviceName")
        name.setStyleSheet(f"color: {accent};")

        close_btn = QPushButton("×")
        close_btn.setObjectName("closeCard")
        close_btn.setFixedSize(18, 18)
        close_btn.setToolTip(f"Remove {service}")
        close_btn.clicked.connect(lambda: self.closed.emit(self._service))

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(close_btn)

        self.ring = DoubleRing(accent)

        self.inner_label = QLabel("—")
        self.inner_label.setObjectName("legend")
        self.inner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.outer_label = QLabel("—")
        self.outer_label.setObjectName("legend")
        self.outer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        layout.addLayout(top)
        layout.addWidget(self.ring, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.inner_label)
        layout.addWidget(self.outer_label)

    def _legend(self, stat: RingStat | None, color: str) -> str:
        if stat is None:
            return f'<span style="color:{color}">●</span> n/a'
        return f'<span style="color:{color}">●</span> {stat.label} {_fmt_reset(stat)}'

    def update_snapshot(self, snap: UsageSnapshot) -> None:
        if not snap.ok:
            self.ring.set_stats(None, None)
            self.inner_label.setText(
                f'<span style="color:#e57373">error</span>'
            )
            self.outer_label.setText("—")
            self.setToolTip(snap.error or "Unknown error")
            return
        self.ring.set_stats(
            snap.inner.remaining_percent if snap.inner else None,
            snap.outer.remaining_percent if snap.outer else None,
        )
        self.inner_label.setText(self._legend(snap.inner, self._inner_color))
        self.outer_label.setText(self._legend(snap.outer, self._outer_color))
        self.setToolTip("\n".join(snap.detail_lines))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("API Usage Monitor")

        self._cards: dict[str, ServiceCard] = {}
        self._signals = FetchSignals()
        self._signals.result.connect(self._on_result)
        self._pool = QThreadPool(self)
        self._pending = 0
        self._selected = self._load_config()

        # Header: service selector dropdown + updated time + refresh button
        # The dropdown always lists ALL services with checkboxes;
        # clicking an item toggles whether that card is shown.
        self.select_combo = QComboBox()
        self.select_model = QStandardItemModel(self.select_combo)
        for service, _ in SERVICES:
            item = QStandardItem(service)
            item.setCheckable(True)
            item.setCheckState(
                Qt.CheckState.Checked
                if service in self._selected
                else Qt.CheckState.Unchecked
            )
            self.select_model.appendRow(item)
        self.select_combo.setModel(self.select_model)
        self.select_combo.setEditable(True)
        line_edit = self.select_combo.lineEdit()
        line_edit.setReadOnly(True)
        line_edit.setText("+ Select")
        line_edit.installEventFilter(self)  # click text -> open popup
        # Default combo views only toggle when clicking the tiny checkbox;
        # intercept viewport clicks so clicking the whole row toggles,
        # and keep the popup open for multiple toggles.
        self.select_combo.view().viewport().installEventFilter(self)
        self.select_model.itemChanged.connect(self._on_item_toggled)
        self.updated_label = QLabel("—")
        self.updated_label.setObjectName("updated")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_all)

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.select_combo)
        header.addStretch(1)
        header.addWidget(self.updated_label)
        header.addWidget(self.refresh_btn)

        # Cards row (+ empty-state hint)
        self.hint_label = QLabel("Use “+ Select” to show a service")
        self.hint_label.setObjectName("hint")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(6)
        self.cards_row.addWidget(self.hint_label)

        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(8, 6, 8, 8)
        root_layout.setSpacing(6)
        root_layout.addLayout(header)
        root_layout.addLayout(self.cards_row)

        central = QWidget()
        central.setLayout(root_layout)
        self.setCentralWidget(central)

        for service in self._selected:
            self._add_card(service, fetch=False)
        self._sync_layout()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self.refresh_all)
        self._refresh_timer.start()

        self.refresh_all()

    # -- selection persistence --

    def _load_config(self) -> list[str]:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            known = [s for s, _ in SERVICES]
            return [s for s in data.get("services", []) if s in known]
        except Exception:
            return [s for s, _ in SERVICES]

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps({"services": list(self._cards)}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # -- card management --

    def _add_card(self, service: str, fetch: bool = True) -> None:
        if service in self._cards:
            return
        card = ServiceCard(service, SERVICE_COLORS[service])
        card.closed.connect(self._remove_card)
        self._cards[service] = card
        self.cards_row.addWidget(card)
        self._set_checked(service, True)
        self._sync_layout()
        self._save_config()
        if fetch:
            self._pending += 1
            self.refresh_btn.setEnabled(False)
            self._pool.start(FetchWorker(service, self._signals))

    def _remove_card(self, service: str) -> None:
        card = self._cards.pop(service, None)
        if card is None:
            return
        self.cards_row.removeWidget(card)
        card.deleteLater()
        self._set_checked(service, False)
        self._sync_layout()
        self._save_config()

    def _sync_layout(self) -> None:
        """Update empty-state hint and window width."""
        self.hint_label.setVisible(not self._cards)
        n = max(len(self._cards), 1)
        self.setFixedSize(16 + n * (CARD_WIDTH + 6), WINDOW_HEIGHT)

    def eventFilter(self, obj, event) -> bool:
        # Clicking the read-only "+ Select" text opens the dropdown
        if (
            obj is self.select_combo.lineEdit()
            and event.type() == QEvent.Type.MouseButtonPress
        ):
            self.select_combo.showPopup()
            return True
        # Dropdown rows: click anywhere on a row toggles its checkbox.
        # Consuming press+release prevents the native checkbox double-toggle
        # and keeps the popup open.
        if obj is self.select_combo.view().viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                index = self.select_combo.view().indexAt(event.position().toPoint())
                if index.isValid():
                    item = self.select_model.itemFromIndex(index)
                    if item is not None and item.isCheckable():
                        item.setCheckState(
                            Qt.CheckState.Unchecked
                            if item.checkState() == Qt.CheckState.Checked
                            else Qt.CheckState.Checked
                        )
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                return True
        return super().eventFilter(obj, event)

    def _on_item_toggled(self, item: QStandardItem) -> None:
        service = item.text()
        if item.checkState() == Qt.CheckState.Checked:
            self._add_card(service)
        else:
            self._remove_card(service)

    def _set_checked(self, service: str, checked: bool) -> None:
        """Sync a dropdown checkbox without re-triggering itemChanged."""
        items = self.select_model.findItems(service)
        if items:
            self.select_model.blockSignals(True)
            items[0].setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.select_model.blockSignals(False)

    # -- refresh cycle --

    def refresh_all(self) -> None:
        if self._pending or not self._cards:
            return
        self._pending = len(self._cards)
        self.refresh_btn.setEnabled(False)
        self.updated_label.setText("…")
        for service in self._cards:
            self._pool.start(FetchWorker(service, self._signals))

    def _on_result(self, service: str, snap: UsageSnapshot) -> None:
        card = self._cards.get(service)
        if card is not None:
            card.update_snapshot(snap)
        self._pending -= 1
        if self._pending <= 0:
            self._pending = 0
            self.refresh_btn.setEnabled(True)
            self.updated_label.setText(datetime.now().strftime("%H:%M"))


def _resource(name: str) -> Path:
    """Locate a bundled resource, both from source and inside the frozen exe."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).parent
    return Path(base) / name


def main() -> None:
    from PySide6.QtGui import QIcon

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    icon_path = _resource("icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
