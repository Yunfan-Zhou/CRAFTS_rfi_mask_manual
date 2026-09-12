#!/usr/bin/env python3
"""Interactive two-polarization manual mask editor for CRAFTS TOD files."""

from __future__ import annotations

import argparse
from collections import deque
import copy
from dataclasses import dataclass
import gc
import json
from pathlib import Path
import sys
import threading
import time

import matplotlib.pyplot as plt
from matplotlib.backend_bases import MouseButton
from matplotlib.patches import Rectangle
from matplotlib.widgets import Button, CheckButtons, RectangleSelector
import numpy as np

from crafts_tod_mask import (
    AutoMaskDirectories,
    BeamAveragedTod,
    MaskDocument,
    TodGroup,
    beam_mask_path,
    discover_groups,
    load_beam_averaged_tod_with_auto_masks,
    load_mask_document,
    mask_is_done,
    mask_path,
    masked_time_average,
    normalize_document,
    save_mask_document,
    save_beam_mask_products,
    validate_group,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = ROOT.parent
DEFAULT_MASK_DIR = ROOT / "masks"
MASK_COLOR = "#d62728"
STATUS_LINES: deque[str] = deque(maxlen=400)
STATUS_LOCK = threading.Lock()
STATUS_STATE: dict[str, object] = {
    "busy": True,
    "phase": "starting",
    "message": "程序正在启动",
    "updated_at": time.time(),
}


def status_message(message: object, *, phase: str | None = None, busy: bool | None = None) -> None:
    """Print and retain a short status history for the browser console."""
    text = str(message)
    timestamp = time.strftime("%H:%M:%S")
    with STATUS_LOCK:
        STATUS_LINES.append(f"[{timestamp}] {text}")
        STATUS_STATE["message"] = text
        STATUS_STATE["updated_at"] = time.time()
        if phase is not None:
            STATUS_STATE["phase"] = phase
        if busy is not None:
            STATUS_STATE["busy"] = bool(busy)
    print(text, flush=True)


def web_status_payload() -> dict[str, object]:
    with STATUS_LOCK:
        return {**STATUS_STATE, "lines": list(STATUS_LINES)}


def _console_page_html(figure_ids: list[int], prefix: str = "") -> str:
    """Return a WebAgg page with a native browser-side status console."""
    ids = json.dumps(figure_ids)
    prefix_json = json.dumps(prefix)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="{prefix}/_static/css/page.css" type="text/css">
  <link rel="stylesheet" href="{prefix}/_static/css/boilerplate.css" type="text/css">
  <link rel="stylesheet" href="{prefix}/_static/css/fbm.css" type="text/css">
  <link rel="stylesheet" href="{prefix}/_static/css/mpl.css" type="text/css">
  <script src="{prefix}/_static/js/mpl_tornado.js"></script>
  <script src="{prefix}/js/mpl.js"></script>
  <style>
    html, body {{ margin: 0; height: 100%; overflow: hidden; font-family: system-ui, sans-serif; }}
    #layout {{ display: flex; width: 100vw; height: 100vh; }}
    #figures {{ flex: 1 1 auto; min-width: 0; overflow: auto; padding: 8px; }}
    #console {{ flex: 0 0 360px; display: flex; flex-direction: column; color: #d8dee9;
                background: #111820; border-left: 1px solid #45515f; }}
    #console header {{ padding: 12px; background: #18222d; border-bottom: 1px solid #45515f; }}
    #badge {{ display: inline-block; margin-top: 8px; padding: 4px 9px; border-radius: 12px;
              font-weight: 700; background: #8a5a00; color: white; }}
    #badge.ready {{ background: #26734d; }}
    #badge.waiting {{ background: #9a3412; }}
    #interaction {{ padding: 9px 12px; color: #ffe08a; border-bottom: 1px solid #45515f;
                    min-height: 2.4em; }}
    #log {{ flex: 1 1 auto; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap;
            overflow-wrap: anywhere; font: 12px/1.45 Menlo, Monaco, monospace; }}
    .hint {{ color: #a9b7c6; font-size: 12px; margin-top: 7px; }}
  </style>
  <title>CRAFTS Manual RFI Mask</title>
</head>
<body>
<div id="layout">
  <div id="figures"></div>
  <aside id="console">
    <header><strong>服务器运行状态</strong><br><span id="badge">正在连接…</span>
      <div class="hint">这里显示读取、计算、交互和保存日志。</div></header>
    <div id="interaction">鼠标状态：等待操作</div>
    <pre id="log">正在获取服务器日志…</pre>
  </aside>
</div>
<script>
const prefix = {prefix_json};
const figureIds = {ids};
const badge = document.getElementById('badge');
const logBox = document.getElementById('log');
const interaction = document.getElementById('interaction');
let lastReply = Date.now();

function setInteraction(text) {{ interaction.textContent = '鼠标状态：' + text; }}
function createFigure(figId) {{
  const host = document.getElementById('figures');
  const figureDiv = document.createElement('div');
  host.appendChild(figureDiv);
  const wsType = mpl.get_websocket_type();
  let uri = 'ws://' + window.location.host + prefix + '/' + figId + '/ws';
  if (window.location.protocol === 'https:') uri = uri.replace('ws:', 'wss:');
  const websocket = new wsType(uri);
  const fig = new mpl.figure(figId, websocket, mpl_ondownload, figureDiv);
  fig.focus_on_mouseover = true;
  fig.canvas.setAttribute('tabindex', figId);
  fig.canvas_div.addEventListener('mousedown', () => setInteraction('已按下；请继续拖动后松开'));
  // Stock WebAgg forwards every mousemove and can request dozens of remote
  // PNG redraws per second.  Capture and throttle it for an SSH tunnel.
  let lastMotion = 0;
  fig.canvas_div.addEventListener('mousemove', (event) => {{
    event.stopImmediatePropagation();
    if (event.buttons) setInteraction('正在拖动选区…');
    const now = performance.now();
    if (now - lastMotion >= 120) {{
      lastMotion = now;
      fig.mouse_event(event, 'motion_notify');
    }}
  }}, true);
  fig.canvas_div.addEventListener('mouseup', () => setInteraction('已松开；等待服务器处理'));
  websocket.addEventListener('open', () => setInteraction('WebSocket 已连接，可以拖拽'));
  websocket.addEventListener('close', () => setInteraction('WebSocket 已断开，请刷新页面'));
}}
figureIds.forEach(createFigure);

async function pollStatus() {{
  try {{
    const response = await fetch(prefix + '/status.json?t=' + Date.now(), {{cache: 'no-store'}});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    lastReply = Date.now();
    badge.className = data.busy ? '' : 'ready';
    badge.textContent = data.busy ? '正在读取或计算' : '服务器可交互';
    logBox.textContent = (data.lines || []).join('\\n');
    logBox.scrollTop = logBox.scrollHeight;
  }} catch (error) {{
    if (Date.now() - lastReply > 2500) {{
      badge.className = 'waiting';
      badge.textContent = '服务器忙或暂时无响应';
    }}
  }} finally {{
    // Schedule only after the previous request has settled.  This prevents a
    // busy renderer from accumulating aborted SSH-forwarded HTTP connections.
    setTimeout(pollStatus, 1500);
  }}
}}
pollStatus();
setInterval(() => {{
  if (Date.now() - lastReply > 3000) {{
    badge.className = 'waiting';
    badge.textContent = '服务器忙或暂时无响应';
  }}
}}, 500);
</script>
</body>
</html>"""


def install_webagg_status_console() -> None:
    """Replace the stock WebAgg index with a figure plus live log console."""
    import matplotlib.backends.backend_webagg as webagg
    from matplotlib._pylab_helpers import Gcf
    import tornado.web

    base_application = webagg.WebAggApplication
    if getattr(base_application, "_crafts_console", False):
        return

    class StatusHandler(tornado.web.RequestHandler):
        def get(self) -> None:
            self.set_header("Cache-Control", "no-store")
            self.write(web_status_payload())

    class ConsoleFiguresPage(base_application.AllFiguresPage):
        def get(self) -> None:
            self.set_header("Content-Type", "text/html; charset=UTF-8")
            self.write(_console_page_html(sorted(Gcf.figs), self.url_prefix))

    class ConsoleWebAggApplication(base_application):
        initialized = False
        started = False
        _crafts_console = True
        AllFiguresPage = ConsoleFiguresPage

        def __init__(self, url_prefix: str = "") -> None:
            super().__init__(url_prefix=url_prefix)
            self.add_handlers(
                r".*$",
                [(url_prefix + r"/status\.json", StatusHandler)],
            )

    webagg.WebAggApplication = ConsoleWebAggApplication


@dataclass(frozen=True)
class ContrastDrag:
    start_x: float
    start_y: float
    start_limits: tuple[tuple[float, float], ...]
    axes_width: float
    axes_height: float


def contrast_limits_from_drag(
    start_limits: tuple[tuple[float, float], ...],
    dx_pixels: float,
    dy_pixels: float,
    axes_width: float,
    axes_height: float,
) -> list[tuple[float, float]]:
    """Shift the color center horizontally and scale its width vertically."""
    horizontal_fraction = dx_pixels / max(float(axes_width), 1.0)
    vertical_fraction = dy_pixels / max(float(axes_height), 1.0)
    width_factor = float(np.exp(vertical_fraction * 2.8))
    adjusted = []
    for low, high in start_limits:
        start_width = max(float(high - low), 1.0e-8)
        start_center = 0.5 * float(low + high)
        center = start_center + horizontal_fraction * start_width
        width = float(
            np.clip(
                start_width * width_factor,
                max(start_width / 1000.0, 1.0e-8),
                max(start_width * 20.0, 1.0),
            )
        )
        adjusted.append((center - 0.5 * width, center + 0.5 * width))
    return adjusted


def configure_webagg(port: int) -> None:
    """Bind WebAgg to localhost on one deterministic SSH-forwarded port."""
    port = int(port)
    if not 1024 <= port <= 65535:
        raise ValueError("--web-port must be between 1024 and 65535")
    if "webagg" not in plt.get_backend().lower():
        raise RuntimeError(
            "--web-port requires MPLBACKEND=WebAgg; use the browser launcher"
        )
    plt.rcParams["webagg.address"] = "127.0.0.1"
    plt.rcParams["webagg.port"] = port
    # This Matplotlib version interprets the value as the total number of
    # candidate ports, not as retries after the first attempt.  One therefore
    # means "try exactly the requested port".
    plt.rcParams["webagg.port_retries"] = 1
    plt.rcParams["webagg.open_in_browser"] = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Average all 19 CRAFTS beams and interactively mask the two "
            "polarizations. With no target, print the grouped scan list."
        )
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="One-based list number, scan name, file serial, or SCAN:SERIAL.",
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--mask-dir", type=Path, default=DEFAULT_MASK_DIR)
    parser.add_argument(
        "--one-channel-dir", type=Path,
        help="Override OneChannel directory; default is each FITS file's sibling product directory.",
    )
    parser.add_argument(
        "--sat1380-dir", type=Path,
        help="Override SAT1380 directory; default is each FITS file's sibling product directory.",
    )
    parser.add_argument(
        "--require-all-auto-masks", action="store_true",
        help="Fail if either science-RFI mask is absent for any beam.",
    )
    parser.add_argument("--list", action="store_true", help="Print the scan list and exit.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Check frequency/time coordinates across beams and exit.",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="Render the selected entry to a PNG without opening the GUI.",
    )
    parser.add_argument("--block-records", type=int, default=128)
    parser.add_argument(
        "--web-port",
        type=int,
        help="Deterministic localhost port for MPLBACKEND=WebAgg over SSH forwarding.",
    )
    return parser.parse_args()


def print_group_list(groups: list[TodGroup], mask_dir: Path) -> None:
    if not groups:
        print("No matching CRAFTS TOD FITS files found.")
        return
    print(f"{'#':>3}  {'scan':<24} {'file':<6} {'beams':<25} status")
    for index, group in enumerate(groups, start=1):
        beams = ",".join(group.beams)
        status = "done" if mask_is_done(mask_path(mask_dir, group)) else "pending"
        print(f"{index:>3}  {group.scan_name:<24} {group.serial:<6} {beams:<25} {status}")


def resolve_group_index(target: str, groups: list[TodGroup]) -> int:
    if target.isdigit() and 1 <= int(target) <= len(groups):
        return int(target) - 1
    normalized = target.strip().lower()
    matches = []
    for index, group in enumerate(groups):
        aliases = {
            group.scan_name.lower(),
            group.serial.lower(),
            f"{group.scan_name}:{group.serial}".lower(),
            group.key.lower(),
        }
        if normalized in aliases:
            matches.append(index)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Target {target!r} is ambiguous; use SCAN:SERIAL or list number")
    raise ValueError(f"Target {target!r} was not found")


class CraftsTodMaskEditor:
    def __init__(
        self,
        groups: list[TodGroup],
        group_index: int,
        mask_dir: Path,
        auto_mask_directories: AutoMaskDirectories,
        block_records: int = 128,
        require_all_auto_masks: bool = False,
    ) -> None:
        self.groups = groups
        self.group_index = group_index
        self.mask_dir = mask_dir
        self.auto_mask_directories = auto_mask_directories
        self.require_all_auto_masks = require_all_auto_masks
        self.block_records = max(1, int(block_records))
        self.mode = "box"
        self.apply_both_pols = False
        self.vertical_wheel_enabled = False
        self.apply_auto_rfi = True
        self.show_auto_rfi = True
        self.shift_held = False
        self.pan_enabled = False
        self.pan_drag: tuple[float, tuple[float, float]] | None = None
        self.contrast_drag: ContrastDrag | None = None
        self.dirty = False
        self.undo_stack: list[MaskDocument] = []
        self.contrast_level = 0
        self.frequency = np.empty(0)
        self.time = np.empty(0)
        self.tod = np.empty((0, 0, 2), dtype=np.float32)
        self.raw_tod = np.empty((0, 0, 2), dtype=np.float32)
        self.auto_masked_tod = np.empty((0, 0, 2), dtype=np.float32)
        self.aggregate: BeamAveragedTod | None = None
        self.local_medians = [0.0, 0.0]
        self.full_xlim = (0.0, 1.0)
        self.full_ylim = (0.0, 1.0)
        self.document = MaskDocument.empty(self.groups[group_index])
        self.axes: list[plt.Axes] = []
        self.spectrum_axis: plt.Axes | None = None
        self.spectrum_lines = []
        self.masked_spectra = np.empty((0, 2), dtype=np.float64)
        self.images = []
        self.colorbars = []
        self.selectors: list[RectangleSelector] = []
        self.overlay_artists = []
        self.buttons: list[Button] = []
        self.mode_buttons: dict[str, Button] = {}
        self.both_check: CheckButtons | None = None

        self.fig = plt.figure(figsize=(15.5, 9.2))
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)
        self.fig.canvas.mpl_connect("key_release_event", self.on_key_release)
        self.fig.canvas.mpl_connect("scroll_event", self.on_scroll)
        self.fig.canvas.mpl_connect("button_press_event", self.on_button_press)
        self.fig.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.fig.canvas.mpl_connect("button_release_event", self.on_button_release)
        self.load_group(group_index)

    @property
    def group(self) -> TodGroup:
        return self.groups[self.group_index]

    @property
    def frequency_axes(self) -> list[plt.Axes]:
        axes = list(self.axes)
        if self.spectrum_axis is not None:
            axes.append(self.spectrum_axis)
        return axes

    def load_group(self, index: int) -> None:
        if self.dirty:
            self.set_status("Unsaved edits: save or revert before changing entries.")
            return
        self.group_index = index % len(self.groups)
        group = self.group
        status_message(
            f"Selected #{self.group_index + 1}: {group.scan_name} file {group.serial}; "
            f"beams={','.join(group.beams)}",
            phase="loading",
            busy=True,
        )
        try:
            self.aggregate = load_beam_averaged_tod_with_auto_masks(
                group,
                self.auto_mask_directories,
                block_records=self.block_records,
                progress=status_message,
                require_all_masks=self.require_all_auto_masks,
            )
        except Exception as error:
            status_message(f"ERROR while loading: {error}", phase="error", busy=False)
            raise
        self.frequency = self.aggregate.frequency_mhz
        self.time = self.aggregate.mjd
        self.raw_tod = self.aggregate.raw_mean
        self.auto_masked_tod = self.aggregate.auto_masked_mean
        self.document = load_mask_document(mask_path(self.mask_dir, group), group)
        self.document.auto_mask_files = self.aggregate.auto_mask_files
        self.undo_stack.clear()
        self.dirty = False
        self.contrast_level = 0
        self.local_medians = []
        for polarization in (0, 1):
            sample = self._sample(self.raw_tod[:, :, polarization])
            median = float(np.nanmedian(sample))
            self.local_medians.append(median)
            self.raw_tod[:, :, polarization] -= median
            self.auto_masked_tod[:, :, polarization] -= median
        self.tod = self.auto_masked_tod if self.apply_auto_rfi else self.raw_tod
        self.full_xlim = (float(self.frequency[0]), float(self.frequency[-1]))
        self.full_ylim = (0.0, float(len(self.time)))
        self.masked_spectra = np.empty((0, 2), dtype=np.float64)
        self.build_figure()
        for warning in self.aggregate.warnings:
            status_message(f"WARNING: {warning}")
        gc.collect()
        status_message(
            f"Ready: {group.scan_name} file {group.serial}; drag inside a waterfall panel",
            phase="ready",
            busy=False,
        )

    @staticmethod
    def _sample(values: np.ndarray, max_values: int = 600_000) -> np.ndarray:
        stride = max(1, int(np.sqrt(values.size / max_values)))
        return values[::stride, ::stride]

    def robust_limits(self, polarization: int) -> tuple[float, float]:
        percentile_pairs = ((0.5, 99.5), (1.0, 99.0), (2.0, 98.0), (5.0, 95.0))
        low_percentile, high_percentile = percentile_pairs[self.contrast_level]
        sample = self._sample(self.tod[:, :, polarization])
        low, high = np.nanpercentile(sample, (low_percentile, high_percentile))
        if not np.isfinite(low) or not np.isfinite(high) or low == high:
            return -1.0, 1.0
        return float(low), float(high)

    def build_figure(self) -> None:
        self.fig.clear()
        grid = self.fig.add_gridspec(
            3,
            2,
            width_ratios=(1.0, 0.025),
            height_ratios=(1.0, 1.0, 0.58),
            left=0.075,
            right=0.93,
            top=0.89,
            bottom=0.17,
            hspace=0.18,
            wspace=0.035,
        )
        first_axis = self.fig.add_subplot(grid[0, 0])
        second_axis = self.fig.add_subplot(grid[1, 0], sharex=first_axis, sharey=first_axis)
        self.axes = [first_axis, second_axis]
        self.spectrum_axis = self.fig.add_subplot(grid[2, 0], sharex=first_axis)
        self.images = []
        self.colorbars = []
        self.selectors = []
        self.overlay_artists = []
        extent = [self.frequency[0], self.frequency[-1], 0, len(self.time)]
        for polarization, axis in enumerate(self.axes):
            low, high = self.robust_limits(polarization)
            image = axis.imshow(
                self.tod[:, :, polarization],
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=extent,
                cmap="viridis",
                vmin=low,
                vmax=high,
                rasterized=True,
            )
            self.images.append(image)
            colorbar_axis = self.fig.add_subplot(grid[polarization, 1])
            colorbar = self.fig.colorbar(image, cax=colorbar_axis)
            colorbar.set_label("Beam mean - median (K)")
            self.colorbars.append(colorbar)
            axis.set_ylabel("Record")
            axis.tick_params(axis="x", which="both", labelbottom=False)
            axis.set_title(
                f"Pol {polarization} | beam-mean local median = "
                f"{self.local_medians[polarization]:.4g} K",
                fontsize=10,
            )
            selector = RectangleSelector(
                axis,
                lambda click, release, pol=polarization: self.on_select(pol, click, release),
                useblit=True,
                button=[1],
                minspanx=max(np.nanmedian(np.diff(self.frequency)), 1.0e-6),
                minspany=1,
                spancoords="data",
                props={"facecolor": MASK_COLOR, "edgecolor": MASK_COLOR, "alpha": 0.24},
            )
            self.selectors.append(selector)
        self.spectrum_lines = []
        for polarization, color in ((0, "#1f77b4"), (1, "#ff7f0e")):
            (line,) = self.spectrum_axis.plot(
                self.frequency,
                np.full_like(self.frequency, np.nan),
                color=color,
                linewidth=0.9,
                label=f"Pol {polarization}",
            )
            self.spectrum_lines.append(line)
        self.spectrum_axis.set_ylabel("Mean T (K)")
        self.spectrum_axis.set_xlabel("Frequency (MHz)")
        self.spectrum_axis.set_title("Masked time-averaged spectrum", fontsize=10)
        self.spectrum_axis.legend(loc="best", ncol=2, fontsize=8, frameon=True)
        self.spectrum_axis.grid(alpha=0.2, linewidth=0.5)
        self.set_xlim(*self.full_xlim)
        self.set_ylim(*self.full_ylim)

        start_mjd = float(self.time[0]) if len(self.time) else np.nan
        end_mjd = float(self.time[-1]) if len(self.time) else np.nan
        self.fig.suptitle(
            f"#{self.group_index + 1}  {self.group.scan_name}  file {self.group.serial}  |  "
            f"beams: {', '.join(self.group.beams)}  |  MJD {start_mjd:.6f}-{end_mjd:.6f}",
            fontsize=12,
        )
        self.add_controls()
        self.draw_mask_overlays()
        self.update_mode_appearance()
        self.set_status(self.default_status())

    def add_controls(self) -> None:
        controls = [
            ("Freq", 0.075, 0.052, lambda event: self.set_mode("frequency"), "frequency"),
            ("Time", 0.135, 0.052, lambda event: self.set_mode("time"), "time"),
            ("Box", 0.195, 0.052, lambda event: self.set_mode("box"), "box"),
            ("Delete", 0.255, 0.065, lambda event: self.set_mode("delete"), "delete"),
            ("Undo", 0.328, 0.055, self.undo, None),
            ("Revert", 0.391, 0.062, self.revert, None),
            ("Contrast", 0.461, 0.072, self.cycle_contrast, None),
            ("Pan", 0.541, 0.055, self.toggle_pan, None),
            ("Prev", 0.604, 0.052, self.previous_group, None),
            ("Next", 0.664, 0.052, self.next_group, None),
            ("Save", 0.724, 0.055, self.save, None),
        ]
        self.buttons = []
        self.mode_buttons = {}
        for label, left, width, callback, mode in controls:
            button_axis = self.fig.add_axes([left, 0.075, width, 0.042])
            button = Button(button_axis, label, hovercolor="0.88")
            button.on_clicked(callback)
            self.buttons.append(button)
            if mode is not None:
                self.mode_buttons[mode] = button

        check_axis = self.fig.add_axes([0.80, 0.035, 0.17, 0.125])
        self.both_check = CheckButtons(
            check_axis,
            ["both pols", "vertical wheel", "apply auto RFI", "show auto RFI"],
            [
                self.apply_both_pols,
                self.vertical_wheel_enabled,
                self.apply_auto_rfi,
                self.show_auto_rfi,
            ],
        )
        self.both_check.on_clicked(self.toggle_options)
        self.status_text = self.fig.text(0.075, 0.018, "", fontsize=8.5, va="bottom")

    def default_status(self) -> str:
        state = "done" if self.document.done and not self.dirty else "unsaved" if self.dirty else "pending"
        availability = ""
        if self.aggregate is not None:
            availability = (
                " | masks: OneChannel "
                f"{self.aggregate.layer_available_beams['one_channel']}/19, "
                f"SAT1380 {self.aggregate.layer_available_beams['sat1380']}/19"
            )
        return (
            f"{state} | mode={self.mode}{availability} | drag in a panel to edit that polarization; "
            "right-drag contrast (horizontal=center, vertical=range); "
            "wheel=x zoom, Shift+wheel or v/vertical-wheel=record zoom; "
            "f/t/b/d modes, u undo, s save, g pan, ,/. shift view, 0 reset"
        )

    def set_status(self, message: str) -> None:
        if hasattr(self, "status_text"):
            self.status_text.set_text(message)
            self.fig.canvas.draw_idle()
        status_message(message)

    def target_polarizations(self, selected: int) -> tuple[int, ...]:
        return (0, 1) if self.apply_both_pols else (selected,)

    def push_undo(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.document))
        if len(self.undo_stack) > 50:
            self.undo_stack.pop(0)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if self.pan_enabled:
            self.pan_enabled = False
        self.update_selector_activity()
        self.update_mode_appearance()
        self.set_status(self.default_status())

    def update_mode_appearance(self) -> None:
        for mode, button in self.mode_buttons.items():
            button.ax.set_facecolor("#f4c7c3" if mode == self.mode else "0.94")
        self.fig.canvas.draw_idle()

    def update_selector_activity(self) -> None:
        for selector in self.selectors:
            selector.set_active(not self.pan_enabled)

    def toggle_options(self, _label: str) -> None:
        if self.both_check is not None:
            both_pols, vertical_wheel, apply_auto_rfi, show_auto_rfi = self.both_check.get_status()
            self.apply_both_pols = bool(both_pols)
            self.vertical_wheel_enabled = bool(vertical_wheel)
            changed_data = self.apply_auto_rfi != bool(apply_auto_rfi)
            self.apply_auto_rfi = bool(apply_auto_rfi)
            self.show_auto_rfi = bool(show_auto_rfi)
            if changed_data:
                self.tod = self.auto_masked_tod if self.apply_auto_rfi else self.raw_tod
                for polarization, image in enumerate(self.images):
                    image.set_data(self.tod[:, :, polarization])
                    image.set_clim(*self.robust_limits(polarization))
            self.draw_mask_overlays()
        self.set_status(self.default_status())

    def on_select(self, polarization: int, click, release) -> None:
        if self.pan_enabled or None in (click.xdata, click.ydata, release.xdata, release.ydata):
            return
        x0, x1 = sorted((float(click.xdata), float(release.xdata)))
        y0, y1 = sorted((float(click.ydata), float(release.ydata)))
        x0 = max(self.full_xlim[0], x0)
        x1 = min(self.full_xlim[1], x1)
        start = max(0, int(np.floor(y0)))
        stop = min(len(self.time), int(np.ceil(y1)))
        if x1 <= x0 or stop <= start:
            return

        self.push_undo()
        for pol in self.target_polarizations(polarization):
            pol_mask = self.document.polarizations[pol]
            if self.mode == "frequency":
                pol_mask.frequency_ranges_mhz.append([x0, x1])
            elif self.mode == "time":
                pol_mask.time_ranges_records.append([start, stop])
            elif self.mode == "box":
                pol_mask.boxes.append(
                    {"frequency_mhz": [x0, x1], "records": [start, stop]}
                )
            elif self.mode == "delete":
                self.delete_intersections(pol, x0, x1, start, stop)
        normalize_document(self.document)
        self.document.done = False
        self.dirty = True
        self.draw_mask_overlays()
        self.set_status(self.default_status())

    def delete_intersections(
        self,
        polarization: int,
        x0: float,
        x1: float,
        start: int,
        stop: int,
    ) -> None:
        pol_mask = self.document.polarizations[polarization]
        pol_mask.frequency_ranges_mhz = [
            item for item in pol_mask.frequency_ranges_mhz if item[1] < x0 or item[0] > x1
        ]
        pol_mask.time_ranges_records = [
            item for item in pol_mask.time_ranges_records if item[1] <= start or item[0] >= stop
        ]
        kept_boxes = []
        for box in pol_mask.boxes:
            box_x0, box_x1 = box["frequency_mhz"]
            box_y0, box_y1 = box["records"]
            if box_x1 < x0 or box_x0 > x1 or box_y1 <= start or box_y0 >= stop:
                kept_boxes.append(box)
        pol_mask.boxes = kept_boxes

    def update_masked_spectrum(self) -> None:
        if self.spectrum_axis is None or len(self.spectrum_lines) != 2:
            return
        self.masked_spectra = masked_time_average(
            self.tod,
            self.frequency,
            self.document,
        )
        for polarization, line in enumerate(self.spectrum_lines):
            spectrum = self.masked_spectra[:, polarization] + self.local_medians[polarization]
            line.set_data(self.frequency, spectrum)
        self.update_spectrum_ylim()

    def update_spectrum_ylim(self) -> None:
        if self.spectrum_axis is None or self.masked_spectra.size == 0:
            return
        low_frequency, high_frequency = self.spectrum_axis.get_xlim()
        visible = (self.frequency >= low_frequency) & (self.frequency <= high_frequency)
        values = self.masked_spectra[visible].copy()
        if values.size == 0:
            return
        values[:, 0] += self.local_medians[0]
        values[:, 1] += self.local_medians[1]
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return
        low, high = np.nanpercentile(finite, (0.5, 99.5))
        if not np.isfinite(low) or not np.isfinite(high):
            return
        if low == high:
            padding = max(abs(float(low)) * 0.05, 1.0e-3)
        else:
            padding = 0.08 * float(high - low)
        self.spectrum_axis.set_ylim(float(low - padding), float(high + padding))

    def draw_mask_overlays(self) -> None:
        for artist in self.overlay_artists:
            artist.remove()
        self.overlay_artists = []
        for polarization, axis in enumerate(self.axes):
            if self.show_auto_rfi and self.aggregate is not None:
                flagged_beams = np.ma.masked_equal(
                    self.aggregate.union_flagged_beam_count[:, :, polarization],
                    0,
                    copy=False,
                )
                auto_artist = axis.imshow(
                    flagged_beams,
                    origin="lower",
                    aspect="auto",
                    interpolation="nearest",
                    extent=[self.frequency[0], self.frequency[-1], 0, len(self.time)],
                    cmap="autumn",
                    vmin=1,
                    vmax=19,
                    alpha=0.38,
                    zorder=3,
                    rasterized=True,
                )
                self.overlay_artists.append(auto_artist)
            pol_mask = self.document.polarizations[polarization]
            for low, high in pol_mask.frequency_ranges_mhz:
                self.overlay_artists.append(
                    axis.axvspan(low, high, color=MASK_COLOR, alpha=0.46, linewidth=0, zorder=4)
                )
            for start, stop in pol_mask.time_ranges_records:
                self.overlay_artists.append(
                    axis.axhspan(start, stop, color=MASK_COLOR, alpha=0.46, linewidth=0, zorder=4)
                )
            for box in pol_mask.boxes:
                low, high = box["frequency_mhz"]
                start, stop = box["records"]
                patch = Rectangle(
                    (low, start),
                    high - low,
                    stop - start,
                    facecolor=MASK_COLOR,
                    edgecolor="#8b0000",
                    linewidth=0.8,
                    alpha=0.52,
                    zorder=5,
                )
                axis.add_patch(patch)
                self.overlay_artists.append(patch)
        self.update_masked_spectrum()
        self.fig.canvas.draw_idle()

    def undo(self, _event=None) -> None:
        if not self.undo_stack:
            self.set_status("Nothing to undo.")
            return
        self.document = self.undo_stack.pop()
        self.dirty = True
        self.draw_mask_overlays()
        self.set_status(self.default_status())

    def revert(self, _event=None) -> None:
        self.document = load_mask_document(mask_path(self.mask_dir, self.group), self.group)
        self.undo_stack.clear()
        self.dirty = False
        self.draw_mask_overlays()
        self.set_status("Reverted to the saved mask. " + self.default_status())

    def save(self, _event=None) -> None:
        self.document.done = True
        json_target = mask_path(self.mask_dir, self.group)
        if self.aggregate is None:
            raise RuntimeError("No beam-averaged data loaded")
        self.document.auto_mask_files = self.aggregate.auto_mask_files
        save_mask_document(json_target, self.document)
        beam_targets = save_beam_mask_products(
            self.mask_dir,
            group=self.group,
            frequency_mhz=self.frequency,
            mjd=self.time,
            document=self.document,
            json_path=json_target,
        )
        self.dirty = False
        self.set_status(
            f"Saved JSON + {len(beam_targets)} beam NPZ products and marked done: "
            f"{json_target}; {beam_mask_path(self.mask_dir, self.group.files[0])} ..."
        )

    def cycle_contrast(self, _event=None) -> None:
        self.contrast_level = (self.contrast_level + 1) % 4
        for polarization, image in enumerate(self.images):
            image.set_clim(*self.robust_limits(polarization))
        self.set_status(f"Contrast preset {self.contrast_level + 1}/4. " + self.default_status())

    def toggle_pan(self, _event=None) -> None:
        self.pan_enabled = not self.pan_enabled
        self.pan_drag = None
        self.update_selector_activity()
        self.set_status(("Pan enabled. " if self.pan_enabled else "Pan disabled. ") + self.default_status())

    def previous_group(self, _event=None) -> None:
        self.load_group(self.group_index - 1)

    def next_group(self, _event=None) -> None:
        self.load_group(self.group_index + 1)

    def set_xlim(self, low: float, high: float) -> None:
        full_low, full_high = self.full_xlim
        width = min(high - low, full_high - full_low)
        low = max(full_low, min(low, full_high - width))
        high = low + width
        for axis in self.frequency_axes:
            axis.set_xlim(low, high)
        self.update_spectrum_ylim()
        self.fig.canvas.draw_idle()

    def set_ylim(self, low: float, high: float) -> None:
        full_low, full_high = self.full_ylim
        height = min(high - low, full_high - full_low)
        low = max(full_low, min(low, full_high - height))
        high = low + height
        for axis in self.axes:
            axis.set_ylim(low, high)
        self.fig.canvas.draw_idle()

    def pan_view(self, shift_mhz: float) -> None:
        low, high = self.axes[-1].get_xlim()
        self.set_xlim(low + shift_mhz, high + shift_mhz)

    def on_scroll(self, event) -> None:
        if event.inaxes not in self.frequency_axes:
            return
        factor = 0.80 if event.button == "up" else 1.25
        shift_held = self.shift_held or "shift" in str(event.key or "").lower()
        vertical_zoom = self.vertical_wheel_enabled or shift_held
        if vertical_zoom and event.inaxes in self.axes and event.ydata is not None:
            low, high = event.inaxes.get_ylim()
            minimum_height = min(8.0, self.full_ylim[1] - self.full_ylim[0])
            height = min(
                max((high - low) * factor, minimum_height),
                self.full_ylim[1] - self.full_ylim[0],
            )
            fraction = (float(event.ydata) - low) / (high - low)
            self.set_ylim(
                float(event.ydata) - fraction * height,
                float(event.ydata) + (1 - fraction) * height,
            )
            return
        if event.xdata is None:
            return
        low, high = event.inaxes.get_xlim()
        minimum_width = max(float(np.nanmedian(np.diff(self.frequency))) * 8, 1.0e-4)
        width = min(max((high - low) * factor, minimum_width), self.full_xlim[1] - self.full_xlim[0])
        fraction = (float(event.xdata) - low) / (high - low)
        self.set_xlim(float(event.xdata) - fraction * width, float(event.xdata) + (1 - fraction) * width)

    def on_button_press(self, event) -> None:
        if (
            self.pan_enabled
            and event.button in (1, MouseButton.LEFT)
            and event.inaxes in self.frequency_axes
            and event.xdata is not None
        ):
            self.pan_drag = (float(event.xdata), event.inaxes.get_xlim())
            return
        if (
            event.button in (3, MouseButton.RIGHT)
            and event.inaxes in self.axes
            and event.x is not None
            and event.y is not None
        ):
            self.contrast_drag = ContrastDrag(
                start_x=float(event.x),
                start_y=float(event.y),
                start_limits=tuple(
                    (float(image.get_clim()[0]), float(image.get_clim()[1]))
                    for image in self.images
                ),
                axes_width=max(float(event.inaxes.bbox.width), 1.0),
                axes_height=max(float(event.inaxes.bbox.height), 1.0),
            )

    def on_motion(self, event) -> None:
        if self.pan_drag is not None:
            if event.xdata is None:
                return
            anchor, (low, high) = self.pan_drag
            shift = anchor - float(event.xdata)
            self.set_xlim(low + shift, high + shift)
            return
        if self.contrast_drag is None or event.x is None or event.y is None:
            return
        drag = self.contrast_drag
        limits = contrast_limits_from_drag(
            drag.start_limits,
            float(event.x) - drag.start_x,
            float(event.y) - drag.start_y,
            drag.axes_width,
            drag.axes_height,
        )
        for image, (low, high) in zip(self.images, limits, strict=True):
            image.set_clim(low, high)
        self.fig.canvas.draw_idle()

    def on_button_release(self, _event) -> None:
        self.pan_drag = None
        self.contrast_drag = None

    def on_key(self, event) -> None:
        key = (event.key or "").lower()
        if key in {"shift", "shift_l", "shift_r"}:
            self.shift_held = True
        elif key == "f":
            self.set_mode("frequency")
        elif key == "t":
            self.set_mode("time")
        elif key == "b":
            self.set_mode("box")
        elif key in {"d", "delete"}:
            self.set_mode("delete")
        elif key == "u":
            self.undo()
        elif key == "s":
            self.save()
        elif key == "g":
            self.toggle_pan()
        elif key == "n":
            self.next_group()
        elif key == "p":
            self.previous_group()
        elif key == "c":
            self.cycle_contrast()
        elif key == "v":
            if self.both_check is not None:
                self.both_check.set_active(1)
        elif key == ",":
            low, high = self.axes[-1].get_xlim()
            self.pan_view(-0.5 * (high - low))
        elif key == ".":
            low, high = self.axes[-1].get_xlim()
            self.pan_view(0.5 * (high - low))
        elif key == "[":
            self.pan_view(-8.0)
        elif key == "]":
            self.pan_view(8.0)
        elif key == "0":
            self.set_xlim(*self.full_xlim)
            self.set_ylim(*self.full_ylim)
        elif key == "q":
            plt.close(self.fig)

    def on_key_release(self, event) -> None:
        key = (event.key or "").lower()
        if key in {"shift", "shift_l", "shift_r"}:
            self.shift_held = False


def main() -> None:
    args = parse_args()
    if args.web_port is not None:
        try:
            configure_webagg(args.web_port)
            install_webagg_status_console()
            status_message(
                f"Starting Web UI on 127.0.0.1:{args.web_port}",
                phase="starting",
                busy=True,
            )
        except (RuntimeError, ValueError) as error:
            raise SystemExit(str(error)) from error
    input_dir = args.input_dir.expanduser().resolve()
    mask_dir = args.mask_dir.expanduser().resolve()
    auto_mask_directories = AutoMaskDirectories(
        one_channel=(
            args.one_channel_dir.expanduser().resolve()
            if args.one_channel_dir is not None else None
        ),
        sat1380=(
            args.sat1380_dir.expanduser().resolve()
            if args.sat1380_dir is not None else None
        ),
    )
    groups = discover_groups(input_dir)
    if not groups:
        raise SystemExit(f"No matching FITS files under {input_dir}")

    print_group_list(groups, mask_dir)
    if args.list or (args.target is None and args.snapshot is None and not args.validate):
        return
    if args.validate:
        failed = False
        for index, group in enumerate(groups, start=1):
            try:
                frequency, time, shape = validate_group(group)
                print(
                    f"OK #{index}: {group.scan_name} {group.serial}; beams={len(group.files)}; "
                    f"shape={shape}; freq={frequency[0]:.6f}-{frequency[-1]:.6f} MHz; "
                    f"records={len(time)}"
                )
            except Exception as error:
                failed = True
                print(f"FAILED #{index}: {group.scan_name} {group.serial}: {error}", file=sys.stderr)
        raise SystemExit(1 if failed else 0)

    target = args.target or "1"
    try:
        group_index = resolve_group_index(target, groups)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    editor = CraftsTodMaskEditor(
        groups,
        group_index,
        mask_dir,
        auto_mask_directories,
        args.block_records,
        args.require_all_auto_masks,
    )
    if args.snapshot is not None:
        snapshot = args.snapshot.expanduser().resolve()
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        editor.fig.savefig(snapshot, dpi=120, bbox_inches="tight")
        print(f"Wrote {snapshot}")
        plt.close(editor.fig)
    else:
        plt.show()


if __name__ == "__main__":
    main()
