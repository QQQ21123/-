
"""
PhotoStepper Pro Controller v2
==============================

Python GUI controller for an ESP32 CNC controller running FluidNC / GRBL-style firmware.

What this version improves:
- Recipe-based parameters, saved as JSON.
- One-axis-at-a-time planner: X and Y are never emitted in the same G-code line.
- Explicit wafer-loading, mount-confirm, detour-to-exposure, 3x3 snake step-repeat, return flow.
- Dry-run first workflow.
- Preflight checklist before real motion.
- Optional keepout-zone path warning/blocking.
- Top-down path preview in a Tkinter canvas.
- Export generated G-code for review.

Install:
    pip install pyserial

Run:
    python photostepper_pro.py

Safety notes:
    This program is a controller shell, not a safety system. Real equipment still needs
    physical emergency stop, limit switches, safe homing, guarded moving parts, and
    UV/laser enclosure/interlock if any optical source is used.
"""

from __future__ import annotations

import json
import math
import queue
import re
import threading
import time
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SERIAL_IMPORT_ERROR: Optional[str] = None
LIST_PORTS_IMPORT_ERROR: Optional[str] = None

try:
    import serial
except Exception as exc:
    serial = None
    SERIAL_IMPORT_ERROR = str(exc)

try:
    from serial.tools import list_ports
except Exception as exc:
    list_ports = None
    LIST_PORTS_IMPORT_ERROR = str(exc)


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR
DEFAULT_RECIPE_PATH = APP_DIR / "recipe_pro.json"
USER_RECIPE_PATH = APP_DIR / "user_recipe_pro.json"
ASSET_DIR = RESOURCE_DIR / "assets"


AXES = ("X", "Y", "Z")


@dataclass
class AxisLimits:
    x_min: float = 0.0
    x_max: float = 320.0
    y_min: float = 0.0
    y_max: float = 330.0
    z_min: float = -5.0
    z_max: float = 60.0


@dataclass
class KeepoutZone:
    enabled: bool = False
    block_on_violation: bool = False
    x_min: float = 120.0
    x_max: float = 190.0
    y_min: float = 170.0
    y_max: float = 280.0


@dataclass
class ExposureRecipe:
    # Legacy fields are kept for old recipe compatibility; the planner uses the
    # reference/offset/vector model below.
    start_x: float = 150.0
    start_y: float = 150.0
    cols: int = 3
    rows: int = 3
    pitch_x: float = 10.0
    pitch_y: float = 10.0
    exposure_time_s: float = 1.0
    exposure_ref_x: float = 150.0
    exposure_ref_y: float = 150.0
    die1_offset_x: float = 0.0
    die1_offset_y: float = 0.0
    col_step_dx: float = -10.0
    col_step_dy: float = 0.0
    row_step_dx: float = 0.0
    row_step_dy: float = -10.0
    selected_die_numbers: List[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7, 8, 9])
    move_through_all_die_positions: bool = False
    per_die_exposure_time_s: Dict[str, float] = field(default_factory=dict)
    per_die_uv_intensity: Dict[str, float] = field(default_factory=dict)
    exposure_z: float = 0.0
    exposure_z_relative_drop: bool = False

    def exposure_time_for_die(self, die: int) -> float:
        return float(self.per_die_exposure_time_s.get(str(int(die)), self.exposure_time_s))

    def uv_intensity_for_die(self, die: int) -> Optional[float]:
        value = self.per_die_uv_intensity.get(str(int(die)))
        return None if value is None else float(value)


@dataclass
class InspectionRecipe:
    alignment_mode: str = "DIE1_CENTER_ONLY"
    camera_ref_x: float = 155.0
    camera_ref_y: float = 41.0
    camera_die1_offset_x: float = 0.0
    camera_die1_offset_y: float = 0.0
    camera_col_step_dx: float = -10.0
    camera_col_step_dy: float = 0.0
    camera_row_step_dx: float = 0.0
    camera_row_step_dy: float = -10.0
    camera_to_exposure_dx: float = 0.0
    camera_to_exposure_dy: float = 0.0
    measured_die1_center_x: float = 155.0
    measured_die1_center_y: float = 41.0
    die1_center_alignment_active: bool = False
    measured_die1_marks: Dict[str, Dict[str, float]] = field(default_factory=dict)
    selected_inspection_die_numbers: List[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7, 8, 9])
    move_through_all_inspection_positions: bool = False
    inspection_z_mode: str = "SINGLE"
    inspection_z_single: float = 35.0
    inspection_z_values: List[float] = field(default_factory=lambda: [35.0])
    z_sweep_start: float = 34.0
    z_sweep_end: float = 40.0
    z_sweep_step: float = 0.5
    z_settle_time_s: float = 0.3
    capture_each_z: bool = False
    safe_z: float = 60.0
    return_to_safe_z_after_die: bool = False
    camera_enabled: bool = False
    camera_index: int = 0
    camera_width: int = 1280
    camera_height: int = 720
    camera_fps: int = 30
    capture_folder: str = "inspection_runs"


@dataclass
class IORecipe:
    uv_enabled: bool = False
    uv_on_gcode: str = "M3 S255"
    uv_off_gcode: str = "M5"


@dataclass
class UIRecipe:
    unlock_ordered_flow: bool = False


@dataclass
class MotionRecipe:
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    wait_idle: bool = True
    idle_timeout_s: float = 90.0
    command_timeout_s: float = 15.0


@dataclass
class JogRecipe:
    xy_step_mm: float = 1.0
    z_step_mm: float = 0.5
    feed_xy: float = 600.0
    feed_z: float = 120.0


@dataclass
class SerialRecipe:
    port: str = "COM3"
    baud: int = 115200


@dataclass
class StageRecipe:
    initial_x: float = 0.0
    initial_y: float = 0.0
    initial_z: float = 0.0
    invert_z_output: bool = False
    load_x: float = 150.0
    load_y: float = 300.0
    load_z: float = 40.0
    lowered_z: float = 0.0


@dataclass
class Recipe:
    serial: SerialRecipe = field(default_factory=SerialRecipe)
    motion: MotionRecipe = field(default_factory=MotionRecipe)
    jog: JogRecipe = field(default_factory=JogRecipe)
    limits: AxisLimits = field(default_factory=AxisLimits)
    keepout: KeepoutZone = field(default_factory=KeepoutZone)
    stage: StageRecipe = field(default_factory=StageRecipe)
    exposure: ExposureRecipe = field(default_factory=ExposureRecipe)
    inspection: InspectionRecipe = field(default_factory=InspectionRecipe)
    io: IORecipe = field(default_factory=IORecipe)
    ui: UIRecipe = field(default_factory=UIRecipe)

    # One line = one axis move. Example: {"X": 250.0}
    to_loading_waypoints: List[Dict[str, float]] = field(default_factory=lambda: [
        {"Z": 40.0},
        {"Y": 300.0},
        {"X": 150.0},
    ])

    to_exposure_waypoints: List[Dict[str, float]] = field(default_factory=lambda: [
        {"Y": 270.0},
        {"X": 20.0},
        {"Y": 130.0},
        {"X": 150.0},
        {"Y": 150.0},
    ])

    to_camera_alignment_waypoints: List[Dict[str, float]] = field(default_factory=lambda: [
        {"Y": 270.0},
        {"X": 20.0},
        {"Y": 130.0},
        {"X": 150.0},
        {"Y": 150.0},
    ])

    return_waypoints: List[Dict[str, float]] = field(default_factory=lambda: [
        {"Y": 130.0},
        {"X": 30.0},
        {"Y": 300.0},
        {"X": 150.0},
    ])


@dataclass
class PlannedCommand:
    gcode: str
    label: str = ""
    is_motion: bool = False
    is_exposure: bool = False
    is_relative_motion: bool = False
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None

    def export_line(self) -> str:
        if self.label:
            return f"{self.gcode:<28} ; {self.label}"
        return self.gcode


class RecipeCodec:
    @staticmethod
    def _merge_dataclass(cls, value: Any):
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError(f"{cls.__name__} must be a JSON object")
        base = asdict(cls())
        base.update(value)
        return cls(**base)

    @staticmethod
    def from_json_dict(data: Dict[str, Any]) -> Recipe:
        if not isinstance(data, dict):
            raise ValueError("Recipe root must be a JSON object")

        recipe = Recipe()
        recipe.serial = RecipeCodec._merge_dataclass(SerialRecipe, data.get("serial"))
        recipe.motion = RecipeCodec._merge_dataclass(MotionRecipe, data.get("motion"))
        recipe.jog = RecipeCodec._merge_dataclass(JogRecipe, data.get("jog"))
        recipe.limits = RecipeCodec._merge_dataclass(AxisLimits, data.get("limits"))
        recipe.keepout = RecipeCodec._merge_dataclass(KeepoutZone, data.get("keepout"))
        recipe.stage = RecipeCodec._merge_dataclass(StageRecipe, data.get("stage"))
        exposure_data = data.get("exposure") or {}
        recipe.exposure = RecipeCodec._merge_dataclass(ExposureRecipe, exposure_data)
        if isinstance(exposure_data, dict):
            # Migrate older recipes that only had start/pitch fields.
            if "exposure_ref_x" not in exposure_data and "start_x" in exposure_data:
                recipe.exposure.exposure_ref_x = float(recipe.exposure.start_x)
            if "exposure_ref_y" not in exposure_data and "start_y" in exposure_data:
                recipe.exposure.exposure_ref_y = float(recipe.exposure.start_y)
            if "col_step_dx" not in exposure_data and "pitch_x" in exposure_data:
                recipe.exposure.col_step_dx = float(recipe.exposure.pitch_x)
            if "row_step_dy" not in exposure_data and "pitch_y" in exposure_data:
                recipe.exposure.row_step_dy = float(recipe.exposure.pitch_y)
            if "exposure_z" not in exposure_data:
                recipe.exposure.exposure_z = float(recipe.stage.lowered_z)
        recipe.inspection = RecipeCodec._merge_dataclass(InspectionRecipe, data.get("inspection"))
        recipe.io = RecipeCodec._merge_dataclass(IORecipe, data.get("io"))
        recipe.ui = RecipeCodec._merge_dataclass(UIRecipe, data.get("ui"))
        recipe.to_loading_waypoints = data.get("to_loading_waypoints", recipe.to_loading_waypoints)
        recipe.to_exposure_waypoints = data.get("to_exposure_waypoints", recipe.to_exposure_waypoints)
        recipe.to_camera_alignment_waypoints = data.get(
            "to_camera_alignment_waypoints",
            recipe.to_exposure_waypoints,
        )
        recipe.return_waypoints = data.get("return_waypoints", recipe.return_waypoints)
        validate_waypoints(recipe.to_loading_waypoints)
        validate_waypoints(recipe.to_exposure_waypoints)
        validate_waypoints(recipe.to_camera_alignment_waypoints)
        validate_waypoints(recipe.return_waypoints)
        load_endpoint = {
            "X": float(recipe.stage.initial_x),
            "Y": float(recipe.stage.initial_y),
            "Z": float(recipe.stage.initial_z),
        }
        for wp in recipe.to_loading_waypoints:
            axis = next(iter(wp)).upper()
            load_endpoint[axis] = float(wp[next(iter(wp))])
        recipe.stage.load_x = load_endpoint["X"]
        recipe.stage.load_y = load_endpoint["Y"]
        recipe.stage.load_z = load_endpoint["Z"]
        return recipe

    @staticmethod
    def load(path: Path = DEFAULT_RECIPE_PATH) -> Recipe:
        if not path.exists():
            recipe = Recipe()
            RecipeCodec.save(recipe, path)
            return recipe
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return RecipeCodec.from_json_dict(data)
        except Exception as exc:
            raise ValueError(f"Recipe load failed: {path}\n{exc}") from exc

    @staticmethod
    def save(recipe: Recipe, path: Path = DEFAULT_RECIPE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(recipe), ensure_ascii=False, indent=2)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        backup_path = path.with_suffix(path.suffix + ".bak")
        if path.exists():
            try:
                backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(path)

    @staticmethod
    def load_active(default_path: Path = DEFAULT_RECIPE_PATH, user_path: Path = USER_RECIPE_PATH) -> Tuple[Recipe, Path, str]:
        if user_path.exists():
            return RecipeCodec.load(user_path), user_path, "user"
        if default_path.exists():
            recipe = RecipeCodec.load(default_path)
            RecipeCodec.save(recipe, user_path)
            return recipe, user_path, "default-copied"
        recipe = Recipe()
        RecipeCodec.save(recipe, user_path)
        return recipe, user_path, "created"


def validate_waypoints(waypoints: List[Dict[str, float]]) -> None:
    if not isinstance(waypoints, list):
        raise ValueError("Waypoints must be a list")
    for item in waypoints:
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError(f"Each waypoint must be one axis only, got: {item!r}")
        axis = next(iter(item)).upper()
        if axis not in AXES:
            raise ValueError(f"Invalid waypoint axis: {axis}")
        float(item[next(iter(item))])


def waypoints_to_text(waypoints: List[Dict[str, float]]) -> str:
    lines: List[str] = []
    for wp in waypoints:
        axis = next(iter(wp)).upper()
        value = wp[next(iter(wp))]
        lines.append(f"{axis}={float(value):g}")
    return "\n".join(lines)


