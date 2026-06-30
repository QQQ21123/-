from __future__ import annotations

import json
import math
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import cv2
except Exception:
    cv2 = None

from PySide6.QtCore import QEvent, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QTimer, Qt, Signal, QSize
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from photostepper_pro import (
    AXES,
    APP_DIR,
    ASSET_DIR,
    DEFAULT_RECIPE_PATH,
    AxisLimits,
    ExposureRecipe,
    InspectionRecipe,
    IORecipe,
    JogRecipe,
    KeepoutZone,
    MotionPlanner,
    MotionRecipe,
    PlannedCommand,
    Recipe,
    RecipeCodec,
    LIST_PORTS_IMPORT_ERROR,
    SerialGcodeTransport,
    SerialRecipe,
    StageRecipe,
    UIRecipe,
    USER_RECIPE_PATH,
    format_axis_value,
    list_ports,
    parse_waypoints,
    waypoint_endpoint,
    waypoints_to_text,
)


def asset_path(name: str) -> Path:
    return ASSET_DIR / name


def pixmap_for(name: str) -> QPixmap:
    path = asset_path(name)
    return QPixmap(str(path)) if path.exists() else QPixmap()


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)


def keyed_die_values_to_text(values: Dict[str, float]) -> str:
    if not values:
        return ""
    items = []
    for key in sorted(values, key=lambda item: int(item)):
        items.append(f"{int(key)}:{float(values[key]):g}")
    return ",".join(items)


class Card(QFrame):
    def __init__(self, title: Optional[str] = None, subtitle: Optional[str] = None, *, soft: bool = False):
        super().__init__()
        self.setObjectName("softCard" if soft else "card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(28, 35, 48, 28))
        self.setGraphicsEffect(shadow)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(24, 22, 24, 22)
        self.layout.setSpacing(14)
        if title:
            header = QVBoxLayout()
            header.setSpacing(3)
            label = QLabel(title)
            label.setObjectName("cardTitle")
            header.addWidget(label)
            if subtitle:
                sub = QLabel(subtitle)
                sub.setObjectName("muted")
                sub.setWordWrap(True)
                header.addWidget(sub)
            self.layout.addLayout(header)
            line = QFrame()
            line.setObjectName("hairline")
            line.setFixedHeight(1)
            self.layout.addWidget(line)
        elif subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("muted")
            sub.setWordWrap(True)
            self.layout.addWidget(sub)


class PillButton(QPushButton):
    def __init__(self, text: str, variant: str = "primary"):
        super().__init__(text)
        self.setObjectName(f"pill_{variant}")
        self.setMinimumHeight(42)
        self.setCursor(Qt.PointingHandCursor)


class RecipeSaveDialog(QDialog):
    def __init__(self, recipe_name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("recipeSaveDialog")
        self.setWindowTitle("Save recipe")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(470)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 18)
        shadow.setColor(QColor(28, 35, 48, 46))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 26)
        layout.setSpacing(18)

        badge = QLabel("RECIPE CHANGE")
        badge.setObjectName("dialogBadge")
        badge.setFixedWidth(132)
        layout.addWidget(badge)

        title = QLabel("Save these settings?")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        body = QLabel(
            f"The current settings changed in memory.\nSave them to {recipe_name} so they stay after restart or update?"
        )
        body.setObjectName("dialogBody")
        body.setWordWrap(True)
        layout.addWidget(body)

        line = QFrame()
        line.setObjectName("hairline")
        line.setFixedHeight(1)
        layout.addWidget(line)

        row = QHBoxLayout()
        row.addStretch()
        no_btn = PillButton("Not now", "soft")
        yes_btn = PillButton("Save", "primary")
        no_btn.setMinimumWidth(132)
        yes_btn.setMinimumWidth(132)
        no_btn.clicked.connect(self.reject)
        yes_btn.clicked.connect(self.accept)
        row.addWidget(no_btn)
        row.addWidget(yes_btn)
        layout.addLayout(row)


class MetricTile(QFrame):
    def __init__(self, axis: str):
        super().__init__()
        self.setObjectName("metricTile")
        self.setMinimumHeight(78)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(14, 10, 14, 10)
        self.layout.setSpacing(3)
        axis_label = QLabel(axis)
        axis_label.setObjectName("metricAxis")
        self.value = QLabel("0.000")
        self.value.setObjectName("metric")
        self.layout.addWidget(axis_label)
        self.layout.addWidget(self.value)


class SmoothExposureBar(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("smoothExposureBar")
        self.setMinimumHeight(24)
        self.setMaximumHeight(24)
        self._target = 0.0
        self._display = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(16)

    def setValue(self, value: int):
        self._target = max(0.0, min(1.0, float(value) / 1000.0))
        if value <= 0 or value >= 1000:
            self._display = self._target
        self.update()

    def setRange(self, _minimum: int, _maximum: int):
        pass

    def setTextVisible(self, _visible: bool):
        pass

    def _animate(self):
        self._phase = (self._phase + 0.0045) % 1.0
        delta = self._target - self._display
        if abs(delta) > 0.0005:
            self._display += delta * 0.10
        else:
            self._display = self._target
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 2, 0, -2)
        radius = rect.height() / 2

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#E7EDF6"))
        painter.drawRoundedRect(QRectF(rect), radius, radius)

        inset = rect.adjusted(2, 2, -2, -2)
        fill_w = max(inset.height(), inset.width() * self._display)
        fill = QRectF(inset.left(), inset.top(), fill_w, inset.height())
        path = QPainterPath()
        path.addRoundedRect(fill, inset.height() / 2, inset.height() / 2)

        grad = QLinearGradient(fill.left(), 0, fill.right(), 0)
        grad.setColorAt(0.00, QColor("#0071E3"))
        grad.setColorAt(0.42, QColor("#58AFFF"))
        grad.setColorAt(0.78, QColor("#B8E7FF"))
        grad.setColorAt(1.00, QColor("#1D1D1F"))
        painter.setBrush(grad)
        painter.drawPath(path)

        shine_x = fill.left() + fill.width() * self._phase
        shine = QLinearGradient(shine_x - 80, 0, shine_x + 80, 0)
        shine.setColorAt(0.0, QColor(255, 255, 255, 0))
        shine.setColorAt(0.5, QColor(255, 255, 255, 92))
        shine.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(shine)
        painter.drawPath(path)

        painter.setPen(QPen(QColor(255, 255, 255, 120), 1))
        painter.drawRoundedRect(QRectF(inset), inset.height() / 2, inset.height() / 2)


class ExposureProgressPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("exposurePanel")
        self.durations: Dict[int, float] = {}
        self.current_die: Optional[int] = None
        self.current_seconds = 0.0
        self.current_started_at = 0.0
        self.die_labels: Dict[int, QLabel] = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        eyebrow = QLabel("H-LINE EXPOSURE")
        eyebrow.setObjectName("exposureEyebrow")
        title = QLabel("Die Exposure Monitor")
        title.setObjectName("exposureTitle")
        subtitle = QLabel("Snake order: 1-2-3 / 6-5-4 / 7-8-9")
        subtitle.setObjectName("muted")
        title_col.addWidget(eyebrow)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        self.state_badge = QLabel("STANDBY")
        self.state_badge.setObjectName("exposureStateIdle")
        header.addLayout(title_col, 1)
        header.addWidget(self.state_badge)
        layout.addLayout(header)

        self.current_label = QLabel("No exposure running")
        self.current_label.setObjectName("exposureCurrent")
        self.time_label = QLabel("Waiting for an exposure step")
        self.time_label.setObjectName("muted")
        layout.addWidget(self.current_label)
        layout.addWidget(self.time_label)

        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("exposurePercent")
        percent_row = QHBoxLayout()
        percent_row.addStretch()
        percent_row.addWidget(self.percent_label)
        layout.addLayout(percent_row)

        self.progress = SmoothExposureBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setObjectName("exposureProgress")
        layout.addWidget(self.progress)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for row, die_row in enumerate(((1, 2, 3), (6, 5, 4), (7, 8, 9))):
            for col, die in enumerate(die_row):
                pill = QLabel(f"Die {die}\nIDLE")
                pill.setAlignment(Qt.AlignCenter)
                pill.setObjectName("dieIdle")
                self.die_labels[die] = pill
                grid.addWidget(pill, row, col)
        layout.addLayout(grid)

    def load_plan(self, durations: Dict[int, float]):
        self.timer.stop()
        self.durations = {int(die): float(seconds) for die, seconds in durations.items()}
        self.current_die = None
        self.current_seconds = 0.0
        self.progress.setValue(0)
        if self.durations:
            self.state_badge.setText("READY")
            self.state_badge.setObjectName("exposureStateReady")
            self.current_label.setText(f"{len(self.durations)} die exposure plan ready")
            self.time_label.setText("Planned dies are waiting for exposure.")
            self.percent_label.setText("0%")
        else:
            self.state_badge.setText("NO EXPOSURE")
            self.state_badge.setObjectName("exposureStateIdle")
            self.current_label.setText("No exposure in this step")
            self.time_label.setText("Motion-only route or setup step.")
            self.percent_label.setText("0%")
        self.repolish(self.state_badge)
        for die, label in self.die_labels.items():
            if die in self.durations:
                label.setText(f"Die {die}\nWAIT {self.durations[die]:g}s")
                label.setObjectName("diePending")
            else:
                label.setText(f"Die {die}\nIDLE")
                label.setObjectName("dieIdle")
            self.repolish(label)

    def handle_event(self, event: Dict[str, object]):
        kind = str(event.get("type", ""))
        if kind == "plan":
            raw = event.get("durations", {})
            durations = raw if isinstance(raw, dict) else {}
            self.load_plan({int(k): float(v) for k, v in durations.items()})
            return
        if kind == "start":
            die = event.get("die")
            seconds = float(event.get("seconds", 0.0) or 0.0)
            self.start_exposure(int(die) if die is not None else None, seconds)
            return
        if kind == "progress":
            seconds = float(event.get("seconds", self.current_seconds) or self.current_seconds)
            elapsed = float(event.get("elapsed", 0.0) or 0.0)
            self.set_progress(elapsed, seconds)
            return
        if kind == "done":
            die = event.get("die", self.current_die)
            self.finish_die(int(die) if die is not None else self.current_die, True)
            return
        if kind == "finish":
            ok = bool(event.get("ok", False))
            if not ok:
                self.finish_die(self.current_die, False)
            elif self.current_die is None and self.durations:
                self.state_badge.setText("COMPLETE")
                self.state_badge.setObjectName("exposureStateDone")
                self.current_label.setText("Exposure sequence complete")
                self.time_label.setText("All selected die exposures are complete.")
                self.repolish(self.state_badge)

    def start_exposure(self, die: Optional[int], seconds: float):
        self.current_die = die
        self.current_seconds = max(0.0, float(seconds))
        self.current_started_at = time.monotonic()
        self.progress.setValue(0)
        self.percent_label.setText("0%")
        die_text = f"Die {die}" if die is not None else "Exposure"
        self.current_label.setText(f"{die_text} exposing")
        self.time_label.setText(f"0.0 / {self.current_seconds:.1f}s")
        self.state_badge.setText("LIVE")
        self.state_badge.setObjectName("exposureStateLive")
        self.repolish(self.state_badge)
        if die in self.die_labels:
            label = self.die_labels[die]
            label.setText(f"Die {die}\nLIVE")
            label.setObjectName("dieActive")
            self.repolish(label)
        self.timer.start(33)

    def tick(self):
        if self.current_die is None and self.current_seconds <= 0:
            self.timer.stop()
            return
        elapsed = time.monotonic() - self.current_started_at
        self.set_progress(elapsed, self.current_seconds)

    def set_progress(self, elapsed: float, seconds: float):
        seconds = max(0.0, float(seconds))
        elapsed = max(0.0, min(float(elapsed), seconds if seconds > 0 else float(elapsed)))
        value = 1000 if seconds <= 0 else int(min(1000, elapsed / seconds * 1000))
        self.progress.setValue(value)
        remaining = max(0.0, seconds - elapsed)
        percent = 100 if seconds <= 0 else int(min(100, elapsed / seconds * 100))
        self.percent_label.setText(f"{percent}%")
        die_text = f"Die {self.current_die}" if self.current_die is not None else "Exposure"
        self.current_label.setText(f"{die_text} exposing")
        self.time_label.setText(f"{elapsed:.1f} / {seconds:.1f}s   remaining {remaining:.1f}s")

    def finish_die(self, die: Optional[int], ok: bool):
        self.timer.stop()
        if die is not None and die in self.die_labels:
            label = self.die_labels[die]
            if ok:
                label.setText(f"Die {die}\nDONE")
                label.setObjectName("dieDone")
            else:
                label.setText(f"Die {die}\nFAULT")
                label.setObjectName("dieFault")
            self.repolish(label)
        if ok:
            self.progress.setValue(1000)
            self.percent_label.setText("100%")
            self.state_badge.setText("DONE")
            self.state_badge.setObjectName("exposureStateDone")
            self.current_label.setText(f"Die {die} exposure complete" if die else "Exposure complete")
            self.time_label.setText("Ready for the next die.")
        else:
            self.state_badge.setText("FAULT")
            self.percent_label.setText("STOP")
            self.state_badge.setObjectName("exposureStateFault")
            self.current_label.setText("Exposure interrupted")
            self.time_label.setText("Check log, UV state, and controller alarm before continuing.")
        self.repolish(self.state_badge)
        self.current_die = None
        self.current_seconds = 0.0

    def repolish(self, widget: QWidget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


class ExposureProgressDialog(QDialog):
    def __init__(self, stop_handler, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("exposureDialog")
        self.setWindowTitle("Exposure Status")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(False)
        self.setMinimumWidth(620)
        self.setMaximumWidth(760)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(42)
        shadow.setOffset(0, 18)
        shadow.setColor(QColor(28, 35, 48, 58))
        self.setGraphicsEffect(shadow)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        shell = QFrame()
        shell.setObjectName("exposureDialogShell")
        root.addWidget(shell)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        self.panel = ExposureProgressPanel()
        layout.addWidget(self.panel)

        actions = QHBoxLayout()
        hint = QLabel("Emergency stop for exposure sequence. Check UV and controller state after stop.")
        hint.setObjectName("muted")
        actions.addWidget(hint, 1)
        close_btn = PillButton("Hide", "soft")
        stop_btn = PillButton("Stop Exposure", "danger")
        close_btn.setMinimumWidth(112)
        stop_btn.setMinimumWidth(164)
        close_btn.clicked.connect(self.hide)
        stop_btn.clicked.connect(stop_handler)
        actions.addWidget(close_btn)
        actions.addWidget(stop_btn)
        layout.addLayout(actions)

    def closeEvent(self, event):
        self.hide()
        event.ignore()


class SplashPage(QWidget):
    done = Signal()

    def __init__(self):
        super().__init__()
        self.phase = 0.0
        self.logo = pixmap_for("splash_logo.png")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)
        QTimer.singleShot(5200, self.done.emit)

    def tick(self):
        self.phase = min(1.0, self.phase + 0.0044)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor("#FFFFFF"))

        if not self.logo.isNull():
            target = QSize(max(1, int(rect.width() * 0.92)), max(1, int(rect.height() * 0.82)))
            scaled = self.logo.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = (rect.width() - scaled.width()) // 2
            y = max(24, (rect.height() - scaled.height()) // 2 - 18)
            painter.drawPixmap(x, y, scaled)

        bar_w = min(620, int(rect.width() * 0.42))
        bar_h = 6
        bar_x = (rect.width() - bar_w) / 2
        bar_y = rect.height() - 66
        eased = 1 - pow(1 - self.phase, 3)
        progress = min(1.0, eased)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#E3E7EE"))
        painter.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 3, 3)
        painter.setBrush(QColor("#1D1D1F"))
        painter.drawRoundedRect(bar_x, bar_y, bar_w * progress, bar_h, 3, 3)

        for idx in range(3):
            dot_phase = min(1.0, max(0.0, self.phase - idx * 0.12))
            radius = 3 + 3 * dot_phase
            cx = rect.width() / 2 - 18 + idx * 18
            cy = rect.height() - 34
            painter.setBrush(QColor("#1D1D1F"))
            painter.drawEllipse(QPointF(cx, cy), radius, radius)


class RouteWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.recipe: Optional[Recipe] = None
        self.route_mode = "primary"
        self.phase = 0.0
        self.live_xy: Optional[Tuple[float, float]] = None
        self.setMinimumHeight(560)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(24)

    def tick(self):
        self.phase = (self.phase + 0.0016) % 1
        self.update()

    def set_recipe(self, recipe: Recipe):
        self.recipe = recipe
        self.live_xy = (recipe.stage.initial_x, recipe.stage.initial_y)
        self.update()

    def set_route_mode(self, mode: str):
        self.route_mode = "second" if mode == "second" else "primary"
        self.update()

    def set_live_position(self, x: Optional[float], y: Optional[float]):
        if x is None or y is None:
            return
        self.live_xy = (float(x), float(y))
        self.update()

    def _route_sections(self, r: Recipe) -> Dict[str, List[Tuple[float, float]]]:
        def compact(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
            out: List[Tuple[float, float]] = []
            for point in points:
                if not out or math.hypot(out[-1][0] - point[0], out[-1][1] - point[1]) > 1e-9:
                    out.append(point)
            return out

        def apply(start_x: float, start_y: float, waypoints: List[Dict[str, float]]) -> Tuple[List[Tuple[float, float]], float, float]:
            cur_x, cur_y = start_x, start_y
            local = [(cur_x, cur_y)]
            for wp in waypoints:
                axis = next(iter(wp)).upper()
                value = float(wp[next(iter(wp))])
                if axis == "X":
                    cur_x = value
                elif axis == "Y":
                    cur_y = value
                else:
                    continue
                local.append((cur_x, cur_y))
            return compact(local), cur_x, cur_y

        is_second = self.route_mode == "second"
        load, _cx, _cy = apply(r.stage.initial_x, r.stage.initial_y, r.to_loading_waypoints)
        if is_second:
            exposure, cx, cy = apply(r.stage.load_x, r.stage.load_y, r.to_camera_alignment_waypoints)
            planner_for_camera = MotionPlanner(r)
            cam_x, cam_y = planner_for_camera.calculate_camera_die1_stage_position()
            camera_tail = list(exposure)
            if abs(cx - cam_x) > 1e-9:
                cx = cam_x
                camera_tail.append((cx, cy))
            if abs(cy - cam_y) > 1e-9:
                cy = cam_y
                camera_tail.append((cx, cy))
            exposure = compact(camera_tail)
        else:
            exposure, cx, cy = apply(r.stage.load_x, r.stage.load_y, r.to_exposure_waypoints)

        grid: List[Tuple[float, float]] = [(cx, cy)]
        cur_x, cur_y = cx, cy
        exposure_planner = MotionPlanner(r, use_camera_alignment=is_second)
        for _die, _row, _col, x, y, _selected in exposure_planner.visited_exposure_positions():
            if abs(cur_x - x) > 1e-9:
                cur_x = x
                grid.append((cur_x, cur_y))
            if abs(cur_y - y) > 1e-9:
                cur_y = y
                grid.append((cur_x, cur_y))
        grid = compact(grid)

        if grid:
            cx, cy = grid[-1]
        ret, _cx, _cy = apply(cx, cy, r.return_waypoints)
        if is_second:
            return {
                "To Load": load,
                "To Camera": exposure,
                "2nd Exposure": grid,
                "Return": ret,
            }
        return {"To Load": load, "To Exposure": exposure, "Step Repeat": grid, "Return": ret}

    def _all_route_points(self, sections: Dict[str, List[Tuple[float, float]]]) -> List[Tuple[float, float]]:
        points: List[Tuple[float, float]] = []
        for section in sections.values():
            for point in section:
                if not points or math.hypot(points[-1][0] - point[0], points[-1][1] - point[1]) > 1e-9:
                    points.append(point)
        return points

    def _orthogonalize(self, points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if len(points) < 2:
            return points
        out = [points[0]]
        for x, y in points[1:]:
            prev_x, prev_y = out[-1]
            if abs(prev_x - x) > 1e-9 and abs(prev_y - y) > 1e-9:
                out.append((x, prev_y))
            out.append((x, y))
        compact: List[Tuple[float, float]] = []
        for point in out:
            if not compact or math.hypot(compact[-1][0] - point[0], compact[-1][1] - point[1]) > 1e-9:
                compact.append(point)
        return compact

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor("#F8FAFD"))

        gradient = QLinearGradient(0, 0, rect.width(), rect.height())
        gradient.setColorAt(0.0, QColor("#FFFFFF"))
        gradient.setColorAt(1.0, QColor("#EEF3F9"))
        painter.fillRect(rect, gradient)

        if not self.recipe:
            return
        r = self.recipe
        lim = r.limits
        span_x = max(1.0, lim.x_max - lim.x_min)
        span_y = max(1.0, lim.y_max - lim.y_min)
        plot_margin_x = span_x * 0.16
        plot_margin_y = span_y * 0.18
        plot_x_min = lim.x_min - plot_margin_x
        plot_x_max = lim.x_max + plot_margin_x
        plot_y_min = lim.y_min - plot_margin_y
        plot_y_max = lim.y_max + plot_margin_y
        plot_span_x = max(1.0, plot_x_max - plot_x_min)
        plot_span_y = max(1.0, plot_y_max - plot_y_min)
        pad_left = 40
        pad_top = 112
        pad_right = 34
        pad_bottom = 34
        scale = min((rect.width() - pad_left - pad_right) / plot_span_x, (rect.height() - pad_top - pad_bottom) / plot_span_y)

        section_colors = {
            "Start / Live": QColor("#007AFF"),
            "To Load": QColor("#374151"),
            "To Exposure": QColor("#007AFF"),
            "To Camera": QColor("#5E5CE6"),
            "Step Repeat": QColor("#30D158"),
            "2nd Exposure": QColor("#FF9F0A"),
            "Return": QColor("#A66A00"),
        }
        is_second = self.route_mode == "second"

        painter.setFont(QFont("Segoe UI Variable Display", 8, QFont.Bold))
        legend_items = [
            ("Start / Live", section_colors["Start / Live"]),
            ("To Load", section_colors["To Load"]),
            ("To Camera" if is_second else "To Exposure", section_colors["To Camera" if is_second else "To Exposure"]),
            ("2nd Exposure" if is_second else "Step Repeat", section_colors["2nd Exposure" if is_second else "Step Repeat"]),
            ("Return", section_colors["Return"]),
        ]
        legend_x = 18.0
        legend_y = 22.0
        for label, color in legend_items:
            text_w = painter.fontMetrics().horizontalAdvance(label)
            pill_w = text_w + 30
            if legend_x + pill_w > rect.width() - 18:
                legend_x = 18.0
                legend_y += 34.0
            painter.setPen(QPen(QColor("#E1E6EF"), 1))
            painter.setBrush(QColor(255, 255, 255, 205))
            painter.drawRoundedRect(legend_x, legend_y, pill_w, 28, 14, 14)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(legend_x + 13, legend_y + 14), 4.2, 4.2)
            painter.setPen(QColor("#283142"))
            painter.drawText(legend_x + 23, legend_y + 18, label)
            legend_x += pill_w + 8

        mode_label = "2ND PROCESS ROUTE" if is_second else "1ST PROCESS ROUTE"
        painter.setFont(QFont("Segoe UI Variable Display", 9, QFont.Bold))
        mode_w = painter.fontMetrics().horizontalAdvance(mode_label)
        painter.setPen(QPen(QColor("#E1E6EF"), 1))
        painter.setBrush(QColor(255, 255, 255, 218))
        painter.drawRoundedRect(rect.width() - mode_w - 42, 22, mode_w + 24, 28, 14, 14)
        painter.setPen(QColor("#1D1D1F"))
        painter.drawText(rect.width() - mode_w - 30, 41, mode_label)

        def map_xy(x: float, y: float) -> QPointF:
            return QPointF(pad_left + (x - plot_x_min) * scale, pad_top + (y - plot_y_min) * scale)

        stage = QPainterPath()
        stage_min = map_xy(plot_x_min, plot_y_min)
        stage_max = map_xy(plot_x_max, plot_y_max)
        x0, y0 = stage_min.x(), stage_min.y()
        w, h = stage_max.x() - stage_min.x(), stage_max.y() - stage_min.y()
        stage.addRoundedRect(x0, y0, w, h, 24, 24)
        painter.setPen(QPen(QColor("#D7DEE9"), 1.2))
        painter.setBrush(QColor("#FCFDFF"))
        painter.drawPath(stage)

        painter.setPen(QPen(QColor("#EEF2F7"), 1))
        for i in range(1, 6):
            px = x0 + w * i / 7
            py = y0 + h * i / 7
            painter.drawLine(QPointF(px, y0), QPointF(px, y0 + h))
            painter.drawLine(QPointF(x0, py), QPointF(x0 + w, py))

        soft = QPainterPath()
        soft_min = map_xy(lim.x_min, lim.y_min)
        soft_max = map_xy(lim.x_max, lim.y_max)
        soft.addRoundedRect(soft_min.x(), soft_min.y(), soft_max.x() - soft_min.x(), soft_max.y() - soft_min.y(), 18, 18)
        painter.setPen(QPen(QColor("#DCE3ED"), 1.0, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(soft)

        origin = map_xy(0, 0)
        painter.setPen(QPen(QColor("#A7B0BE"), 1.1))
        painter.setBrush(QColor("#111111"))
        painter.drawEllipse(origin, 4.0, 4.0)
        painter.setFont(QFont("Segoe UI Variable Display", 8, QFont.Bold))
        painter.setPen(QColor("#4E596C"))
        painter.drawText(origin.x() + 8, origin.y() + 15, "START 0,0")

        def draw_tag(label: str, x: float, y: float, color: QColor, dx: float = 10.0, dy: float = -10.0):
            p = map_xy(x, y)
            painter.setFont(QFont("Segoe UI Variable Display", 8, QFont.Bold))
            text_w = painter.fontMetrics().horizontalAdvance(label)
            tag_x = max(6.0, min(rect.width() - text_w - 26.0, p.x() + dx))
            tag_y = max(58.0, min(rect.height() - 24.0, p.y() + dy))
            painter.setPen(QPen(QColor("#E1E6EF"), 1))
            painter.setBrush(QColor(255, 255, 255, 225))
            painter.drawRoundedRect(tag_x, tag_y - 16, text_w + 20, 24, 12, 12)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(tag_x + 9, tag_y - 4), 3.4, 3.4)
            painter.setPen(QColor("#283142"))
            painter.drawText(tag_x + 16, tag_y, label)

        def draw_target_marker(label: str, x: float, y: float, color: QColor, dx: float = 14.0, dy: float = -18.0):
            p = map_xy(x, y)
            painter.setPen(QPen(QColor(255, 255, 255, 230), 7, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(p.x() - 15, p.y()), QPointF(p.x() + 15, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 15), QPointF(p.x(), p.y() + 15))
            painter.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(QColor(255, 255, 255, 235))
            painter.drawEllipse(p, 13.5, 13.5)
            painter.drawLine(QPointF(p.x() - 15, p.y()), QPointF(p.x() + 15, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 15), QPointF(p.x(), p.y() + 15))
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(p, 4.2, 4.2)
            draw_tag(label, x, y, color, dx, dy)

        if r.keepout.enabled:
            k = r.keepout
            a = map_xy(k.x_min, k.y_min)
            b = map_xy(k.x_max, k.y_max)
            painter.setPen(QPen(QColor("#FF3B30"), 2, Qt.DashLine))
            painter.setBrush(QColor(255, 59, 48, 28))
            painter.drawRoundedRect(min(a.x(), b.x()), min(a.y(), b.y()), abs(b.x() - a.x()), abs(b.y() - a.y()), 16, 16)

        sections = self._route_sections(r)

        for name, points in sections.items():
            orthogonal_points = self._orthogonalize(points)
            mapped = [map_xy(x, y) for x, y in orthogonal_points]
            if len(mapped) < 2:
                continue
            color = section_colors[name]
            painter.setPen(QPen(QColor(255, 255, 255, 225), 8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            for a, b in zip(mapped, mapped[1:]):
                painter.drawLine(a, b)
            painter.setPen(QPen(color, 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            for a, b in zip(mapped, mapped[1:]):
                painter.drawLine(a, b)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            for p in mapped:
                painter.drawEllipse(p, 2.4, 2.4)

        planner = MotionPlanner(r, use_camera_alignment=is_second)
        selected = set(planner.selected_die_numbers())
        grid_points = [(die, x, y, die in selected) for die, _row, _col, x, y in planner.exposure_positions()]
        die_radius = max(8.0, min(12.0, scale * 0.78))
        painter.setFont(QFont("Segoe UI Variable Display", 8, QFont.Bold))
        for die, x, y, is_selected in grid_points:
            p = map_xy(x, y)
            painter.setPen(QPen(QColor("#18A64A") if is_selected else QColor("#A7B0BE"), 1.8))
            painter.setBrush(QColor("#F6FFF8") if is_selected else QColor("#F5F7FA"))
            painter.drawEllipse(p, die_radius, die_radius)
            painter.setPen(QColor("#0C7A32") if is_selected else QColor("#778294"))
            text = str(die)
            tw = painter.fontMetrics().horizontalAdvance(text)
            painter.drawText(p.x() - tw / 2, p.y() + 3.2, text)

        draw_tag("LOAD", r.stage.load_x, r.stage.load_y, section_colors["To Load"], 12, -10)
        if is_second:
            i = r.inspection
            cam_x, cam_y = MotionPlanner(r).calculate_camera_die1_stage_position()
            draw_target_marker("CAM DIE 1", cam_x, cam_y, section_colors["To Camera"], 14, -20)
            if i.die1_center_alignment_active:
                draw_target_marker("MEASURED DIE 1", i.measured_die1_center_x, i.measured_die1_center_y, QColor("#FF2D55"), 14, 28)
        else:
            draw_tag("EXPOSURE REF", r.exposure.exposure_ref_x, r.exposure.exposure_ref_y, section_colors["Step Repeat"], 12, -18)

        full_points = self._orthogonalize(self._all_route_points(sections))
        marker = self._interpolate(full_points)
        if marker:
            p = map_xy(*marker)
            pulse = 5 + 2 * abs(math.sin(self.phase * math.tau))
            painter.setPen(QPen(QColor("#6B7280"), 1.1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(p, pulse, pulse)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#6B7280"))
            painter.drawEllipse(p, 3.2, 3.2)

        if self.live_xy:
            p = map_xy(*self.live_xy)
            pulse = 8 + 3 * abs(math.sin(self.phase * math.tau * 1.4))
            painter.setPen(QPen(QColor("#007AFF"), 2.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(p, pulse, pulse)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#007AFF"))
            painter.drawEllipse(p, 4.2, 4.2)

    def _interpolate(self, points: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
        if len(points) < 2:
            return None
        segments = []
        total = 0.0
        for a, b in zip(points, points[1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length > 1e-9:
                segments.append((a, b, length))
                total += length
        if total <= 1e-9:
            return points[-1]
        target = total * self.phase
        walked = 0.0
        for a, b, length in segments:
            if walked + length >= target:
                ratio = (target - walked) / length
                return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)
            walked += length
        return points[-1]


class CameraPreviewWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(360)
        self.camera_on = False
        self.stage_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.active_die: Optional[int] = None
        self.frame_info = "Camera standby"
        self.frame_pixmap = QPixmap()
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    def tick(self):
        self.phase = (self.phase + 0.012) % 1.0
        self.update()

    def set_camera_on(self, enabled: bool):
        self.camera_on = bool(enabled)
        self.frame_info = "Camera live" if enabled else "Camera standby"
        self.update()

    def set_frame_bgr(self, frame):
        if frame is None:
            return
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if cv2 is not None else frame
            h, w, ch = rgb.shape
            image = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
            self.frame_pixmap = QPixmap.fromImage(image)
            self.frame_info = f"OpenCV frame {w}x{h}"
            self.update()
        except Exception:
            pass

    def set_video_frame(self, frame):
        if frame is None or not frame.isValid():
            return
        try:
            image = frame.toImage()
            if image.isNull():
                return
            self.frame_pixmap = QPixmap.fromImage(image)
            self.camera_on = True
            self.frame_info = f"Qt camera frame {image.width()}x{image.height()}"
            self.update()
        except Exception:
            pass

    def set_stage_position(self, x: float, y: float, z: float):
        self.stage_pos = {"X": float(x), "Y": float(y), "Z": float(z)}
        self.update()

    def set_active_die(self, die: Optional[int]):
        self.active_die = die
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(rect, 28, 28)
        painter.save()
        painter.setClipPath(path)
        if not self.frame_pixmap.isNull():
            scaled = self.frame_pixmap.scaled(rect.size().toSize(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = rect.center().x() - scaled.width() / 2
            y = rect.center().y() - scaled.height() / 2
            painter.drawPixmap(int(x), int(y), scaled)
            painter.fillRect(rect, QColor(0, 0, 0, 42))
        else:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor("#111827"))
            gradient.setColorAt(0.55, QColor("#1F2937"))
            gradient.setColorAt(1.0, QColor("#05070A"))
            painter.fillPath(path, gradient)
        painter.restore()

        painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
        for i in range(1, 6):
            x = rect.left() + rect.width() * i / 6
            y = rect.top() + rect.height() * i / 6
            painter.drawLine(QPointF(x, rect.top() + 62), QPointF(x, rect.bottom() - 42))
            painter.drawLine(QPointF(rect.left() + 24, y), QPointF(rect.right() - 24, y))

        cx = rect.center().x()
        cy = rect.center().y()
        pulse = 28 + 5 * abs(math.sin(self.phase * math.tau))
        cross = QColor("#F5C542" if self.camera_on else "#A7B0BE")
        painter.setPen(QPen(cross, 2.0))
        painter.drawLine(QPointF(cx - 82, cy), QPointF(cx - 18, cy))
        painter.drawLine(QPointF(cx + 18, cy), QPointF(cx + 82, cy))
        painter.drawLine(QPointF(cx, cy - 82), QPointF(cx, cy - 18))
        painter.drawLine(QPointF(cx, cy + 18), QPointF(cx, cy + 82))
        painter.setPen(QPen(cross, 1.4))
        painter.drawEllipse(QPointF(cx, cy), pulse, pulse)
        painter.setBrush(cross)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(cx, cy), 4.5, 4.5)

        painter.setFont(QFont("Segoe UI Variable Display", 10, QFont.Bold))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect.left() + 24, rect.top() + 34, "CAMERA LIVE / ALIGNMENT")
        painter.setFont(QFont("Segoe UI Variable Display", 9, QFont.Normal))
        die_text = f"Die {self.active_die}" if self.active_die else "No die selected"
        pos = self.stage_pos
        hud = f"X{pos['X']:.3f}  Y{pos['Y']:.3f}  Z{pos['Z']:.3f}  |  {die_text}"
        painter.setPen(QColor("#D6DEE9"))
        painter.drawText(rect.left() + 24, rect.top() + 58, hud)

        status_color = QColor("#30D158" if self.camera_on else "#8B95A5")
        painter.setBrush(QColor(255, 255, 255, 24))
        painter.setPen(QPen(QColor(255, 255, 255, 42), 1))
        painter.drawRoundedRect(rect.right() - 170, rect.top() + 18, 142, 32, 16, 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(status_color)
        painter.drawEllipse(QPointF(rect.right() - 146, rect.top() + 34), 4.5, 4.5)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect.right() - 132, rect.top() + 39, "ON" if self.camera_on else "STANDBY")

        painter.setFont(QFont("Segoe UI Variable Display", 8, QFont.Normal))
        painter.setPen(QColor("#AEB8C7"))
        painter.drawText(rect.left() + 24, rect.bottom() - 22, f"{self.frame_info} | Crosshair ON")


class CameraCrosshairOverlay(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        if parent is None:
            self.setWindowFlags(
                Qt.Tool
                | Qt.FramelessWindowHint
                | Qt.WindowStaysOnTopHint
                | Qt.WindowDoesNotAcceptFocus
                | Qt.WindowTransparentForInput
            )
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.stage_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.active_die: Optional[int] = None
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    def tick(self):
        self.phase = (self.phase + 0.012) % 1.0
        self.update()

    def set_stage_position(self, x: float, y: float, z: float):
        self.stage_pos = {"X": float(x), "Y": float(y), "Z": float(z)}
        self.update()

    def set_active_die(self, die: Optional[int]):
        self.active_die = die
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(10, 10, -10, -10)
        cx = rect.center().x()
        cy = rect.center().y()
        reticle = QColor("#7DF9FF")
        reticle_soft = QColor(125, 249, 255, 96)
        white_soft = QColor(255, 255, 255, 150)

        painter.setPen(QPen(QColor(0, 0, 0, 110), 2.6, Qt.SolidLine, Qt.RoundCap))
        for gap, length in ((10, 96), (128, 180)):
            painter.drawLine(QPointF(cx - length, cy), QPointF(cx - gap, cy))
            painter.drawLine(QPointF(cx + gap, cy), QPointF(cx + length, cy))
            painter.drawLine(QPointF(cx, cy - length), QPointF(cx, cy - gap))
            painter.drawLine(QPointF(cx, cy + gap), QPointF(cx, cy + length))

        painter.setPen(QPen(reticle, 1.05, Qt.SolidLine, Qt.RoundCap))
        for gap, length in ((10, 96), (128, 180)):
            painter.drawLine(QPointF(cx - length, cy), QPointF(cx - gap, cy))
            painter.drawLine(QPointF(cx + gap, cy), QPointF(cx + length, cy))
            painter.drawLine(QPointF(cx, cy - length), QPointF(cx, cy - gap))
            painter.drawLine(QPointF(cx, cy + gap), QPointF(cx, cy + length))

        painter.setPen(QPen(white_soft, 0.8))
        tick_lengths = {36: 7, 72: 11, 108: 7, 144: 11}
        for dist, tick in tick_lengths.items():
            for sign in (-1, 1):
                painter.drawLine(QPointF(cx + sign * dist, cy - tick), QPointF(cx + sign * dist, cy + tick))
                painter.drawLine(QPointF(cx - tick, cy + sign * dist), QPointF(cx + tick, cy + sign * dist))

        painter.setPen(QPen(reticle_soft, 0.9))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), 28, 28)
        painter.drawEllipse(QPointF(cx, cy), 64, 64)
        painter.setPen(QPen(reticle, 1.2))
        painter.drawEllipse(QPointF(cx, cy), 5.5, 5.5)
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(cx, cy), 1.9, 1.9)

        corner_len = 28
        corner_pad = 22
        painter.setPen(QPen(QColor(255, 255, 255, 92), 1.0))
        for sx, sy in ((rect.left() + corner_pad, rect.top() + corner_pad), (rect.right() - corner_pad, rect.top() + corner_pad), (rect.left() + corner_pad, rect.bottom() - corner_pad), (rect.right() - corner_pad, rect.bottom() - corner_pad)):
            x_dir = 1 if sx < cx else -1
            y_dir = 1 if sy < cy else -1
            painter.drawLine(QPointF(sx, sy), QPointF(sx + x_dir * corner_len, sy))
            painter.drawLine(QPointF(sx, sy), QPointF(sx, sy + y_dir * corner_len))

        painter.setFont(QFont("Segoe UI Variable Display", 9, QFont.DemiBold))
        die_text = f"Die {self.active_die}" if self.active_die else "No die selected"
        pos = self.stage_pos
        hud = f"RETICLE CENTER   X{pos['X']:.3f}   Y{pos['Y']:.3f}   Z{pos['Z']:.3f}   {die_text}"
        text_w = painter.fontMetrics().horizontalAdvance(hud)
        box_w = min(rect.width() - 28, text_w + 32)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.setBrush(QColor(8, 12, 18, 138))
        painter.drawRoundedRect(rect.left() + 18, rect.top() + 18, box_w, 30, 15, 15)
        painter.setPen(QColor(232, 252, 255, 220))
        painter.drawText(rect.left() + 34, rect.top() + 38, hud)


class AppSignals(QWidget):
    log = Signal(str)
    status = Signal(str)
    planned_position = Signal(object)
    finished = Signal(str, bool)
    connected_port = Signal(str)
    camera_frame = Signal(object)
    camera_on = Signal(bool)
    camera_selected = Signal(int)
    exposure_event = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aurelith Arita")
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        else:
            self.resize(1440, 920)
        self.recipe, self.recipe_path, recipe_source = RecipeCodec.load_active(DEFAULT_RECIPE_PATH, USER_RECIPE_PATH)
        self.transport = SerialGcodeTransport(self.safe_log)
        self.running = False
        self.running_sequence: Optional[str] = None
        self.running_step_index: Optional[int] = None
        self.jog_running = False
        self.hold_requested = False
        self.abort_requested = False
        self.exposure_active = False
        self.current_step = 0
        self.second_current_step = 0
        self.sim_pos = {
            "X": self.recipe.stage.initial_x,
            "Y": self.recipe.stage.initial_y,
            "Z": self.recipe.stage.initial_z,
        }
        self.signals = AppSignals()
        self.signals.log.connect(self.append_log)
        self.signals.status.connect(self.set_status)
        self.signals.planned_position.connect(self.apply_planned_position)
        self.signals.finished.connect(self.run_finished)
        self.signals.connected_port.connect(self.apply_connected_port)
        self.signals.exposure_event.connect(self.handle_exposure_event)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.splash = SplashPage()
        self.splash.done.connect(lambda: self.stack.setCurrentWidget(self.home))
        self.stack.addWidget(self.splash)
        self.home = QWidget()
        self.process = QWidget()
        self.inspection = QWidget()
        self.settings = QWidget()
        self.stack.addWidget(self.home)
        self.stack.addWidget(self.process)
        self.stack.addWidget(self.inspection)
        self.stack.addWidget(self.settings)
        self.stack.currentChanged.connect(self.handle_page_changed)

        self.fields: Dict[str, QLineEdit | QCheckBox] = {}
        self.route_fields: Dict[str, QTextEdit] = {}
        self.quick_fields: Dict[str, QLineEdit] = {}
        self.per_die_time_fields: Dict[int, QLineEdit] = {}
        self.inspection_fields: Dict[str, QLineEdit | QCheckBox | QComboBox] = {}
        self.inspection_die_buttons: Dict[int, QPushButton] = {}
        self.process_alignment_labels: Dict[str, QLabel] = {}
        self.process_mark_labels: Dict[str, QLabel] = {}
        self.step_buttons: List[QPushButton] = []
        self.step_state_labels: List[QLabel] = []
        self.second_step_buttons: List[QPushButton] = []
        self.second_step_state_labels: List[QLabel] = []
        self.die1_direct_button: Optional[QPushButton] = None
        self.camera_die1_direct_button: Optional[QPushButton] = None
        self.log_box: Optional[QTextEdit] = None
        self.camera_log_box: Optional[QTextEdit] = None
        self.process_camera_log_box: Optional[QTextEdit] = None
        self.route_widget = RouteWidget()
        self.camera_preview = CameraPreviewWidget()
        self.signals.camera_frame.connect(self.camera_preview.set_frame_bgr)
        self.signals.camera_on.connect(self.camera_preview.set_camera_on)
        self.signals.camera_selected.connect(self.set_camera_index_from_worker)
        self.camera_stack = QStackedWidget()
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(520)
        self.video_container = QWidget()
        video_layout = QVBoxLayout(self.video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(self.video_widget)
        self.camera_stack.addWidget(self.camera_preview)
        self.camera_stack.addWidget(self.video_container)
        self.video_overlay = CameraCrosshairOverlay()
        self.video_overlay.hide()
        self.overlay_timer = QTimer(self)
        self.overlay_timer.timeout.connect(self.update_camera_overlay_geometry)
        self.overlay_timer.start(100)
        self.camera: Optional[QCamera] = None
        self.capture_session: Optional[QMediaCaptureSession] = None
        self.image_capture: Optional[QImageCapture] = None
        self.cv_capture = None
        self.camera_thread: Optional[threading.Thread] = None
        self.camera_stop_event = threading.Event()
        self.latest_camera_frame = None
        self.status_label = QLabel("Idle")
        self.camera_status_label = QLabel("Camera Standby")
        self.process_camera_status_label = QLabel("Camera Standby")
        self.connection_label = QLabel("Not connected")
        self.position_save_label = QLabel("Position save: not loaded")
        self.pos_labels: Dict[str, QLabel] = {}
        self.feed_label = QLabel()
        self.jog_label = QLabel()
        self.dry_run = QCheckBox("Dry Run")
        self.dry_run.setChecked(True)
        self.keyboard_jog = QCheckBox("Keyboard Jog Mode")
        self.keyboard_jog_timer = QTimer(self)
        self.keyboard_jog_timer.timeout.connect(self.keyboard_jog_tick)
        self.active_jog_key: Optional[int] = None
        self.active_jog_axis: Optional[str] = None
        self.active_jog_direction = 0
        self.manual_uv_on = False
        self.manual_uv_button: Optional[QPushButton] = None
        self.exposure_dialog: Optional[ExposureProgressDialog] = None
        self.suppress_preview = False
        self.recipe_save_prompt_pending = False
        self.recipe_save_prompt_active = False
        self.recipe_save_prompt_timer = QTimer(self)
        self.recipe_save_prompt_timer.setSingleShot(True)
        self.recipe_save_prompt_timer.timeout.connect(self.confirm_recipe_save)
        self.position_state_path = APP_DIR / "position_state.json"
        self.last_position_save_ts = 0.0
        self.camera_preview_on = False
        self.active_inspection_die: Optional[int] = 1
        self.process_right_tabs: Optional[QTabWidget] = None
        self.process_camera_tab_index = -1
        self.process_camera_host: Optional[QWidget] = None
        self.inspection_camera_host: Optional[QWidget] = None

        self.build_home()
        self.build_process()
        self.build_inspection()
        self.build_settings()
        self.suppress_preview = True
        try:
            self.load_recipe_to_ui(self.recipe)
        finally:
            self.suppress_preview = False
        self.load_saved_position(auto=True)
        self.route_widget.set_recipe(self.recipe)
        self.refresh_process_buttons()
        self.safe_log(f"[RECIPE] loaded {self.recipe_path.name} ({recipe_source})")
        QApplication.instance().installEventFilter(self)
        self.exit_shortcut = QShortcut(QKeySequence("Esc"), self)
        self.exit_shortcut.activated.connect(self.exit_application)
        self.enter_fullscreen()
        QTimer.singleShot(1400, self.auto_connect_on_startup)

    def enter_fullscreen(self):
        self.showFullScreen()
        self.splash.update()

    def eventFilter(self, obj, event):
        try:
            if self.keyboard_jog.isChecked() and event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
                focus = QApplication.focusWidget()
                if isinstance(focus, (QLineEdit, QTextEdit, QComboBox)):
                    return super().eventFilter(obj, event)
                if event.isAutoRepeat():
                    return True
                key = event.key()
                key_map = self.keyboard_jog_key_map()
                if key in key_map:
                    if event.type() == QEvent.KeyPress:
                        axis, direction = key_map[key]
                        self.start_keyboard_jog(key, axis, direction)
                    else:
                        self.stop_keyboard_jog(key)
                    return True
                if key == Qt.Key_Space:
                    if event.type() == QEvent.KeyPress:
                        self.stop_keyboard_jog(force=True)
                        self.jog_cancel()
                    return True
        except Exception as exc:
            self.keyboard_jog_timer.stop()
            self.active_jog_key = None
            self.active_jog_axis = None
            self.active_jog_direction = 0
            self.jog_running = False
            self.safe_log(f"[JOG KEY ERROR] {exc}")
            self.set_status("Jog Key Error")
            return True
        return super().eventFilter(obj, event)

    def keyboard_jog_key_map(self) -> Dict[int, Tuple[str, int]]:
        return {
            Qt.Key_Left: ("X", -1),
            Qt.Key_A: ("X", -1),
            Qt.Key_Right: ("X", 1),
            Qt.Key_D: ("X", 1),
            Qt.Key_Up: ("Y", 1),
            Qt.Key_W: ("Y", 1),
            Qt.Key_Down: ("Y", -1),
            Qt.Key_S: ("Y", -1),
            Qt.Key_PageUp: ("Z", 1),
            Qt.Key_E: ("Z", 1),
            Qt.Key_PageDown: ("Z", -1),
            Qt.Key_Q: ("Z", -1),
        }

    def keyboard_jog_interval_ms(self, axis: str) -> int:
        try:
            recipe = self.recipe_from_ui()
            step = recipe.jog.z_step_mm if axis == "Z" else recipe.jog.xy_step_mm
            feed = recipe.jog.feed_z if axis == "Z" else recipe.jog.feed_xy
            travel_ms = 0 if feed <= 0 else step / feed * 60000.0
            return max(80, min(500, int(travel_ms + 25)))
        except Exception:
            return 120

    def start_keyboard_jog(self, key: int, axis: str, direction: int):
        if self.active_jog_key is not None and self.active_jog_key != key:
            self.stop_keyboard_jog(force=True)
        self.active_jog_key = key
        self.active_jog_axis = axis
        self.active_jog_direction = direction
        self.safe_log(f"[JOG KEY] hold {axis}{'+' if direction >= 0 else '-'}")
        self.keyboard_jog_tick()
        self.keyboard_jog_timer.start(self.keyboard_jog_interval_ms(axis))

    def stop_keyboard_jog(self, key: Optional[int] = None, force: bool = False):
        if not force and key is not None and self.active_jog_key != key:
            return
        if self.active_jog_key is None and not force:
            return
        self.keyboard_jog_timer.stop()
        self.active_jog_key = None
        self.active_jog_axis = None
        self.active_jog_direction = 0
        self.jog_cancel()
        self.safe_log("[JOG KEY] released")

    def keyboard_jog_tick(self):
        if not self.active_jog_axis or self.active_jog_direction == 0:
            return
        if self.jog_running:
            return
        self.jog_axis(self.active_jog_axis, self.active_jog_direction)

    def exit_application(self):
        self.abort_requested = True
        self.hold_requested = False
        try:
            self.save_position_state("exit", force=True)
            self.manual_uv_off_safely()
            self.stop_camera_preview(silent=True)
            if self.transport.connected:
                self.transport.close()
        finally:
            QApplication.instance().quit()

    def closeEvent(self, event):
        self.abort_requested = True
        try:
            self.save_position_state("close", force=True)
            self.manual_uv_off_safely()
            self.stop_camera_preview(silent=True)
            if self.transport.connected:
                self.transport.close()
        finally:
            event.accept()

    def manual_uv_off_safely(self):
        if not self.manual_uv_on:
            return
        command = self.recipe.io.uv_off_gcode.strip()
        try:
            if command and not self.dry_run.isChecked() and self.transport.connected:
                self.transport.send_line(command, timeout_s=self.recipe.motion.command_timeout_s)
            self.safe_log("[MANUAL UV] OFF before exit")
        except Exception as exc:
            self.safe_log(f"[MANUAL UV] OFF before exit failed: {exc}")
        finally:
            self.manual_uv_on = False
            self.update_manual_uv_button()

    def header(self, title: str, subtitle: str) -> Card:
        card = Card()
        row = QHBoxLayout()
        title_col = QVBoxLayout()
        label = QLabel(title)
        label.setObjectName("pageTitle")
        sub = QLabel(subtitle)
        sub.setObjectName("muted")
        title_col.addWidget(label)
        title_col.addWidget(sub)
        row.addLayout(title_col, 1)
        for text, page in (("Process", self.process), ("Inspection", self.inspection), ("Settings", self.settings), ("Home", self.home)):
            btn = PillButton(text, "soft")
            btn.clicked.connect(lambda _=False, p=page: self.stack.setCurrentWidget(p))
            row.addWidget(btn)
        card.layout.addLayout(row)
        return card

    def build_home(self):
        root = QVBoxLayout(self.home)
        root.setContentsMargins(54, 42, 54, 42)
        root.setSpacing(24)

        hero = Card()
        hero.layout.setContentsMargins(38, 34, 38, 34)
        hero_row = QHBoxLayout()
        logo_col = QVBoxLayout()
        logo = QLabel()
        pm = pixmap_for("wordmark_logo.png")
        if not pm.isNull():
            logo.setPixmap(pm.scaled(720, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo_col.addWidget(logo)
        arita_pm = pixmap_for("arita_logo.png")
        if not arita_pm.isNull():
            arita = QLabel()
            arita.setPixmap(arita_pm.scaled(270, 92, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            arita = QLabel('<span style="color:#070A1F;">Ari</span><span style="color:#342BC2;">ta</span>')
            arita.setTextFormat(Qt.RichText)
            arita.setObjectName("aritaWordmark")
        logo_col.addWidget(arita)
        hero_row.addLayout(logo_col, 2)

        detail = QVBoxLayout()
        kicker = QLabel("PRECISION MOTION SYSTEM")
        kicker.setObjectName("heroKicker")
        title = QLabel("H-line Stepper")
        title.setObjectName("heroTitle")
        body = QLabel("1:4 Projection / Off-axis Alignment / XYZ Stage")
        body.setObjectName("heroSpecs")
        body.setWordWrap(True)
        detail.addWidget(kicker)
        detail.addWidget(title)
        detail.addWidget(body)
        chip_row = QHBoxLayout()
        for text in ("RECIPE ROUTE", "3x3 SNAKE", "GRBL CONTROL"):
            chip = QLabel(text)
            chip.setObjectName("homeChip")
            chip_row.addWidget(chip)
        chip_row.addStretch()
        detail.addLayout(chip_row)
        hero_row.addLayout(detail, 1)
        hero.layout.addLayout(hero_row)
        exit_row = QHBoxLayout()
        exit_row.addStretch()
        exit_btn = PillButton("Exit", "soft")
        exit_btn.setMinimumWidth(160)
        exit_btn.clicked.connect(self.exit_application)
        exit_row.addWidget(exit_btn)
        hero.layout.addLayout(exit_row)
        root.addWidget(hero)

        cards = QHBoxLayout()
        cards.setSpacing(18)
        for title, desc, features, page in (
            ("Process Start", "Run the machine from a guided process console.", ["Ordered Flow always visible", "Position / Control / Safety tabs", "Live position and status log"], self.process),
            ("Inspection", "Align and inspect dies with a large camera workspace.", ["Camera live view and crosshair", "Separate camera die vectors", "Z sweep inspection planning"], self.inspection),
            ("Settings", "Tune every motion and safety parameter.", ["Route based loading coordinate", "Soft limits and keepout zone", "Confirm before saving recipe"], self.settings),
        ):
            card = Card(soft=True)
            card.setMinimumHeight(270)
            title_label = QLabel(title)
            title_label.setObjectName("homeCardTitle")
            desc_label = QLabel(desc)
            desc_label.setObjectName("homeCardDesc")
            desc_label.setWordWrap(True)
            card.layout.addWidget(title_label)
            card.layout.addWidget(desc_label)
            line = QFrame()
            line.setObjectName("hairline")
            line.setFixedHeight(1)
            card.layout.addWidget(line)
            for item in features:
                row = QLabel(f"- {item}")
                row.setObjectName("featureText")
                card.layout.addWidget(row)
            btn = PillButton("Open", "primary")
            btn.clicked.connect(lambda _=False, p=page: self.stack.setCurrentWidget(p))
            card.layout.addStretch()
            card.layout.addWidget(btn)
            cards.addWidget(card)
        root.addLayout(cards)
        root.addStretch()

    def build_process(self):
        root = QVBoxLayout(self.process)
        root.setContentsMargins(38, 30, 38, 30)
        root.setSpacing(18)
        root.addWidget(self.header("Process Sequence", "Ordered flow remains visible while controls switch on the right."))

        body = QHBoxLayout()
        body.setSpacing(22)

        process_tabs = QTabWidget()
        process_tabs.setObjectName("rightTabs")
        process_tabs.setMinimumWidth(430)
        process_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        flow = self.primary_process_flow()
        process_tabs.addTab(flow, "1st Process")
        second_flow = self.second_process_flow()
        process_tabs.addTab(second_flow, "2nd Process")
        process_tabs.currentChanged.connect(self.process_tab_changed)
        body.addWidget(process_tabs, 3)

        tabs = QTabWidget()
        tabs.setObjectName("rightTabs")
        tabs.setMinimumWidth(390)
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        tabs.addTab(self.position_tab(), "Position")
        self.process_camera_tab_index = tabs.addTab(self.process_camera_tab(), "Camera")
        tabs.addTab(self.control_tab(), "Control")
        tabs.addTab(self.quick_settings_tab(), "Quick Settings")
        tabs.addTab(self.safety_tab(), "Safety")
        tabs.currentChanged.connect(self.process_right_tab_changed)
        self.process_right_tabs = tabs
        body.addWidget(tabs, 2)
        root.addLayout(body, 1)

    def process_tab_changed(self, index: int):
        mode = "second" if index == 1 else "primary"
        self.route_widget.set_route_mode(mode)
        try:
            self.recipe = self.recipe_from_ui()
            self.route_widget.set_recipe(self.recipe)
        except Exception:
            pass
        self.safe_log("[ROUTE VIEW] 2nd process route" if mode == "second" else "[ROUTE VIEW] 1st process route")

    def process_right_tab_changed(self, index: int):
        if index == self.process_camera_tab_index:
            self.attach_camera_stack("process")
        self.update_camera_overlay_geometry()

    def primary_process_flow(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        flow = Card("Ordered Flow", "Run one process step at a time.")
        steps = [
            ("load", "01 Load Position", "Move through To loading route and wait for wafer placement."),
            ("mount", "02 Route To Exposure", "Follow the configured detour path. Add Z lines in Routes only when needed."),
            ("grid", "03 Step Repeat", "Run 3x3 snake order exposure routine."),
            ("return", "04 Return To Load", "Return through the configured route."),
        ]
        for idx, (plan, title, desc) in enumerate(steps):
            row_card = Card(soft=True)
            row = QHBoxLayout()
            text_col = QVBoxLayout()
            t = QLabel(title)
            t.setObjectName("stepTitle")
            d = QLabel(desc)
            d.setObjectName("muted")
            d.setWordWrap(True)
            text_col.addWidget(t)
            text_col.addWidget(d)
            state = QLabel("READY")
            state.setObjectName("badge")
            btn = PillButton("Run", "blue")
            btn.setFixedWidth(96)
            btn.clicked.connect(lambda _=False, i=idx, p=plan: self.run_process_step(i, p))
            self.step_buttons.append(btn)
            self.step_state_labels.append(state)
            row.addLayout(text_col, 1)
            row.addWidget(state)
            if plan == "mount":
                direct = PillButton("Go Die 1", "soft")
                direct.setFixedWidth(112)
                direct.clicked.connect(lambda: self.run_plan("die1_direct", sequence="primary"))
                self.die1_direct_button = direct
                row.addWidget(direct)
            row.addWidget(btn)
            row_card.layout.addLayout(row)
            flow.layout.addWidget(row_card)
        reset = PillButton("Reset Sequence", "soft")
        reset.clicked.connect(self.reset_sequence)
        full = PillButton("One Click Full Cycle", "primary")
        full.clicked.connect(lambda: self.run_plan("full"))
        quick = QHBoxLayout()
        quick.addWidget(reset)
        quick.addWidget(full)
        flow.layout.addLayout(quick)
        layout.addWidget(flow)
        layout.addStretch()
        scroll.setWidget(wrapper)
        return scroll

    def second_process_flow(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        flow = Card(
            "2nd Process",
            "Capture Camera Die 1 marks, convert them to corrected Exposure Die 1, then expose.",
        )
        guide = QLabel(
            "Camera Die 1 is the alignment position under the camera. Corrected Exposure uses the 1st-process exposure grid plus the measured camera alignment delta."
        )
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        flow.layout.addWidget(guide)
        steps = [
            ("load", "01 Load", "Move wafer to the 2nd-process loading position."),
            ("camera_align", "02 Go Camera Die 1", "Move to nominal Camera Die 1 for alignment."),
            ("save_center", "03 Capture Camera Marks", "Open camera and save Camera Die 1 TL/TR/BL/BR marks."),
            ("preview", "04 Apply Camera Correction", "Average Camera Die 1 marks and calculate corrected exposure positions."),
            ("go_corrected_die1", "05 Go Corrected Exposure Die 1", "Move to corrected Exposure Die 1 without exposing."),
            ("grid", "06 Corrected Exposure", "Run selected dies from corrected exposure coordinates."),
            ("return", "07 Return / Clear", "Return to wafer loading position, then clear this wafer's alignment data."),
        ]
        for idx, (action, title, desc) in enumerate(steps):
            row_card = Card(soft=True)
            row = QHBoxLayout()
            text_col = QVBoxLayout()
            t = QLabel(title)
            t.setObjectName("stepTitle")
            d = QLabel(desc)
            d.setObjectName("muted")
            d.setWordWrap(True)
            text_col.addWidget(t)
            text_col.addWidget(d)
            state = QLabel("READY")
            state.setObjectName("badge")
            btn = PillButton("Run", "blue")
            btn.setFixedWidth(96)
            btn.clicked.connect(lambda _=False, i=idx, a=action: self.run_second_process_step(i, a))
            self.second_step_buttons.append(btn)
            self.second_step_state_labels.append(state)
            row.addLayout(text_col, 1)
            row.addWidget(state)
            if action == "camera_align":
                direct = PillButton("Go Camera Die 1", "soft")
                direct.setFixedWidth(132)
                direct.clicked.connect(lambda: self.run_aux_motion("camera_die1_direct", "second", "Go Nominal Camera Die 1"))
                self.camera_die1_direct_button = direct
                row.addWidget(direct)
            if action == "go_corrected_die1":
                direct = PillButton("Go Exposure Die 1", "soft")
                direct.setFixedWidth(112)
                direct.clicked.connect(lambda: self.run_aux_motion("corrected_die1_direct", "second", "Go Corrected Exposure Die 1", require_second_alignment=True))
                row.addWidget(direct)
            row.addWidget(btn)
            row_card.layout.addLayout(row)
            flow.layout.addWidget(row_card)
        camera_tools = Card("Alignment Tools", "Use while watching the camera view. Jog controls are in the Control tab.")
        tools_grid = QGridLayout()
        tools_grid.setHorizontalSpacing(8)
        tools_grid.setVerticalSpacing(8)
        tool_items = [
            ("Open Camera + Start", "blue", self.open_process_camera_tab),
            ("Save Nominal Camera Die 1", "primary", self.save_current_as_die1_camera_center),
            ("Use Nominal As Correction", "soft", self.use_default_camera_die1_as_alignment_center),
            ("Go Nominal Camera Die 1", "soft", lambda: self.run_aux_motion("camera_die1_direct", "second", "Go Nominal Camera Die 1")),
            ("Preview Comparison", "soft", self.preview_die1_center_aligned_positions),
        ]
        for idx, (text, variant, handler) in enumerate(tool_items):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            tools_grid.addWidget(btn, idx // 2, idx % 2)
        mark_items = [
            ("Save TOP mark", "TOP"),
            ("Save BOTTOM mark", "BOTTOM"),
        ]
        start_row = (len(tool_items) + 1) // 2
        for idx, (text, mark) in enumerate(mark_items):
            btn = PillButton(text, "soft")
            btn.clicked.connect(lambda _=False, m=mark: self.save_die1_alignment_mark(m))
            tools_grid.addWidget(btn, start_row + idx // 2, idx % 2)
        calc_tb = PillButton("Calculate center from TOP/BOTTOM", "soft")
        calc_tb.clicked.connect(self.calculate_die1_center_from_top_bottom_marks)
        tools_grid.addWidget(calc_tb, start_row + 1, 0, 1, 2)
        start_row += 2
        for idx, mark in enumerate(("TL", "TR", "BR", "BL")):
            btn = PillButton(f"Save {mark}", "soft")
            btn.clicked.connect(lambda _=False, m=mark: self.save_die1_alignment_mark(m))
            tools_grid.addWidget(btn, start_row + idx // 2, idx % 2)
        calc4 = PillButton("Calculate center from 4 marks", "soft")
        calc4.clicked.connect(self.calculate_die1_center_from_marks)
        tools_grid.addWidget(calc4, start_row + 2, 0, 1, 2)
        camera_tools.layout.addLayout(tools_grid)
        flow.layout.addWidget(camera_tools)
        reset = PillButton("Reset 2nd Sequence", "soft")
        reset.clicked.connect(self.reset_second_sequence)
        open_inspection = PillButton("Open Camera Alignment", "primary")
        open_inspection.clicked.connect(lambda: self.stack.setCurrentWidget(self.inspection))
        quick = QHBoxLayout()
        quick.addWidget(reset)
        quick.addWidget(open_inspection)
        flow.layout.addLayout(quick)
        layout.addWidget(flow)
        layout.addStretch()
        scroll.setWidget(wrapper)
        return scroll

    def build_inspection(self):
        root = QVBoxLayout(self.inspection)
        root.setContentsMargins(38, 30, 38, 30)
        root.setSpacing(18)
        root.addWidget(self.header("Camera Inspection", "Large alignment workspace with camera preview, die selection, and Z sweep planning."))
        root.addWidget(self.inspection_tab(), 1)

    def attach_camera_stack(self, target: str):
        host = self.process_camera_host if target == "process" else self.inspection_camera_host
        if host is None:
            return
        layout = host.layout()
        if layout is None:
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
        if self.camera_stack.parent() is not host:
            self.camera_stack.setParent(None)
            layout.addWidget(self.camera_stack)
        self.update_camera_overlay_geometry()

    def position_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget = QWidget()
        scroll.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(14)

        telemetry = Card("Current Position")
        top = QHBoxLayout()
        top.addWidget(QLabel("State"))
        self.status_label.setObjectName("stateText")
        top.addStretch()
        top.addWidget(self.status_label)
        telemetry.layout.addLayout(top)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        for col, axis in enumerate(AXES):
            mini = MetricTile(axis)
            self.pos_labels[axis] = mini.value
            grid.addWidget(mini, 0, col)
        telemetry.layout.addLayout(grid)
        self.feed_label.setObjectName("muted")
        self.jog_label.setObjectName("muted")
        self.position_save_label.setObjectName("muted")
        telemetry.layout.addWidget(self.feed_label)
        telemetry.layout.addWidget(self.jog_label)
        telemetry.layout.addWidget(self.position_save_label)
        layout.addWidget(telemetry)

        route = Card("Top down route", "Live position and planned orthogonal path.")
        route.layout.addWidget(self.route_widget)
        layout.addWidget(route, 1)
        return scroll

    def process_camera_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget = QWidget()
        scroll.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(14)

        camera_card = Card("Camera", "Keep the live camera open while running 2nd-process alignment.")
        self.process_camera_host = QWidget()
        host_layout = QVBoxLayout(self.process_camera_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        camera_card.layout.addWidget(self.process_camera_host)

        status_row = QHBoxLayout()
        status_title = QLabel("Camera Status")
        status_title.setObjectName("muted")
        self.process_camera_status_label.setObjectName("stateText")
        status_row.addWidget(status_title)
        status_row.addStretch()
        status_row.addWidget(self.process_camera_status_label)
        camera_card.layout.addLayout(status_row)
        layout.addWidget(camera_card, 1)

        controls = Card("Camera Control", "Use jog controls in the Control tab, then save Camera Die 1 alignment data here.")
        row = QHBoxLayout()
        for text, variant, handler in (
            ("Start Camera", "blue", self.start_camera_preview),
            ("Stop Camera", "soft", self.stop_camera_preview),
            ("Capture Frame", "primary", self.capture_current_frame),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            row.addWidget(btn)
        controls.layout.addLayout(row)

        align_row = QHBoxLayout()
        for text, variant, handler in (
            ("Go Camera Die 1", "soft", lambda: self.run_aux_motion("camera_die1_direct", "second", "Go Nominal Camera Die 1")),
            ("Save Nominal Camera Die 1", "blue", self.save_current_as_die1_camera_center),
            ("Preview Alignment", "primary", self.preview_die1_center_aligned_positions),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            align_row.addWidget(btn)
        controls.layout.addLayout(align_row)

        layout.addWidget(controls)

        marks = Card(
            "Camera Die 1 Mark Alignment",
            "Save TL/TR/BR/BL at the crosshair. The 4-mark average becomes the measured Camera Die 1 center.",
        )
        mark_buttons = QGridLayout()
        for idx, mark in enumerate(("TL", "TR", "BL", "BR")):
            btn = PillButton(f"Save {mark}", "soft")
            btn.clicked.connect(lambda _=False, m=mark: self.save_die1_alignment_mark(m))
            mark_buttons.addWidget(btn, idx // 2, idx % 2)
        calc4 = PillButton("Apply 4-Mark Average To Camera Die 1", "blue")
        calc4.clicked.connect(self.calculate_die1_center_from_marks)
        mark_buttons.addWidget(calc4, 2, 0, 1, 2)
        marks.layout.addLayout(mark_buttons)

        mark_grid = QGridLayout()
        mark_grid.setHorizontalSpacing(10)
        mark_grid.setVerticalSpacing(6)
        for idx, mark in enumerate(("TL", "TR", "BL", "BR")):
            title = QLabel(mark)
            title.setObjectName("muted")
            value = QLabel("not saved")
            value.setObjectName("metricValueSmall")
            self.process_mark_labels[mark] = value
            mark_grid.addWidget(title, idx, 0)
            mark_grid.addWidget(value, idx, 1)
        marks.layout.addLayout(mark_grid)

        summary_grid = QGridLayout()
        summary_items = [
            ("nominal", "Nominal Camera Die 1"),
            ("average", "4-mark Camera average"),
            ("measured", "Active measured Camera Die 1"),
            ("offset", "Camera delta vs nominal"),
            ("final", "Corrected Exposure Die 1"),
        ]
        for row_idx, (key, label_text) in enumerate(summary_items):
            label = QLabel(label_text)
            label.setObjectName("muted")
            value = QLabel("-")
            value.setObjectName("metricValueSmall")
            self.process_alignment_labels[key] = value
            summary_grid.addWidget(label, row_idx, 0)
            summary_grid.addWidget(value, row_idx, 1)
        marks.layout.addLayout(summary_grid)
        layout.addWidget(marks)

        log_card = Card("Camera Log")
        self.process_camera_log_box = QTextEdit()
        self.process_camera_log_box.setReadOnly(True)
        self.process_camera_log_box.setMinimumHeight(96)
        self.process_camera_log_box.setPlaceholderText("Camera events and capture logs.")
        log_card.layout.addWidget(self.process_camera_log_box)
        layout.addWidget(log_card)
        return scroll

    def control_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget = QWidget()
        scroll.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(14)

        connection = Card("Connection", "Auto-detect a GRBL/FluidNC serial port before live motion.")
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Serial"))
        self.connection_label.setObjectName("muted")
        status_row.addStretch()
        status_row.addWidget(self.connection_label)
        connection.layout.addLayout(status_row)
        row0 = QHBoxLayout()
        for text, variant, handler in (
            ("Scan Ports", "soft", self.scan_ports),
            ("Auto Connect", "blue", self.auto_connect),
            ("Connect", "primary", self.connect_transport),
            ("Disconnect", "soft", self.disconnect_transport),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            row0.addWidget(btn)
        connection.layout.addLayout(row0)
        layout.addWidget(connection)

        live = Card("Live Control")
        row = QHBoxLayout()
        for text, variant, handler in (
            ("Hold !", "danger", self.feed_hold),
            ("Resume ~", "soft", self.resume),
            ("Reset", "soft", self.soft_reset),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            row.addWidget(btn)
        live.layout.addLayout(row)
        row2 = QHBoxLayout()
        status = PillButton("Status ?", "blue")
        status.clicked.connect(self.query_status)
        zero = PillButton("Set Zero", "soft")
        zero.clicked.connect(self.set_current_zero)
        row2.addWidget(status)
        row2.addWidget(zero)
        live.layout.addLayout(row2)
        row3 = QHBoxLayout()
        manual_uv = PillButton("Manual UV ON", "soft")
        manual_uv.clicked.connect(self.toggle_manual_uv)
        self.manual_uv_button = manual_uv
        row3.addWidget(manual_uv)
        live.layout.addLayout(row3)
        row4 = QHBoxLayout()
        save_pos = PillButton("Save Position Now", "blue")
        save_pos.clicked.connect(lambda: self.save_position_state("manual", force=True))
        restore_pos = PillButton("Restore Saved Pos", "soft")
        restore_pos.clicked.connect(lambda: self.load_saved_position(auto=False))
        row4.addWidget(save_pos)
        row4.addWidget(restore_pos)
        live.layout.addLayout(row4)
        layout.addWidget(live)

        jog = Card("Manual Jog", "Step/feed values are managed in Settings.")
        jog.setMinimumHeight(310)
        keyboard_row = QHBoxLayout()
        self.keyboard_jog.stateChanged.connect(
            lambda _=0: (
                self.safe_log(
                    "[JOG] keyboard mode ON: hold arrows/WASD=XY, PgUp/PgDn or E/Q=Z, release=stop, Space=stop"
                    if self.keyboard_jog.isChecked()
                    else "[JOG] keyboard mode OFF"
                ),
                None if self.keyboard_jog.isChecked() else self.stop_keyboard_jog(force=True),
            )
        )
        hint = QLabel("Hold Arrows/WASD: XY   PgUp/PgDn or E/Q: Z   Release/Space: Stop")
        hint.setObjectName("muted")
        keyboard_row.addWidget(self.keyboard_jog)
        keyboard_row.addStretch()
        keyboard_row.addWidget(hint)
        jog.layout.addLayout(keyboard_row)
        jog_pad_wrap = QHBoxLayout()
        jog_pad_wrap.addStretch()
        jog_pad = QFrame()
        jog_pad.setObjectName("jogPad")
        jog_pad.setMinimumSize(292, 210)
        jog_pad.setMaximumWidth(386)
        jog_pad.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid = QGridLayout(jog_pad)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        jog_buttons = [
            ("Y+", 0, 1, lambda: self.jog_axis("Y", 1), "soft"),
            ("X-", 1, 0, lambda: self.jog_axis("X", -1), "soft"),
            ("Jog Stop", 1, 1, self.jog_cancel, "danger"),
            ("X+", 1, 2, lambda: self.jog_axis("X", 1), "soft"),
            ("Y-", 2, 1, lambda: self.jog_axis("Y", -1), "soft"),
            ("Z+", 0, 2, lambda: self.jog_axis("Z", 1), "soft"),
            ("Z-", 2, 2, lambda: self.jog_axis("Z", -1), "soft"),
        ]
        for text, row, col, handler, variant in jog_buttons:
            btn = PillButton(text, variant)
            btn.setMinimumHeight(48)
            btn.clicked.connect(handler)
            grid.addWidget(btn, row, col)
        jog_pad_wrap.addWidget(jog_pad)
        jog_pad_wrap.addStretch()
        jog.layout.addLayout(jog_pad_wrap)
        capture_row = QHBoxLayout()
        capture_row.addStretch()
        save_die1 = PillButton("Save Die 1 Position", "blue")
        save_die1.setMinimumWidth(220)
        save_die1.clicked.connect(self.save_current_as_die1_offset)
        capture_row.addWidget(save_die1)
        capture_row.addStretch()
        jog.layout.addLayout(capture_row)
        layout.addWidget(jog)

        log_card = Card("Log")
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(170)
        log_card.layout.addWidget(self.log_box)
        layout.addWidget(log_card, 1)
        return scroll

    def inspection_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget = QWidget()
        scroll.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(14)

        camera_card = Card("Camera Live View", "Start Camera switches from alignment preview to the real USB camera feed.")
        self.inspection_camera_host = QWidget()
        inspection_host_layout = QVBoxLayout(self.inspection_camera_host)
        inspection_host_layout.setContentsMargins(0, 0, 0, 0)
        camera_card.layout.addWidget(self.inspection_camera_host)
        self.attach_camera_stack("inspection")
        camera_status_row = QHBoxLayout()
        status_title = QLabel("Camera Status")
        status_title.setObjectName("muted")
        self.camera_status_label.setObjectName("stateText")
        camera_status_row.addWidget(status_title)
        camera_status_row.addStretch()
        camera_status_row.addWidget(self.camera_status_label)
        camera_card.layout.addLayout(camera_status_row)
        self.camera_log_box = QTextEdit()
        self.camera_log_box.setReadOnly(True)
        self.camera_log_box.setMinimumHeight(92)
        self.camera_log_box.setPlaceholderText("Camera scan log will appear here after pressing Start Camera.")
        camera_card.layout.addWidget(self.camera_log_box)
        layout.addWidget(camera_card)

        controls = QHBoxLayout()
        controls.setSpacing(14)

        left = Card("Camera / Alignment", "Use jog controls to center Die 1 on the crosshair, then save camera offsets.")
        cam_row = QHBoxLayout()
        for text, variant, handler in (
            ("Start Camera", "blue", self.start_camera_preview),
            ("Stop Camera", "soft", self.stop_camera_preview),
            ("Capture Frame", "primary", self.capture_current_frame),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            cam_row.addWidget(btn)
        left.layout.addLayout(cam_row)
        self.inspection_field(left, "inspection.camera_index", "Camera index")
        self.inspection_field(left, "inspection.camera_width", "Width")
        self.inspection_field(left, "inspection.camera_height", "Height")
        self.inspection_field(left, "inspection.camera_fps", "FPS")
        for text, handler in (
            ("Save camera reference", self.save_current_as_camera_reference),
            ("Save camera Die 1", self.save_current_as_camera_die1_offset),
            ("Save camera Die 2 step", self.save_current_as_camera_die2_step),
            ("Save camera Die 6 step", self.save_current_as_camera_die6_step),
        ):
            btn = PillButton(text, "soft")
            btn.clicked.connect(handler)
            left.layout.addWidget(btn)
        controls.addWidget(left, 1)

        right = Card("Inspection Selection", "Click a Die to move the camera stage there. Selected dies are used for inspection runs.")
        die_grid = QGridLayout()
        die_layout = [(1, 0, 0), (2, 0, 1), (3, 0, 2), (6, 1, 0), (5, 1, 1), (4, 1, 2), (7, 2, 0), (8, 2, 1), (9, 2, 2)]
        for die, row, col in die_layout:
            btn = PillButton(f"Die {die}", "soft")
            btn.setMinimumHeight(54)
            btn.clicked.connect(lambda _=False, d=die: self.toggle_inspection_die(d))
            self.inspection_die_buttons[die] = btn
            die_grid.addWidget(btn, row, col)
        right.layout.addLayout(die_grid)
        select_row = QHBoxLayout()
        for text, dies in (
            ("All", [1, 2, 3, 4, 5, 6, 7, 8, 9]),
            ("Clear", []),
            ("Corners", [1, 3, 7, 9]),
            ("1/5/9", [1, 5, 9]),
        ):
            btn = PillButton(text, "soft")
            btn.clicked.connect(lambda _=False, ds=dies: self.set_inspection_dies(ds))
            select_row.addWidget(btn)
        right.layout.addLayout(select_row)
        through = QCheckBox("Move through all inspection positions")
        through.stateChanged.connect(self.queue_preview)
        self.inspection_fields["inspection.move_through_all_inspection_positions"] = through
        right.layout.addWidget(through)
        controls.addWidget(right, 1)
        layout.addLayout(controls)

        align_card = Card(
            "Die 1 Center Alignment",
            "Fast second-process alignment: save Die 1 camera center, then apply camera-to-exposure offset only at final exposure.",
        )
        align_grid = QGridLayout()
        mode = QComboBox()
        mode.addItems(["DIE1_CENTER_ONLY", "NOMINAL", "THREE_POINT"])
        mode.currentTextChanged.connect(self.queue_preview)
        self.inspection_fields["inspection.alignment_mode"] = mode
        align_grid.addWidget(QLabel("Alignment mode"), 0, 0)
        align_grid.addWidget(mode, 0, 1)
        for row, (key, label) in enumerate(
            (
                ("inspection.measured_die1_center_x", "Measured Die 1 center X"),
                ("inspection.measured_die1_center_y", "Measured Die 1 center Y"),
                ("inspection.camera_to_exposure_dx", "Camera -> exposure dX"),
                ("inspection.camera_to_exposure_dy", "Camera -> exposure dY"),
            ),
            start=1,
        ):
            align_grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit()
            edit.textChanged.connect(self.queue_preview)
            self.inspection_fields[key] = edit
            align_grid.addWidget(edit, row, 1)
        active = QCheckBox("Die 1 center alignment active")
        active.stateChanged.connect(self.queue_preview)
        self.inspection_fields["inspection.die1_center_alignment_active"] = active
        align_card.layout.addLayout(align_grid)
        align_card.layout.addWidget(active)
        center_row = QHBoxLayout()
        for text, variant, handler in (
            ("Save current as nominal Camera Die 1", "blue", self.save_current_as_die1_camera_center),
            ("Preview Die 1 center aligned positions", "primary", self.preview_die1_center_aligned_positions),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            center_row.addWidget(btn)
        align_card.layout.addLayout(center_row)
        top_bottom_row = QHBoxLayout()
        for text, mark in (
            ("Save Die 1 TOP mark", "TOP"),
            ("Save Die 1 BOTTOM mark", "BOTTOM"),
        ):
            btn = PillButton(text, "soft")
            btn.clicked.connect(lambda _=False, m=mark: self.save_die1_alignment_mark(m))
            top_bottom_row.addWidget(btn)
        calc_tb = PillButton("Calculate center from TOP/BOTTOM", "soft")
        calc_tb.clicked.connect(self.calculate_die1_center_from_top_bottom_marks)
        top_bottom_row.addWidget(calc_tb)
        align_card.layout.addLayout(top_bottom_row)
        marks_row = QHBoxLayout()
        for mark in ("TL", "TR", "BR", "BL"):
            btn = PillButton(f"Save Die 1 {mark} mark", "soft")
            btn.clicked.connect(lambda _=False, m=mark: self.save_die1_alignment_mark(m))
            marks_row.addWidget(btn)
        align_card.layout.addLayout(marks_row)
        calc = PillButton("Calculate Die 1 center from marks", "soft")
        calc.clicked.connect(self.calculate_die1_center_from_marks)
        align_card.layout.addWidget(calc)
        layout.addWidget(align_card)

        z_card = Card("Z Inspection", "Z moves are generated as single-axis commands.")
        z_grid = QGridLayout()
        mode = QComboBox()
        mode.addItems(["SINGLE", "LIST", "SWEEP"])
        mode.currentTextChanged.connect(self.queue_preview)
        self.inspection_fields["inspection.inspection_z_mode"] = mode
        z_grid.addWidget(QLabel("Z mode"), 0, 0)
        z_grid.addWidget(mode, 0, 1)
        z_items = [
            ("inspection.inspection_z_single", "Z single"),
            ("inspection.inspection_z_values", "Z list"),
            ("inspection.z_sweep_start", "Sweep start"),
            ("inspection.z_sweep_end", "Sweep end"),
            ("inspection.z_sweep_step", "Sweep step"),
            ("inspection.z_settle_time_s", "Settle s"),
            ("inspection.safe_z", "Safe Z"),
        ]
        for idx, (key, label) in enumerate(z_items, start=1):
            z_grid.addWidget(QLabel(label), idx, 0)
            edit = QLineEdit()
            edit.textChanged.connect(self.queue_preview)
            self.inspection_fields[key] = edit
            z_grid.addWidget(edit, idx, 1)
        z_card.layout.addLayout(z_grid)
        for key, label in (
            ("inspection.capture_each_z", "Capture each Z"),
            ("inspection.return_to_safe_z_after_die", "Return to safe Z after each die"),
        ):
            cb = QCheckBox(label)
            cb.stateChanged.connect(self.queue_preview)
            self.inspection_fields[key] = cb
            z_card.layout.addWidget(cb)
        action_row = QHBoxLayout()
        for text, variant, handler in (
            ("Preview Inspection Plan", "blue", self.preview_inspection_plan),
            ("Move To Active Die", "soft", self.move_to_active_inspection_die),
            ("Run Selected Inspection", "primary", self.run_selected_inspection),
            ("Stop", "danger", self.soft_reset),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            action_row.addWidget(btn)
        z_card.layout.addLayout(action_row)
        layout.addWidget(z_card)
        return scroll

    def inspection_field(self, card: Card, key: str, label: str):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        edit = QLineEdit()
        edit.textChanged.connect(self.queue_preview)
        self.inspection_fields[key] = edit
        row.addWidget(edit)
        card.layout.addLayout(row)

    def safety_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget = QWidget()
        scroll.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(14)

        dry = Card("Run Mode", "Dry Run simulates motion and exposure without sending serial commands.")
        dry.layout.addWidget(self.dry_run)
        layout.addWidget(dry)

        preflight = Card("Preflight")
        self.coord_check = QCheckBox("Coordinate zero confirmed")
        self.estop_check = QCheckBox("Limits and E-stop confirmed")
        self.uv_check = QCheckBox("UV shield and interlock confirmed")
        for cb in (self.coord_check, self.estop_check, self.uv_check):
            preflight.layout.addWidget(cb)
        actions = QHBoxLayout()
        for text, variant, handler in (
            ("Save settings", "primary", self.save_settings),
            ("Check plan", "blue", self.check_plan),
            ("Export G-code", "soft", self.export_gcode),
        ):
            btn = PillButton(text, variant)
            btn.clicked.connect(handler)
            actions.addWidget(btn)
        preflight.layout.addLayout(actions)
        layout.addWidget(preflight)
        layout.addStretch()
        return scroll

    def quick_settings_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget = QWidget()
        scroll.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(14)

        card = Card("Quick Settings", "Frequently changed process values.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        items = [
            ("motion.feed_xy", "Feed XY", "mm/min"),
            ("motion.feed_z", "Feed Z", "mm/min"),
            ("jog.xy_step_mm", "Jog XY step", "mm"),
            ("jog.z_step_mm", "Jog Z step", "mm"),
            ("jog.feed_xy", "Jog XY feed", "mm/min"),
            ("jog.feed_z", "Jog Z feed", "mm/min"),
            ("exposure.exposure_z", "Exposure Z / drop", "mm"),
            ("exposure.exposure_time_s", "Exposure time", "s"),
            ("exposure.per_die_exposure_time_s", "Per-die time", "1:1.0,2:1.5"),
            ("exposure.per_die_uv_intensity", "Per-die light", "1:800,2:1000"),
        ]
        for row, (key, label, unit) in enumerate(items):
            title = QLabel(label)
            title.setObjectName("stepTitle")
            edit = QLineEdit()
            edit.setPlaceholderText(unit)
            edit.textChanged.connect(lambda _text, k=key: self.sync_quick_field(k))
            self.quick_fields[key] = edit
            suffix = QLabel(unit)
            suffix.setObjectName("muted")
            grid.addWidget(title, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(suffix, row, 2)
        card.layout.addLayout(grid)

        presets = QHBoxLayout()
        for value in ("0.5", "1.0", "2.0", "5.0"):
            btn = PillButton(f"{value}s", "soft")
            btn.clicked.connect(lambda _=False, v=value: self.set_quick_value("exposure.exposure_time_s", v))
            presets.addWidget(btn)
        card.layout.addLayout(presets)
        layout.addWidget(card)

        die_card = Card("Per-die Exposure Time", "Blank = use the default exposure time. Filled values override only that die.")
        die_grid = QGridLayout()
        die_grid.setHorizontalSpacing(10)
        die_grid.setVerticalSpacing(10)
        for die in range(1, 10):
            cell = QVBoxLayout()
            label = QLabel(f"Die {die}")
            label.setObjectName("metricAxis")
            edit = QLineEdit()
            edit.setPlaceholderText("default")
            edit.textChanged.connect(self.sync_per_die_time_grid)
            self.per_die_time_fields[die] = edit
            cell.addWidget(label)
            cell.addWidget(edit)
            die_grid.addLayout(cell, (die - 1) // 3, (die - 1) % 3)
        die_card.layout.addLayout(die_grid)
        layout.addWidget(die_card)
        layout.addStretch()
        return scroll

    def build_settings(self):
        root = QVBoxLayout(self.settings)
        root.setContentsMargins(38, 30, 38, 30)
        root.setSpacing(18)
        root.addWidget(self.header("Settings", "After edits, the app asks before saving to user_recipe_pro.json."))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        motion = QWidget()
        stage = QWidget()
        routes = QWidget()
        io = QWidget()
        tabs.addTab(motion, "Motion / Limits")
        tabs.addTab(stage, "Stage / Grid")
        tabs.addTab(routes, "Routes")
        tabs.addTab(io, "I/O / UV")
        self.form(motion, [
            ("serial.port", "Port"), ("serial.baud", "Baud"),
            ("motion.feed_xy", "XY feed"), ("motion.feed_z", "Z feed"),
            ("motion.idle_timeout_s", "Idle timeout s"), ("motion.command_timeout_s", "Command timeout s"),
            ("jog.xy_step_mm", "Jog XY step mm"), ("jog.z_step_mm", "Jog Z step mm"),
            ("jog.feed_xy", "Jog XY feed"), ("jog.feed_z", "Jog Z feed"),
            ("limits.x_min", "X min"), ("limits.x_max", "X max"),
            ("limits.y_min", "Y min"), ("limits.y_max", "Y max"),
            ("limits.z_min", "Z min"), ("limits.z_max", "Z max"),
            ("keepout.x_min", "Keepout X min"), ("keepout.x_max", "Keepout X max"),
            ("keepout.y_min", "Keepout Y min"), ("keepout.y_max", "Keepout Y max"),
        ], checks=[
            ("motion.wait_idle", "Wait until Idle"),
            ("ui.unlock_ordered_flow", "Unlock ordered flow"),
            ("keepout.enabled", "Enable keepout"),
            ("keepout.block_on_violation", "Block keepout violation"),
        ])
        self.form(stage, [
            ("stage.initial_x", "Initial X"), ("stage.initial_y", "Initial Y"), ("stage.initial_z", "Initial Z"),
            ("stage.lowered_z", "Lowered Z"),
            ("exposure.exposure_ref_x", "Exposure reference X"),
            ("exposure.exposure_ref_y", "Exposure reference Y"),
            ("exposure.die1_offset_x", "Die 1 offset X"),
            ("exposure.die1_offset_y", "Die 1 offset Y"),
            ("exposure.col_step_dx", "Die 1 -> Die 2 stage dX"),
            ("exposure.col_step_dy", "Die 1 -> Die 2 stage dY"),
            ("exposure.row_step_dx", "Die 1 -> Die 6 stage dX"),
            ("exposure.row_step_dy", "Die 1 -> Die 6 stage dY"),
            ("exposure.selected_die_numbers", "Selected dies"),
            ("exposure.exposure_z", "Exposure Z"),
            ("exposure.exposure_time_s", "Exposure time s"),
            ("exposure.per_die_exposure_time_s", "Per-die exposure times"),
            ("exposure.per_die_uv_intensity", "Per-die UV intensity S"),
        ], checks=[
            ("stage.invert_z_output", "Invert Z output in G-code"),
            ("exposure.move_through_all_die_positions", "Move through all die positions, expose selected dies only"),
        ])
        self.exposure_capture_controls(stage)
        self.route_form(routes)
        self.form(io, [("io.uv_on_gcode", "UV ON command"), ("io.uv_off_gcode", "UV OFF command")], checks=[("io.uv_enabled", "Enable UV/LED G-code output")])

        save = PillButton("Save settings", "primary")
        save.clicked.connect(self.save_settings)
        layout.addWidget(save)
        root.addWidget(scroll, 1)

    def form(self, parent: QWidget, fields: List[Tuple[str, str]], checks: List[Tuple[str, str]] = []):
        layout = QVBoxLayout(parent)
        card = Card()
        grid = QGridLayout()
        for row, (key, label) in enumerate(fields):
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit()
            edit.textChanged.connect(self.queue_preview)
            if key == "exposure.per_die_exposure_time_s":
                edit.textChanged.connect(self.update_per_die_time_grid_from_text)
            self.fields[key] = edit
            grid.addWidget(edit, row, 1)
        card.layout.addLayout(grid)
        for key, label in checks:
            cb = QCheckBox(label)
            cb.stateChanged.connect(self.queue_preview)
            if key == "ui.unlock_ordered_flow":
                cb.stateChanged.connect(self.refresh_process_buttons)
            self.fields[key] = cb
            card.layout.addWidget(cb)
        layout.addWidget(card)
        layout.addStretch()

    def exposure_capture_controls(self, parent: QWidget):
        layout = parent.layout()
        if layout is None:
            return
        card = Card("Stage-vector capture", "Capture current controller position into the exposure reference model.")
        buttons = [
            ("Save current as exposure reference", self.save_current_as_exposure_reference),
            ("Save current as Die 1 offset", self.save_current_as_die1_offset),
            ("Save current as Die 2 step", self.save_current_as_die2_step),
            ("Save current as Die 6 step", self.save_current_as_die6_step),
        ]
        grid = QGridLayout()
        for idx, (text, handler) in enumerate(buttons):
            btn = PillButton(text, "soft")
            btn.clicked.connect(handler)
            grid.addWidget(btn, idx // 2, idx % 2)
        card.layout.addLayout(grid)
        layout.insertWidget(max(0, layout.count() - 1), card)

    def route_form(self, parent: QWidget):
        layout = QVBoxLayout(parent)
        card = Card("Routes", "One axis per line. The final To loading coordinate becomes the Load position.")
        for key, label in (
            ("to_loading", "To loading"),
            ("to_exposure", "To exposure"),
            ("to_camera_alignment", "To camera alignment"),
            ("return", "Return"),
        ):
            card.layout.addWidget(QLabel(label))
            text = QTextEdit()
            text.setMinimumHeight(120)
            text.textChanged.connect(self.queue_preview)
            self.route_fields[key] = text
            card.layout.addWidget(text)
        layout.addWidget(card)
        layout.addStretch()

    def queue_preview(self):
        if self.suppress_preview:
            return
        try:
            recipe = self.recipe_from_ui()
        except Exception:
            return
        self.recipe = recipe
        self.route_widget.set_recipe(recipe)
        self.update_telemetry_from_recipe(recipe, reset_position=False)
        self.update_process_alignment_summary()
        self.schedule_recipe_save_prompt()

    def schedule_recipe_save_prompt(self):
        if self.suppress_preview or self.recipe_save_prompt_active:
            return
        self.recipe_save_prompt_pending = True
        self.recipe_save_prompt_timer.start(2000)

    def confirm_recipe_save(self):
        if not self.recipe_save_prompt_pending or self.recipe_save_prompt_active:
            return
        if self.running:
            self.recipe_save_prompt_timer.start(2000)
            return
        self.recipe_save_prompt_active = True
        try:
            self.recipe = self.recipe_from_ui()
            dialog = RecipeSaveDialog(self.recipe_path.name, self)
            if dialog.exec() == QDialog.Accepted:
                RecipeCodec.save(self.recipe, self.recipe_path)
                self.route_widget.set_recipe(self.recipe)
                self.update_process_alignment_summary()
                self.safe_log(f"[SAVE CONFIRMED] {self.recipe_path.name}")
            else:
                self.safe_log("[SAVE SKIPPED] settings changed in memory only")
            self.recipe_save_prompt_pending = False
        except Exception as exc:
            self.safe_log(f"[SAVE PROMPT SKIP] {exc}")
        finally:
            self.recipe_save_prompt_active = False

    def load_recipe_to_ui(self, r: Recipe):
        values = {
            "serial.port": r.serial.port,
            "serial.baud": r.serial.baud,
            "motion.feed_xy": r.motion.feed_xy,
            "motion.feed_z": r.motion.feed_z,
            "motion.wait_idle": r.motion.wait_idle,
            "motion.idle_timeout_s": r.motion.idle_timeout_s,
            "motion.command_timeout_s": r.motion.command_timeout_s,
            "ui.unlock_ordered_flow": r.ui.unlock_ordered_flow,
            "jog.xy_step_mm": r.jog.xy_step_mm,
            "jog.z_step_mm": r.jog.z_step_mm,
            "jog.feed_xy": r.jog.feed_xy,
            "jog.feed_z": r.jog.feed_z,
            "limits.x_min": r.limits.x_min,
            "limits.x_max": r.limits.x_max,
            "limits.y_min": r.limits.y_min,
            "limits.y_max": r.limits.y_max,
            "limits.z_min": r.limits.z_min,
            "limits.z_max": r.limits.z_max,
            "keepout.enabled": r.keepout.enabled,
            "keepout.block_on_violation": r.keepout.block_on_violation,
            "keepout.x_min": r.keepout.x_min,
            "keepout.x_max": r.keepout.x_max,
            "keepout.y_min": r.keepout.y_min,
            "keepout.y_max": r.keepout.y_max,
            "stage.initial_x": r.stage.initial_x,
            "stage.initial_y": r.stage.initial_y,
            "stage.initial_z": r.stage.initial_z,
            "stage.invert_z_output": r.stage.invert_z_output,
            "stage.lowered_z": r.stage.lowered_z,
            "exposure.exposure_ref_x": r.exposure.exposure_ref_x,
            "exposure.exposure_ref_y": r.exposure.exposure_ref_y,
            "exposure.die1_offset_x": r.exposure.die1_offset_x,
            "exposure.die1_offset_y": r.exposure.die1_offset_y,
            "exposure.col_step_dx": r.exposure.col_step_dx,
            "exposure.col_step_dy": r.exposure.col_step_dy,
            "exposure.row_step_dx": r.exposure.row_step_dx,
            "exposure.row_step_dy": r.exposure.row_step_dy,
            "exposure.selected_die_numbers": ",".join(str(die) for die in r.exposure.selected_die_numbers),
            "exposure.move_through_all_die_positions": r.exposure.move_through_all_die_positions,
            "exposure.exposure_z": r.exposure.exposure_z,
            "exposure.exposure_time_s": r.exposure.exposure_time_s,
            "exposure.per_die_exposure_time_s": keyed_die_values_to_text(r.exposure.per_die_exposure_time_s),
            "exposure.per_die_uv_intensity": keyed_die_values_to_text(r.exposure.per_die_uv_intensity),
            "io.uv_enabled": r.io.uv_enabled,
            "io.uv_on_gcode": r.io.uv_on_gcode,
            "io.uv_off_gcode": r.io.uv_off_gcode,
        }
        for key, value in values.items():
            field = self.fields.get(key)
            if isinstance(field, QCheckBox):
                field.setChecked(bool(value))
            elif isinstance(field, QLineEdit):
                field.setText(str(value))
            quick = self.quick_fields.get(key)
            if quick is not None:
                quick.blockSignals(True)
                quick.setText(str(value))
                quick.blockSignals(False)
        self.load_per_die_time_grid(r.exposure.per_die_exposure_time_s)
        inspection_values = {
            "inspection.alignment_mode": r.inspection.alignment_mode,
            "inspection.camera_index": r.inspection.camera_index,
            "inspection.camera_width": r.inspection.camera_width,
            "inspection.camera_height": r.inspection.camera_height,
            "inspection.camera_fps": r.inspection.camera_fps,
            "inspection.measured_die1_center_x": r.inspection.measured_die1_center_x,
            "inspection.measured_die1_center_y": r.inspection.measured_die1_center_y,
            "inspection.die1_center_alignment_active": r.inspection.die1_center_alignment_active,
            "inspection.camera_to_exposure_dx": r.inspection.camera_to_exposure_dx,
            "inspection.camera_to_exposure_dy": r.inspection.camera_to_exposure_dy,
            "inspection.inspection_z_mode": r.inspection.inspection_z_mode,
            "inspection.inspection_z_single": r.inspection.inspection_z_single,
            "inspection.inspection_z_values": ",".join(str(v) for v in r.inspection.inspection_z_values),
            "inspection.z_sweep_start": r.inspection.z_sweep_start,
            "inspection.z_sweep_end": r.inspection.z_sweep_end,
            "inspection.z_sweep_step": r.inspection.z_sweep_step,
            "inspection.z_settle_time_s": r.inspection.z_settle_time_s,
            "inspection.capture_each_z": r.inspection.capture_each_z,
            "inspection.safe_z": r.inspection.safe_z,
            "inspection.return_to_safe_z_after_die": r.inspection.return_to_safe_z_after_die,
            "inspection.move_through_all_inspection_positions": r.inspection.move_through_all_inspection_positions,
        }
        for key, value in inspection_values.items():
            field = self.inspection_fields.get(key)
            if isinstance(field, QCheckBox):
                field.setChecked(bool(value))
            elif isinstance(field, QComboBox):
                field.setCurrentText(str(value).upper())
            elif isinstance(field, QLineEdit):
                field.setText(str(value))
        self.set_inspection_dies(r.inspection.selected_inspection_die_numbers, save=False)
        self.route_fields["to_loading"].setPlainText(waypoints_to_text(r.to_loading_waypoints))
        self.route_fields["to_exposure"].setPlainText(waypoints_to_text(r.to_exposure_waypoints))
        self.route_fields["to_camera_alignment"].setPlainText(waypoints_to_text(r.to_camera_alignment_waypoints))
        self.route_fields["return"].setPlainText(waypoints_to_text(r.return_waypoints))
        self.update_telemetry_from_recipe(r, reset_position=True)
        self.update_process_alignment_summary()

    def set_quick_value(self, key: str, value: str):
        quick = self.quick_fields.get(key)
        if quick is not None:
            quick.setText(value)

    def sync_quick_field(self, key: str):
        quick = self.quick_fields.get(key)
        if quick is None:
            return
        value = quick.text().strip()
        settings_field = self.fields.get(key)
        if isinstance(settings_field, QLineEdit) and settings_field.text().strip() != value:
            settings_field.blockSignals(True)
            settings_field.setText(value)
            settings_field.blockSignals(False)
        if key == "exposure.per_die_exposure_time_s":
            try:
                self.load_per_die_time_grid(self.parse_die_value_map_text(value))
            except Exception:
                pass
        self.queue_preview()
        try:
            recipe = self.recipe_from_ui()
            self.feed_label.setText(f"Feed  XY {recipe.motion.feed_xy:g} / Z {recipe.motion.feed_z:g}")
            self.jog_label.setText(f"Jog  XY {recipe.jog.xy_step_mm:g} mm / Z {recipe.jog.z_step_mm:g} mm")
        except Exception:
            pass

    def parse_die_value_map_text(self, raw: str) -> Dict[str, float]:
        raw = (raw or "").replace("\n", ",").replace(" ", "")
        if not raw:
            return {}
        values: Dict[str, float] = {}
        for part in raw.split(","):
            if not part:
                continue
            if ":" not in part:
                raise ValueError("Use die:value pairs")
            die_text, value_text = part.split(":", 1)
            die = int(die_text)
            if die < 1 or die > 9:
                raise ValueError("Die must be 1..9")
            values[str(die)] = float(value_text)
        return values

    def load_per_die_time_grid(self, values: Dict[str, float]):
        for die, edit in self.per_die_time_fields.items():
            edit.blockSignals(True)
            value = values.get(str(die))
            edit.setText("" if value is None else f"{float(value):g}")
            edit.blockSignals(False)

    def update_per_die_time_grid_from_text(self, text: str):
        try:
            self.load_per_die_time_grid(self.parse_die_value_map_text(text))
        except Exception:
            pass

    def sync_per_die_time_grid(self):
        values: Dict[str, float] = {}
        for die, edit in self.per_die_time_fields.items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                values[str(die)] = float(text)
            except ValueError:
                return
        text_value = keyed_die_values_to_text(values)
        for field in (self.quick_fields.get("exposure.per_die_exposure_time_s"), self.fields.get("exposure.per_die_exposure_time_s")):
            if isinstance(field, QLineEdit):
                field.blockSignals(True)
                field.setText(text_value)
                field.blockSignals(False)
        self.queue_preview()

    def set_camera_index_from_worker(self, index: int):
        field = self.inspection_fields.get("inspection.camera_index")
        if isinstance(field, QLineEdit):
            field.blockSignals(True)
            field.setText(str(index))
            field.blockSignals(False)

    def set_inspection_text_field(self, key: str, value: float | str):
        field = self.inspection_fields.get(key)
        if isinstance(field, QLineEdit):
            field.blockSignals(True)
            field.setText(f"{float(value):.3f}" if isinstance(value, (float, int)) else str(value))
            field.blockSignals(False)

    def set_inspection_check(self, key: str, value: bool):
        field = self.inspection_fields.get(key)
        if isinstance(field, QCheckBox):
            field.blockSignals(True)
            field.setChecked(bool(value))
            field.blockSignals(False)

    def set_inspection_combo(self, key: str, value: str):
        field = self.inspection_fields.get(key)
        if isinstance(field, QComboBox):
            field.blockSignals(True)
            field.setCurrentText(str(value).upper())
            field.blockSignals(False)

    def xy_text(self, x: float, y: float) -> str:
        return f"X{x:.3f}  Y{y:.3f}"

    def update_process_alignment_summary(self):
        if not self.process_alignment_labels and not self.process_mark_labels:
            return
        try:
            r = self.recipe
            planner = MotionPlanner(r)
            nominal_x, nominal_y = planner.calculate_camera_die1_stage_position()
            nominal_exposure_x = float(r.exposure.exposure_ref_x) + float(r.exposure.die1_offset_x)
            nominal_exposure_y = float(r.exposure.exposure_ref_y) + float(r.exposure.die1_offset_y)
            marks = r.inspection.measured_die1_marks or {}
            measured_x = float(r.inspection.measured_die1_center_x)
            measured_y = float(r.inspection.measured_die1_center_y)

            for mark in ("TL", "TR", "BL", "BR"):
                label = self.process_mark_labels.get(mark)
                if label is None:
                    continue
                data = marks.get(mark)
                if data:
                    label.setText(self.xy_text(float(data["x"]), float(data["y"])))
                else:
                    label.setText("not saved")

            four_marks = [marks.get(mark) for mark in ("TL", "TR", "BL", "BR") if marks.get(mark)]
            if four_marks:
                avg_x = sum(float(mark["x"]) for mark in four_marks) / len(four_marks)
                avg_y = sum(float(mark["y"]) for mark in four_marks) / len(four_marks)
                avg_text = f"{self.xy_text(avg_x, avg_y)}  ({len(four_marks)}/4)"
                offset_x = avg_x - nominal_x
                offset_y = avg_y - nominal_y
                offset_note = "avg"
            else:
                avg_x = avg_y = 0.0
                avg_text = "not available"
                offset_x = measured_x - nominal_x
                offset_y = measured_y - nominal_y
                offset_note = "active"
            if r.inspection.die1_center_alignment_active:
                final_x = nominal_exposure_x + (measured_x - nominal_x)
                final_y = nominal_exposure_y + (measured_y - nominal_y)
            elif four_marks:
                final_x = nominal_exposure_x + (avg_x - nominal_x)
                final_y = nominal_exposure_y + (avg_y - nominal_y)
            else:
                final_x = nominal_exposure_x
                final_y = nominal_exposure_y

            values = {
                "nominal": self.xy_text(nominal_x, nominal_y),
                "average": avg_text,
                "measured": self.xy_text(measured_x, measured_y) + ("  ACTIVE" if r.inspection.die1_center_alignment_active else "  inactive"),
                "offset": f"dX{offset_x:.3f}  dY{offset_y:.3f}  ({offset_note})",
                "final": self.xy_text(final_x, final_y),
            }
            for key, value in values.items():
                label = self.process_alignment_labels.get(key)
                if label is not None:
                    label.setText(value)
        except Exception as exc:
            for label in list(self.process_alignment_labels.values()) + list(self.process_mark_labels.values()):
                label.setText("-")
            self.safe_log(f"[ALIGN SUMMARY ERROR] {exc}")

    def set_text_field(self, key: str, value: float | str):
        field = self.fields.get(key)
        if isinstance(field, QLineEdit):
            field.blockSignals(True)
            field.setText(f"{float(value):.3f}" if isinstance(value, (float, int)) else str(value))
            field.blockSignals(False)

    def current_inspection_dies(self) -> List[int]:
        selected = []
        for die, btn in self.inspection_die_buttons.items():
            if btn.property("selected"):
                selected.append(die)
        return sorted(selected, key=lambda d: list(MotionPlanner.DIE_TO_ROW_COL.keys()).index(d))

    def set_inspection_dies(self, dies: List[int], save: bool = True):
        selected = {int(d) for d in dies}
        for die, btn in self.inspection_die_buttons.items():
            is_selected = die in selected
            btn.setProperty("selected", is_selected)
            btn.setObjectName("pill_blue" if is_selected else "pill_soft")
            self.repolish(btn)
        if save:
            self.refresh_preview_without_save_prompt()

    def refresh_preview_without_save_prompt(self):
        old_pending = self.recipe_save_prompt_pending
        self.recipe_save_prompt_timer.stop()
        self.recipe_save_prompt_pending = False
        try:
            self.suppress_preview = True
            try:
                self.recipe = self.recipe_from_ui()
                self.route_widget.set_recipe(self.recipe)
                self.update_telemetry_from_recipe(self.recipe, reset_position=False)
                self.update_process_alignment_summary()
            finally:
                self.suppress_preview = False
        finally:
            self.recipe_save_prompt_pending = old_pending
            if old_pending:
                self.recipe_save_prompt_timer.start(2000)

    def toggle_inspection_die(self, die: int):
        btn = self.inspection_die_buttons[die]
        btn.setProperty("selected", not bool(btn.property("selected")))
        self.active_inspection_die = die
        self.camera_preview.set_active_die(die)
        self.video_overlay.set_active_die(die)
        self.set_inspection_dies(self.current_inspection_dies())
        self.safe_log(f"[INSPECTION] Die {die} selected")
        if self.running:
            self.safe_log(f"[INSPECTION] Die {die} selected; move skipped because a job is running")
            return
        QTimer.singleShot(0, self.move_to_active_inspection_die)

    def persist_capture(self, label: str):
        try:
            self.recipe = self.recipe_from_ui()
            RecipeCodec.save(self.recipe, self.recipe_path)
            self.recipe_save_prompt_pending = False
            self.recipe_save_prompt_timer.stop()
            self.route_widget.set_recipe(self.recipe)
            self.update_telemetry_from_recipe(self.recipe, reset_position=False)
            self.safe_log(f"[CAPTURE] {label} saved to {self.recipe_path.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Capture failed", str(exc))

    def save_current_as_exposure_reference(self):
        self.suppress_preview = True
        try:
            self.set_text_field("exposure.exposure_ref_x", self.sim_pos["X"])
            self.set_text_field("exposure.exposure_ref_y", self.sim_pos["Y"])
        finally:
            self.suppress_preview = False
        self.persist_capture("exposure reference")

    def save_current_as_die1_offset(self):
        ref_x = float(self.fields["exposure.exposure_ref_x"].text())
        ref_y = float(self.fields["exposure.exposure_ref_y"].text())
        self.suppress_preview = True
        try:
            self.set_text_field("exposure.die1_offset_x", self.sim_pos["X"] - ref_x)
            self.set_text_field("exposure.die1_offset_y", self.sim_pos["Y"] - ref_y)
        finally:
            self.suppress_preview = False
        self.persist_capture("Die 1 offset")

    def save_current_as_die2_step(self):
        ref_x = float(self.fields["exposure.exposure_ref_x"].text())
        ref_y = float(self.fields["exposure.exposure_ref_y"].text())
        off_x = float(self.fields["exposure.die1_offset_x"].text())
        off_y = float(self.fields["exposure.die1_offset_y"].text())
        die1_x = ref_x + off_x
        die1_y = ref_y + off_y
        self.suppress_preview = True
        try:
            self.set_text_field("exposure.col_step_dx", self.sim_pos["X"] - die1_x)
            self.set_text_field("exposure.col_step_dy", self.sim_pos["Y"] - die1_y)
        finally:
            self.suppress_preview = False
        self.persist_capture("Die 2 step vector")

    def save_current_as_die6_step(self):
        ref_x = float(self.fields["exposure.exposure_ref_x"].text())
        ref_y = float(self.fields["exposure.exposure_ref_y"].text())
        off_x = float(self.fields["exposure.die1_offset_x"].text())
        off_y = float(self.fields["exposure.die1_offset_y"].text())
        die1_x = ref_x + off_x
        die1_y = ref_y + off_y
        self.suppress_preview = True
        try:
            self.set_text_field("exposure.row_step_dx", self.sim_pos["X"] - die1_x)
            self.set_text_field("exposure.row_step_dy", self.sim_pos["Y"] - die1_y)
        finally:
            self.suppress_preview = False
        self.persist_capture("Die 6 step vector")

    def persist_camera_capture(self, label: str):
        try:
            self.recipe = self.recipe_from_ui()
            RecipeCodec.save(self.recipe, self.recipe_path)
            self.recipe_save_prompt_pending = False
            self.recipe_save_prompt_timer.stop()
            self.safe_log(f"[CAMERA CAPTURE] {label} saved to {self.recipe_path.name}")
            self.route_widget.set_recipe(self.recipe)
            self.update_process_alignment_summary()
            self.preview_inspection_plan(log_only=True)
        except Exception as exc:
            QMessageBox.critical(self, "Camera capture failed", str(exc))

    def save_current_as_camera_reference(self):
        self.recipe.inspection.camera_ref_x = float(self.sim_pos["X"])
        self.recipe.inspection.camera_ref_y = float(self.sim_pos["Y"])
        self.persist_camera_capture("camera reference")

    def save_current_as_camera_die1_offset(self):
        i = self.recipe.inspection
        i.camera_die1_offset_x = float(self.sim_pos["X"]) - i.camera_ref_x
        i.camera_die1_offset_y = float(self.sim_pos["Y"]) - i.camera_ref_y
        self.persist_camera_capture("camera Die 1 offset")

    def save_current_as_camera_die2_step(self):
        i = self.recipe.inspection
        die1_x = i.camera_ref_x + i.camera_die1_offset_x
        die1_y = i.camera_ref_y + i.camera_die1_offset_y
        i.camera_col_step_dx = float(self.sim_pos["X"]) - die1_x
        i.camera_col_step_dy = float(self.sim_pos["Y"]) - die1_y
        self.persist_camera_capture("camera Die 2 step")

    def save_current_as_camera_die6_step(self):
        i = self.recipe.inspection
        die1_x = i.camera_ref_x + i.camera_die1_offset_x
        die1_y = i.camera_ref_y + i.camera_die1_offset_y
        i.camera_row_step_dx = float(self.sim_pos["X"]) - die1_x
        i.camera_row_step_dy = float(self.sim_pos["Y"]) - die1_y
        self.persist_camera_capture("camera Die 6 step")

    def open_camera_alignment_workspace(self):
        self.stack.setCurrentWidget(self.inspection)
        self.start_camera_preview()
        try:
            self.recipe = self.recipe_from_ui()
            planner = MotionPlanner(self.recipe)
            cam_x, cam_y = planner.calculate_camera_die1_stage_position()
            self.safe_log(f"[2ND FLOW] camera alignment workspace opened. Nominal Camera Die 1: X{cam_x:.3f} Y{cam_y:.3f}")
        except Exception:
            self.safe_log("[2ND FLOW] camera alignment workspace opened. Jog until the target mark is on the crosshair, then save the mark.")

    def open_process_camera_tab(self):
        self.stack.setCurrentWidget(self.process)
        if self.process_right_tabs is not None and self.process_camera_tab_index >= 0:
            self.process_right_tabs.setCurrentIndex(self.process_camera_tab_index)
        self.attach_camera_stack("process")
        self.start_camera_preview()
        try:
            self.recipe = self.recipe_from_ui()
            planner = MotionPlanner(self.recipe)
            cam_x, cam_y = planner.calculate_camera_die1_stage_position()
            self.safe_log(f"[2ND FLOW] process camera tab opened. Nominal Camera Die 1: X{cam_x:.3f} Y{cam_y:.3f}")
        except Exception:
            self.safe_log("[2ND FLOW] process camera tab opened. Jog until Die 1 is on the crosshair, then save center.")

    def unlock_second_alignment_capture(self):
        if self.second_current_step <= 2:
            self.second_current_step = 3
            self.refresh_process_buttons()
            self.safe_log("[2ND FLOW] Camera Die 1 center captured; correction preview step unlocked")

    def save_current_as_die1_camera_center(self):
        self.recipe = self.recipe_from_ui()
        x = float(self.sim_pos["X"])
        y = float(self.sim_pos["Y"])
        i = self.recipe.inspection
        i.camera_die1_offset_x = x - float(i.camera_ref_x)
        i.camera_die1_offset_y = y - float(i.camera_ref_y)
        i.measured_die1_center_x = x
        i.measured_die1_center_y = y
        i.measured_die1_marks = {}
        i.die1_center_alignment_active = False
        self.recipe.inspection.alignment_mode = "DIE1_CENTER_ONLY"
        self.set_inspection_text_field("inspection.camera_die1_offset_x", i.camera_die1_offset_x)
        self.set_inspection_text_field("inspection.camera_die1_offset_y", i.camera_die1_offset_y)
        self.set_inspection_text_field("inspection.measured_die1_center_x", x)
        self.set_inspection_text_field("inspection.measured_die1_center_y", y)
        self.set_inspection_check("inspection.die1_center_alignment_active", False)
        self.set_inspection_combo("inspection.alignment_mode", "DIE1_CENTER_ONLY")
        self.safe_log(f"[CAMERA NOMINAL] Camera Die 1 nominal saved: X{x:.3f} Y{y:.3f}")
        self.persist_camera_capture("nominal Camera Die 1")

    def use_default_camera_die1_as_alignment_center(self):
        self.recipe = self.recipe_from_ui()
        planner = MotionPlanner(self.recipe)
        x, y = planner.calculate_camera_die1_stage_position()
        self.recipe.inspection.measured_die1_center_x = x
        self.recipe.inspection.measured_die1_center_y = y
        self.recipe.inspection.die1_center_alignment_active = True
        self.recipe.inspection.alignment_mode = "DIE1_CENTER_ONLY"
        self.set_inspection_text_field("inspection.measured_die1_center_x", x)
        self.set_inspection_text_field("inspection.measured_die1_center_y", y)
        self.set_inspection_check("inspection.die1_center_alignment_active", True)
        self.set_inspection_combo("inspection.alignment_mode", "DIE1_CENTER_ONLY")
        self.safe_log(f"[ALIGN] default Camera Die 1 used as alignment center: X{x:.3f} Y{y:.3f}")
        self.persist_camera_capture("default Camera Die 1 alignment center")
        self.unlock_second_alignment_capture()

    def second_alignment_ready(self, show_message: bool = True) -> bool:
        try:
            recipe = self.recipe_from_ui()
            marks = recipe.inspection.measured_die1_marks or {}
            missing = [mark for mark in ("TL", "TR", "BL", "BR") if mark not in marks]
            active = (
                (recipe.inspection.alignment_mode or "").upper() == "DIE1_CENTER_ONLY"
                and recipe.inspection.die1_center_alignment_active
            )
            if missing:
                if show_message:
                    QMessageBox.critical(self, "Camera marks needed", "Save all Camera Die 1 marks first: " + ", ".join(missing))
                return False
            if not active:
                if show_message:
                    QMessageBox.critical(self, "Camera correction needed", "Apply the 4-mark Camera Die 1 correction first.")
                return False
            return True
        except Exception as exc:
            if show_message:
                QMessageBox.critical(self, "Alignment check failed", str(exc))
            return False

    def clear_second_alignment_data(self, reason: str = "2nd process complete"):
        try:
            self.recipe = self.recipe_from_ui()
            planner = MotionPlanner(self.recipe)
            nominal_x, nominal_y = planner.calculate_camera_die1_stage_position()
            self.recipe.inspection.measured_die1_marks = {}
            self.recipe.inspection.measured_die1_center_x = nominal_x
            self.recipe.inspection.measured_die1_center_y = nominal_y
            self.recipe.inspection.die1_center_alignment_active = False
            self.recipe.inspection.alignment_mode = "DIE1_CENTER_ONLY"
            self.set_inspection_text_field("inspection.measured_die1_center_x", nominal_x)
            self.set_inspection_text_field("inspection.measured_die1_center_y", nominal_y)
            self.set_inspection_check("inspection.die1_center_alignment_active", False)
            self.set_inspection_combo("inspection.alignment_mode", "DIE1_CENTER_ONLY")
            RecipeCodec.save(self.recipe, self.recipe_path)
            self.recipe_save_prompt_pending = False
            self.recipe_save_prompt_timer.stop()
            self.route_widget.set_recipe(self.recipe)
            self.update_process_alignment_summary()
            self.safe_log(f"[ALIGN CLEAR] {reason}; Camera Die 1 marks and active correction cleared")
        except Exception as exc:
            self.safe_log(f"[ALIGN CLEAR ERROR] {exc}")

    def save_die1_alignment_mark(self, mark: str):
        mark = mark.upper()
        if mark not in {"TOP", "BOTTOM", "TL", "TR", "BR", "BL"}:
            return
        self.recipe = self.recipe_from_ui()
        marks = dict(self.recipe.inspection.measured_die1_marks or {})
        marks[mark] = {"x": float(self.sim_pos["X"]), "y": float(self.sim_pos["Y"])}
        self.recipe.inspection.measured_die1_marks = marks
        self.safe_log(f"[ALIGN] Camera Die 1 {mark} mark saved: X{self.sim_pos['X']:.3f} Y{self.sim_pos['Y']:.3f}")
        self.persist_camera_capture(f"Camera Die 1 {mark} mark")

    def calculate_die1_center_from_top_bottom_marks(self):
        self.recipe = self.recipe_from_ui()
        marks = self.recipe.inspection.measured_die1_marks or {}
        required = ["TOP", "BOTTOM"]
        missing = [mark for mark in required if mark not in marks]
        if missing:
            QMessageBox.warning(self, "Missing marks", "Save TOP and BOTTOM marks first: " + ", ".join(missing))
            self.safe_log("[ALIGN] missing Die 1 TOP/BOTTOM marks: " + ", ".join(missing))
            return
        center_x = (float(marks["TOP"]["x"]) + float(marks["BOTTOM"]["x"])) / 2.0
        center_y = (float(marks["TOP"]["y"]) + float(marks["BOTTOM"]["y"])) / 2.0
        self.recipe.inspection.measured_die1_center_x = center_x
        self.recipe.inspection.measured_die1_center_y = center_y
        self.recipe.inspection.die1_center_alignment_active = True
        self.recipe.inspection.alignment_mode = "DIE1_CENTER_ONLY"
        self.set_inspection_text_field("inspection.measured_die1_center_x", center_x)
        self.set_inspection_text_field("inspection.measured_die1_center_y", center_y)
        self.set_inspection_check("inspection.die1_center_alignment_active", True)
        self.set_inspection_combo("inspection.alignment_mode", "DIE1_CENTER_ONLY")
        self.safe_log(f"[ALIGN] Die 1 center from TOP/BOTTOM: X{center_x:.3f} Y{center_y:.3f}")
        self.persist_camera_capture("Die 1 center from TOP/BOTTOM marks")
        self.unlock_second_alignment_capture()

    def calculate_die1_center_from_marks(self, show_messages: bool = True) -> bool:
        self.recipe = self.recipe_from_ui()
        marks = self.recipe.inspection.measured_die1_marks or {}
        required = ["TL", "TR", "BR", "BL"]
        missing = [mark for mark in required if mark not in marks]
        if missing:
            if show_messages:
                QMessageBox.warning(self, "Missing marks", "Save all Camera Die 1 marks first: " + ", ".join(missing))
            self.safe_log("[ALIGN] missing Camera Die 1 marks: " + ", ".join(missing))
            return False
        xs = [float(marks[mark]["x"]) for mark in required]
        ys = [float(marks[mark]["y"]) for mark in required]
        center_x = sum(xs) / 4.0
        center_y = sum(ys) / 4.0
        self.recipe.inspection.measured_die1_center_x = center_x
        self.recipe.inspection.measured_die1_center_y = center_y
        self.recipe.inspection.die1_center_alignment_active = True
        self.recipe.inspection.alignment_mode = "DIE1_CENTER_ONLY"
        self.set_inspection_text_field("inspection.measured_die1_center_x", center_x)
        self.set_inspection_text_field("inspection.measured_die1_center_y", center_y)
        self.set_inspection_check("inspection.die1_center_alignment_active", True)
        self.set_inspection_combo("inspection.alignment_mode", "DIE1_CENTER_ONLY")
        self.safe_log(f"[ALIGN] Die 1 center from marks: X{center_x:.3f} Y{center_y:.3f}")
        self.persist_camera_capture("Die 1 center from 4 marks")
        self.unlock_second_alignment_capture()
        return True

    def preview_die1_center_aligned_positions(self):
        try:
            self.recipe = self.recipe_from_ui()
            planner = MotionPlanner(self.recipe, use_camera_alignment=True)
            lines = planner.preview_exposure_plan()
            self.safe_log("[DIE1 CENTER ALIGNMENT PREVIEW]")
            for line in lines:
                self.safe_log(line)
            warnings = list(planner.warnings)
            errors = list(planner.errors)
            _commands, seq_warnings, seq_errors = planner.exposure_grid_sequence(with_prepare=False)
            warnings.extend(seq_warnings)
            errors.extend(seq_errors)
            for warning in warnings:
                self.safe_log(f"[WARN] {warning}")
            for error in errors:
                self.safe_log(f"[ERROR] {error}")
            self.set_status("Alignment Preview Ready")
            self.safe_log(f"[ALIGN PREVIEW] {len(lines)} lines previewed. Final exposure positions are in Log.")
        except Exception as exc:
            QMessageBox.critical(self, "Alignment preview failed", str(exc))

    def recipe_from_ui(self) -> Recipe:
        def text(key: str) -> str:
            field = self.fields[key]
            if isinstance(field, QCheckBox):
                return "1" if field.isChecked() else "0"
            return field.text().strip()

        def f(key: str) -> float:
            return float(text(key))

        def i(key: str) -> int:
            return int(float(text(key)))

        def b(key: str) -> bool:
            field = self.fields[key]
            return bool(field.isChecked()) if isinstance(field, QCheckBox) else bool(text(key))

        def selected_dies(key: str) -> List[int]:
            raw = text(key).replace(" ", "")
            if not raw:
                return []
            dies: List[int] = []
            for part in raw.split(","):
                die = int(part)
                if die < 1 or die > 9:
                    raise ValueError("Selected dies must be comma-separated numbers from 1 to 9.")
                if die not in dies:
                    dies.append(die)
            return dies

        def die_value_map(key: str) -> Dict[str, float]:
            raw = text(key).replace("\n", ",").replace(" ", "")
            if not raw:
                return {}
            values: Dict[str, float] = {}
            for part in raw.split(","):
                if not part:
                    continue
                if ":" not in part:
                    raise ValueError(f"{key} must use die:value pairs, e.g. 1:1.0,2:1.5")
                die_text, value_text = part.split(":", 1)
                die = int(die_text)
                if die < 1 or die > 9:
                    raise ValueError(f"{key} die number must be 1..9")
                values[str(die)] = float(value_text)
            return values

        def inspection_text(key: str) -> str:
            field = self.inspection_fields[key]
            if isinstance(field, QCheckBox):
                return "1" if field.isChecked() else "0"
            if isinstance(field, QComboBox):
                return field.currentText().strip()
            return field.text().strip()

        def inspection_f(key: str) -> float:
            return float(inspection_text(key))

        def inspection_i(key: str) -> int:
            return int(float(inspection_text(key)))

        def inspection_b(key: str) -> bool:
            field = self.inspection_fields[key]
            return bool(field.isChecked()) if isinstance(field, QCheckBox) else bool(inspection_text(key))

        def float_list_from_text(raw: str) -> List[float]:
            cleaned = raw.replace("\n", ",").replace(" ", "")
            if not cleaned:
                return []
            return [float(part) for part in cleaned.split(",") if part]

        initial = {"X": f("stage.initial_x"), "Y": f("stage.initial_y"), "Z": f("stage.initial_z")}
        to_loading = parse_waypoints(self.route_fields["to_loading"].toPlainText())
        endpoint = waypoint_endpoint(initial, to_loading)
        exposure_ref_x = f("exposure.exposure_ref_x")
        exposure_ref_y = f("exposure.exposure_ref_y")
        return Recipe(
            serial=SerialRecipe(text("serial.port"), i("serial.baud")),
            motion=MotionRecipe(f("motion.feed_xy"), f("motion.feed_z"), b("motion.wait_idle"), f("motion.idle_timeout_s"), f("motion.command_timeout_s")),
            jog=JogRecipe(f("jog.xy_step_mm"), f("jog.z_step_mm"), f("jog.feed_xy"), f("jog.feed_z")),
            limits=AxisLimits(f("limits.x_min"), f("limits.x_max"), f("limits.y_min"), f("limits.y_max"), f("limits.z_min"), f("limits.z_max")),
            keepout=KeepoutZone(b("keepout.enabled"), b("keepout.block_on_violation"), f("keepout.x_min"), f("keepout.x_max"), f("keepout.y_min"), f("keepout.y_max")),
            stage=StageRecipe(initial["X"], initial["Y"], initial["Z"], b("stage.invert_z_output"), endpoint["X"], endpoint["Y"], endpoint["Z"], f("stage.lowered_z")),
            exposure=ExposureRecipe(
                exposure_ref_x,
                exposure_ref_y,
                3,
                3,
                f("exposure.col_step_dx"),
                f("exposure.row_step_dy"),
                f("exposure.exposure_time_s"),
                exposure_ref_x,
                exposure_ref_y,
                f("exposure.die1_offset_x"),
                f("exposure.die1_offset_y"),
                f("exposure.col_step_dx"),
                f("exposure.col_step_dy"),
                f("exposure.row_step_dx"),
                f("exposure.row_step_dy"),
                selected_dies("exposure.selected_die_numbers"),
                b("exposure.move_through_all_die_positions"),
                die_value_map("exposure.per_die_exposure_time_s"),
                die_value_map("exposure.per_die_uv_intensity"),
                f("exposure.exposure_z"),
            ),
            inspection=InspectionRecipe(
                alignment_mode=inspection_text("inspection.alignment_mode").upper(),
                camera_ref_x=self.recipe.inspection.camera_ref_x,
                camera_ref_y=self.recipe.inspection.camera_ref_y,
                camera_die1_offset_x=self.recipe.inspection.camera_die1_offset_x,
                camera_die1_offset_y=self.recipe.inspection.camera_die1_offset_y,
                camera_col_step_dx=self.recipe.inspection.camera_col_step_dx,
                camera_col_step_dy=self.recipe.inspection.camera_col_step_dy,
                camera_row_step_dx=self.recipe.inspection.camera_row_step_dx,
                camera_row_step_dy=self.recipe.inspection.camera_row_step_dy,
                camera_to_exposure_dx=inspection_f("inspection.camera_to_exposure_dx"),
                camera_to_exposure_dy=inspection_f("inspection.camera_to_exposure_dy"),
                measured_die1_center_x=inspection_f("inspection.measured_die1_center_x"),
                measured_die1_center_y=inspection_f("inspection.measured_die1_center_y"),
                die1_center_alignment_active=inspection_b("inspection.die1_center_alignment_active"),
                measured_die1_marks=self.recipe.inspection.measured_die1_marks,
                selected_inspection_die_numbers=self.current_inspection_dies(),
                move_through_all_inspection_positions=inspection_b("inspection.move_through_all_inspection_positions"),
                inspection_z_mode=inspection_text("inspection.inspection_z_mode").upper(),
                inspection_z_single=inspection_f("inspection.inspection_z_single"),
                inspection_z_values=float_list_from_text(inspection_text("inspection.inspection_z_values")),
                z_sweep_start=inspection_f("inspection.z_sweep_start"),
                z_sweep_end=inspection_f("inspection.z_sweep_end"),
                z_sweep_step=inspection_f("inspection.z_sweep_step"),
                z_settle_time_s=inspection_f("inspection.z_settle_time_s"),
                capture_each_z=inspection_b("inspection.capture_each_z"),
                safe_z=inspection_f("inspection.safe_z"),
                return_to_safe_z_after_die=inspection_b("inspection.return_to_safe_z_after_die"),
                camera_enabled=self.camera_preview_on,
                camera_index=inspection_i("inspection.camera_index"),
                camera_width=inspection_i("inspection.camera_width"),
                camera_height=inspection_i("inspection.camera_height"),
                camera_fps=inspection_i("inspection.camera_fps"),
                capture_folder=self.recipe.inspection.capture_folder,
            ),
            io=IORecipe(b("io.uv_enabled"), text("io.uv_on_gcode"), text("io.uv_off_gcode")),
            ui=UIRecipe(b("ui.unlock_ordered_flow")),
            to_loading_waypoints=to_loading,
            to_exposure_waypoints=parse_waypoints(self.route_fields["to_exposure"].toPlainText()),
            to_camera_alignment_waypoints=parse_waypoints(self.route_fields["to_camera_alignment"].toPlainText()),
            return_waypoints=parse_waypoints(self.route_fields["return"].toPlainText()),
        )

    def save_settings(self):
        try:
            self.recipe = self.recipe_from_ui()
            RecipeCodec.save(self.recipe, self.recipe_path)
            self.recipe_save_prompt_pending = False
            self.recipe_save_prompt_timer.stop()
            self.route_widget.set_recipe(self.recipe)
            self.safe_log(f"[SAVE] {self.recipe_path}")
            QMessageBox.information(self, "Saved", "Settings saved.")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def update_telemetry_from_recipe(self, r: Recipe, reset_position: bool = False):
        if reset_position:
            self.sim_pos = {
                "X": r.stage.initial_x,
                "Y": r.stage.initial_y,
                "Z": r.stage.initial_z,
            }
        for axis in ("X", "Y", "Z"):
            if axis in self.pos_labels:
                self.pos_labels[axis].setText(f"{self.sim_pos[axis]:.3f}")
        self.route_widget.set_live_position(self.sim_pos["X"], self.sim_pos["Y"])
        self.camera_preview.set_stage_position(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
        self.video_overlay.set_stage_position(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
        self.feed_label.setText(f"Feed  XY {r.motion.feed_xy:g} / Z {r.motion.feed_z:g}")
        self.jog_label.setText(f"Jog  XY {r.jog.xy_step_mm:g} mm / Z {r.jog.z_step_mm:g} mm")

    def update_position_labels(self):
        for axis in ("X", "Y", "Z"):
            if axis in self.pos_labels:
                self.pos_labels[axis].setText(f"{self.sim_pos[axis]:.3f}")
        self.route_widget.set_live_position(self.sim_pos["X"], self.sim_pos["Y"])

    def save_position_state(self, reason: str = "position", force: bool = False):
        now = time.monotonic()
        if not force and now - self.last_position_save_ts < 0.4:
            return
        self.last_position_save_ts = now
        wall = time.strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "saved_at": wall,
            "reason": reason,
            "position": {
                "X": round(float(self.sim_pos["X"]), 6),
                "Y": round(float(self.sim_pos["Y"]), 6),
                "Z": round(float(self.sim_pos["Z"]), 6),
            },
            "status": self.status_label.text(),
            "note": "Display/app position restore only. Verify controller WPos/MPos before live motion.",
        }
        try:
            self.position_state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.position_save_label.setText(
                f"Position save: {data['position']['X']:.3f}, {data['position']['Y']:.3f}, {data['position']['Z']:.3f}  {wall}"
            )
            if force:
                self.safe_log(f"[POSITION SAVE] {self.position_state_path}")
        except Exception as exc:
            self.safe_log(f"[POSITION SAVE ERROR] {exc}")

    def load_saved_position(self, auto: bool = False):
        if not self.position_state_path.exists():
            self.position_save_label.setText("Position save: no saved file")
            if not auto:
                QMessageBox.information(self, "No saved position", "No position_state.json found yet.")
            return
        try:
            data = json.loads(self.position_state_path.read_text(encoding="utf-8"))
            pos = data.get("position", {})
            self.sim_pos = {
                "X": float(pos["X"]),
                "Y": float(pos["Y"]),
                "Z": float(pos["Z"]),
            }
            self.update_position_labels()
            saved_at = str(data.get("saved_at", "unknown time"))
            self.position_save_label.setText(
                f"Position save loaded: {self.sim_pos['X']:.3f}, {self.sim_pos['Y']:.3f}, {self.sim_pos['Z']:.3f}  {saved_at}"
            )
            self.safe_log(f"[POSITION RESTORE] loaded {self.position_state_path}")
            if not auto:
                QMessageBox.information(
                    self,
                    "Position restored",
                    "Saved display position restored. Verify controller status before live motion.",
                )
        except Exception as exc:
            self.position_save_label.setText("Position save: load failed")
            if not auto:
                QMessageBox.critical(self, "Position restore failed", str(exc))
            else:
                self.safe_log(f"[POSITION RESTORE ERROR] {exc}")

    def get_plan(self, name: str, sequence: str = "primary"):
        self.recipe = self.recipe_from_ui()
        self.route_widget.set_recipe(self.recipe)
        use_second_alignment = sequence == "second" and name in {"grid", "return", "corrected_die1_direct"}
        planner = MotionPlanner(self.recipe, use_camera_alignment=use_second_alignment)
        if name == "load":
            return planner.loading_sequence()
        if name == "mount":
            return planner.mount_to_exposure_sequence()
        if name == "camera_align":
            planner.route_to_camera_alignment_sequence()
            cam_x, cam_y = planner.calculate_camera_die1_stage_position()
            if abs(planner.pos["X"] - cam_x) > 1e-9:
                planner.move_axis("X", cam_x, "nominal Camera Die 1 X")
            if abs(planner.pos["Y"] - cam_y) > 1e-9:
                planner.move_axis("Y", cam_y, "nominal Camera Die 1 Y")
            return planner.commands, planner.warnings, planner.errors
        if name == "camera_die1_direct":
            planner.reset(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
            planner.prepare()
            cam_x, cam_y = planner.calculate_camera_die1_stage_position()
            if abs(planner.pos["X"] - cam_x) > 1e-9:
                planner.move_axis("X", cam_x, "direct nominal Camera Die 1 X")
            if abs(planner.pos["Y"] - cam_y) > 1e-9:
                planner.move_axis("Y", cam_y, "direct nominal Camera Die 1 Y")
            return planner.commands, planner.warnings, planner.errors
        if name == "corrected_die1_direct":
            planner.reset(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
            planner.prepare()
            i = self.recipe.inspection
            if not (
                (i.alignment_mode or "").upper() == "DIE1_CENTER_ONLY"
                and i.die1_center_alignment_active
            ):
                planner.errors.append("Camera Die 1 correction is not active. Apply 4-mark correction first.")
                return planner.commands, planner.warnings, planner.errors
            _row, _col, die1_x, die1_y = planner.calculate_die_stage_position(1)
            planner.move_axis("X", die1_x, "direct corrected Exposure Die 1 X")
            planner.move_axis("Y", die1_y, "direct corrected Exposure Die 1 Y")
            planner.move_axis("Z", self.recipe.exposure.exposure_z, "direct corrected Exposure Die 1 Z")
            self.safe_log(f"[2ND FLOW] corrected Exposure Die 1 target X{die1_x:.3f} Y{die1_y:.3f} Z{self.recipe.exposure.exposure_z:.3f}")
            return planner.commands, planner.warnings, planner.errors
        if name == "die1_direct":
            planner.reset(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
            planner.prepare()
            die1_x, die1_y = planner.calculate_die1_stage_position()
            planner.move_axis("X", die1_x, "direct Die 1 exposure X")
            planner.move_axis("Y", die1_y, "direct Die 1 exposure Y")
            planner.move_axis("Z", self.recipe.exposure.exposure_z, "direct Die 1 exposure Z")
            return planner.commands, planner.warnings, planner.errors
        if name == "grid":
            planner.reset(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
            return planner.exposure_grid_sequence()
        if name == "return":
            return planner.return_sequence()
        if name == "full":
            return planner.full_cycle_sequence()
        raise ValueError(name)

    def start_camera_preview(self):
        try:
            self.recipe = self.recipe_from_ui()
            self.stop_camera_preview(silent=True)
            if self.camera_log_box:
                self.camera_log_box.clear()
            if self.process_camera_log_box:
                self.process_camera_log_box.clear()
            index = max(0, int(self.recipe.inspection.camera_index))
            self.camera_stop_event = threading.Event()
            self.latest_camera_frame = None

            devices = QMediaDevices.videoInputs()
            if not devices:
                self.camera_preview_on = False
                self.set_status("Camera Failed")
                self.safe_log("[CAMERA] no Qt camera devices found. Check USB camera connection and Windows camera permission.")
                return
            self.safe_log("[CAMERA] Qt devices: " + ", ".join(f"{i}:{device.description()}" for i, device in enumerate(devices)))
            selected_index = index if index < len(devices) else 0
            selected_device = devices[selected_index]
            self.signals.camera_selected.emit(selected_index)

            self.camera = QCamera(selected_device)
            self.camera.errorOccurred.connect(self.handle_qt_camera_error)
            self.camera.activeChanged.connect(self.handle_qt_camera_active_changed)
            self.capture_session = QMediaCaptureSession()
            self.capture_session.setCamera(self.camera)
            self.capture_session.setVideoOutput(self.video_widget)
            try:
                self.image_capture = QImageCapture()
                self.capture_session.setImageCapture(self.image_capture)
            except Exception as exc:
                self.image_capture = None
                self.safe_log(f"[CAMERA] image capture hook unavailable: {exc}")

            self.camera_preview_on = True
            self.camera_preview.set_camera_on(True)
            self.camera_stack.setCurrentWidget(self.video_container)
            self.update_camera_overlay_geometry()
            self.set_status("Camera Starting")
            self.safe_log(f"[CAMERA] starting Qt camera index {selected_index}: {selected_device.description()}")
            self.camera.start()
            QTimer.singleShot(1400, self.confirm_qt_camera_started)
        except Exception as exc:
            self.camera_stack.setCurrentWidget(self.camera_preview)
            QMessageBox.critical(self, "Camera start failed", str(exc))

    def camera_start_watchdog(self, stop_event: threading.Event):
        if stop_event.is_set() or not self.camera_preview_on:
            return
        if self.latest_camera_frame is None:
            self.set_status("Camera Searching")
            self.safe_log("[CAMERA] still searching. If this stays blank, check Windows Camera permission or another app using the camera.")

    def update_camera_overlay_geometry(self):
        if not hasattr(self, "video_overlay"):
            return
        video_is_live = self.camera_stack.currentWidget() is self.video_container and self.camera_preview_on
        camera_area_ready = self.camera_stack.isVisible() and self.camera_stack.width() > 40 and self.camera_stack.height() > 40
        if video_is_live and camera_area_ready:
            top_left = self.camera_stack.mapToGlobal(self.camera_stack.rect().topLeft())
            self.video_overlay.setGeometry(top_left.x(), top_left.y(), self.camera_stack.width(), self.camera_stack.height())
            self.video_overlay.show()
            self.video_overlay.raise_()
        else:
            self.video_overlay.hide()

    def handle_page_changed(self, _index: int):
        if not hasattr(self, "video_overlay"):
            return
        if self.stack.currentWidget() is self.inspection:
            self.attach_camera_stack("inspection")
            self.update_camera_overlay_geometry()
        elif (
            self.stack.currentWidget() is self.process
            and self.process_right_tabs is not None
            and self.process_right_tabs.currentIndex() == self.process_camera_tab_index
        ):
            self.attach_camera_stack("process")
            self.update_camera_overlay_geometry()
        else:
            self.video_overlay.hide()

    def confirm_qt_camera_started(self):
        if not self.camera_preview_on or self.camera is None:
            return
        active = False
        try:
            active = bool(self.camera.isActive())
        except Exception:
            active = False
        if active:
            self.set_status("Camera Preview")
            self.safe_log("[CAMERA] Qt preview ON")
        else:
            self.set_status("Camera Starting")
            self.safe_log("[CAMERA] Qt camera is still starting. If the preview stays blank, press Stop Camera and try another camera index.")

    def handle_qt_camera_active_changed(self, active: bool):
        if active:
            self.camera_preview_on = True
            self.set_status("Camera Preview")
            self.safe_log("[CAMERA] Qt camera active")
        else:
            if self.camera_preview_on:
                self.safe_log("[CAMERA] Qt camera inactive")

    def handle_qt_camera_error(self, error, error_string: str = ""):
        message = error_string or str(error)
        if not message or message == "Error.NoError":
            return
        self.set_status("Camera Failed")
        self.safe_log(f"[CAMERA] Qt camera error: {message}")

    def stop_camera_preview(self, silent: bool = False):
        self.camera_stop_event.set()
        self.camera_thread = None
        self.cv_capture = None
        try:
            if self.camera:
                self.camera.stop()
                self.camera.deleteLater()
        except Exception as exc:
            self.safe_log(f"[CAMERA] stop warning: {exc}")
        self.camera = None
        self.capture_session = None
        self.image_capture = None
        self.camera_preview_on = False
        self.camera_preview.set_camera_on(False)
        self.video_overlay.hide()
        self.camera_stack.setCurrentWidget(self.camera_preview)
        self.set_status("Camera Standby")
        if not silent:
            self.safe_log("[CAMERA] preview OFF")

    def camera_worker(self, index: int, width: int, height: int, fps: int, stop_event: threading.Event):
        cap = None
        backend_attempts = [
            ("MSMF", cv2.CAP_MSMF),
            ("DSHOW", cv2.CAP_DSHOW),
            ("DEFAULT", 0),
        ]
        opened_backend = ""
        candidate_indices = [index] + [i for i in range(6) if i != index]
        property_modes = [
            ("requested", width, height, fps),
            ("default", 0, 0, 0),
        ]
        try:
            for candidate_index in candidate_indices:
                for backend_name, backend in backend_attempts:
                    for mode_name, mode_width, mode_height, mode_fps in property_modes:
                        if stop_event.is_set():
                            return
                        self.safe_log(f"[CAMERA] trying {backend_name} index {candidate_index} ({mode_name})")
                        candidate = cv2.VideoCapture(candidate_index, backend) if backend else cv2.VideoCapture(candidate_index)
                        try:
                            if mode_width > 0:
                                candidate.set(cv2.CAP_PROP_FRAME_WIDTH, mode_width)
                            if mode_height > 0:
                                candidate.set(cv2.CAP_PROP_FRAME_HEIGHT, mode_height)
                            if mode_fps > 0:
                                candidate.set(cv2.CAP_PROP_FPS, mode_fps)
                            if candidate.isOpened():
                                for _ in range(36):
                                    if stop_event.is_set():
                                        candidate.release()
                                        return
                                    ok, frame = candidate.read()
                                    if ok and frame is not None:
                                        cap = candidate
                                        opened_backend = backend_name
                                        self.cv_capture = cap
                                        self.latest_camera_frame = frame.copy()
                                        self.signals.camera_frame.emit(frame)
                                        self.signals.camera_selected.emit(candidate_index)
                                        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                                        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                                        actual_fps = cap.get(cv2.CAP_PROP_FPS)
                                        self.signals.status.emit("Camera Preview")
                                        self.safe_log(f"[CAMERA] OpenCV preview ON index {candidate_index} via {opened_backend}: {actual_w}x{actual_h} @ {actual_fps:.1f}fps")
                                        break
                                    time.sleep(0.05)
                        finally:
                            if cap is None:
                                candidate.release()
                        if cap is not None:
                            break
                    if cap is not None:
                        break
                if cap is not None:
                    break
            if cap is None:
                self.camera_preview_on = False
                self.signals.camera_on.emit(False)
                self.signals.status.emit("Camera Failed")
                self.safe_log(f"[CAMERA] no readable camera found. Tried indices: {', '.join(str(i) for i in candidate_indices)}")
                self.safe_log("[CAMERA] check: camera privacy permission, USB connection, and whether another app is using it.")
                return
            self.camera_loop(candidate_index, cap, stop_event)
        finally:
            if cap is not None:
                cap.release()
            self.cv_capture = None
            self.camera_preview_on = False
            if not stop_event.is_set():
                self.signals.camera_on.emit(False)
                self.signals.status.emit("Camera Standby")

    def camera_loop(self, index: int, cap, stop_event: threading.Event):
        failures = 0
        while not stop_event.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 20:
                    self.safe_log(f"[CAMERA] frame read failed on index {index}")
                    break
                time.sleep(0.05)
                continue
            failures = 0
            self.latest_camera_frame = frame.copy()
            self.signals.camera_frame.emit(frame)
            time.sleep(0.001)

    def capture_current_frame(self):
        folder = APP_DIR / self.recipe.inspection.capture_folder
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        image_path = None
        if cv2 is not None and self.latest_camera_frame is not None:
            image_path = folder / f"manual_capture_{stamp}.jpg"
            ok = cv2.imwrite(str(image_path), self.latest_camera_frame)
            if ok:
                self.safe_log(f"[CAPTURE] image saved {image_path}")
            else:
                self.safe_log(f"[CAPTURE] image save failed {image_path}")
        else:
            self.safe_log("[CAPTURE] camera frame not ready; saving metadata only")
        path = folder / f"manual_capture_{stamp}.json"
        data = {
            "timestamp": stamp,
            "stage_x": self.sim_pos["X"],
            "stage_y": self.sim_pos["Y"],
            "stage_z": self.sim_pos["Z"],
            "camera_active": self.camera_preview_on,
            "image_path": str(image_path) if image_path else "",
            "note": "Metadata saved with manual capture. Image file is saved when camera capture is ready.",
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.safe_log(f"[CAPTURE] metadata saved {path}")

    def camera_image_saved(self, _id: int, file_name: str):
        self.safe_log(f"[CAPTURE] image saved {file_name}")

    def preview_inspection_plan(self, log_only: bool = False):
        try:
            self.recipe = self.recipe_from_ui()
            planner = MotionPlanner(self.recipe)
            lines = planner.preview_inspection_plan()
            self.safe_log("[INSPECTION PREVIEW]")
            for line in lines:
                self.safe_log(line)
            if planner.errors and not log_only:
                QMessageBox.critical(self, "Inspection plan errors", "\n".join(planner.errors[:8]))
            elif not log_only:
                QMessageBox.information(self, "Inspection Plan", f"{len(lines)} inspection positions previewed. Check Log for details.")
        except Exception as exc:
            if not log_only:
                QMessageBox.critical(self, "Inspection preview failed", str(exc))
            else:
                self.safe_log(f"[INSPECTION PREVIEW ERROR] {exc}")

    def inspection_move_commands(self, die: int, include_z: bool = False) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        self.recipe = self.recipe_from_ui()
        planner = MotionPlanner(self.recipe)
        planner.reset(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
        planner.prepare()
        row, col, x, y = planner.calculate_camera_die_stage_position(die)
        planner.move_axis("X", x, f"camera die {die} row {row + 1} col {col + 1} X")
        planner.move_axis("Y", y, f"camera die {die} row {row + 1} col {col + 1} Y")
        if include_z:
            for z in planner.get_inspection_z_values():
                planner.move_axis("Z", z, f"camera die {die} inspect Z {z:g}")
                if self.recipe.inspection.z_settle_time_s > 0:
                    planner.commands.append(PlannedCommand(f"G4 P{self.recipe.inspection.z_settle_time_s:.3f}", f"settle die {die} Z {z:g}", is_exposure=True))
                if self.recipe.inspection.capture_each_z:
                    planner.commands.append(PlannedCommand("", f"CAPTURE die {die} Z {z:g}", is_exposure=False))
            if self.recipe.inspection.return_to_safe_z_after_die:
                planner.move_axis("Z", self.recipe.inspection.safe_z, f"camera die {die} safe Z")
        return planner.commands, planner.warnings, planner.errors

    def move_to_active_inspection_die(self):
        die = self.active_inspection_die or (self.current_inspection_dies()[0] if self.current_inspection_dies() else 1)
        self.camera_preview.set_active_die(die)
        self.run_inspection_commands(f"inspection-move-die-{die}", *self.inspection_move_commands(die, include_z=False))

    def run_selected_inspection(self):
        try:
            self.recipe = self.recipe_from_ui()
            planner = MotionPlanner(self.recipe)
            commands: List[PlannedCommand] = []
            warnings: List[str] = []
            errors: List[str] = []
            for die, _row, _col, _x, _y, selected in planner.make_inspection_plan():
                if not selected:
                    self.safe_log(f"[INSPECTION] SKIP die {die}")
                    continue
                die_cmds, die_warnings, die_errors = self.inspection_move_commands(die, include_z=True)
                commands.extend(die_cmds if not commands else [cmd for cmd in die_cmds if cmd.gcode not in ("G21", "G90", "G94")])
                warnings.extend(die_warnings)
                errors.extend(die_errors)
            self.run_inspection_commands("inspection-selected", commands, warnings, errors)
        except Exception as exc:
            QMessageBox.critical(self, "Inspection failed", str(exc))

    def run_inspection_commands(self, name: str, commands: List[PlannedCommand], warnings: List[str], errors: List[str]):
        if self.running:
            QMessageBox.warning(self, "Busy", "A job is already running.")
            return
        for warning in warnings:
            self.safe_log(f"[WARN] {warning}")
        if errors:
            QMessageBox.critical(self, "Inspection blocked", "\n".join(errors[:8]))
            return
        if not commands:
            QMessageBox.information(self, "Inspection", "No inspection commands to run.")
            return
        if not self.real_run_allowed():
            return
        self.running = True
        self.running_sequence = "inspection"
        self.abort_requested = False
        self.hold_requested = False
        self.set_status("Inspection")
        thread = threading.Thread(target=self.execute_commands, args=(name, commands, None), daemon=True)
        thread.start()

    def real_run_allowed(self) -> bool:
        if self.dry_run.isChecked():
            return True
        if not self.coord_check.isChecked():
            QMessageBox.critical(self, "Preflight needed", "Coordinate zero must be confirmed.")
            return False
        if not self.estop_check.isChecked():
            QMessageBox.critical(self, "Preflight needed", "Limits and E-stop must be confirmed.")
            return False
        if self.recipe.io.uv_enabled and not self.uv_check.isChecked():
            QMessageBox.critical(self, "Preflight needed", "UV shield and interlock must be confirmed.")
            return False
        return True

    def run_process_step(self, index: int, name: str):
        if not self.ordered_flow_unlocked() and index != self.current_step:
            QMessageBox.warning(self, "Locked", "Turn on Settings > Unlock ordered flow to run steps out of order.")
            return
        self.run_plan(name, step_index=index, sequence="primary")

    def run_second_process_step(self, index: int, action: str):
        if not self.ordered_flow_unlocked() and index != self.second_current_step:
            QMessageBox.warning(self, "Locked", "Turn on Settings > Unlock ordered flow to run 2nd-process steps out of order.")
            return
        if action in {"load", "camera_align", "go_corrected_die1", "grid", "return"}:
            if action in {"go_corrected_die1", "grid"}:
                if not self.second_alignment_ready(show_message=True):
                    return
            plan_name = "corrected_die1_direct" if action == "go_corrected_die1" else action
            self.run_plan(plan_name, step_index=index, sequence="second")
            return
        if action == "save_center":
            self.open_process_camera_tab()
            self.set_status("Capture Camera Marks")
            self.safe_log("[2ND FLOW] Capture Camera Die 1 marks: jog TL/TR/BL/BR to the crosshair and save each mark.")
            return
        if action == "preview":
            try:
                if not self.calculate_die1_center_from_marks(show_messages=False):
                    marks = (self.recipe.inspection.measured_die1_marks or {}) if self.recipe else {}
                    missing = [mark for mark in ("TL", "TR", "BL", "BR") if mark not in marks]
                    QMessageBox.warning(self, "4 marks needed", "Save all Camera Die 1 marks first: " + ", ".join(missing))
                    return
            except Exception as exc:
                QMessageBox.critical(self, "4-mark correction failed", str(exc))
                return
            self.preview_die1_center_aligned_positions()
            self.second_current_step = max(self.second_current_step, index + 1)
            self.refresh_process_buttons()
            self.safe_log("[2ND FLOW] 4-mark correction applied and previewed; exposure step unlocked")
            return
        QMessageBox.warning(self, "Unknown step", action)

    def run_aux_motion(self, name: str, sequence: str, label: str, require_second_alignment: bool = False):
        if require_second_alignment and not self.second_alignment_ready(show_message=True):
            return
        self.safe_log(f"[AUX MOVE] {label}: sequence state will not be marked DONE.")
        self.run_plan(name, step_index=None, sequence=sequence)

    def run_plan(self, name: str, step_index: Optional[int] = None, sequence: str = "primary"):
        if self.running:
            QMessageBox.warning(self, "Busy", "A job is already running.")
            return
        try:
            commands, warnings, errors = self.get_plan(name, sequence=sequence)
            self.safe_log(
                f"[PLAN FEED] XY F{self.recipe.motion.feed_xy:g} / Z F{self.recipe.motion.feed_z:g} / absolute exposure Z{self.recipe.exposure.exposure_z:g} / {self.recipe.exposure.exposure_time_s:g}s"
            )
            for warning in warnings:
                self.safe_log(f"[WARN] {warning}")
            if errors:
                QMessageBox.critical(self, "Plan blocked", "\n".join(errors[:8]))
                return
            if not self.real_run_allowed():
                return
        except Exception as exc:
            QMessageBox.critical(self, "Plan failed", str(exc))
            return
        self.signals.exposure_event.emit({
            "type": "plan",
            "durations": self.exposure_durations_from_commands(commands),
        })
        self.running = True
        self.running_sequence = sequence
        self.running_step_index = step_index
        self.abort_requested = False
        self.hold_requested = False
        self.set_status(f"Running: {name}")
        self.refresh_process_buttons()
        thread = threading.Thread(target=self.execute_commands, args=(name, commands, step_index, sequence), daemon=True)
        thread.start()

    def handle_exposure_event(self, event: object):
        if not isinstance(event, dict):
            return
        kind = str(event.get("type", ""))
        if kind == "plan":
            raw = event.get("durations", {})
            durations = raw if isinstance(raw, dict) else {}
            if not durations:
                if self.exposure_dialog is not None:
                    self.exposure_dialog.hide()
                return
            self.ensure_exposure_dialog()
            self.exposure_dialog.panel.handle_event(event)
            self.position_exposure_dialog()
            self.exposure_dialog.show()
            self.exposure_dialog.raise_()
            self.exposure_dialog.activateWindow()
            return
        if self.exposure_dialog is not None:
            self.exposure_dialog.panel.handle_event(event)
            if kind == "finish":
                ok = bool(event.get("ok", False))
                delay_ms = 900 if ok else 3000
                QTimer.singleShot(delay_ms, self.hide_exposure_dialog_if_finished)

    def ensure_exposure_dialog(self):
        if self.exposure_dialog is None:
            self.exposure_dialog = ExposureProgressDialog(self.stop_exposure_from_popup, self)

    def position_exposure_dialog(self):
        if self.exposure_dialog is None:
            return
        self.exposure_dialog.adjustSize()
        parent_rect = self.geometry()
        dialog_rect = self.exposure_dialog.frameGeometry()
        x = parent_rect.x() + (parent_rect.width() - dialog_rect.width()) // 2
        y = parent_rect.y() + max(80, (parent_rect.height() - dialog_rect.height()) // 2)
        self.exposure_dialog.move(x, y)

    def stop_exposure_from_popup(self):
        self.safe_log("[EXPOSURE POPUP] Stop Exposure pressed")
        self.soft_reset()

    def hide_exposure_dialog_if_finished(self):
        if self.running:
            return
        if self.exposure_dialog is not None:
            self.exposure_dialog.hide()

    def exposure_durations_from_commands(self, commands: List[PlannedCommand]) -> Dict[int, float]:
        durations: Dict[int, float] = {}
        for cmd in commands:
            if not self.is_die_exposure_command(cmd):
                continue
            die = self.die_from_label(cmd.label)
            if die is None:
                continue
            dwell = self.gcode_dwell_seconds(cmd.gcode)
            durations[int(die)] = dwell if dwell > 0 else self.recipe.exposure.exposure_time_for_die(int(die))
        return durations

    def execute_commands(self, name: str, commands: List[PlannedCommand], step_index: Optional[int], sequence: str = "primary"):
        ok = True
        try:
            if not self.dry_run.isChecked() and not self.transport.connected:
                raise RuntimeError("Not connected")
            sim_pos = dict(self.sim_pos)
            for idx, cmd in enumerate(commands, start=1):
                if self.abort_requested:
                    raise RuntimeError("Run aborted")
                self.wait_while_held()
                if cmd.label:
                    self.safe_log(f"[{idx}/{len(commands)}] {cmd.label}")
                if not cmd.gcode:
                    if self.is_die_exposure_command(cmd):
                        die = self.die_from_label(cmd.label)
                        seconds = self.recipe.exposure.exposure_time_for_die(die) if die is not None else self.recipe.exposure.exposure_time_s
                        if seconds > 0:
                            self.exposure_wait(seconds, die)
                    continue
                if self.dry_run.isChecked():
                    self.safe_log(f"[DRY] {cmd.gcode}")
                    if cmd.is_motion:
                        self.simulate_motion(cmd, sim_pos)
                    elif cmd.is_exposure and self.gcode_dwell_seconds(cmd.gcode) > 0:
                        dwell = self.gcode_dwell_seconds(cmd.gcode)
                        if self.is_die_exposure_command(cmd):
                            self.exposure_wait(dwell, self.die_from_label(cmd.label))
                        else:
                            self.dwell_wait(dwell)
                    else:
                        time.sleep(0.025)
                else:
                    timeout_s = self.command_timeout_for(cmd)
                    die = self.die_from_label(cmd.label)
                    dwell = self.gcode_dwell_seconds(cmd.gcode)
                    live_exposure = self.is_die_exposure_command(cmd) and dwell > 0
                    if live_exposure:
                        self.safe_log(f"[LIVE EXPOSURE TIMER] app-timed dwell die {die}: {dwell:g}s")
                        self.exposure_wait(dwell, die)
                    else:
                        self.transport.send_line(cmd.gcode, timeout_s=timeout_s)
                    if cmd.is_motion and self.recipe.motion.wait_idle:
                        self.wait_idle_respecting_hold(timeout_s=self.recipe.motion.idle_timeout_s)
                if cmd.is_motion and not self.dry_run.isChecked():
                    self.signals.planned_position.emit(cmd)
        except Exception as exc:
            ok = False
            self.safe_log(f"[ERROR] {exc}")
            self.signals.exposure_event.emit({"type": "finish", "ok": False})
            self.force_uv_off_after_error()
        self.signals.finished.emit(name if step_index is None else f"{sequence}:{step_index}:{name}", ok)

    def wait_idle_respecting_hold(self, timeout_s: float):
        active_elapsed = 0.0
        last = time.monotonic()
        while True:
            status = self.transport.request_status(timeout_s=2.0)
            self.apply_status_report(status)
            state_match = re.match(r"<([^|>]+)", status)
            state = state_match.group(1) if state_match else ""
            now = time.monotonic()
            delta = now - last
            last = now

            if state == "Idle":
                return
            if self.hold_requested or state in ("Hold", "Door"):
                time.sleep(0.15)
                continue

            active_elapsed += delta
            if active_elapsed > timeout_s:
                raise TimeoutError("Controller did not report Idle before timeout")
            time.sleep(0.15)

    def wait_while_held(self):
        logged = False
        while self.hold_requested:
            if not logged:
                self.safe_log("[HOLD] paused. Press Resume ~ to continue.")
                logged = True
            time.sleep(0.05)

    def gcode_dwell_seconds(self, gcode: str) -> float:
        match = re.search(r"\bP([-+0-9.]+)", gcode or "", re.IGNORECASE)
        if match:
            return max(0.0, float(match.group(1)))
        return 0.0

    def command_timeout_for(self, cmd: PlannedCommand) -> float:
        base = float(self.recipe.motion.command_timeout_s)
        dwell = self.gcode_dwell_seconds(cmd.gcode) if cmd.is_exposure else 0.0
        if dwell > 0:
            timeout = max(base, dwell + 10.0)
            self.safe_log(f"[TIMEOUT] dwell command timeout {timeout:g}s for {dwell:g}s exposure")
            return timeout
        return base

    def die_from_label(self, label: str = "") -> Optional[int]:
        match = re.search(r"\bdie\s+(\d+)\b", label or "", re.IGNORECASE)
        return int(match.group(1)) if match else None

    def is_die_exposure_command(self, cmd: PlannedCommand) -> bool:
        label = cmd.label or ""
        if not cmd.is_exposure:
            return False
        return bool(re.search(r"\b(dwell|simulated exposure)\s+die\s+\d+\b", label, re.IGNORECASE))

    def force_uv_off_after_error(self):
        if self.dry_run.isChecked() or not self.recipe.io.uv_enabled:
            return
        cmd = self.recipe.io.uv_off_gcode.strip()
        if not cmd or not self.transport.connected:
            return
        try:
            self.safe_log("[SAFETY] sending UV OFF after error/abort")
            self.transport.send_line(cmd, timeout_s=self.recipe.motion.command_timeout_s)
        except Exception as exc:
            self.safe_log(f"[SAFETY] UV OFF failed: {exc}")
        finally:
            self.manual_uv_on = False
            self.update_manual_uv_button()

    def exposure_wait(self, seconds: float, die: Optional[int] = None):
        seconds = max(0.0, float(seconds))
        if seconds <= 0:
            return
        label = f"die {die} " if die is not None else ""
        self.signals.status.emit(f"Exposure {label}{seconds:g}s")
        self.signals.exposure_event.emit({"type": "start", "die": die, "seconds": seconds})
        self.safe_log(f"[EXPOSURE WAIT] {label}{seconds:g}s")
        remaining = seconds
        self.exposure_active = True
        try:
            while remaining > 0:
                if self.abort_requested:
                    raise RuntimeError("Run aborted")
                if self.hold_requested:
                    self.abort_requested = True
                    raise RuntimeError("Exposure interrupted by Hold")
                chunk = min(0.1, remaining)
                time.sleep(chunk)
                remaining -= chunk
                elapsed = seconds - max(0.0, remaining)
                pct = 100.0 if seconds <= 0 else min(100.0, elapsed / seconds * 100.0)
                self.signals.status.emit(f"Exposure {label}{max(0.0, remaining):.1f}/{seconds:.1f}s  {pct:.0f}%")
                self.signals.exposure_event.emit({"type": "progress", "die": die, "elapsed": elapsed, "seconds": seconds})
            self.signals.exposure_event.emit({"type": "done", "die": die})
        finally:
            self.exposure_active = False

    def dwell_wait(self, seconds: float):
        seconds = max(0.0, float(seconds))
        remaining = seconds
        while remaining > 0:
            if self.abort_requested:
                raise RuntimeError("Run aborted")
            self.wait_while_held()
            chunk = min(0.1, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def simulate_motion(self, cmd: PlannedCommand, sim_pos: Dict[str, float]):
        target = dict(sim_pos)
        for axis, value in (("X", cmd.x), ("Y", cmd.y), ("Z", cmd.z)):
            if value is not None:
                target[axis] = float(value)

        distance = math.sqrt(
            (target["X"] - sim_pos["X"]) ** 2
            + (target["Y"] - sim_pos["Y"]) ** 2
            + (target["Z"] - sim_pos["Z"]) ** 2
        )
        feed_match = re.search(r"\bF([-+0-9.]+)", cmd.gcode or "")
        feed = float(feed_match.group(1)) if feed_match else self.recipe.motion.feed_xy
        duration = 0.0 if feed <= 0 or distance <= 1e-9 else distance / feed * 60.0
        frames = max(1, int(duration / 0.033))
        if distance > 1e-9:
            self.safe_log(f"[SIM] distance {distance:.3f} mm / F{feed:g} = {duration:.2f}s")

        start = dict(sim_pos)
        for frame in range(1, frames + 1):
            if self.abort_requested:
                raise RuntimeError("Run aborted")
            self.wait_while_held()
            ratio = frame / frames
            x = start["X"] + (target["X"] - start["X"]) * ratio
            y = start["Y"] + (target["Y"] - start["Y"]) * ratio
            z = start["Z"] + (target["Z"] - start["Z"]) * ratio
            self.signals.planned_position.emit(PlannedCommand("", is_motion=True, x=x, y=y, z=z))
            if duration > 0:
                time.sleep(duration / frames)

        sim_pos.update(target)

    def simulate_jog_motion(self, cmd: PlannedCommand, sim_pos: Dict[str, float]):
        try:
            self.simulate_motion(cmd, sim_pos)
        except RuntimeError as exc:
            self.safe_log(f"[JOG] {exc}")
        finally:
            self.jog_running = False

    def execute_live_jog_motion(self, line: str, target: Dict[str, float]):
        try:
            self.transport.send_line(line, timeout_s=self.recipe.motion.command_timeout_s)
            self.signals.planned_position.emit(
                PlannedCommand("", is_motion=True, x=target["X"], y=target["Y"], z=target["Z"])
            )
        except Exception as exc:
            self.signals.status.emit("Jog Failed")
            self.safe_log(f"[JOG ERROR] {exc}")
        finally:
            self.jog_running = False

    def run_finished(self, name: str, ok: bool):
        self.running = False
        self.running_sequence = None
        self.running_step_index = None
        self.set_status("Idle" if ok else "Fault")
        self.signals.exposure_event.emit({"type": "finish", "ok": ok})
        self.save_position_state("run_finished", force=True)
        if ok and ":" in name:
            parts = name.split(":")
            if len(parts) == 3:
                sequence, idx_text, _plan = parts
                idx = int(idx_text)
                if sequence == "second":
                    if _plan == "return":
                        self.clear_second_alignment_data("2nd process return complete")
                        self.second_current_step = 0
                        self.safe_log("[2ND FLOW] return complete; sequence reset for the next wafer")
                    else:
                        self.second_current_step = max(self.second_current_step, idx + 1)
                else:
                    if _plan == "return":
                        self.current_step = 0
                        self.safe_log("[FLOW] return complete; sequence reset for the next wafer")
                    else:
                        self.current_step = max(self.current_step, idx + 1)
            else:
                _plan, idx_text = name.split(":", 1)
                idx = int(idx_text)
                self.current_step = max(self.current_step, idx + 1)
            self.refresh_process_buttons()
        self.safe_log(f"=== {'DONE' if ok else 'FAILED'}: {name} ===")

    def repolish(self, widget: QWidget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def ordered_flow_unlocked(self) -> bool:
        field = self.fields.get("ui.unlock_ordered_flow")
        if isinstance(field, QCheckBox):
            return bool(field.isChecked())
        return bool(getattr(getattr(self.recipe, "ui", None), "unlock_ordered_flow", False))

    def refresh_process_buttons(self):
        free_run = self.ordered_flow_unlocked()
        for idx, btn in enumerate(self.step_buttons):
            done = idx < self.current_step
            active = self.running and self.running_sequence == "primary" and idx == self.running_step_index
            unlocked = free_run or idx == self.current_step
            btn.setEnabled(unlocked and not self.running)
            btn.setText("Run" if free_run else "Done" if done else "Run")
            btn.setObjectName("pill_blue" if unlocked and not self.running and not done else "pill_soft")
            self.repolish(btn)
            if idx < len(self.step_state_labels):
                label = self.step_state_labels[idx]
                label.setText("RUNNING" if active else "DONE" if done else "READY" if unlocked else "LOCKED")
                label.setObjectName("badge_running" if active else "badge_done" if done else "badge" if unlocked else "badge_locked")
                self.repolish(label)
        if self.die1_direct_button is not None:
            self.die1_direct_button.setEnabled(not self.running)
            self.die1_direct_button.setObjectName("pill_soft" if self.running else "pill_blue")
            self.repolish(self.die1_direct_button)
        if self.camera_die1_direct_button is not None:
            self.camera_die1_direct_button.setEnabled(not self.running)
            self.camera_die1_direct_button.setObjectName("pill_soft" if self.running else "pill_blue")
            self.repolish(self.camera_die1_direct_button)
        for idx, btn in enumerate(self.second_step_buttons):
            done = idx < self.second_current_step
            active = self.running and self.running_sequence == "second" and idx == self.running_step_index
            unlocked = free_run or idx == self.second_current_step
            btn.setEnabled(unlocked and not self.running)
            btn.setText("Run" if free_run else "Done" if done else "Run")
            btn.setObjectName("pill_blue" if unlocked and not self.running and not done else "pill_soft")
            self.repolish(btn)
            if idx < len(self.second_step_state_labels):
                label = self.second_step_state_labels[idx]
                label.setText("RUNNING" if active else "DONE" if done else "READY" if unlocked else "LOCKED")
                label.setObjectName("badge_running" if active else "badge_done" if done else "badge" if unlocked else "badge_locked")
                self.repolish(label)

    def reset_sequence(self):
        self.current_step = 0
        self.refresh_process_buttons()
        self.safe_log("[FLOW] sequence reset")

    def reset_second_sequence(self):
        self.second_current_step = 0
        self.refresh_process_buttons()
        self.safe_log("[2ND FLOW] sequence reset")

    def set_status(self, text: str):
        self.status_label.setText(text)
        if text.startswith("Camera") or text in ("Inspection",):
            self.camera_status_label.setText(text)
            self.process_camera_status_label.setText(text)

    def axis_limits(self, axis: str) -> Tuple[float, float]:
        axis = axis.upper()
        if axis == "X":
            return self.recipe.limits.x_min, self.recipe.limits.x_max
        if axis == "Y":
            return self.recipe.limits.y_min, self.recipe.limits.y_max
        if axis == "Z":
            return self.recipe.limits.z_min, self.recipe.limits.z_max
        raise ValueError(f"Invalid axis {axis}")

    def within_limits(self, axis: str, value: float) -> bool:
        low, high = self.axis_limits(axis)
        return low <= value <= high

    def apply_planned_position(self, cmd: PlannedCommand):
        for axis, value in (("X", cmd.x), ("Y", cmd.y), ("Z", cmd.z)):
            if value is not None and axis in self.pos_labels:
                self.sim_pos[axis] = float(value)
                self.pos_labels[axis].setText(f"{value:.3f}")
        self.route_widget.set_live_position(self.sim_pos["X"], self.sim_pos["Y"])
        self.save_position_state("planned")
        self.camera_preview.set_stage_position(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])
        self.video_overlay.set_stage_position(self.sim_pos["X"], self.sim_pos["Y"], self.sim_pos["Z"])

    def append_log(self, text: str):
        if self.log_box:
            self.log_box.append(text)
        if self.camera_log_box and any(tag in text for tag in ("[CAMERA]", "[CAPTURE]", "[CAMERA CAPTURE]")):
            self.camera_log_box.append(text)
        if self.process_camera_log_box and any(tag in text for tag in ("[CAMERA]", "[CAPTURE]", "[CAMERA CAPTURE]")):
            self.process_camera_log_box.append(text)
        match = re.search(r"(<[^>]+>)", text)
        if match:
            self.apply_status_report(match.group(1))

    def safe_log(self, text: str):
        self.signals.log.emit(str(text))

    def apply_status_report(self, status: str, allow_machine_fallback: bool = True):
        state = re.match(r"<([^|>]+)", status)
        if state:
            self.status_label.setText(state.group(1))
        wpos = re.search(r"WPos:([-+0-9.]+),([-+0-9.]+),([-+0-9.]+)", status)
        mpos = re.search(r"MPos:([-+0-9.]+),([-+0-9.]+),([-+0-9.]+)", status)
        wco = re.search(r"WCO:([-+0-9.]+),([-+0-9.]+),([-+0-9.]+)", status)
        if wpos:
            x, y, z = float(wpos.group(1)), float(wpos.group(2)), float(wpos.group(3))
        elif mpos and wco:
            x = float(mpos.group(1)) - float(wco.group(1))
            y = float(mpos.group(2)) - float(wco.group(2))
            z = float(mpos.group(3)) - float(wco.group(3))
        elif mpos and allow_machine_fallback:
            x, y, z = float(mpos.group(1)), float(mpos.group(2)), float(mpos.group(3))
        else:
            return
        self.sim_pos.update({"X": x, "Y": y, "Z": z})
        self.pos_labels["X"].setText(f"{x:.3f}")
        self.pos_labels["Y"].setText(f"{y:.3f}")
        self.pos_labels["Z"].setText(f"{z:.3f}")
        self.route_widget.set_live_position(x, y)
        self.camera_preview.set_stage_position(x, y, z)
        self.save_position_state("status", force=True)

    def available_ports(self) -> List[Tuple[str, str]]:
        if list_ports is None:
            detail = f": {LIST_PORTS_IMPORT_ERROR}" if LIST_PORTS_IMPORT_ERROR else ""
            self.safe_log(f"[PORTS] serial port scan unavailable{detail}")
            return []
        ports = []
        for port in list_ports.comports():
            desc = getattr(port, "description", "") or ""
            hwid = getattr(port, "hwid", "") or ""
            ports.append((port.device, f"{desc} {hwid}".strip()))
        return ports

    def scan_ports(self):
        ports = self.available_ports()
        if not ports:
            self.safe_log("[PORTS] no serial ports found")
            self.connection_label.setText("No serial ports")
            return
        self.safe_log("[PORTS] " + ", ".join(f"{dev} ({desc})" if desc else dev for dev, desc in ports))
        self.connection_label.setText(f"{len(ports)} port(s) found")

    def auto_connect(self):
        self.start_auto_connect(show_messages=True)

    def auto_connect_on_startup(self):
        self.start_auto_connect(show_messages=False)

    def start_auto_connect(self, show_messages: bool):
        if self.running:
            if show_messages:
                QMessageBox.warning(self, "Busy", "Stop the current job before connecting.")
            return
        if self.transport.connected:
            return
        try:
            recipe = self.recipe_from_ui()
            baud = int(recipe.serial.baud)
        except Exception as exc:
            if show_messages:
                QMessageBox.critical(self, "Auto connect failed", str(exc))
            else:
                self.safe_log(f"[AUTO CONNECT] startup skipped: {exc}")
            return
        ports = self.available_ports()
        if not ports:
            self.safe_log("[AUTO CONNECT] no serial ports found")
            self.connection_label.setText("No serial ports")
            if show_messages:
                QMessageBox.warning(self, "No ports", "No serial ports found.")
            return
        self.connection_label.setText("Auto scanning...")
        self.set_status("Auto Connect")
        self.safe_log("[AUTO CONNECT] scanning on startup" if not show_messages else "[AUTO CONNECT] scanning")
        threading.Thread(target=self.auto_connect_worker, args=(ports, baud), daemon=True).start()

    def auto_connect_worker(self, ports: List[Tuple[str, str]], baud: int):
        for port, desc in ports:
            try:
                self.safe_log(f"[AUTO CONNECT] trying {port} {desc}")
                self.transport.connect(port, baud)
                status = self.transport.request_status(timeout_s=3.0)
                self.safe_log(f"[AUTO CONNECT] {port} status {status}")
                self.signals.connected_port.emit(port)
                return
            except Exception as exc:
                self.safe_log(f"[AUTO CONNECT] {port} failed: {exc}")
                self.transport.close()
        self.signals.status.emit("Connect Failed")
        self.safe_log("[AUTO CONNECT] no GRBL/FluidNC response found")

    def apply_connected_port(self, port: str):
        field = self.fields.get("serial.port")
        if isinstance(field, QLineEdit):
            field.setText(port)
        self.dry_run.setChecked(False)
        self.connection_label.setText(f"Connected: {port}")
        self.set_status("Connected")
        self.safe_log(f"[CONNECTED PORT] {port}")

    def connect_transport(self):
        self.recipe = self.recipe_from_ui()
        if self.running:
            QMessageBox.warning(self, "Busy", "Stop the current job before connecting.")
            return
        if self.dry_run.isChecked():
            self.dry_run.setChecked(False)
            self.safe_log("[CONNECT] Dry Run disabled for live serial connection")
        self.transport.connect(self.recipe.serial.port, self.recipe.serial.baud)
        try:
            status = self.transport.request_status(timeout_s=3.0)
            self.safe_log(f"[STATUS] {status}")
            self.apply_status_report(status)
        except Exception as exc:
            self.safe_log(f"[WARN] connected but no status response yet: {exc}")
        self.connection_label.setText(f"Connected: {self.recipe.serial.port}")
        self.set_status("Connected")

    def disconnect_transport(self):
        if self.running:
            QMessageBox.warning(self, "Busy", "Stop the current job before disconnecting.")
            return
        self.transport.close()
        self.connection_label.setText("Not connected")
        self.set_status("Disconnected")
        self.safe_log("[DISCONNECTED]")

    def run_raw(self, command: str, label: str, coord_ok: bool = False):
        if self.dry_run.isChecked():
            self.safe_log(f"[DRY] {command}")
            if coord_ok:
                self.coord_check.setChecked(True)
                self.sim_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
                self.update_position_labels()
                self.save_position_state("set_zero", force=True)
                self.set_status("Zero Set")
            return
        self.transport.send_line(command, timeout_s=30)
        if coord_ok:
            self.coord_check.setChecked(True)
            self.sim_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
            self.update_position_labels()
            self.save_position_state("set_zero", force=True)
            self.set_status("Zero Set")
            self.safe_log("[ZERO] G92 applied; display position set to X0 Y0 Z0")
            try:
                status = self.transport.request_status(timeout_s=2.0)
                self.safe_log(f"[STATUS] {status}")
                self.apply_status_report(status, allow_machine_fallback=False)
            except Exception as exc:
                self.safe_log(f"[ZERO] status refresh skipped: {exc}")

    def set_current_zero(self):
        try:
            self.run_raw("G92 X0 Y0 Z0", "set zero", True)
        except Exception as exc:
            QMessageBox.critical(self, "Set zero failed", str(exc))

    def update_manual_uv_button(self):
        if not self.manual_uv_button:
            return
        self.manual_uv_button.setText("Manual UV OFF" if self.manual_uv_on else "Manual UV ON")
        self.manual_uv_button.setObjectName("pill_danger" if self.manual_uv_on else "pill_soft")
        self.repolish(self.manual_uv_button)

    def toggle_manual_uv(self):
        try:
            self.recipe = self.recipe_from_ui()
            turn_on = not self.manual_uv_on
            if turn_on and not self.dry_run.isChecked() and not self.uv_check.isChecked():
                QMessageBox.critical(self, "UV preflight needed", "UV shield and interlock must be confirmed before manual UV ON.")
                return
            command = self.recipe.io.uv_on_gcode.strip() if turn_on else self.recipe.io.uv_off_gcode.strip()
            if not command:
                QMessageBox.warning(self, "UV command missing", "Set UV ON/OFF command in Settings first.")
                return
            if self.dry_run.isChecked():
                self.safe_log(f"[DRY UV] {command}")
            else:
                if not self.transport.connected:
                    raise RuntimeError("Not connected")
                self.transport.send_line(command, timeout_s=self.recipe.motion.command_timeout_s)
            self.manual_uv_on = turn_on
            self.update_manual_uv_button()
            self.set_status("Manual UV ON" if self.manual_uv_on else "Manual UV OFF")
            self.safe_log("[MANUAL UV] ON" if self.manual_uv_on else "[MANUAL UV] OFF")
        except Exception as exc:
            QMessageBox.critical(self, "Manual UV failed", str(exc))

    def feed_hold(self):
        if self.exposure_active:
            self.safe_log("[HOLD] exposure is active; aborting exposure and forcing UV OFF instead of pausing the timer.")
            self.soft_reset()
            return
        self.hold_requested = True
        self.set_status("Hold")
        self.realtime(b"!", "[DRY] realtime ! feed hold")

    def resume(self):
        self.hold_requested = False
        self.set_status("Idle")
        self.realtime(b"~", "[DRY] realtime ~ resume")

    def soft_reset(self):
        self.abort_requested = True
        self.hold_requested = False
        self.set_status("Reset")
        self.realtime(b"\x18", "[DRY] realtime Ctrl-X")

    def jog_cancel(self):
        self.realtime(b"\x85", "[DRY] realtime jog cancel")

    def realtime(self, payload: bytes, dry_text: str):
        try:
            if self.dry_run.isChecked():
                self.safe_log(dry_text)
                if payload == b"!":
                    self.hold_requested = True
                    self.set_status("Hold")
                elif payload == b"~":
                    self.hold_requested = False
                    self.set_status("Idle")
                elif payload == b"\x18":
                    self.abort_requested = True
                    self.hold_requested = False
                    self.set_status("Reset")
                    self.sim_pos = {
                        "X": self.recipe.stage.initial_x,
                        "Y": self.recipe.stage.initial_y,
                        "Z": self.recipe.stage.initial_z,
                    }
                    self.update_position_labels()
                    self.save_position_state("reset", force=True)
                elif payload == b"\x85":
                    self.set_status("Jog Cancel")
                return
            self.transport.realtime(payload)
        except Exception as exc:
            QMessageBox.critical(self, "Realtime command failed", str(exc))

    def query_status(self):
        try:
            if self.dry_run.isChecked():
                status = f"<{self.status_label.text()}|MPos:{self.sim_pos['X']:.3f},{self.sim_pos['Y']:.3f},{self.sim_pos['Z']:.3f}|FS:0,0>"
                self.safe_log(f"[DRY STATUS] {status}")
                self.apply_status_report(status)
                return
            status = self.transport.request_status(timeout_s=2.0)
            self.safe_log(f"[STATUS] {status}")
        except Exception as exc:
            QMessageBox.critical(self, "Status failed", str(exc))

    def jog_axis(self, axis: str, direction: int):
        try:
            if self.jog_running:
                return
            self.recipe = self.recipe_from_ui()
            axis = axis.upper()
            step = self.recipe.jog.z_step_mm if axis == "Z" else self.recipe.jog.xy_step_mm
            feed = self.recipe.jog.feed_z if axis == "Z" else self.recipe.jog.feed_xy
            delta = step * (1 if direction >= 0 else -1)
            line = f"$J=G91 G21 {axis}{format_axis_value(self.recipe, axis, delta, relative=True):.3f} F{feed:.1f}"
            start = dict(self.sim_pos)
            target = dict(start)
            target[axis] += delta
            if not self.within_limits(axis, target[axis]):
                low, high = self.axis_limits(axis)
                self.safe_log(f"[BLOCKED] Jog {axis} target {target[axis]:.3f} is outside soft limit {low:g}..{high:g}")
                self.set_status("Limit Block")
                return

            if self.dry_run.isChecked():
                self.safe_log(f"[DRY] {line}")
                self.set_status(f"Jog {axis}{'+' if direction >= 0 else '-'}")
                cmd = PlannedCommand(line, is_motion=True, x=target["X"], y=target["Y"], z=target["Z"])
                self.jog_running = True
                threading.Thread(target=self.simulate_jog_motion, args=(cmd, start), daemon=True).start()
                return

            if not self.transport.connected:
                raise RuntimeError("Not connected")
            self.safe_log(f"[JOG] {line}")
            self.set_status(f"Jog {axis}{'+' if direction >= 0 else '-'}")
            self.jog_running = True
            threading.Thread(target=self.execute_live_jog_motion, args=(line, target), daemon=True).start()
        except Exception as exc:
            self.jog_running = False
            QMessageBox.critical(self, "Jog failed", str(exc))

    def check_plan(self):
        try:
            commands, warnings, errors = self.get_plan("full")
            self.safe_log(f"[PLAN] {len(commands)} commands, {len(warnings)} warnings, {len(errors)} errors")
            self.safe_log("[DIE PREVIEW]")
            for line in MotionPlanner(self.recipe).preview_exposure_plan():
                self.safe_log(line)
            if errors:
                QMessageBox.critical(self, "Plan errors", "\n".join(errors[:8]))
            elif warnings:
                QMessageBox.warning(self, "Plan warnings", "\n".join(warnings[:8]))
            else:
                QMessageBox.information(self, "Plan OK", f"{len(commands)} commands generated.")
        except Exception as exc:
            QMessageBox.critical(self, "Check failed", str(exc))

    def export_gcode(self):
        try:
            commands, warnings, errors = self.get_plan("full")
            if errors:
                QMessageBox.critical(self, "Plan errors", "\n".join(errors[:8]))
                return
            path, _ = QFileDialog.getSaveFileName(self, "Export G-code", str(APP_DIR / "aurelith_full_cycle.nc"), "G-code (*.nc *.gcode *.txt)")
            if not path:
                return
            lines = ["; Aurelith Arita generated program", "; Review before live motion."]
            lines.extend(f"; WARNING: {w}" for w in warnings)
            lines.extend(cmd.export_line() if cmd.gcode else f"; {cmd.label}" for cmd in commands)
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.safe_log(f"[EXPORT] {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


def apply_style(app: QApplication):
    app.setStyleSheet(
        """
        QWidget {
            background: #EEF1F6;
            color: #1D1D1F;
            font-family: "Noto Sans KR", "Segoe UI";
            font-size: 13px;
        }
        QFrame#card, QFrame#softCard {
            border: 1px solid rgba(211, 218, 230, 0.86);
            border-radius: 30px;
            background: #FCFDFF;
        }
        QFrame#softCard {
            background: #F8FAFD;
            border-radius: 26px;
        }
        QFrame#hairline {
            background: #E7EBF2;
            border: 0;
            max-height: 1px;
        }
        QFrame#jogPad {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F7F9FC, stop:1 #E7ECF4);
            border: 1px solid #D8E0EB;
            border-radius: 28px;
        }
        QFrame#metricTile {
            background: #F5F7FA;
            border: 1px solid #E0E6EF;
            border-radius: 18px;
        }
        QFrame#exposurePanel {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFFFFF, stop:0.58 #F7FAFF, stop:1 #EEF4FF);
            border: 1px solid rgba(198, 210, 228, 0.96);
            border-radius: 28px;
        }
        QLabel#pageTitle {
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 30px;
            font-weight: 700;
            letter-spacing: -0.3px;
        }
        QLabel#heroKicker {
            color: #0071E3;
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 11px;
            font-weight: 850;
            letter-spacing: 1.4px;
            background: transparent;
        }
        QLabel#heroTitle {
            color: #1D1D1F;
            font-family: "Bodoni MT", "Palatino Linotype", "Georgia";
            font-size: 68px;
            font-weight: 400;
            letter-spacing: 0.2px;
            background: transparent;
        }
        QLabel#aritaWordmark {
            font-family: "Arial Black", "Bahnschrift SemiBold", "Segoe UI";
            font-size: 72px;
            font-weight: 900;
            letter-spacing: -5px;
            background: transparent;
            padding-top: 4px;
        }
        QLabel#heroSpecs {
            color: #303848;
            font-family: "Bahnschrift SemiLight", "Noto Sans KR", "Segoe UI";
            font-size: 21px;
            font-weight: 500;
            letter-spacing: 0.4px;
            background: transparent;
        }
        QLabel#homeChip {
            border: 1px solid #D8E0EB;
            border-radius: 14px;
            padding: 6px 10px;
            color: #425065;
            background: #F4F7FB;
            font-size: 10px;
            font-weight: 800;
        }
        QLabel#homeCardTitle {
            color: #111827;
            font-family: "Bodoni MT", "Palatino Linotype", "Georgia";
            font-size: 42px;
            font-weight: 400;
            letter-spacing: 0.1px;
            background: transparent;
        }
        QLabel#homeCardDesc {
            color: #5D6678;
            font-family: "Noto Sans KR", "Segoe UI";
            font-size: 15px;
            font-weight: 500;
            background: transparent;
        }
        QLabel#featureText {
            color: #394456;
            font-family: "Bahnschrift SemiLight", "Noto Sans KR", "Segoe UI";
            font-size: 15px;
            font-weight: 500;
            background: transparent;
            padding: 5px 0;
        }
        QLabel#cardTitle, QLabel#stepTitle {
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 17px;
            font-weight: 700;
            letter-spacing: -0.1px;
            background: transparent;
        }
        QLabel#muted {
            color: #6E7681;
            font-family: "Noto Sans KR", "Segoe UI";
            background: transparent;
        }
        QLabel#metric {
            font-size: 22px;
            font-weight: 760;
            background: transparent;
        }
        QLabel#metricValueSmall {
            color: #111827;
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 13px;
            font-weight: 760;
            background: #F6F8FB;
            border: 1px solid #E0E7F0;
            border-radius: 12px;
            padding: 7px 10px;
        }
        QLabel#metricAxis {
            color: #6E7681;
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 1px;
            background: transparent;
        }
        QLabel#stateText {
            font-size: 21px;
            font-weight: 760;
            color: #0071E3;
            background: transparent;
        }
        QLabel#exposureCurrent {
            color: #111827;
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 27px;
            font-weight: 850;
            letter-spacing: -0.2px;
            background: transparent;
        }
        QLabel#exposureEyebrow {
            color: #0071E3;
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 10px;
            font-weight: 900;
            letter-spacing: 1.5px;
            background: transparent;
        }
        QLabel#exposureTitle {
            color: #101828;
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 24px;
            font-weight: 900;
            letter-spacing: -0.5px;
            background: transparent;
        }
        QLabel#exposurePercent {
            color: #111827;
            font-family: "Bahnschrift SemiBold", "Segoe UI";
            font-size: 42px;
            font-weight: 900;
            letter-spacing: -1.2px;
            background: transparent;
        }
        QLabel#exposureStateIdle, QLabel#exposureStateReady, QLabel#exposureStateLive,
        QLabel#exposureStateDone, QLabel#exposureStateFault {
            border-radius: 14px;
            padding: 7px 12px;
            font-size: 10px;
            font-weight: 900;
            letter-spacing: 1px;
        }
        QLabel#exposureStateIdle {
            color: #667085;
            background: #EEF2F7;
        }
        QLabel#exposureStateReady {
            color: #006EDB;
            background: #E7F1FF;
        }
        QLabel#exposureStateLive {
            color: #855400;
            background: #FFF4D5;
        }
        QLabel#exposureStateDone {
            color: #007A3D;
            background: #E7F8EF;
        }
        QLabel#exposureStateFault {
            color: #B42318;
            background: #FFE9E7;
        }
        QLabel#dieIdle, QLabel#diePending, QLabel#dieActive, QLabel#dieDone, QLabel#dieFault {
            border-radius: 18px;
            padding: 11px 8px;
            font-size: 12px;
            font-weight: 850;
            min-height: 52px;
        }
        QLabel#dieIdle {
            color: #98A2B3;
            background: #F4F6FA;
            border: 1px solid #E4E9F1;
        }
        QLabel#diePending {
            color: #344054;
            background: #F8FAFD;
            border: 1px solid #D9E2EE;
        }
        QLabel#dieActive {
            color: #1D1D1F;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFF1B8, stop:0.52 #E8F1FF, stop:1 #D9EAFF);
            border: 1px solid #F2C94C;
        }
        QLabel#dieDone {
            color: #027A48;
            background: #ECFDF3;
            border: 1px solid #ABEFC6;
        }
        QLabel#dieFault {
            color: #B42318;
            background: #FEF3F2;
            border: 1px solid #FDA29B;
        }
        QProgressBar#exposureProgress {
            border: 0;
            border-radius: 11px;
            height: 22px;
            background: #E3EAF5;
        }
        QProgressBar#exposureProgress::chunk {
            border-radius: 11px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0071E3, stop:0.45 #4DA3FF, stop:0.75 #8FD4FF, stop:1 #1D1D1F);
        }
        QDialog#recipeSaveDialog {
            border: 1px solid rgba(205, 214, 228, 0.92);
            border-radius: 28px;
            background: #FCFDFF;
        }
        QFrame#exposureDialogShell {
            border: 1px solid rgba(205, 214, 228, 0.92);
            border-radius: 34px;
            background: rgba(248, 250, 253, 246);
        }
        QLabel#dialogBadge {
            border: 1px solid #D9E2EE;
            border-radius: 13px;
            padding: 6px 10px;
            color: #006EDB;
            background: #EEF6FF;
            font-size: 10px;
            font-weight: 900;
            letter-spacing: 1px;
        }
        QLabel#dialogTitle {
            color: #111827;
            font-family: "Bahnschrift SemiBold", "Noto Sans KR", "Segoe UI";
            font-size: 28px;
            font-weight: 850;
            letter-spacing: -0.4px;
            background: transparent;
        }
        QLabel#dialogBody {
            color: #5D6678;
            font-family: "Noto Sans KR", "Segoe UI";
            font-size: 14px;
            font-weight: 500;
            line-height: 150%;
            background: transparent;
        }
        QLabel#badge {
            border-radius: 11px;
            padding: 4px 10px;
            color: #007A3D;
            background: #E7F8EF;
            font-size: 11px;
            font-weight: 700;
        }
        QLabel#badge_running {
            border-radius: 11px;
            padding: 4px 10px;
            color: #0057B8;
            background: #E4F0FF;
            font-size: 11px;
            font-weight: 800;
        }
        QLabel#badge_done {
            border-radius: 11px;
            padding: 4px 10px;
            color: #495160;
            background: #EEF2F7;
            font-size: 11px;
            font-weight: 800;
        }
        QLabel#badge_locked {
            border-radius: 11px;
            padding: 4px 10px;
            color: #7B8493;
            background: #F0F3F8;
            font-size: 11px;
            font-weight: 800;
        }
        QLabel {
            background: transparent;
        }
        QPushButton {
            border: 0;
            border-radius: 21px;
            min-height: 42px;
            padding: 0 18px;
            font-weight: 700;
        }
        QPushButton#pill_primary {
            color: white;
            background: #1D1D1F;
        }
        QPushButton#pill_primary:hover {
            background: #333336;
        }
        QPushButton#pill_blue {
            color: white;
            background: #0071E3;
        }
        QPushButton#pill_blue:hover {
            background: #0A84FF;
        }
        QPushButton#pill_soft {
            color: #1D1D1F;
            background: #E6EBF2;
        }
        QPushButton#pill_soft:hover {
            background: #D9E1EC;
        }
        QPushButton#pill_danger {
            color: white;
            background: #FF3B30;
        }
        QPushButton#pill_danger:hover {
            background: #D70015;
        }
        QLineEdit, QTextEdit, QComboBox {
            border: 1px solid #D9DEE8;
            border-radius: 14px;
            background: #FFFFFF;
            padding: 9px 11px;
        }
        QScrollArea, QScrollArea > QWidget > QWidget {
            background: transparent;
            border: 0;
        }
        QAbstractScrollArea {
            background: transparent;
            border: 0;
        }
        QScrollBar:vertical {
            width: 12px;
            margin: 10px 3px 10px 3px;
            background: transparent;
            border: 0;
        }
        QScrollBar:horizontal {
            height: 12px;
            margin: 3px 10px 3px 10px;
            background: transparent;
            border: 0;
        }
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
            background: rgba(118, 132, 153, 0.32);
            border-radius: 5px;
            min-height: 46px;
            min-width: 46px;
        }
        QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
            background: rgba(29, 29, 31, 0.46);
        }
        QScrollBar::add-line, QScrollBar::sub-line {
            width: 0;
            height: 0;
            background: transparent;
            border: 0;
        }
        QScrollBar::add-page, QScrollBar::sub-page {
            background: transparent;
        }
        QTabWidget::pane {
            border: 0;
        }
        QTabBar::tab {
            background: #F7F9FC;
            border-radius: 16px;
            padding: 10px 18px;
            margin-right: 8px;
            color: #6E7681;
            font-weight: 700;
        }
        QTabBar::tab:selected {
            background: #1D1D1F;
            color: white;
        }
        QCheckBox {
            spacing: 8px;
            background: transparent;
        }
        """
    )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    apply_style(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