def parse_waypoints(text: str) -> List[Dict[str, float]]:
    result: List[Dict[str, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([XYZxyz])\s*=?\s*(-?\d+(?:\.\d+)?)", line)
        if not match:
            raise ValueError(f"Waypoint format error: {raw!r}. Use X=250, Y=130, Z=40")
        result.append({match.group(1).upper(): float(match.group(2))})
    validate_waypoints(result)
    return result


def waypoint_endpoint(start: Dict[str, float], waypoints: List[Dict[str, float]]) -> Dict[str, float]:
    pos = {axis: float(start.get(axis, 0.0)) for axis in AXES}
    validate_waypoints(waypoints)
    for wp in waypoints:
        axis = next(iter(wp)).upper()
        pos[axis] = float(wp[next(iter(wp))])
    return pos


def format_axis_value(recipe: Recipe, axis: str, value: float, relative: bool = False) -> float:
    axis = axis.upper()
    if axis == "Z" and recipe.stage.invert_z_output:
        if relative:
            return -float(value)
        return float(recipe.limits.z_min) + float(recipe.limits.z_max) - float(value)
    return float(value)


def asset_path(name: str) -> Path:
    return ASSET_DIR / name


class MotionPlanner:
    DIE_TO_ROW_COL: Dict[int, Tuple[int, int]] = {
        1: (0, 0),
        2: (0, 1),
        3: (0, 2),
        4: (1, 2),
        5: (1, 1),
        6: (1, 0),
        7: (2, 0),
        8: (2, 1),
        9: (2, 2),
    }

    def __init__(self, recipe: Recipe, use_camera_alignment: bool = False):
        self.r = recipe
        self.use_camera_alignment = bool(use_camera_alignment)
        self.pos: Dict[str, float] = {
            "X": recipe.stage.initial_x,
            "Y": recipe.stage.initial_y,
            "Z": recipe.stage.initial_z,
        }
        self.commands: List[PlannedCommand] = []
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def reset(self, x: Optional[float] = None, y: Optional[float] = None, z: Optional[float] = None):
        self.commands = []
        self.warnings = []
        self.errors = []
        if x is not None:
            self.pos["X"] = float(x)
        if y is not None:
            self.pos["Y"] = float(y)
        if z is not None:
            self.pos["Z"] = float(z)

    def prepare(self):
        self.commands.append(PlannedCommand("G21", "units = mm"))
        self.commands.append(PlannedCommand("G90", "absolute coordinates"))
        self.commands.append(PlannedCommand("G94", "feed units/min"))

    def _limit_for_axis(self, axis: str) -> Tuple[float, float]:
        lim = self.r.limits
        if axis == "X":
            return lim.x_min, lim.x_max
        if axis == "Y":
            return lim.y_min, lim.y_max
        if axis == "Z":
            return lim.z_min, lim.z_max
        raise ValueError(axis)

    def _check_limit(self, axis: str, value: float):
        lo, hi = self._limit_for_axis(axis)
        if not (lo <= value <= hi):
            self.errors.append(f"{axis}{value:g} is outside soft range {lo:g}..{hi:g}")

    def _axis_line_crosses_keepout(self, old_pos: Dict[str, float], axis: str, value: float) -> bool:
        k = self.r.keepout
        if not k.enabled or axis == "Z":
            return False

        x0 = old_pos["X"]
        y0 = old_pos["Y"]
        x1 = value if axis == "X" else x0
        y1 = value if axis == "Y" else y0

        # Axis-aligned line segment vs rectangle intersection.
        eps = 1e-9
        if abs(y0 - y1) < eps:  # horizontal segment
            y = y0
            if k.y_min <= y <= k.y_max:
                sx0, sx1 = sorted((x0, x1))
                return max(sx0, k.x_min) <= min(sx1, k.x_max)
        if abs(x0 - x1) < eps:  # vertical segment
            x = x0
            if k.x_min <= x <= k.x_max:
                sy0, sy1 = sorted((y0, y1))
                return max(sy0, k.y_min) <= min(sy1, k.y_max)
        return False

    def move_axis(self, axis: str, value: float, label: str = ""):
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Invalid axis {axis}")
        value = float(value)

        self._check_limit(axis, value)
        old = dict(self.pos)

        if self._axis_line_crosses_keepout(old, axis, value):
            msg = (
                f"Path segment {axis}{old[axis]:g}->{value:g} at "
                f"X{old['X']:g}/Y{old['Y']:g} crosses keepout zone"
            )
            if self.r.keepout.block_on_violation:
                self.errors.append(msg)
            else:
                self.warnings.append(msg)

        feed = self.r.motion.feed_z if axis == "Z" else self.r.motion.feed_xy
        self.pos[axis] = value
        gcode_value = format_axis_value(self.r, axis, value)
        self.commands.append(
            PlannedCommand(
                gcode=f"G1 {axis}{gcode_value:.3f} F{feed:.1f}",
                label=label or f"move {axis}",
                is_motion=True,
                x=self.pos["X"],
                y=self.pos["Y"],
                z=self.pos["Z"],
            )
        )

    def apply_waypoints(self, waypoints: List[Dict[str, float]], prefix: str):
        validate_waypoints(waypoints)
        for idx, wp in enumerate(waypoints, start=1):
            axis = next(iter(wp)).upper()
            value = float(wp[next(iter(wp))])
            self.move_axis(axis, value, f"{prefix} waypoint {idx}")

    def move_axis_relative(self, axis: str, delta: float, label: str = ""):
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Invalid axis {axis}")
        delta = float(delta)
        target = dict(self.pos)
        target[axis] += delta
        error_count = len(self.errors)
        self._check_limit(axis, target[axis])
        if len(self.errors) > error_count:
            return
        feed = self.r.motion.feed_z if axis == "Z" else self.r.motion.feed_xy
        gcode_delta = format_axis_value(self.r, axis, delta, relative=True)
        self.commands.append(PlannedCommand("G91", f"{label or axis} relative mode"))
        self.pos[axis] = target[axis]
        self.commands.append(
            PlannedCommand(
                gcode=f"G1 {axis}{gcode_delta:.3f} F{feed:.1f}",
                label=label or f"move {axis} relative",
                is_motion=True,
                is_relative_motion=True,
                x=self.pos["X"],
                y=self.pos["Y"],
                z=self.pos["Z"],
            )
        )
        self.commands.append(PlannedCommand("G90", f"{label or axis} absolute mode"))

    def move_to_loading_position(self):
        self.apply_waypoints(self.r.to_loading_waypoints, "to loading")
        targets = {
            "X": self.r.stage.load_x,
            "Y": self.r.stage.load_y,
            "Z": self.r.stage.load_z,
        }
        for axis, value in targets.items():
            if abs(self.pos[axis] - value) > 1e-9:
                self.move_axis(axis, value, f"loading {axis}")

    def loading_sequence(self) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        self.reset(
            self.r.stage.initial_x,
            self.r.stage.initial_y,
            self.r.stage.initial_z,
        )
        self.prepare()
        self.move_to_loading_position()
        return self.commands, self.warnings, self.errors

    def mount_to_exposure_sequence(self) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        self.reset(
            self.r.stage.load_x,
            self.r.stage.load_y,
            self.r.stage.load_z,
        )
        self.prepare()
        # Z movement is intentionally controlled by the editable route. This
        # avoids an implicit Z=lowered_z move before the detour path starts.
        self.apply_waypoints(self.r.to_exposure_waypoints, "to exposure")
        return self.commands, self.warnings, self.errors

    def route_to_camera_alignment_sequence(self) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        self.reset(
            self.r.stage.load_x,
            self.r.stage.load_y,
            self.r.stage.load_z,
        )
        self.prepare()
        self.apply_waypoints(self.r.to_camera_alignment_waypoints, "to camera alignment")
        return self.commands, self.warnings, self.errors

    def selected_die_numbers(self) -> List[int]:
        selected: List[int] = []
        for raw in self.r.exposure.selected_die_numbers:
            die = int(raw)
            if die not in self.DIE_TO_ROW_COL:
                self.errors.append(f"Invalid selected die number: {die}. Use 1..9.")
                continue
            if die not in selected:
                selected.append(die)
        return selected

    def calculate_die1_stage_position(self) -> Tuple[float, float]:
        e = self.r.exposure
        i = self.r.inspection
        if (
            self.use_camera_alignment
            and
            (i.alignment_mode or "").upper() == "DIE1_CENTER_ONLY"
            and i.die1_center_alignment_active
        ):
            _nom_x, _nom_y, _aligned_x, _aligned_y, final_x, final_y = self.calculate_die1_center_aligned_position(1)
            return final_x, final_y
        return (
            e.exposure_ref_x + e.die1_offset_x,
            e.exposure_ref_y + e.die1_offset_y,
        )

    def calculate_die_stage_position(self, die_number: int) -> Tuple[int, int, float, float]:
        if die_number not in self.DIE_TO_ROW_COL:
            raise ValueError(f"Invalid die number {die_number}; use 1..9")
        e = self.r.exposure
        i = self.r.inspection
        row, col = self.DIE_TO_ROW_COL[die_number]
        if (
            self.use_camera_alignment
            and
            (i.alignment_mode or "").upper() == "DIE1_CENTER_ONLY"
            and i.die1_center_alignment_active
        ):
            _nom_x, _nom_y, _aligned_x, _aligned_y, final_x, final_y = self.calculate_die1_center_aligned_position(die_number)
            return row, col, final_x, final_y
        die1_x, die1_y = self.calculate_die1_stage_position()
        stage_x = die1_x + col * e.col_step_dx + row * e.row_step_dx
        stage_y = die1_y + col * e.col_step_dy + row * e.row_step_dy
        return row, col, stage_x, stage_y

    def calculate_die1_center_aligned_position(self, die_number: int) -> Tuple[float, float, float, float, float, float]:
        if die_number not in self.DIE_TO_ROW_COL:
            raise ValueError(f"Invalid die number {die_number}; use 1..9")
        e = self.r.exposure
        i = self.r.inspection
        row, col = self.DIE_TO_ROW_COL[die_number]
        nominal_die1_x, nominal_die1_y = self.calculate_camera_die1_stage_position()
        nominal_camera_x = nominal_die1_x + col * i.camera_col_step_dx + row * i.camera_row_step_dx
        nominal_camera_y = nominal_die1_y + col * i.camera_col_step_dy + row * i.camera_row_step_dy
        aligned_die1_camera_x = float(i.measured_die1_center_x)
        aligned_die1_camera_y = float(i.measured_die1_center_y)
        aligned_camera_x = aligned_die1_camera_x + col * i.camera_col_step_dx + row * i.camera_row_step_dx
        aligned_camera_y = aligned_die1_camera_y + col * i.camera_col_step_dy + row * i.camera_row_step_dy
        camera_delta_x = aligned_die1_camera_x - nominal_die1_x
        camera_delta_y = aligned_die1_camera_y - nominal_die1_y
        nominal_exposure_die1_x = e.exposure_ref_x + e.die1_offset_x
        nominal_exposure_die1_y = e.exposure_ref_y + e.die1_offset_y
        nominal_exposure_x = nominal_exposure_die1_x + col * e.col_step_dx + row * e.row_step_dx
        nominal_exposure_y = nominal_exposure_die1_y + col * e.col_step_dy + row * e.row_step_dy
        final_exposure_x = nominal_exposure_x + camera_delta_x
        final_exposure_y = nominal_exposure_y + camera_delta_y
        return nominal_camera_x, nominal_camera_y, aligned_camera_x, aligned_camera_y, final_exposure_x, final_exposure_y

    def make_exposure_plan(self) -> List[Tuple[int, int, int, float, float, bool]]:
        selected = set(self.selected_die_numbers())
        visit_order = list(self.DIE_TO_ROW_COL.keys()) if self.r.exposure.move_through_all_die_positions else [
            die for die in self.DIE_TO_ROW_COL if die in selected
        ]
        return [
            (*self._die_position_tuple(die), die in selected)
            for die in visit_order
        ]

    def _die_position_tuple(self, die: int) -> Tuple[int, int, int, float, float]:
        row, col, x, y = self.calculate_die_stage_position(die)
        return die, row, col, x, y

    def preview_exposure_plan(self) -> List[str]:
        lines: List[str] = []
        i = self.r.inspection
        die1_center_mode = (
            self.use_camera_alignment
            and
            (i.alignment_mode or "").upper() == "DIE1_CENTER_ONLY"
            and i.die1_center_alignment_active
        )
        if die1_center_mode:
            nominal_die1_x, nominal_die1_y = self.calculate_camera_die1_stage_position()
            center_dx = float(i.measured_die1_center_x) - nominal_die1_x
            center_dy = float(i.measured_die1_center_y) - nominal_die1_y
            lines.append(
                "alignment_mode=DIE1_CENTER_ONLY active=true; rotation is not corrected. "
                "2nd exposure uses the 1st-process exposure die position plus measured Camera Die 1 translation."
            )
            lines.append(
                f"Die 1 center comparison: nominal camera X{nominal_die1_x:.3f} Y{nominal_die1_y:.3f} | "
                f"measured camera X{i.measured_die1_center_x:.3f} Y{i.measured_die1_center_y:.3f} | "
                f"translation dX{center_dx:.3f} dY{center_dy:.3f}"
            )
            if i.measured_die1_marks:
                mark_text = []
                for name in ("TOP", "BOTTOM", "TL", "TR", "BR", "BL"):
                    mark = i.measured_die1_marks.get(name)
                    if mark:
                        mark_text.append(f"{name}=X{float(mark['x']):.3f}/Y{float(mark['y']):.3f}")
                if mark_text:
                    lines.append("Measured marks: " + ", ".join(mark_text))
        for index, (die, row, col, x, y, selected) in enumerate(self.make_exposure_plan(), start=1):
            action = "EXPOSE" if selected else "SKIP"
            exposure_time = self.r.exposure.exposure_time_for_die(die)
            intensity = self.r.exposure.uv_intensity_for_die(die)
            dose_text = f" | time {exposure_time:g}s"
            if intensity is not None:
                dose_text += f" | intensity S{intensity:g}"
            if die1_center_mode:
                nominal_x, nominal_y, aligned_x, aligned_y, final_x, final_y = self.calculate_die1_center_aligned_position(die)
                lines.append(
                    f"#{index} Die {die} row={row} col={col} selected={selected} "
                    f"nominal camera: X{nominal_x:.3f} Y{nominal_y:.3f} | "
                    f"measured Die 1 center: X{i.measured_die1_center_x:.3f} Y{i.measured_die1_center_y:.3f} | "
                    f"aligned camera: X{aligned_x:.3f} Y{aligned_y:.3f} | "
                    f"camera translation: dX{center_dx:.3f} dY{center_dy:.3f} | "
                    f"final exposure: X{final_x:.3f} Y{final_y:.3f}{dose_text} {action}"
                )
            else:
                lines.append(
                    f"#{index} die={die} row={row} col={col} selected={selected} "
                    f"stage=({x:.3f}, {y:.3f}){dose_text} {action}"
                )
        return lines

    def exposure_positions(self) -> List[Tuple[int, int, int, float, float]]:
        return [self._die_position_tuple(die) for die in self.DIE_TO_ROW_COL]

    def visited_exposure_positions(self) -> List[Tuple[int, int, int, float, float, bool]]:
        return self.make_exposure_plan()

    def selected_inspection_die_numbers(self) -> List[int]:
        selected: List[int] = []
        for raw in self.r.inspection.selected_inspection_die_numbers:
            die = int(raw)
            if die not in self.DIE_TO_ROW_COL:
                self.errors.append(f"Invalid inspection die number: {die}. Use 1..9.")
                continue
            if die not in selected:
                selected.append(die)
        return selected

    def calculate_camera_die1_stage_position(self) -> Tuple[float, float]:
        i = self.r.inspection
        return (
            i.camera_ref_x + i.camera_die1_offset_x,
            i.camera_ref_y + i.camera_die1_offset_y,
        )

    def calculate_camera_die_stage_position(self, die_number: int) -> Tuple[int, int, float, float]:
        if die_number not in self.DIE_TO_ROW_COL:
            raise ValueError(f"Invalid die number {die_number}; use 1..9")
        i = self.r.inspection
        row, col = self.DIE_TO_ROW_COL[die_number]
        die1_x, die1_y = self.calculate_camera_die1_stage_position()
        stage_x = die1_x + col * i.camera_col_step_dx + row * i.camera_row_step_dx
        stage_y = die1_y + col * i.camera_col_step_dy + row * i.camera_row_step_dy
        return row, col, stage_x, stage_y

    def get_inspection_z_values(self) -> List[float]:
        i = self.r.inspection
        mode = (i.inspection_z_mode or "SINGLE").upper()
        if mode == "LIST":
            values = [float(v) for v in i.inspection_z_values]
        elif mode == "SWEEP":
            step = float(i.z_sweep_step)
            if abs(step) < 1e-9:
                self.errors.append("Inspection Z sweep step must not be 0")
                return []
            start = float(i.z_sweep_start)
            end = float(i.z_sweep_end)
            if (end - start) * step < 0:
                step = -step
            values = []
            cur = start
            guard = 0
            while (cur <= end + 1e-9 if step > 0 else cur >= end - 1e-9):
                values.append(round(cur, 6))
                cur += step
                guard += 1
                if guard > 1000:
                    self.errors.append("Inspection Z sweep generated too many points")
                    break
        else:
            values = [float(i.inspection_z_single)]
        for z in values:
            self._check_limit("Z", z)
        return values

    def make_inspection_plan(self) -> List[Tuple[int, int, int, float, float, bool]]:
        selected = set(self.selected_inspection_die_numbers())
        visit_order = list(self.DIE_TO_ROW_COL.keys()) if self.r.inspection.move_through_all_inspection_positions else [
            die for die in self.DIE_TO_ROW_COL if die in selected
        ]
        plan: List[Tuple[int, int, int, float, float, bool]] = []
        for die in visit_order:
            row, col, x, y = self.calculate_camera_die_stage_position(die)
            self._check_limit("X", x)
            self._check_limit("Y", y)
            plan.append((die, row, col, x, y, die in selected))
        return plan

    def preview_inspection_plan(self) -> List[str]:
        z_values = self.get_inspection_z_values()
        z_text = "[" + ", ".join(f"{z:g}" for z in z_values) + "]"
        lines: List[str] = []
        for index, (die, row, col, x, y, selected) in enumerate(self.make_inspection_plan(), start=1):
            action = "INSPECT" if selected else "SKIP"
            detail = f"Z={z_text} " if selected else ""
            lines.append(
                f"#{index} die={die} row={row} col={col} selected={selected} "
                f"camera_stage=({x:.3f}, {y:.3f}) {detail}{action}"
            )
        return lines

    def exposure_grid_sequence(self, with_prepare: bool = True) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        self.reset(self.r.exposure.exposure_ref_x, self.r.exposure.exposure_ref_y, self.pos["Z"])
        if with_prepare:
            self.prepare()
        self.move_to_exposure_z()

        for die, row, col, x, y, selected in self.visited_exposure_positions():
            # Deliberately separate X and Y. Never send G1 X.. Y..
            self.move_axis("X", x, f"die {die} row {row + 1} col {col + 1} X")
            self.move_axis("Y", y, f"die {die} row {row + 1} col {col + 1} Y")
            if selected:
                self.expose(die)
            else:
                self.commands.append(PlannedCommand("", f"SKIPPED die {die}", is_exposure=False))
        return self.commands, self.warnings, self.errors

    def move_to_exposure_z(self):
        self.move_axis("Z", self.r.exposure.exposure_z, "move to absolute exposure Z")

    def expose(self, die: int):
        e = self.r.exposure
        io = self.r.io
        exposure_time = e.exposure_time_for_die(die)

        if io.uv_enabled:
            if io.uv_on_gcode.strip():
                self.commands.append(PlannedCommand(self.uv_on_command_for_die(die), f"UV ON die {die}", is_exposure=True))
            if exposure_time > 0:
                self.commands.append(PlannedCommand(f"G4 P{exposure_time:.3f}", f"dwell die {die}", is_exposure=True))
            if io.uv_off_gcode.strip():
                self.commands.append(PlannedCommand(io.uv_off_gcode.strip(), f"UV OFF die {die}", is_exposure=True))
        else:
            # This is not sent to the controller; runner treats an empty gcode command as a UI delay.
            self.commands.append(PlannedCommand("", f"SIMULATED exposure die {die}: {exposure_time:g}s", is_exposure=True))

    def uv_on_command_for_die(self, die: int) -> str:
        command = self.r.io.uv_on_gcode.strip()
        intensity = self.r.exposure.uv_intensity_for_die(die)
        if intensity is None:
            return command
        formatted = f"S{intensity:g}"
        if re.search(r"\bS[-+0-9.]+", command, flags=re.IGNORECASE):
            return re.sub(r"\bS[-+0-9.]+", formatted, command, count=1, flags=re.IGNORECASE)
        return f"{command} {formatted}".strip()

    def return_sequence(self) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        # Start from final visited die position, not necessarily the exposure origin.
        visited = self.visited_exposure_positions()
        if visited:
            _, _, _, final_x, final_y, _selected = visited[-1]
        else:
            final_x, final_y = self.calculate_die1_stage_position()
        self.reset(final_x, final_y, self.r.exposure.exposure_z)
        self.prepare()
        self.apply_waypoints(self.r.return_waypoints, "return")
        return self.commands, self.warnings, self.errors

    def full_cycle_sequence(self) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        self.reset(
            self.r.stage.initial_x,
            self.r.stage.initial_y,
            self.r.stage.initial_z,
        )
        self.prepare()
        self.move_to_loading_position()
        # Do not inject an automatic Z drop here; put any required Z move in
        # the route text so the operator can control when it happens.
        self.apply_waypoints(self.r.to_exposure_waypoints, "to exposure")
        self.move_to_exposure_z()
        for die, row, col, x, y, selected in self.visited_exposure_positions():
            self.move_axis("X", x, f"die {die} row {row + 1} col {col + 1} X")
            self.move_axis("Y", y, f"die {die} row {row + 1} col {col + 1} Y")
            if selected:
                self.expose(die)
            else:
                self.commands.append(PlannedCommand("", f"SKIPPED die {die}", is_exposure=False))
        self.apply_waypoints(self.r.return_waypoints, "return")
        return self.commands, self.warnings, self.errors


class SerialGcodeTransport:
    def __init__(self, logger: Callable[[str], None]):
        self.ser = None
        self.logger = logger
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.ser is not None and getattr(self.ser, "is_open", False)

    def connect(self, port: str, baud: int):
        if serial is None:
            detail = f" Import error: {SERIAL_IMPORT_ERROR}" if SERIAL_IMPORT_ERROR else ""
            raise RuntimeError(f"pyserial could not be imported. Run: pip install pyserial.{detail}")
        self.close()
        self.ser = serial.Serial(
            port=port,
            baudrate=int(baud),
            timeout=0.25,
            write_timeout=1.0,
        )
        time.sleep(1.5)
        self.ser.reset_input_buffer()
        self.logger(f"[CONNECTED] {port} @ {baud}")

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def realtime(self, payload: bytes):
        with self.lock:
            if not self.connected:
                raise RuntimeError("Not connected")
            self.ser.write(payload)
            self.ser.flush()

    def send_line(self, line: str, timeout_s: float):
        line = line.strip()
        if not line:
            return

        with self.lock:
            if not self.connected:
                raise RuntimeError("Not connected")
            self.logger(f"> {line}")
            self.ser.write((line + "\n").encode("ascii", errors="ignore"))
            self.ser.flush()

            deadline = time.time() + timeout_s
            while time.time() < deadline:
                raw = self.ser.readline()
                if not raw:
                    continue
                text = raw.decode(errors="replace").strip()
                if not text:
                    continue
                self.logger(f"< {text}")

                low = text.lower()
                if low == "ok":
                    return
                if low.startswith("error") or low.startswith("alarm"):
                    raise RuntimeError(text)

            raise TimeoutError(f"No ok/error response for: {line}")

    def wait_idle(self, timeout_s: float):
        with self.lock:
            if not self.connected:
                raise RuntimeError("Not connected")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self.lock:
                self.ser.write(b"?")
                self.ser.flush()
                raw = self.ser.readline()

            if raw:
                text = raw.decode(errors="replace").strip()
                if text:
                    self.logger(f"< {text}")
                # Common GRBL/FluidNC shape: <Idle|MPos:...|FS:...>
                if text.startswith("<Idle") or "|Idle" in text:
                    return

            time.sleep(0.15)
        raise TimeoutError("Controller did not report Idle before timeout")

    def request_status(self, timeout_s: float = 2.0) -> str:
        with self.lock:
            if not self.connected:
                raise RuntimeError("Not connected")

            deadline = time.time() + timeout_s
            while time.time() < deadline:
                self.ser.write(b"?")
                self.ser.flush()
                raw = self.ser.readline()
                if not raw:
                    continue
                text = raw.decode(errors="replace").strip()
                if not text:
                    continue
                self.logger(f"< {text}")
                if text.startswith("<"):
                    return text
            raise TimeoutError("No status response received")


class PreviewCanvas(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, bg="#FFFFFF", highlightthickness=0, **kw)
        self.recipe: Optional[Recipe] = None
        self.motion_phase = 0.0
        self.live_position: Optional[Tuple[float, float]] = None
        self.live_pulse = 0.0
        self.after(250, self._bind_resize)
        self.after(90, self._tick_motion)

    def _bind_resize(self):
        self.bind("<Configure>", lambda event: self.redraw())

    def _tick_motion(self):
        self.motion_phase = (self.motion_phase + 0.008) % 1.0
        self.live_pulse = (self.live_pulse + 0.05) % 1.0
        if self.recipe is not None and self.winfo_ismapped():
            self.redraw()
        self.after(70, self._tick_motion)

    def set_recipe(self, recipe: Recipe):
        self.recipe = recipe
        self.redraw()

    def set_live_position(self, x: Optional[float], y: Optional[float]):
        if x is None or y is None:
            return
        self.live_position = (float(x), float(y))
        self.redraw()

    def redraw(self):
        self.delete("all")
        if not self.recipe:
            return

        r = self.recipe
        pad = 32
        w = max(self.winfo_width(), 200)
        h = max(self.winfo_height(), 200)
        lim = r.limits

        x_span = max(1.0, lim.x_max - lim.x_min)
        y_span = max(1.0, lim.y_max - lim.y_min)
        scale = min((w - 2 * pad) / x_span, (h - 2 * pad) / y_span)

        def map_xy(x: float, y: float) -> Tuple[float, float]:
            # Canvas Y goes down. Machine Y also drawn down for stage intuition.
            px = pad + (x - lim.x_min) * scale
            py = pad + (y - lim.y_min) * scale
            return px, py

        self.create_rectangle(0, 0, w, h, fill="#F8F9FB", outline="")
        glow = 80 + 20 * math.sin(self.motion_phase * math.tau)
        self.create_oval(w - 260 - glow, 30 - glow, w + 120 + glow, 410 + glow, fill="#EEF6FF", outline="")
        self.create_oval(-180 - glow, h - 260 - glow, 220 + glow, h + 140 + glow, fill="#F4F0FF", outline="")

        # Workspace
        x0, y0 = map_xy(lim.x_min, lim.y_min)
        x1, y1 = map_xy(lim.x_max, lim.y_max)
        self.create_rectangle(x0, y0, x1, y1, outline="#D8D2C5", width=2)
        self._draw_grid(x0, y0, x1, y1, 6)
        self.create_text(x0 + 8, y0 + 8, text="WORK AREA", fill="#7A7468", anchor="nw", font=("Segoe UI", 9, "bold"))

        # Keepout
        if r.keepout.enabled:
            k = r.keepout
            kx0, ky0 = map_xy(k.x_min, k.y_min)
            kx1, ky1 = map_xy(k.x_max, k.y_max)
            self.create_rectangle(kx0, ky0, kx1, ky1, outline="#C94B3D", width=2, dash=(4, 3), fill="#FCE9E6")
            self.create_text(kx0 + 5, ky0 + 5, text="KEEP OUT", fill="#A13F35", anchor="nw", font=("Segoe UI", 8, "bold"))

        # Loading point
        ix, iy = map_xy(r.stage.initial_x, r.stage.initial_y)
        self.create_oval(ix - 6, iy - 6, ix + 6, iy + 6, fill="#111111", outline="")
        self.create_text(ix + 10, iy, text="ZERO", fill="#111111", anchor="w", font=("Segoe UI", 9, "bold"))

        lx, ly = map_xy(r.stage.load_x, r.stage.load_y)
        self.create_oval(lx - 7, ly - 7, lx + 7, ly + 7, fill="#B89146", outline="#FFFFFF", width=2)
        self.create_text(lx + 10, ly, text="LOAD", fill="#7A5A19", anchor="w", font=("Segoe UI", 9, "bold"))

        full_path: List[Tuple[float, float]] = [(r.stage.initial_x, r.stage.initial_y)]

        # Route to wafer loading
        loading_route: List[Tuple[float, float]] = [(r.stage.initial_x, r.stage.initial_y)]
        cur_x, cur_y = r.stage.initial_x, r.stage.initial_y
        for wp in r.to_loading_waypoints:
            axis = next(iter(wp)).upper()
            value = float(wp[next(iter(wp))])
            if axis == "X":
                cur_x = value
            elif axis == "Y":
                cur_y = value
            loading_route.append((cur_x, cur_y))
        if (cur_x, cur_y) != (r.stage.load_x, r.stage.load_y):
            loading_route.append((r.stage.load_x, r.stage.load_y))
        full_path.extend(loading_route[1:])
        self._draw_polyline(loading_route, map_xy, "#111111", "loading")

        # Route to exposure
        route: List[Tuple[float, float]] = [(r.stage.load_x, r.stage.load_y)]
        cur_x, cur_y = r.stage.load_x, r.stage.load_y
        for wp in r.to_exposure_waypoints:
            axis = next(iter(wp)).upper()
            value = float(wp[next(iter(wp))])
            if axis == "X":
                cur_x = value
            elif axis == "Y":
                cur_y = value
            route.append((cur_x, cur_y))
        full_path.extend(route[1:])
        self._draw_polyline(route, map_xy, "#3F6C9A", "to exposure")

        # Exposure grid and snake path
        planner = MotionPlanner(r)
        grid_points: List[Tuple[float, float, int]] = []
        for die, row, col, x, y in planner.exposure_positions():
            grid_points.append((x, y, die))
        for x, y, die in grid_points:
            px, py = map_xy(x, y)
            self.create_oval(px - 10, py - 10, px + 10, py + 10, outline="#2D8A55", width=2, fill="#F1FAF4")
            self.create_text(px, py, text=str(die), fill="#1F6A41", font=("Segoe UI", 9, "bold"))
        snake_points = [(x, y) for x, y, _die in grid_points]
        full_path.extend(snake_points)
        self._draw_polyline(snake_points, map_xy, "#2D8A55", "snake")

        # Return route, from final exposure point
        if grid_points:
            fx, fy, _ = grid_points[-1]
        else:
            fx, fy = r.exposure.start_x, r.exposure.start_y
        ret: List[Tuple[float, float]] = [(fx, fy)]
        cur_x, cur_y = fx, fy
        for wp in r.return_waypoints:
            axis = next(iter(wp)).upper()
            value = float(wp[next(iter(wp))])
            if axis == "X":
                cur_x = value
            elif axis == "Y":
                cur_y = value
            ret.append((cur_x, cur_y))
        full_path.extend(ret[1:])
        self._draw_polyline(ret, map_xy, "#B89146", "return")
        self._draw_motion_marker(full_path, map_xy)
        self._draw_live_marker(map_xy)

        # Legend
        legend = "black: to load   blue: to exposure   green: step-repeat   gold: return"
        self.create_text(w - 10, h - 10, text=legend, fill="#6F6A60", anchor="se", font=("Segoe UI", 9))

    def _draw_grid(self, x0: float, y0: float, x1: float, y1: float, count: int):
        if count <= 0:
            return
        for idx in range(1, count):
            tx = x0 + (x1 - x0) * idx / count
            ty = y0 + (y1 - y0) * idx / count
            self.create_line(tx, y0, tx, y1, fill="#F0ECE4")
            self.create_line(x0, ty, x1, ty, fill="#F0ECE4")

    def _draw_polyline(self, points: List[Tuple[float, float]], mapper, color: str, tag: str):
        if len(points) < 2:
            return
        mapped = [mapper(x, y) for x, y in points]
        flat = [v for p in mapped for v in p]
        self.create_line(*flat, fill=color, width=3, arrow=tk.LAST if tag != "snake" else tk.NONE, smooth=False)
        for i, (px, py) in enumerate(mapped):
            self.create_rectangle(px - 2, py - 2, px + 2, py + 2, fill=color, outline="")

    def _draw_motion_marker(self, points: List[Tuple[float, float]], mapper):
        if len(points) < 2:
            return

        segments: List[Tuple[Tuple[float, float], Tuple[float, float], float]] = []
        total = 0.0
        for a, b in zip(points, points[1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length <= 1e-9:
                continue
            segments.append((a, b, length))
            total += length
        if total <= 1e-9:
            return

        target = total * self.motion_phase
        walked = 0.0
        x, y = points[-1]
        for a, b, length in segments:
            if walked + length >= target:
                ratio = (target - walked) / length
                x = a[0] + (b[0] - a[0]) * ratio
                y = a[1] + (b[1] - a[1]) * ratio
                break
            walked += length

        px, py = mapper(x, y)
        pulse = 6 + 4 * abs(math.sin(self.motion_phase * math.tau))
        self.create_oval(px - pulse, py - pulse, px + pulse, py + pulse, outline="#D7B46A", width=2)
        self.create_oval(px - 5, py - 5, px + 5, py + 5, fill="#111111", outline="#FFFFFF", width=2)
        self.create_line(px - 13, py, px + 13, py, fill="#111111", width=1)
        self.create_line(px, py - 13, px, py + 13, fill="#111111", width=1)

    def _draw_live_marker(self, mapper):
        if self.live_position is None:
            return
        px, py = mapper(*self.live_position)
        ring = 13 + 8 * abs(math.sin(self.live_pulse * math.tau))
        self.create_oval(px - ring, py - ring, px + ring, py + ring, outline="#0071E3", width=3)
        self.create_oval(px - 7, py - 7, px + 7, py + 7, fill="#0071E3", outline="#FFFFFF", width=2)
        self.create_text(px + 14, py - 14, text="LIVE", fill="#0057B8", anchor="w", font=("Segoe UI", 8, "bold"))


def draw_rounded_rect(
    canvas: tk.Canvas,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radius: float,
    *,
    fill: str,
    outline: str = "",
    width: int = 1,
    tags: str = "",
):
    radius = min(radius, abs(x1 - x0) / 2, abs(y1 - y0) / 2)
    points = [
        x0 + radius, y0,
        x1 - radius, y0,
        x1, y0,
        x1, y0 + radius,
        x1, y1 - radius,
        x1, y1,
        x1 - radius, y1,
        x0 + radius, y1,
        x0, y1,
        x0, y1 - radius,
        x0, y0 + radius,
        x0, y0,
    ]
    return canvas.create_polygon(
        points,
        smooth=True,
        splinesteps=18,
        fill=fill,
        outline=outline,
        width=width,
        tags=tags,
    )


class RoundedContentFrame(tk.Frame):
    def __init__(self, outer: "RoundedCard", **kw):
        super().__init__(outer.canvas, **kw)
        self._rounded_outer = outer

    def pack(self, *args, **kwargs):
        return self._rounded_outer.pack(*args, **kwargs)

    def grid(self, *args, **kwargs):
        return self._rounded_outer.grid(*args, **kwargs)

    def place(self, *args, **kwargs):
        return self._rounded_outer.place(*args, **kwargs)

    def pack_forget(self):
        return self._rounded_outer.pack_forget()


class RoundedCard(tk.Frame):
    def __init__(
        self,
        master,
        *,
        card_bg: str = "#F9FAFB",
        outer_bg: str = "#EDEFF4",
        border: str = "#E2E5EA",
        shadow: str = "#DDE2EA",
        radius: int = 22,
        padding: int = 14,
    ):
        super().__init__(master, bg=outer_bg, highlightthickness=0, bd=0)
        self.card_bg = card_bg
        self.outer_bg = outer_bg
        self.border = border
        self.shadow = shadow
        self.radius = radius
        self.padding = padding
        self.hovered = False

        self.canvas = tk.Canvas(self, bg=outer_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.content = RoundedContentFrame(self, bg=card_bg, highlightthickness=0, bd=0)
        self.window_id = self.canvas.create_window(
            self.padding,
            self.padding,
            window=self.content,
            anchor="nw",
        )
        self.canvas.bind("<Configure>", self._redraw)
        self.content.bind("<Configure>", self._content_changed)
        self.content.bind("<Enter>", self._hover_on)
        self.content.bind("<Leave>", self._hover_off)

    def _content_changed(self, _event=None):
        req_h = max(80, self.content.winfo_reqheight() + self.padding * 2)
        self.canvas.configure(height=req_h)
        self._redraw()

    def _hover_on(self, _event=None):
        self.hovered = True
        self._redraw()

    def _hover_off(self, _event=None):
        self.hovered = False
        self._redraw()

    def _redraw(self, _event=None):
        w = max(80, self.canvas.winfo_width())
        h = max(70, self.canvas.winfo_height())
        self.canvas.delete("card")
        shadow_offset = 7 if self.hovered else 5
        shadow_fill = "#D2D9E5" if self.hovered else self.shadow
        shadow_id = draw_rounded_rect(
            self.canvas,
            5,
            7,
            w - 5,
            h - 3,
            self.radius,
            fill=shadow_fill,
            outline="",
            width=0,
            tags="card",
        )
        card_id = draw_rounded_rect(
            self.canvas,
            3,
            2,
            w - 7,
            h - shadow_offset,
            self.radius,
            fill=self.card_bg,
            outline=self.border,
            width=1,
            tags="card",
        )
        self.canvas.tag_lower(shadow_id, self.window_id)
        self.canvas.tag_lower(card_id, self.window_id)
        self.canvas.itemconfigure(self.window_id, width=max(40, w - self.padding * 2 - 8))


class PillButton(tk.Canvas):
    def __init__(
        self,
        master,
        text: str,
        command: Callable[[], None],
        *,
        variant: str = "primary",
        height: int = 42,
        bg: str = "#F9FAFB",
    ):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.text = text
        self.command = command
        self.variant = variant
        self.height = height
        self.hovered = False
        self.pressed = False
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._hover_on)
        self.bind("<Leave>", self._hover_off)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _palette(self) -> Tuple[str, str, str]:
        if self.variant == "danger":
            return "#FF3B30", "#FFFFFF", "#D70015"
        if self.variant == "soft":
            return "#F2F4F8", "#1D1D1F", "#E5E9F0"
        if self.variant == "blue":
            return "#0071E3", "#FFFFFF", "#0A84FF"
        return "#1D1D1F", "#FFFFFF", "#303033"

    def _hover_on(self, _event=None):
        self.hovered = True
        self._redraw()

    def _hover_off(self, _event=None):
        self.hovered = False
        self.pressed = False
        self._redraw()

    def _press(self, _event=None):
        self.pressed = True
        self._redraw()

    def _release(self, event=None):
        was_pressed = self.pressed
        self.pressed = False
        self._redraw()
        if was_pressed and event is not None:
            self.command()

    def _redraw(self, _event=None):
        self.delete("all")
        w = max(80, self.winfo_width())
        h = self.height
        fill, fg, active = self._palette()
        if self.pressed:
            fill = active
        elif self.hovered:
            fill = active if self.variant != "soft" else "#EBEEF4"
        draw_rounded_rect(self, 1, 1, w - 1, h - 1, h / 2, fill=fill, outline="", width=0)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=("Segoe UI Variable Display", 11, "bold"))


class PhotoStepperProApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Aurelith DUS-0001 Control Suite")
        self.geometry("1360x860")
        self.minsize(1180, 760)
        try:
            self.state("zoomed")
        except Exception:
            self.attributes("-fullscreen", True)

        self.recipe, self.recipe_path, _recipe_source = RecipeCodec.load_active(DEFAULT_RECIPE_PATH, USER_RECIPE_PATH)

        self.transport = SerialGcodeTransport(self.safe_log)
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None

        self.dry_run_var = tk.BooleanVar(value=True)
        self.running = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value="DRY RUN ACTIVE")
        self.status_var = tk.StringVar(value="Standby")
        self.machine_state_var = tk.StringVar(value="Idle")
        self.pos_x_var = tk.StringVar(value="0.000")
        self.pos_y_var = tk.StringVar(value="0.000")
        self.pos_z_var = tk.StringVar(value="0.000")
        self.feed_rate_var = tk.StringVar(value="0")
        self.jog_profile_var = tk.StringVar(value="XY 1.0 mm / Z 0.5 mm")

        # Preflight flags
        self.coord_confirmed_var = tk.BooleanVar(value=False)
        self.estop_confirmed_var = tk.BooleanVar(value=False)
        self.uv_safety_confirmed_var = tk.BooleanVar(value=False)

        self.fields: Dict[str, tk.Variable] = {}
        self.run_buttons: List[ttk.Button] = []
        self.jog_buttons: List[ttk.Button] = []
        self.process_step_buttons: List[ttk.Button] = []
        self.process_step_states: List[tk.StringVar] = []
        self.process_step_frames: List[ttk.Frame] = []
        self.current_process_step = 0
        self.view_frames: Dict[str, ttk.Frame] = {}
        self.photo_cache: Dict[str, tk.PhotoImage] = {}
        self.logo_refs: Dict[str, tk.PhotoImage] = {}
        self.splash_after_id: Optional[str] = None
        self.preview_after_id: Optional[str] = None
        self.live_preview_bound = False
        self.animation_phase = 0.0
        self.current_view_name = "splash"
        self.log_widgets: List[tk.Text] = []

        self._style()
        self._build_ui()
        self.load_recipe_to_ui(self.recipe)
        self.dry_run_var.trace_add("write", self._on_mode_change)
        self._on_mode_change()
        self.show_view("splash")
        self.splash_after_id = self.after(5000, lambda: self.show_view("home"))
        self.bind("<Button-1>", self._maybe_skip_splash)
        self.after(100, self.drain_log)
        self.after(120, self._animate_hud)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        bg = "#EDEFF4"
        panel = "#F9FAFB"
        panel_alt = "#E5E8EF"
        panel_soft = "#F6F7FA"
        fg = "#1D1D1F"
        muted = "#6E6E73"
        accent = "#0071E3"
        accent_active = "#0A84FF"
        line = "#D2D2D7"
        red = "#FF3B30"
        gold = "#A06A00"

        self.configure(bg=bg)
        style.configure(".", font=("Segoe UI Variable Display", 10), background=bg, foreground=fg, fieldbackground="#F8F9FB")
        style.configure("TFrame", background=bg)
        style.configure("Hero.TFrame", background=panel_alt)
        style.configure("Panel.TFrame", background=panel)
        style.configure("PanelSoft.TFrame", background=panel_soft)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Panel.TLabel", background=panel, foreground=fg)
        style.configure("PanelSoft.TLabel", background=panel_soft, foreground=fg)
        style.configure("HeroTitle.TLabel", background=panel_alt, foreground=fg, font=("Georgia", 24))
        style.configure("HeroSub.TLabel", background=panel_alt, foreground=muted, font=("Segoe UI Variable Display", 10))
        style.configure("Mode.TLabel", background=panel_alt, foreground=accent, font=("Segoe UI Variable Display", 10, "bold"))
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("PanelMuted.TLabel", background=panel, foreground=muted)
        style.configure("TEntry", fieldbackground="#F8F9FB", foreground=fg, bordercolor=line, lightcolor=line, darkcolor=line, insertcolor=fg)
        style.configure("TCombobox", fieldbackground="#F8F9FB", foreground=fg, bordercolor=line, lightcolor=line, darkcolor=line, arrowsize=14)
        style.configure("TButton", padding=9, background=panel_alt, foreground=fg, borderwidth=0, focusthickness=0)
        style.map("TButton", background=[("active", "#E8E8ED"), ("pressed", "#DADAE0")])
        style.configure("Accent.TButton", padding=11, background=accent, foreground="#FFFFFF", borderwidth=0, font=("Segoe UI Variable Display", 11, "bold"))
        style.map("Accent.TButton", background=[("active", accent_active), ("pressed", "#0066CC")])
        style.configure("Danger.TButton", padding=9, background=red, foreground="#FFFFFF", borderwidth=0, font=("Segoe UI Variable Display", 10, "bold"))
        style.map("Danger.TButton", background=[("active", "#FF6159"), ("pressed", "#D70015")])
        style.configure("Ghost.TButton", padding=9, background=panel, foreground=fg, borderwidth=0)
        style.map("Ghost.TButton", background=[("active", "#F2F2F7"), ("pressed", "#E5E5EA")])
        style.configure("Jog.TButton", padding=12, background="#F2F2F7", foreground=fg, borderwidth=0, font=("Segoe UI Variable Display", 12, "bold"))
        style.map("Jog.TButton", background=[("active", "#E5E5EA"), ("pressed", "#D1D1D6")])
        style.configure("Quick.TButton", padding=8, background="#E8F2FF", foreground="#0057B8", borderwidth=0, font=("Segoe UI Variable Display", 10, "bold"))
        style.map("Quick.TButton", background=[("active", "#D6E9FF"), ("pressed", "#C5DEFF")])
        style.configure("Menu.TButton", padding=16, background=fg, foreground="#FFFFFF", borderwidth=0, font=("Segoe UI Variable Display", 14, "bold"))
        style.map("Menu.TButton", background=[("active", "#333336"), ("pressed", "#000000")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("Panel.TCheckbutton", background=panel, foreground=fg)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), background=panel, foreground=muted, font=("Segoe UI Variable Display", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", panel_alt)], foreground=[("selected", fg)])
        style.configure("TSeparator", background=line)

    def _build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        hero = ttk.Frame(root, style="Hero.TFrame", padding=(18, 12))
        hero.pack(fill="x", pady=(0, 12))
        self.header_frame = hero

        title_row = ttk.Frame(hero, style="Hero.TFrame")
        title_row.pack(fill="x")
        title_block = ttk.Frame(title_row, style="Hero.TFrame")
        title_block.pack(side="left", fill="x", expand=True)
        ttk.Label(title_block, text="AURELITH", style="HeroTitle.TLabel").pack(side="left")
        ttk.Label(title_block, text="DUS-0001", style="HeroSub.TLabel").pack(side="left", padx=(18, 0), pady=(7, 0))

        badge_block = ttk.Frame(title_row, style="Hero.TFrame")
        badge_block.pack(side="right", anchor="center")
        ttk.Label(badge_block, textvariable=self.mode_var, style="Mode.TLabel").pack(anchor="e")
        ttk.Checkbutton(badge_block, text="DRY RUN", variable=self.dry_run_var).pack(anchor="e", pady=(8, 0))

        self.view_container = ttk.Frame(root)
        self.view_container.pack(fill="both", expand=True)

        for name in ("splash", "home", "process", "manual", "settings"):
            frame = ttk.Frame(self.view_container)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.view_frames[name] = frame

        self._build_splash_view(self.view_frames["splash"])
        self._build_home_view(self.view_frames["home"])
        self._build_process_view(self.view_frames["process"])
        self._build_manual_view(self.view_frames["manual"])
        self._build_settings_view(self.view_frames["settings"])

    def _on_mode_change(self, *_args):
        self.mode_var.set("DRY RUN ACTIVE" if self.dry_run_var.get() else "LIVE MOTION ARMED")

    def _panel(self, parent, title: str) -> ttk.Frame:
        card = RoundedCard(parent, card_bg="#F9FAFB", outer_bg="#EDEFF4", radius=24, padding=16)
        p = card.content
        ttk.Label(p, text=title, style="Panel.TLabel", font=("Segoe UI Variable Display", 12, "bold")).pack(anchor="w", pady=(0, 8))
        return p

    def _rounded_content(self, parent, *, padding: int = 16, radius: int = 24, soft: bool = False) -> tk.Frame:
        card = RoundedCard(
            parent,
            card_bg="#FFFFFF" if not soft else "#F6F7FA",
            outer_bg="#EDEFF4",
            border="#E1E5EC",
            shadow="#DDE3ED",
            radius=radius,
            padding=padding,
        )
        return card.content

    def _pill_button(self, parent, text: str, command: Callable[[], None], *, variant: str = "primary") -> PillButton:
        button = PillButton(parent, text, command, variant=variant, bg=parent["bg"] if isinstance(parent, tk.Frame) else "#F9FAFB")
        button.pack(fill="x")
        return button

    def _show_logo(self, parent, filename: str, max_width: int, max_height: int, bg: str) -> tk.Label:
        path = asset_path(filename)
        if not path.exists():
            return tk.Label(parent, text=filename, bg=bg, fg="#F7FBFF", font=("Bahnschrift", 12))

        cache_key = f"{filename}:{max_width}x{max_height}"
        if cache_key not in self.photo_cache:
            image = tk.PhotoImage(file=str(path))
            width = max(1, image.width())
            height = max(1, image.height())
            scale = max(width / max_width, height / max_height, 1.0)
            if scale > 1.0:
                factor = max(1, math.ceil(scale))
                image = image.subsample(factor, factor)
            self.photo_cache[cache_key] = image

        label = tk.Label(parent, image=self.photo_cache[cache_key], bg=bg, bd=0, highlightthickness=0)
        label.image = self.photo_cache[cache_key]
        return label

    def _build_view_header(self, parent, title: str, subtitle: str, back_target: Optional[str] = "home") -> ttk.Frame:
        wrap = self._rounded_content(parent, padding=16, radius=24)
        wrap.pack(fill="x", pady=(0, 12))
        title_row = tk.Frame(wrap, bg=wrap["bg"], highlightthickness=0, bd=0)
        title_row.pack(fill="x")
        text_col = tk.Frame(title_row, bg=wrap["bg"], highlightthickness=0, bd=0)
        text_col.pack(side="left", fill="x", expand=True)
        ttk.Label(text_col, text=title, style="Panel.TLabel", font=("Segoe UI Variable Display", 18, "bold")).pack(anchor="w")
        ttk.Label(text_col, text=subtitle, style="PanelMuted.TLabel").pack(anchor="w", pady=(2, 0))
        action_col = tk.Frame(title_row, bg=wrap["bg"], highlightthickness=0, bd=0)
        action_col.pack(side="right")
        if back_target is not None:
            ttk.Button(action_col, text="Back", command=lambda target=back_target: self.show_view(target), style="Ghost.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(action_col, text="Settings", command=lambda: self.show_view("settings"), style="Quick.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(action_col, text="Manual", command=lambda: self.show_view("manual"), style="Quick.TButton").pack(side="left")
        return wrap

    def _scroll_body(self, parent, padding: int = 24) -> ttk.Frame:
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg="#EDEFF4", highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = ttk.Frame(canvas, padding=padding)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event):
            canvas.itemconfigure(window_id, width=event.width)

        def _on_wheel(event):
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")

        body.bind("<Configure>", _sync_scroll_region)
        canvas.bind("<Configure>", _sync_width)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        return body

    def _build_splash_view(self, parent):
        parent.configure(style="Hero.TFrame")
        splash_bg = tk.Frame(parent, bg="#FFFFFF")
        splash_bg.pack(fill="both", expand=True)
        self.splash_canvas = tk.Canvas(splash_bg, bg="#FFFFFF", highlightthickness=0, bd=0)
        self.splash_canvas.pack(fill="both", expand=True)
        self.splash_canvas.bind("<Configure>", self._render_splash)

    def _build_home_view(self, parent):
        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, padx=40, pady=20)
        hero = self._rounded_content(body, padding=30, radius=28)
        hero.pack(fill="x", pady=(10, 18))
        logo = self._show_logo(hero, "wordmark_logo.png", 760, 200, "#F9FAFB")
        logo.pack(anchor="w")
        ttk.Label(hero, text="DUS-0001", style="PanelMuted.TLabel", font=("Segoe UI Variable Display", 13, "bold")).pack(anchor="w", pady=(12, 0))

        menu_row = ttk.Frame(body)
        menu_row.pack(fill="x", expand=True)
        menu_row.columnconfigure((0, 1, 2), weight=1)

        self._build_menu_card(
            menu_row,
            0,
            "START PROCESS",
            "LOAD  ROUTE  EXPOSE  RETURN",
            lambda: self.show_view("process"),
        )
        self._build_menu_card(
            menu_row,
            1,
            "MANUAL CONTROL",
            "JOG  HOLD  STATUS  ZERO",
            lambda: self.show_view("manual"),
        )
        self._build_menu_card(
            menu_row,
            2,
            "SETTINGS",
            "RECIPE  MOTION  LIMITS",
            lambda: self.show_view("settings"),
        )

    def _build_menu_card(self, parent, col: int, title: str, desc: str, command: Callable[[], None]):
        card = self._rounded_content(parent, padding=20, radius=24, soft=True)
        card.grid(row=0, column=col, sticky="nsew", padx=8)
        ttk.Label(card, text=title, style="PanelSoft.TLabel", font=("Segoe UI Variable Display", 16, "bold")).pack(anchor="w")
        ttk.Label(card, text=desc, style="PanelMuted.TLabel", wraplength=280).pack(anchor="w", pady=(10, 16))
        self._pill_button(card, "Enter", command, variant="primary")

    def _build_process_view(self, parent):
        body = self._scroll_body(parent, padding=24)
        self._build_view_header(body, "Process Sequence", "DUS-0001")

        content = ttk.Frame(body)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        left = ttk.Frame(content)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ttk.Frame(content)
        right.grid(row=0, column=1, sticky="nsew")

        self._process_sequence_panel(left)

        tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True)

        control_tab = ttk.Frame(tabs, padding=12)
        route_tab = ttk.Frame(tabs, padding=12)
        tabs.add(control_tab, text="Control")
        tabs.add(route_tab, text="Route & Safety")

        self._telemetry_panel(control_tab)
        self._transport_panel(control_tab)
        self._jog_panel(control_tab)
        self._log_only_panel(control_tab, "Log", height=12)

        self._top_down_route_panel(route_tab)
        self._preflight_panel(route_tab)
        self._route_actions_panel(route_tab)

    def _build_manual_view(self, parent):
        body = ttk.Frame(parent, padding=24)
        body.pack(fill="both", expand=True)
        self._build_view_header(body, "Manual Control", "DUS-0001")

        content = ttk.Frame(body)
        content.pack(fill="both", expand=True)
        left = ttk.Frame(content)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(content)
        right.pack(side="left", fill="both", expand=True)

        self._connection_panel(left)
        self._transport_panel(left)
        self._jog_panel(left)
        self._preflight_panel(left)
        self._log_only_panel(right, "Operator Log")

    def _build_settings_view(self, parent):
        body = ttk.Frame(parent, padding=24)
        body.pack(fill="both", expand=True)
        self._build_view_header(body, "Settings", "DUS-0001")

        top = ttk.Frame(body)
        top.pack(fill="x", pady=(0, 10))
        self._connection_panel(top)

        center = ttk.Frame(body)
        center.pack(fill="both", expand=True)
        self._settings_tabs(center)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Save settings", command=self.save_recipe_from_ui, style="Accent.TButton").pack(side="left")
        ttk.Button(actions, text="Load recipe", command=self.load_recipe_dialog, style="Ghost.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Back to Home", command=lambda: self.show_view("home"), style="Quick.TButton").pack(side="right")

    def _process_sequence_panel(self, parent):
        panel = self._panel(parent, "Ordered Flow")
        panel.pack(fill="both", expand=True)

        steps = [
            ("load", "01  Load Position", "Move to the wafer loading position and wait for operator placement."),
            ("mount", "02  Route To Exposure", "Lower to process height and follow the detour path safely."),
            ("grid", "03  Step Repeat", "Run the 3x3 snake order exposure routine."),
            ("return", "04  Return To Load", "Return through the safe route and raise Z for unload."),
        ]
        for idx, (plan_name, title, desc) in enumerate(steps):
            card = self._rounded_content(panel, padding=16, radius=22, soft=True)
            card.pack(fill="x", pady=6)
            self.process_step_frames.append(card)
            head = tk.Frame(card, bg=card["bg"], highlightthickness=0, bd=0)
            head.pack(fill="x")
            ttk.Label(head, text=title, style="PanelSoft.TLabel", font=("Bahnschrift SemiBold", 14)).pack(side="left")
            state_var = tk.StringVar(value="READY" if idx == 0 else "LOCKED")
            self.process_step_states.append(state_var)
            ttk.Label(head, textvariable=state_var, style="PanelMuted.TLabel", font=("Bahnschrift SemiBold", 10)).pack(side="right")
            ttk.Label(card, text=desc, style="PanelMuted.TLabel", wraplength=520).pack(anchor="w", pady=(8, 12))
            b = ttk.Button(card, text=f"Execute {title}", command=lambda i=idx, name=plan_name: self.run_process_step(i, name), style="Accent.TButton" if idx == 0 else "Ghost.TButton")
            if idx != 0:
                b.configure(state="disabled")
            b.pack(fill="x")
            self.process_step_buttons.append(b)

        quick = tk.Frame(panel, bg=panel["bg"], highlightthickness=0, bd=0)
        quick.pack(fill="x", pady=(14, 0))
        ttk.Button(quick, text="Reset Sequence", command=self.reset_process_sequence, style="Ghost.TButton").pack(side="left")
        ttk.Button(quick, text="One Click Full Cycle", command=lambda: self.run_plan("full"), style="Quick.TButton").pack(side="right")

    def show_view(self, name: str):
        if name == "home" and self.splash_after_id is not None:
            try:
                self.after_cancel(self.splash_after_id)
            except Exception:
                pass
            self.splash_after_id = None
        if name == "splash":
            self.header_frame.pack_forget()
        elif not self.header_frame.winfo_manager():
            self.header_frame.pack(fill="x", pady=(0, 12), before=self.view_container)
        frame = self.view_frames[name]
        self.current_view_name = name
        frame.tkraise()

    def _maybe_skip_splash(self, _event):
        if self.current_view_name == "splash":
            self.show_view("home")

    def _render_splash(self, event):
        canvas = self.splash_canvas
        canvas.delete("all")
        w = max(1, event.width)
        h = max(1, event.height)
        canvas.create_rectangle(0, 0, w, h, fill="#F7F8FA", outline="")
        glow = 18 + 10 * math.sin(self.animation_phase * math.tau)
        canvas.create_oval(
            w * 0.5 - 420 - glow,
            h * 0.5 - 260 - glow,
            w * 0.5 + 420 + glow,
            h * 0.5 + 260 + glow,
            fill="#EEF2F7",
            outline="",
        )
        img = self._show_logo(canvas, "splash_logo.png", int(w * 0.92), int(h * 0.92), "#FFFFFF")
        image = img.image if hasattr(img, "image") else None
        if image is not None:
            canvas.create_image(w // 2, int(h * 0.47), image=image)
            self.logo_refs["splash"] = image

        bar_w = min(520, int(w * 0.36))
        bar_h = 6
        bar_x0 = (w - bar_w) / 2
        bar_y0 = int(h * 0.82)
        progress = 0.18 + 0.72 * ((math.sin((self.animation_phase * math.tau) - math.pi / 2) + 1) / 2)
        draw_rounded_rect(
            canvas,
            bar_x0,
            bar_y0,
            bar_x0 + bar_w,
            bar_y0 + bar_h,
            4,
            fill="#E5E8EF",
            outline="",
            width=0,
        )
        draw_rounded_rect(
            canvas,
            bar_x0,
            bar_y0,
            bar_x0 + bar_w * progress,
            bar_y0 + bar_h,
            4,
            fill="#1D1D1F",
            outline="",
            width=0,
        )
        dot_y = bar_y0 + 28
        for idx in range(3):
            phase = (self.animation_phase + idx * 0.16) % 1.0
            alpha_size = 3 + 3 * ((math.sin(phase * math.tau) + 1) / 2)
            dot_x = w / 2 - 18 + idx * 18
            canvas.create_oval(dot_x - alpha_size, dot_y - alpha_size, dot_x + alpha_size, dot_y + alpha_size, fill="#1D1D1F", outline="")

    def _animate_hud(self):
        self.animation_phase = (self.animation_phase + 0.025) % 1.0
        if hasattr(self, "splash_canvas") and self.view_frames["splash"].winfo_ismapped():
            self._render_splash(type("Event", (), {"width": self.splash_canvas.winfo_width(), "height": self.splash_canvas.winfo_height()})())
        self.after(80, self._animate_hud)

    def _field(self, parent, key: str, label: str, width: int = 12, panel: bool = True):
        row = ttk.Frame(parent, style="Panel.TFrame" if panel else "TFrame")
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=18, style="Panel.TLabel" if panel else "TLabel").pack(side="left")
        var = self.fields.get(key)
        if var is None:
            var = tk.StringVar()
            self.fields[key] = var
        entry = ttk.Entry(row, textvariable=var, width=width)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _bool_field(self, parent, key: str, text: str, panel: bool = True):
        var = tk.BooleanVar()
        ttk.Checkbutton(
            parent,
            text=text,
            variable=var,
            style="Panel.TCheckbutton" if panel else "TCheckbutton",
        ).pack(anchor="w", pady=2)
        self.fields[key] = var
        return var

    def _connection_panel(self, parent):
        p = self._panel(parent, "Connection")
        p.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(p, style="Panel.TFrame")
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Port", width=18, style="Panel.TLabel").pack(side="left")
        if "serial.port" not in self.fields:
            self.fields["serial.port"] = tk.StringVar()
        self.port_combo = ttk.Combobox(row, textvariable=self.fields["serial.port"], width=16)
        self.port_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="↻", width=3, command=self.refresh_ports).pack(side="left", padx=(4, 0))
        self._field(p, "serial.baud", "Baud")

        btns = ttk.Frame(p, style="Panel.TFrame")
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Connect", command=self.connect).pack(side="left", fill="x", expand=True)
        ttk.Button(btns, text="Disconnect", command=self.disconnect).pack(side="left", fill="x", expand=True, padx=(5, 0))

        home_row = ttk.Frame(p, style="Panel.TFrame")
        home_row.pack(fill="x", pady=(6, 0))
        ttk.Button(home_row, text="Home $H", command=self.home_machine, style="Quick.TButton").pack(side="left", fill="x", expand=True)
        ttk.Button(home_row, text="Unlock $X", command=self.unlock_machine, style="Ghost.TButton").pack(side="left", fill="x", expand=True, padx=(5, 0))

        zero_row = ttk.Frame(p, style="Panel.TFrame")
        zero_row.pack(fill="x", pady=(6, 0))
        ttk.Button(zero_row, text="Set Zero G92", command=self.set_current_zero, style="Accent.TButton").pack(side="left", fill="x", expand=True)

        self.refresh_ports()

    def _transport_panel(self, parent):
        p = self._panel(parent, "Live Control")
        p.pack(fill="x", pady=(0, 10))

        row = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        row.pack(fill="x")
        ttk.Button(row, text="HOLD !", command=self.feed_hold, style="Danger.TButton").pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="RESUME ~", command=self.resume, style="Ghost.TButton").pack(side="left", expand=True, fill="x", padx=(5, 0))
        ttk.Button(row, text="RESET", command=self.soft_reset, style="Ghost.TButton").pack(side="left", expand=True, fill="x", padx=(5, 0))

        row2 = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="STATUS ?", command=self.query_status, style="Quick.TButton").pack(side="left", expand=True, fill="x")
        ttk.Button(row2, text="SET ZERO", command=self.set_current_zero, style="Quick.TButton").pack(side="left", expand=True, fill="x", padx=(5, 0))

    def _telemetry_panel(self, parent):
        p = self._panel(parent, "Current Position")
        p.pack(fill="x", pady=(0, 10))

        top = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        top.pack(fill="x")
        ttk.Label(top, text="State", style="PanelMuted.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.machine_state_var, style="Panel.TLabel", font=("Segoe UI Variable Display", 16, "bold")).pack(side="right")

        grid = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        grid.pack(fill="x", pady=(10, 4))
        for col in range(3):
            grid.columnconfigure(col, weight=1)
        for col, (axis, var) in enumerate((("X", self.pos_x_var), ("Y", self.pos_y_var), ("Z", self.pos_z_var))):
            cell = self._rounded_content(grid, padding=10, radius=18, soft=True)
            cell.grid(row=0, column=col, sticky="nsew", padx=3)
            ttk.Label(cell, text=axis, style="PanelMuted.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=var, style="PanelSoft.TLabel", font=("Segoe UI Variable Display", 15, "bold")).pack(anchor="w")

        footer = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, text="Feed", style="PanelMuted.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.feed_rate_var, style="Panel.TLabel", font=("Segoe UI Variable Display", 11, "bold")).pack(side="left", padx=(6, 18))
        ttk.Label(footer, text="Jog", style="PanelMuted.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.jog_profile_var, style="Panel.TLabel", font=("Segoe UI Variable Display", 11, "bold")).pack(side="left", padx=(6, 0))

    def _jog_panel(self, parent):
        p = self._panel(parent, "Manual Jog")
        p.pack(fill="x", pady=(0, 10))

        ttk.Label(p, text="Step/feed values are managed in Settings.", style="PanelMuted.TLabel").pack(anchor="w")

        jog_grid = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        jog_grid.pack(fill="x", pady=(10, 4))
        for col in range(3):
            jog_grid.columnconfigure(col, weight=1)

        buttons = [
            ("Y+", 0, 1, lambda: self.jog_axis("Y", +1)),
            ("X-", 1, 0, lambda: self.jog_axis("X", -1)),
            ("JOG STOP", 1, 1, self.jog_cancel, "Danger.TButton"),
            ("X+", 1, 2, lambda: self.jog_axis("X", +1)),
            ("Y-", 2, 1, lambda: self.jog_axis("Y", -1)),
            ("Z+", 0, 2, lambda: self.jog_axis("Z", +1)),
            ("Z-", 2, 2, lambda: self.jog_axis("Z", -1)),
        ]
        for item in buttons:
            if len(item) == 5:
                text, row_idx, col_idx, cmd, style_name = item
            else:
                text, row_idx, col_idx, cmd = item
                style_name = "Jog.TButton"
            b = ttk.Button(jog_grid, text=text, command=cmd, style=style_name)
            b.grid(row=row_idx, column=col_idx, sticky="nsew", padx=3, pady=3)
            self.jog_buttons.append(b)

        quick_row = tk.Frame(p, bg=p["bg"], highlightthickness=0, bd=0)
        quick_row.pack(fill="x", pady=(8, 0))
        b = ttk.Button(quick_row, text="Status ?", command=self.query_status, style="Quick.TButton")
        b.pack(side="left", fill="x", expand=True)
        self.jog_buttons.append(b)
        b = ttk.Button(quick_row, text="Go Load", command=lambda: self.run_plan("load"), style="Quick.TButton")
        b.pack(side="left", fill="x", expand=True, padx=(5, 0))
        self.jog_buttons.append(b)

        ttk.Label(
            p,
            text="Jog uses GRBL-style $J incremental moves. Use after homing and coordinate check.",
            style="PanelMuted.TLabel",
            wraplength=250,
        ).pack(anchor="w", pady=(8, 0))

    def _preflight_panel(self, parent):
        p = self._panel(parent, "Preflight")
        p.pack(fill="x", pady=(0, 10))

        ttk.Checkbutton(
            p,
            text="Coordinate zero confirmed",
            variable=self.coord_confirmed_var,
            style="Panel.TCheckbutton",
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            p,
            text="Limits and E-stop confirmed",
            variable=self.estop_confirmed_var,
            style="Panel.TCheckbutton",
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            p,
            text="UV shield and interlock confirmed",
            variable=self.uv_safety_confirmed_var,
            style="Panel.TCheckbutton",
        ).pack(anchor="w", pady=2)

        ttk.Label(
            p,
            text="Required for live motion. Dry run remains available.",
            style="PanelMuted.TLabel",
            wraplength=250,
        ).pack(anchor="w", pady=(6, 0))

    def _run_panel(self, parent):
        p = self._panel(parent, "Sequence")
        p.pack(fill="x", pady=(0, 10))

        self.status_var = tk.StringVar(value="Standby")
        ttk.Label(p, textvariable=self.status_var, style="Panel.TLabel", font=("Bahnschrift SemiBold", 12)).pack(anchor="w", pady=(0, 8))

        for text, plan_name in [
            ("01  Load Position", "load"),
            ("02  Route To Exposure", "mount"),
            ("03  Step Repeat", "grid"),
            ("04  Return To Load", "return"),
            ("Full Cycle", "full"),
        ]:
            style_name = "Accent.TButton" if plan_name == "full" else "TButton"
            b = ttk.Button(p, text=text, command=lambda name=plan_name: self.run_plan(name), style=style_name)
            b.pack(fill="x", pady=3)
            self.run_buttons.append(b)

        ctrl = ttk.Frame(p, style="Panel.TFrame")
        ctrl.pack(fill="x", pady=(8, 0))
        ttk.Button(ctrl, text="Hold !", command=self.feed_hold, style="Danger.TButton").pack(side="left", expand=True, fill="x")
        ttk.Button(ctrl, text="Resume ~", command=self.resume, style="Ghost.TButton").pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(ctrl, text="Reset", command=self.soft_reset, style="Ghost.TButton").pack(side="left", expand=True, fill="x")

        row = ttk.Frame(p, style="Panel.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Check plan", command=self.check_plan, style="Quick.TButton").pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Export G-code", command=self.export_gcode, style="Quick.TButton").pack(side="left", expand=True, fill="x", padx=(5, 0))

        row2 = ttk.Frame(p, style="Panel.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="Save settings", command=self.save_recipe_from_ui, style="Ghost.TButton").pack(side="left", expand=True, fill="x")
        ttk.Button(row2, text="Load recipe", command=self.load_recipe_dialog, style="Ghost.TButton").pack(side="left", expand=True, fill="x", padx=(5, 0))

    def _settings_tabs(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)

        motion = ttk.Frame(nb, padding=10)
        stage = ttk.Frame(nb, padding=10)
        route = ttk.Frame(nb, padding=10)
        io = ttk.Frame(nb, padding=10)

        nb.add(motion, text="Motion / Limits")
        nb.add(stage, text="Stage / Grid")
        nb.add(route, text="Routes")
        nb.add(io, text="I/O / UV")

        # Motion tab
        self._field(motion, "motion.feed_xy", "XY feed", panel=False)
        self._field(motion, "motion.feed_z", "Z feed", panel=False)
        self._bool_field(motion, "motion.wait_idle", "Wait until Idle after motion", panel=False)
        self._field(motion, "motion.idle_timeout_s", "Idle timeout s", panel=False)
        self._field(motion, "motion.command_timeout_s", "Command timeout s", panel=False)

        sep = ttk.Separator(motion)
        sep.pack(fill="x", pady=12)
        ttk.Label(motion, text="Manual jog profile", font=("Bahnschrift SemiBold", 11)).pack(anchor="w")
        self._field(motion, "jog.xy_step_mm", "Jog XY step mm", panel=False)
        self._field(motion, "jog.z_step_mm", "Jog Z step mm", panel=False)
        self._field(motion, "jog.feed_xy", "Jog XY feed", panel=False)
        self._field(motion, "jog.feed_z", "Jog Z feed", panel=False)

        sep = ttk.Separator(motion)
        sep.pack(fill="x", pady=12)
        ttk.Label(motion, text="Soft travel range", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        for key, label in [
            ("limits.x_min", "X min"), ("limits.x_max", "X max"),
            ("limits.y_min", "Y min"), ("limits.y_max", "Y max"),
            ("limits.z_min", "Z min"), ("limits.z_max", "Z max"),
        ]:
            self._field(motion, key, label, panel=False)

        sep2 = ttk.Separator(motion)
        sep2.pack(fill="x", pady=12)
        ttk.Label(motion, text="Optional keepout zone", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self._bool_field(motion, "keepout.enabled", "Enable keepout warning", panel=False)
        self._bool_field(motion, "keepout.block_on_violation", "Block run if path crosses keepout", panel=False)
        for key, label in [
            ("keepout.x_min", "Keepout X min"), ("keepout.x_max", "Keepout X max"),
            ("keepout.y_min", "Keepout Y min"), ("keepout.y_max", "Keepout Y max"),
        ]:
            self._field(motion, key, label, panel=False)

        # Stage tab
        ttk.Label(stage, text="Wafer loading stage", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        for key, label in [
            ("stage.initial_x", "Initial X"),
            ("stage.initial_y", "Initial Y"),
            ("stage.initial_z", "Initial Z"),
        ]:
            self._field(stage, key, label, panel=False)
        self._bool_field(stage, "stage.invert_z_output", "Invert Z output in G-code", panel=False)
        ttk.Label(
            stage,
            text="The machine starts from the Initial coordinate. The wafer Load position is calculated from the last coordinate in To loading route.",
            style="Muted.TLabel",
            wraplength=480,
        ).pack(anchor="w", pady=(4, 10))
        self._field(stage, "stage.lowered_z", "Lowered Z", panel=False)

        ttk.Separator(stage).pack(fill="x", pady=12)
        ttk.Label(stage, text="Exposure grid", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        for key, label in [
            ("exposure.start_x", "Start X"),
            ("exposure.start_y", "Start Y"),
            ("exposure.cols", "Cols"),
            ("exposure.rows", "Rows"),
            ("exposure.pitch_x", "Pitch X mm"),
            ("exposure.pitch_y", "Pitch Y mm"),
            ("exposure.exposure_time_s", "Exposure time s"),
        ]:
            self._field(stage, key, label, panel=False)
        ttk.Label(
            stage,
            text="1 cm pitch = 10 mm. Use a negative Pitch Y value to run the grid in the opposite Y direction.",
            style="Muted.TLabel",
            wraplength=480,
        ).pack(anchor="w", pady=(8, 0))

        # Route tab
        ttk.Label(route, text="To loading route: one axis per line", font=("Segoe UI Variable Display", 11, "bold")).pack(anchor="w")
        self.load_text = tk.Text(route, height=7, bg="#F8F9FB", fg="#111111", insertbackground="#111111", relief="flat", padx=12, pady=10)
        self.load_text.pack(fill="both", expand=True, pady=(4, 10))

        ttk.Label(route, text="To exposure route: one axis per line", font=("Segoe UI Variable Display", 11, "bold")).pack(anchor="w")
        self.to_text = tk.Text(route, height=8, bg="#F8F9FB", fg="#111111", insertbackground="#111111", relief="flat", padx=12, pady=10)
        self.to_text.pack(fill="both", expand=True, pady=(4, 10))

        ttk.Label(route, text="Return to loading route: one axis per line", font=("Segoe UI Variable Display", 11, "bold")).pack(anchor="w")
        self.return_text = tk.Text(route, height=8, bg="#F8F9FB", fg="#111111", insertbackground="#111111", relief="flat", padx=12, pady=10)
        self.return_text.pack(fill="both", expand=True, pady=(4, 8))
        ttk.Label(
            route,
            text="Example: X=250, Y=130, X=200. XY combined moves are blocked by the parser and planner.",
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w")

        # IO tab
        self._bool_field(io, "io.uv_enabled", "Enable UV/LED G-code output", panel=False)
        self._field(io, "io.uv_on_gcode", "UV ON command", width=24, panel=False)
        self._field(io, "io.uv_off_gcode", "UV OFF command", width=24, panel=False)
        ttk.Label(
            io,
            text="UV output is off by default. Enable only with shielding, interlock, and E-stop verified.",
            style="Muted.TLabel",
            wraplength=500,
        ).pack(anchor="w", pady=(8, 0))

    def _preview_and_log(self, parent):
        p = ttk.Frame(parent)
        p.pack(fill="both", expand=True)

        preview_box = ttk.Frame(p, style="Panel.TFrame", padding=12)
        preview_box.pack(fill="both", expand=True, pady=(0, 10))
        ttk.Label(preview_box, text="Top down route", style="Panel.TLabel", font=("Segoe UI Variable Display", 12, "bold")).pack(anchor="w", pady=(0, 8))
        self.preview = PreviewCanvas(preview_box, height=280)
        self.preview.pack(fill="both", expand=True)

        log_box = ttk.Frame(p, style="Panel.TFrame", padding=12)
        log_box.pack(fill="both", expand=True)
        ttk.Label(log_box, text="Log", style="Panel.TLabel", font=("Bahnschrift SemiBold", 12)).pack(anchor="w", pady=(0, 8))
        self.log = tk.Text(
            log_box,
            height=11,
            bg="#FFFFFF",
            fg="#111111",
            insertbackground="#111111",
            relief="flat",
            padx=10,
            pady=10,
            font=("Consolas", 10),
        )
        self.log.pack(fill="both", expand=True)
        self.log.insert("end", "Start in DRY RUN. Check plan before real motion.\n")
        self.log_widgets.append(self.log)

    def _top_down_route_panel(self, parent):
        preview_box = self._panel(parent, "Top down route")
        preview_box.pack(fill="both", expand=True)
        self.preview = PreviewCanvas(preview_box, height=500)
        self.preview.pack(fill="both", expand=True)
        ttk.Label(
            preview_box,
            text="Route updates from Settings. The animated head shows the planned path, not live encoder feedback.",
            style="PanelMuted.TLabel",
            wraplength=720,
        ).pack(anchor="w", pady=(8, 0))

    def _route_actions_panel(self, parent):
        p = self._panel(parent, "Plan")
        p.pack(fill="x", pady=(0, 10))
        ttk.Button(p, text="Save settings", command=self.save_recipe_from_ui, style="Accent.TButton").pack(fill="x", pady=(0, 6))
        ttk.Button(p, text="Check plan", command=self.check_plan, style="Accent.TButton").pack(fill="x", pady=(0, 6))
        ttk.Button(p, text="Export G-code", command=self.export_gcode, style="Quick.TButton").pack(fill="x")
        ttk.Label(
            p,
            text="Use this before live motion. It verifies soft limits, keepout, and one-axis movement.",
            style="PanelMuted.TLabel",
            wraplength=260,
        ).pack(anchor="w", pady=(8, 0))

    def _log_only_panel(self, parent, title: str, height: int = 20):
        box = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=title, style="Panel.TLabel", font=("Segoe UI Variable Display", 12, "bold")).pack(anchor="w", pady=(0, 8))
        text = tk.Text(
            box,
            height=height,
            bg="#FFFFFF",
            fg="#111111",
            insertbackground="#111111",
            relief="flat",
            padx=10,
            pady=10,
            font=("Consolas", 10),
        )
        text.pack(fill="both", expand=True)
        text.insert("end", "Manual operations and transport messages appear here.\n")
        self.log_widgets.append(text)

    def refresh_ports(self):
        ports = []
        if list_ports is not None:
            try:
                ports = [p.device for p in list_ports.comports()]
            except Exception:
                ports = []
        elif LIST_PORTS_IMPORT_ERROR:
            self.safe_log(f"[WARN] serial port scan unavailable: {LIST_PORTS_IMPORT_ERROR}")
        self.port_combo["values"] = ports

    def safe_log(self, text: str):
        text = str(text)
        status_match = re.search(r"(<[^>]+>)", text)
        if status_match and hasattr(self, "machine_state_var"):
            self.after(0, lambda s=status_match.group(1): self._apply_status_report(s))
        self.log_queue.put(text)

    def drain_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                for widget in self.log_widgets:
                    widget.insert("end", msg + "\n")
                    widget.see("end")
        except queue.Empty:
            pass
        self.after(100, self.drain_log)

    def set_status(self, text: str):
        self.after(0, self.status_var.set, text)
        if text.startswith("Running"):
            self.after(0, self.machine_state_var.set, "Running")
        elif text in {"Standby", "Complete"}:
            self.after(0, self.machine_state_var.set, "Idle")
        elif text == "Fault":
            self.after(0, self.machine_state_var.set, "Fault")

    def _refresh_telemetry_from_recipe(self, r: Recipe):
        self.pos_x_var.set(f"{r.stage.initial_x:.3f}")
        self.pos_y_var.set(f"{r.stage.initial_y:.3f}")
        self.pos_z_var.set(f"{r.stage.initial_z:.3f}")
        self.feed_rate_var.set(f"{r.motion.feed_xy:g} XY / {r.motion.feed_z:g} Z")
        self.jog_profile_var.set(f"XY {r.jog.xy_step_mm:g} mm / Z {r.jog.z_step_mm:g} mm")
        if hasattr(self, "preview"):
            self.preview.set_live_position(r.stage.initial_x, r.stage.initial_y)

    def _apply_planned_position(self, cmd: PlannedCommand):
        if cmd.x is not None:
            self.after(0, self.pos_x_var.set, f"{cmd.x:.3f}")
        if cmd.y is not None:
            self.after(0, self.pos_y_var.set, f"{cmd.y:.3f}")
        if cmd.z is not None:
            self.after(0, self.pos_z_var.set, f"{cmd.z:.3f}")
        if hasattr(self, "preview") and cmd.x is not None and cmd.y is not None:
            self.after(0, self.preview.set_live_position, cmd.x, cmd.y)

    def _apply_status_report(self, status: str):
        match = re.match(r"<([^|>]+)", status)
        if match:
            self.machine_state_var.set(match.group(1))
        pos_match = re.search(r"(?:MPos|WPos):([-+0-9.]+),([-+0-9.]+),([-+0-9.]+)", status)
        if pos_match:
            x = float(pos_match.group(1))
            y = float(pos_match.group(2))
            self.pos_x_var.set(f"{x:.3f}")
            self.pos_y_var.set(f"{y:.3f}")
            self.pos_z_var.set(f"{float(pos_match.group(3)):.3f}")
            if hasattr(self, "preview"):
                self.preview.set_live_position(x, y)
        fs_match = re.search(r"FS:([-+0-9.]+),([-+0-9.]+)", status)
        if fs_match:
            self.feed_rate_var.set(f"{float(fs_match.group(1)):g}")

    def _wire_live_preview_bindings(self):
        if self.live_preview_bound:
            return
        self.live_preview_bound = True
        for var in self.fields.values():
            var.trace_add("write", lambda *_args: self._queue_recipe_preview_update())
        for text_widget in (self.load_text, self.to_text, self.return_text):
            text_widget.bind("<KeyRelease>", lambda _event: self._queue_recipe_preview_update())
            text_widget.bind("<FocusOut>", lambda _event: self._queue_recipe_preview_update())

    def _queue_recipe_preview_update(self):
        if self.preview_after_id is not None:
            try:
                self.after_cancel(self.preview_after_id)
            except Exception:
                pass
        self.preview_after_id = self.after(250, self._update_preview_from_ui)

    def _update_preview_from_ui(self):
        self.preview_after_id = None
        try:
            r = self.recipe_from_ui()
        except Exception:
            return
        self.recipe = r
        if hasattr(self, "preview"):
            self.preview.set_recipe(r)
        self._refresh_telemetry_from_recipe(r)

    def set_running(self, value: bool):
        def _apply():
            self.running.set(value)
            state = "disabled" if value else "normal"
            for b in self.run_buttons:
                b.configure(state=state)
            for idx, b in enumerate(self.process_step_buttons):
                if value:
                    b.configure(state="disabled")
                else:
                    unlocked = idx <= self.current_process_step
                    b.configure(state="normal" if unlocked else "disabled")
        self.after(0, _apply)

    def load_recipe_to_ui(self, r: Recipe):
        # Flat map
        values = {
            "serial.port": r.serial.port,
            "serial.baud": r.serial.baud,
            "motion.feed_xy": r.motion.feed_xy,
            "motion.feed_z": r.motion.feed_z,
            "motion.wait_idle": r.motion.wait_idle,
            "motion.idle_timeout_s": r.motion.idle_timeout_s,
            "motion.command_timeout_s": r.motion.command_timeout_s,
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
            "exposure.start_x": r.exposure.start_x,
            "exposure.start_y": r.exposure.start_y,
            "exposure.cols": r.exposure.cols,
            "exposure.rows": r.exposure.rows,
            "exposure.pitch_x": r.exposure.pitch_x,
            "exposure.pitch_y": r.exposure.pitch_y,
            "exposure.exposure_time_s": r.exposure.exposure_time_s,
            "io.uv_enabled": r.io.uv_enabled,
            "io.uv_on_gcode": r.io.uv_on_gcode,
            "io.uv_off_gcode": r.io.uv_off_gcode,
        }

        for key, value in values.items():
            var = self.fields[key]
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set(str(value))

        self.load_text.delete("1.0", "end")
        self.load_text.insert("1.0", waypoints_to_text(r.to_loading_waypoints))
        self.to_text.delete("1.0", "end")
        self.to_text.insert("1.0", waypoints_to_text(r.to_exposure_waypoints))
        self.return_text.delete("1.0", "end")
        self.return_text.insert("1.0", waypoints_to_text(r.return_waypoints))

        self.preview.set_recipe(r)
        self._refresh_telemetry_from_recipe(r)
        self._wire_live_preview_bindings()
        self._refresh_process_sequence_ui()

    def recipe_from_ui(self) -> Recipe:
        def s(key: str) -> str:
            return str(self.fields[key].get()).strip()

        def f(key: str) -> float:
            text = s(key)
            if text == "":
                raise ValueError(f"{key} is empty")
            return float(text)

        def i(key: str) -> int:
            return int(float(s(key)))

        def b(key: str) -> bool:
            return bool(self.fields[key].get())

        initial_x = f("stage.initial_x")
        initial_y = f("stage.initial_y")
        initial_z = f("stage.initial_z")
        to_loading = parse_waypoints(self.load_text.get("1.0", "end"))
        load_endpoint = waypoint_endpoint(
            {"X": initial_x, "Y": initial_y, "Z": initial_z},
            to_loading,
        )

        r = Recipe(
            serial=SerialRecipe(port=s("serial.port"), baud=i("serial.baud")),
            motion=MotionRecipe(
                feed_xy=f("motion.feed_xy"),
                feed_z=f("motion.feed_z"),
                wait_idle=b("motion.wait_idle"),
                idle_timeout_s=f("motion.idle_timeout_s"),
                command_timeout_s=f("motion.command_timeout_s"),
            ),
            jog=JogRecipe(
                xy_step_mm=f("jog.xy_step_mm"),
                z_step_mm=f("jog.z_step_mm"),
                feed_xy=f("jog.feed_xy"),
                feed_z=f("jog.feed_z"),
            ),
            limits=AxisLimits(
                x_min=f("limits.x_min"), x_max=f("limits.x_max"),
                y_min=f("limits.y_min"), y_max=f("limits.y_max"),
                z_min=f("limits.z_min"), z_max=f("limits.z_max"),
            ),
            keepout=KeepoutZone(
                enabled=b("keepout.enabled"),
                block_on_violation=b("keepout.block_on_violation"),
                x_min=f("keepout.x_min"), x_max=f("keepout.x_max"),
                y_min=f("keepout.y_min"), y_max=f("keepout.y_max"),
            ),
            stage=StageRecipe(
                initial_x=initial_x,
                initial_y=initial_y,
                initial_z=initial_z,
                invert_z_output=b("stage.invert_z_output"),
                load_x=load_endpoint["X"],
                load_y=load_endpoint["Y"],
                load_z=load_endpoint["Z"],
                lowered_z=f("stage.lowered_z"),
            ),
            exposure=ExposureRecipe(
                start_x=f("exposure.start_x"),
                start_y=f("exposure.start_y"),
                cols=i("exposure.cols"),
                rows=i("exposure.rows"),
                pitch_x=f("exposure.pitch_x"),
                pitch_y=f("exposure.pitch_y"),
                exposure_time_s=f("exposure.exposure_time_s"),
                exposure_ref_x=f("exposure.start_x"),
                exposure_ref_y=f("exposure.start_y"),
                col_step_dx=f("exposure.pitch_x"),
                row_step_dy=f("exposure.pitch_y"),
            ),
            io=IORecipe(
                uv_enabled=b("io.uv_enabled"),
                uv_on_gcode=s("io.uv_on_gcode"),
                uv_off_gcode=s("io.uv_off_gcode"),
            ),
            to_loading_waypoints=to_loading,
            to_exposure_waypoints=parse_waypoints(self.to_text.get("1.0", "end")),
            return_waypoints=parse_waypoints(self.return_text.get("1.0", "end")),
        )

        if r.exposure.rows <= 0 or r.exposure.cols <= 0:
            raise ValueError("Rows/Cols must be positive")
        if r.exposure.exposure_time_s < 0:
            raise ValueError("Exposure time must be >= 0")
        if r.jog.xy_step_mm <= 0 or r.jog.z_step_mm <= 0:
            raise ValueError("Jog steps must be positive")
        if r.jog.feed_xy <= 0 or r.jog.feed_z <= 0:
            raise ValueError("Jog feeds must be positive")
        if r.limits.x_min >= r.limits.x_max or r.limits.y_min >= r.limits.y_max or r.limits.z_min >= r.limits.z_max:
            raise ValueError("Invalid soft limits")
        if r.keepout.x_min >= r.keepout.x_max or r.keepout.y_min >= r.keepout.y_max:
            raise ValueError("Invalid keepout rectangle")
        return r

    def save_recipe_from_ui(self):
        try:
            self.recipe = self.recipe_from_ui()
            RecipeCodec.save(self.recipe, self.recipe_path)
            self.preview.set_recipe(self.recipe)
            self.safe_log(f"[SAVE] {self.recipe_path.name}")
        except Exception as exc:
            messagebox.showerror("Recipe error", str(exc))

    def load_recipe_dialog(self):
        path = filedialog.askopenfilename(
            title="Load recipe JSON",
            filetypes=[("JSON recipe", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.recipe_path = Path(path)
            self.recipe = RecipeCodec.load(self.recipe_path)
            self.load_recipe_to_ui(self.recipe)
            self.safe_log(f"[LOAD] {self.recipe_path}")
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))

    def connect(self):
        try:
            self.recipe = self.recipe_from_ui()
            if self.dry_run_var.get():
                self.safe_log("[DRY RUN] Connect skipped")
                return
            self.transport.connect(self.recipe.serial.port, self.recipe.serial.baud)
        except Exception as exc:
            messagebox.showerror("Connect failed", str(exc))

    def disconnect(self):
        self.transport.close()
        self.safe_log("[DISCONNECT]")

    def home_machine(self):
        self.run_raw_command("$H", "homing $H", set_coord_confirmed=True)

    def unlock_machine(self):
        self.run_raw_command("$X", "unlock $X", set_coord_confirmed=False)

    def set_current_zero(self):
        self.run_raw_command("G92 X0 Y0 Z0", "set current zero G92", set_coord_confirmed=True)

    def run_raw_command(self, command: str, label: str, set_coord_confirmed: bool = False):
        if self.running.get():
            messagebox.showwarning("Busy", "A job is already running.")
            return

        def task():
            self.set_running(True)
            try:
                self.set_status(label)
                if self.dry_run_var.get():
                    self.safe_log(f"[DRY] {command}")
                else:
                    if not self.transport.connected:
                        raise RuntimeError("Not connected")
                    self.transport.send_line(command, timeout_s=30.0)
                if set_coord_confirmed:
                    self.after(0, self.coord_confirmed_var.set, True)
                self.set_status("Standby")
            except Exception as exc:
                self.set_status("Fault")
                self.safe_log(f"[ERROR] {exc}")
                self.after(0, lambda: messagebox.showerror("Command failed", str(exc)))
            finally:
                self.set_running(False)

        threading.Thread(target=task, daemon=True).start()

    def get_plan(self, name: str) -> Tuple[List[PlannedCommand], List[str], List[str]]:
        r = self.recipe_from_ui()
        self.recipe = r
        self.preview.set_recipe(r)
        planner = MotionPlanner(r)
        if name == "load":
            return planner.loading_sequence()
        if name == "mount":
            return planner.mount_to_exposure_sequence()
        if name == "grid":
            return planner.exposure_grid_sequence()
        if name == "return":
            return planner.return_sequence()
        if name == "full":
            return planner.full_cycle_sequence()
        raise ValueError(f"Unknown plan {name}")

    def check_plan(self):
        try:
            commands, warnings, errors = self.get_plan("full")
            self.log_plan(commands, warnings, errors, limit=200)
            if errors:
                messagebox.showerror("Plan has errors", "\n".join(errors[:8]))
            elif warnings:
                messagebox.showwarning("Plan warnings", "\n".join(warnings[:8]))
            else:
                messagebox.showinfo("Plan OK", f"{len(commands)} commands generated. No errors.")
        except Exception as exc:
            messagebox.showerror("Check failed", str(exc))

    def log_plan(self, commands: List[PlannedCommand], warnings: List[str], errors: List[str], limit: int = 60):
        self.safe_log("\n=== PLAN PREVIEW ===")
        for w in warnings:
            self.safe_log(f"[WARN] {w}")
        for e in errors:
            self.safe_log(f"[ERROR] {e}")
        for idx, cmd in enumerate(commands[:limit], start=1):
            if cmd.gcode:
                self.safe_log(f"{idx:03d}: {cmd.export_line()}")
            else:
                self.safe_log(f"{idx:03d}: ; {cmd.label}")
        if len(commands) > limit:
            self.safe_log(f"... {len(commands) - limit} more commands")
        self.safe_log("=== END PREVIEW ===\n")

    def reset_process_sequence(self):
        self.current_process_step = 0
        self._refresh_process_sequence_ui()
        self.safe_log("[FLOW] sequence reset")

    def run_process_step(self, index: int, plan_name: str):
        if index > self.current_process_step:
            messagebox.showwarning("Sequence locked", "Finish the earlier process steps first.")
            return

        def on_complete(success: bool):
            if success and index == self.current_process_step and self.current_process_step < len(self.process_step_buttons) - 1:
                self.current_process_step += 1
            elif success and index == len(self.process_step_buttons) - 1:
                self.current_process_step = len(self.process_step_buttons)
            self._refresh_process_sequence_ui()

        self.run_plan(plan_name, on_complete=on_complete)

    def _refresh_process_sequence_ui(self):
        for idx, button in enumerate(self.process_step_buttons):
            unlocked = idx <= self.current_process_step
            completed = idx < self.current_process_step or self.current_process_step >= len(self.process_step_buttons)
            if completed:
                self.process_step_states[idx].set("COMPLETE")
                button.configure(state="normal", style="Quick.TButton")
            elif unlocked:
                self.process_step_states[idx].set("READY")
                button.configure(state="normal", style="Accent.TButton")
            else:
                self.process_step_states[idx].set("LOCKED")
                button.configure(state="disabled", style="Ghost.TButton")

    def real_run_allowed(self, r: Recipe) -> bool:
        if self.dry_run_var.get():
            return True
        if not self.coord_confirmed_var.get():
            messagebox.showerror("Preflight needed", "Use Home $H or Set Zero G92 before live motion.")
            return False
        if not self.estop_confirmed_var.get():
            messagebox.showerror("Preflight needed", "Confirm limits and E-stop before live motion.")
            return False
        if r.io.uv_enabled and not self.uv_safety_confirmed_var.get():
            messagebox.showerror("Preflight needed", "Confirm UV shield and interlock before exposure.")
            return False
        return True

    def run_plan(self, name: str, on_complete: Optional[Callable[[bool], None]] = None):
        if self.running.get():
            messagebox.showwarning("Busy", "A job is already running.")
            return

        try:
            commands, warnings, errors = self.get_plan(name)
            RecipeCodec.save(self.recipe, self.recipe_path)
            self.log_plan(commands, warnings, errors)
            if errors:
                messagebox.showerror("Plan blocked", "\n".join(errors[:10]))
                return
            if not self.real_run_allowed(self.recipe):
                return
        except Exception as exc:
            messagebox.showerror("Plan failed", str(exc))
            return

        def task():
            self.set_running(True)
            try:
                self.execute_commands(commands, title=name)
                self.set_status("Complete")
                self.safe_log(f"=== DONE: {name} ===\n")
                if on_complete is not None:
                    self.after(0, lambda: on_complete(True))
            except Exception as exc:
                self.set_status("Fault")
                self.safe_log(f"[ERROR] {exc}")
                self.after(0, lambda: messagebox.showerror("Run failed", str(exc)))
                if on_complete is not None:
                    self.after(0, lambda: on_complete(False))
            finally:
                self.set_running(False)

        threading.Thread(target=task, daemon=True).start()

    def execute_commands(self, commands: List[PlannedCommand], title: str):
        self.set_status(f"Running: {title}")
        r = self.recipe

        if not self.dry_run_var.get() and not self.transport.connected:
            raise RuntimeError("Not connected")

        for idx, cmd in enumerate(commands, start=1):
            if cmd.label:
                self.safe_log(f"[{idx}/{len(commands)}] {cmd.label}")

            if not cmd.gcode:
                # Dry/simulated exposure delay. In real mode, empty gcode appears only when UV disabled.
                if cmd.is_exposure and r.exposure.exposure_time_s > 0:
                    if self.dry_run_var.get():
                        self.safe_log(f"[DRY/SIM] wait {r.exposure.exposure_time_s:g}s")
                    time.sleep(min(r.exposure.exposure_time_s, 1.0))
                continue

            if self.dry_run_var.get():
                self.safe_log(f"[DRY] {cmd.gcode}")
                time.sleep(0.02)
            else:
                self.transport.send_line(cmd.gcode, timeout_s=self.command_timeout_for(cmd))
                if cmd.is_motion and r.motion.wait_idle:
                    self.transport.wait_idle(timeout_s=r.motion.idle_timeout_s)
            if cmd.is_motion:
                self._apply_planned_position(cmd)

    def command_timeout_for(self, cmd: PlannedCommand) -> float:
        base = float(self.recipe.motion.command_timeout_s)
        if cmd.is_exposure:
            match = re.search(r"\bP([-+0-9.]+)", cmd.gcode or "", re.IGNORECASE)
            if match:
                dwell = max(0.0, float(match.group(1)))
                if dwell > 0:
                    timeout = max(base, dwell + 10.0)
                    self.safe_log(f"[TIMEOUT] dwell command timeout {timeout:g}s for {dwell:g}s exposure")
                    return timeout
        return base

    def execute_inline_gcode(self, lines: List[str], title: str, is_motion: bool = False):
        self.set_status(f"Running: {title}")
        r = self.recipe

        if not self.dry_run_var.get() and not self.transport.connected:
            raise RuntimeError("Not connected")

        for line in lines:
            if self.dry_run_var.get():
                self.safe_log(f"[DRY] {line}")
                time.sleep(0.02)
                continue
            self.transport.send_line(line, timeout_s=r.motion.command_timeout_s)
            if is_motion and line.startswith("G1 "):
                self.transport.wait_idle(timeout_s=r.motion.idle_timeout_s)

    def jog_axis(self, axis: str, direction: int):
        try:
            self.recipe = self.recipe_from_ui()
        except Exception as exc:
            messagebox.showerror("Jog settings error", str(exc))
            return

        axis = axis.upper()
        if axis not in AXES:
            messagebox.showerror("Jog error", f"Invalid axis: {axis}")
            return

        if not self.dry_run_var.get() and not self.coord_confirmed_var.get():
            messagebox.showerror("Preflight needed", "Use Home $H or Set Zero G92 before live jog.")
            return

        step = self.recipe.jog.z_step_mm if axis == "Z" else self.recipe.jog.xy_step_mm
        feed = self.recipe.jog.feed_z if axis == "Z" else self.recipe.jog.feed_xy
        delta = step * (1 if direction >= 0 else -1)
        gcode_delta = format_axis_value(self.recipe, axis, delta, relative=True)
        line = f"$J=G91 G21 {axis}{gcode_delta:.3f} F{feed:.1f}"
        label = f"jog {axis}{'+' if direction >= 0 else '-'} {step:g}mm"
        was_running = self.running.get()

        def task():
            if not was_running:
                self.set_running(True)
            try:
                self.safe_log(f"=== JOG: {label} ===")
                if was_running:
                    self.safe_log("[NOTE] Jog requested while a process is active. Use HOLD first for live hardware.")
                self.execute_inline_gcode([line], title=label, is_motion=False)
                if not self.dry_run_var.get():
                    self.transport.wait_idle(timeout_s=self.recipe.motion.idle_timeout_s)
                self.set_status("Running" if was_running else "Standby")
            except Exception as exc:
                self.set_status("Fault")
                self.safe_log(f"[ERROR] {exc}")
                self.after(0, lambda: messagebox.showerror("Jog failed", str(exc)))
            finally:
                if not was_running:
                    self.set_running(False)

        threading.Thread(target=task, daemon=True).start()

    def jog_cancel(self):
        try:
            if self.dry_run_var.get():
                self.safe_log("[DRY] realtime 0x85 jog cancel")
                return
            self.transport.realtime(b"\x85")
            self.safe_log("> realtime 0x85 jog cancel")
        except Exception as exc:
            messagebox.showerror("Jog cancel failed", str(exc))

    def query_status(self):
        was_running = self.running.get()

        def task():
            if not was_running:
                self.set_running(True)
            try:
                self.set_status("Status")
                if self.dry_run_var.get():
                    self.safe_log("[DRY] realtime ?")
                    self.set_status("Standby")
                else:
                    status = self.transport.request_status(timeout_s=2.0)
                    self.safe_log(f"[STATUS] {status}")
                    self.after(0, lambda s=status: self._apply_status_report(s))
                    self.after(0, self.status_var.set, "Standby")
            except Exception as exc:
                self.set_status("Fault")
                self.safe_log(f"[ERROR] {exc}")
                self.after(0, lambda: messagebox.showerror("Status failed", str(exc)))
            finally:
                if not was_running:
                    self.set_running(False)

        threading.Thread(target=task, daemon=True).start()

    def export_gcode(self):
        try:
            commands, warnings, errors = self.get_plan("full")
            if errors:
                messagebox.showerror("Plan has errors", "\n".join(errors[:10]))
                return
            path = filedialog.asksaveasfilename(
                title="Export G-code",
                defaultextension=".nc",
                initialfile="photostepper_full_cycle.nc",
                filetypes=[("G-code", "*.nc *.gcode *.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            lines = [
                "; PhotoStepper Pro v2 generated program",
                "; Review in DRY RUN before using real motion.",
                "; XY moves are separated by design.",
            ]
            for w in warnings:
                lines.append(f"; WARNING: {w}")
            for cmd in commands:
                if cmd.gcode:
                    lines.append(cmd.export_line())
                else:
                    lines.append(f"; {cmd.label}")
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.safe_log(f"[EXPORT] {path}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def feed_hold(self):
        try:
            if self.dry_run_var.get():
                self.safe_log("[DRY] realtime ! feed hold")
                return
            self.transport.realtime(b"!")
            self.safe_log("> realtime !")
        except Exception as exc:
            messagebox.showerror("Feed hold failed", str(exc))

    def resume(self):
        try:
            if self.dry_run_var.get():
                self.safe_log("[DRY] realtime ~ resume")
                return
            self.transport.realtime(b"~")
            self.safe_log("> realtime ~")
        except Exception as exc:
            messagebox.showerror("Resume failed", str(exc))

    def soft_reset(self):
        if not self.dry_run_var.get():
            ok = messagebox.askyesno("Soft reset", "Send Ctrl-X soft reset?")
            if not ok:
                return
        try:
            if self.dry_run_var.get():
                self.safe_log("[DRY] realtime Ctrl-X")
                return
            self.transport.realtime(b"\x18")
            self.safe_log("> realtime Ctrl-X")
        except Exception as exc:
            messagebox.showerror("Soft reset failed", str(exc))


if __name__ == "__main__":
    app = PhotoStepperProApp()
    app.mainloop()
