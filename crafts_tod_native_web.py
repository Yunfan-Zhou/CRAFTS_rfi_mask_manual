#!/usr/bin/env python3
"""Responsive browser UI for manual CRAFTS TOD masking.

The browser draws selections, axes, and spectra locally.  The server only loads
the exact-grid arrays, emits bounded PNG views, and writes the existing
manual-only JSON/NPZ products.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
import threading
import time
import traceback
from typing import Any

import numpy as np
from PIL import Image
import tornado.ioloop
import tornado.web

from crafts_tod_mask import (
    AutoMaskDirectories,
    BeamAveragedTod,
    MaskDocument,
    PolarizationMask,
    SingleBeamTod,
    beam_mask_path,
    discover_groups,
    load_beam_averaged_tod_with_auto_masks,
    load_single_beam_tod_with_auto_masks,
    load_mask_document,
    mask_document_region_summary,
    mask_path,
    masked_time_average,
    normalize_document,
    save_beam_mask_products,
    save_mask_document,
)


APP_VERSION = "native-web-v1"
DEFAULT_INPUT_DIR = Path(
    "/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/"
    "gzn_20260831/five_stripes_current_modules_0001_0010_v2/server_data/"
    "ZD2022_1_2/Dec+2654_22_09__20230716"
)
DEFAULT_MASK_DIR = Path(
    "/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/"
    "crafts_rfi_manual_masks/Dec+2654_22_09__20230716"
)


def _jsonable_document(document: MaskDocument) -> dict[str, Any]:
    return asdict(document)


def _viridis_lut() -> np.ndarray:
    # Compact samples from viridis, interpolated to 256 entries.  Keeping the
    # LUT here avoids importing Matplotlib and its rendering stack.
    anchors = np.asarray(
        [
            (68, 1, 84), (71, 44, 122), (59, 82, 139), (44, 113, 142),
            (33, 145, 140), (39, 173, 129), (92, 200, 99),
            (170, 220, 50), (253, 231, 37),
        ],
        dtype=np.float32,
    )
    x = np.linspace(0.0, 1.0, len(anchors))
    xi = np.linspace(0.0, 1.0, 256)
    return np.stack([np.interp(xi, x, anchors[:, channel]) for channel in range(3)], axis=1).astype(np.uint8)


VIRIDIS = _viridis_lut()
INVALID_RGB = np.asarray((108, 116, 126), dtype=np.uint8)


def _bounded_indices(length: int, low: int, high: int, maximum: int) -> np.ndarray:
    low = max(0, min(int(low), length - 1))
    high = max(low + 1, min(int(high), length))
    count = min(maximum, high - low)
    if count >= high - low:
        return np.arange(low, high, dtype=np.int64)
    # Center sampling is intentionally used for speed.  Zooming reveals the
    # exact channels/records, while automatic masks are separately max-pooled.
    return np.linspace(low, high - 1, count, dtype=np.int64)


def _mask_pool(
    mask: np.ndarray,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    height: int,
    width: int,
    thickness: int = 1,
) -> np.ndarray:
    """Max-pool a mask/count layer so narrow RFI remains visible."""
    source = np.ascontiguousarray(mask[y0:y1, x0:x1], dtype=np.uint8)
    source_height, source_width = source.shape
    factor_y = max(1, int(np.ceil(source_height / height)))
    factor_x = max(1, int(np.ceil(source_width / width)))
    pooled_height = int(np.ceil(source_height / factor_y))
    pooled_width = int(np.ceil(source_width / factor_x))
    padded = np.zeros((pooled_height * factor_y, pooled_width * factor_x), dtype=np.uint8)
    padded[:source_height, :source_width] = source
    pooled = padded.reshape(pooled_height, factor_y, pooled_width, factor_x).max(axis=(1, 3))
    if pooled.shape != (height, width):
        pooled = np.asarray(
            Image.fromarray(pooled).resize((width, height), resample=Image.Resampling.NEAREST),
            dtype=np.uint8,
        )
    # Display-only dilation along frequency.  Thin one-channel RFI remains easy
    # to see through the SSH preview without changing the underlying mask data.
    thickness = max(0, min(int(thickness), 4))
    thickened = pooled.copy()
    for offset in range(1, thickness + 1):
        thickened[:, offset:] = np.maximum(thickened[:, offset:], pooled[:, :-offset])
        thickened[:, :-offset] = np.maximum(thickened[:, :-offset], pooled[:, offset:])
    return thickened


def render_view_image(
    values: np.ndarray,
    auto_counts: np.ndarray | None,
    *,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    width: int,
    height: int,
    contrast: int,
    auto_width: int = 1,
    value_mask: np.ndarray | None = None,
    auto_total_beams: int = 19,
    vmin: float | None = None,
    vmax: float | None = None,
) -> tuple[bytes, float, float, float, float]:
    width = max(1, min(int(width), 3200))
    height = max(1, min(int(height), 1600))
    yi = _bounded_indices(values.shape[0], y0, y1, height)
    xi = _bounded_indices(values.shape[1], x0, x1, width)
    sampled = values[np.ix_(yi, xi)]
    if value_mask is not None:
        if value_mask.shape != values.shape:
            raise ValueError("value_mask must match values")
        sampled = sampled.copy()
        sampled[value_mask[np.ix_(yi, xi)]] = np.nan
    finite = sampled[np.isfinite(sampled)]
    percentiles = ((0.5, 99.5), (1.0, 99.0), (2.0, 98.0), (5.0, 95.0))
    if finite.size:
        auto_lo, auto_hi = np.percentile(
            finite, percentiles[int(contrast) % len(percentiles)]
        )
    else:
        auto_lo, auto_hi = -1.0, 1.0
    if not np.isfinite(auto_lo) or not np.isfinite(auto_hi) or auto_lo == auto_hi:
        auto_lo, auto_hi = -1.0, 1.0
    custom_limits_valid = (
        vmin is not None
        and vmax is not None
        and np.isfinite(vmin)
        and np.isfinite(vmax)
        and vmax > vmin
    )
    lo, hi = (float(vmin), float(vmax)) if custom_limits_valid else (auto_lo, auto_hi)
    invalid = ~np.isfinite(sampled)
    scaled = np.nan_to_num((sampled - lo) / (hi - lo), nan=0.0, posinf=1.0, neginf=0.0)
    rgb = VIRIDIS[np.clip(np.rint(scaled * 255.0), 0, 255).astype(np.uint8)]
    if auto_counts is not None:
        pooled = _mask_pool(
            auto_counts,
            y0,
            y1,
            x0,
            x1,
            len(yi),
            len(xi),
            thickness=auto_width,
        )
        denominator = max(1, int(auto_total_beams))
        alpha = np.minimum(0.62, 0.25 + pooled.astype(np.float32) / denominator * 0.37)
        active = pooled > 0
        orange = np.asarray((255, 126, 20), dtype=np.float32)
        rgb[active] = (
            rgb[active].astype(np.float32) * (1.0 - alpha[active, None])
            + orange * alpha[active, None]
        ).astype(np.uint8)
    # A fully invalid 19-beam pixel is NaN, not a very low measurement.  Paint
    # it neutral grey after all overlays so it cannot be mistaken for either
    # the bottom of viridis or an automatic-RFI highlight.
    rgb[invalid] = INVALID_RGB
    image = Image.fromarray(np.flipud(rgb))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=80, subsampling=1, optimize=False)
    return (
        stream.getvalue(),
        float(lo),
        float(hi),
        float(auto_lo),
        float(auto_hi),
    )


class NativeMaskState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.groups = discover_groups(args.input_dir)
        if not self.groups:
            raise FileNotFoundError(f"No *_T.fits groups found under {args.input_dir}")
        self.index = self._target_index(args.target)
        self.aggregate: BeamAveragedTod | None = None
        self.document = MaskDocument.empty(self.groups[self.index])
        self.frequency = np.empty(0, dtype=np.float64)
        self.mjd = np.empty(0, dtype=np.float64)
        self.local_medians = [0.0, 0.0]
        self.spectra = {
            False: np.empty((0, 2), dtype=np.float64),
            True: np.empty((0, 2), dtype=np.float64),
        }
        self.view_mode = "mean"
        self.selected_beam = "M01"
        self.single: SingleBeamTod | None = None
        self.single_medians = [0.0, 0.0]
        self.single_spectra = {
            False: np.empty((0, 2), dtype=np.float64),
            True: np.empty((0, 2), dtype=np.float64),
        }
        self.single_loading = False
        self.spectrum_revision = 0
        self.revision = 0
        self.dirty = False
        self.phase = "starting"
        self.busy = True
        self.message = "Starting native web UI"
        self.logs: list[dict[str, Any]] = []
        self.undo_stack: list[MaskDocument] = []
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crafts-data")
        self.render_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crafts-view")
        self.image_cache: dict[
            tuple[Any, ...], tuple[bytes, float, float, float, float]
        ] = {}
        self.load_token = 0
        self._log(self.message, "starting", True)

    @property
    def group(self):
        return self.groups[self.index]

    def _target_index(self, target: str | None) -> int:
        if not target:
            return 0
        target = target.strip()
        for index, group in enumerate(self.groups):
            if target in {group.key, group.serial, f"{group.scan_name}:{group.serial}"}:
                return index
        raise ValueError(f"Target {target!r} not found")

    def _log(self, message: str, phase: str | None = None, busy: bool | None = None) -> None:
        with self.lock:
            if phase is not None:
                self.phase = phase
            if busy is not None:
                self.busy = busy
            self.message = message
            self.logs.append({"time": time.strftime("%H:%M:%S"), "message": message})
            self.logs = self.logs[-120:]
        print(message, flush=True)

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            ready = self.aggregate is not None and self.phase not in {"loading", "beam-loading", "error"}
            group = self.group
            return {
                "version": APP_VERSION,
                "phase": self.phase,
                "busy": self.busy,
                "message": self.message,
                "ready": ready,
                "dirty": self.dirty,
                "revision": self.revision,
                "spectrum_revision": self.spectrum_revision,
                "index": self.index,
                "group_count": len(self.groups),
                "group": {"scan": group.scan_name, "serial": group.serial, "key": group.key, "beams": list(group.beams)},
                "groups": [{"index": i, "scan": g.scan_name, "serial": g.serial, "done": mask_path(self.args.mask_dir, g).exists()} for i, g in enumerate(self.groups)],
                "shape": [len(self.mjd), len(self.frequency)],
                "frequency_range": [float(self.frequency[0]), float(self.frequency[-1])] if len(self.frequency) else None,
                "frequency_step_mhz": float(np.nanmedian(np.diff(self.frequency))) if len(self.frequency) > 1 else None,
                "mjd_range": [float(self.mjd[0]), float(self.mjd[-1])] if len(self.mjd) else None,
                "local_medians": self.local_medians,
                "auto_available": dict(self.aggregate.layer_available_beams) if self.aggregate else {},
                "view_mode": self.view_mode,
                "selected_beam": self.selected_beam,
                "single_loaded_beam": self.single.beam if self.single is not None else None,
                "single_loading": self.single_loading,
                "document": _jsonable_document(self.document),
                "logs": list(self.logs),
            }

    async def load(self, index: int | None = None) -> None:
        with self.lock:
            if self.dirty and index is not None and index != self.index:
                raise ValueError("Unsaved edits: save or revert before changing nfile")
            if index is not None:
                self.index = index % len(self.groups)
            self.load_token += 1
            token = self.load_token
            self.aggregate = None
            self.frequency = np.empty(0)
            self.mjd = np.empty(0)
            self.spectra = {
                False: np.empty((0, 2), dtype=np.float64),
                True: np.empty((0, 2), dtype=np.float64),
            }
            self.view_mode = "mean"
            self.single = None
            self.single_medians = [0.0, 0.0]
            self.single_spectra = {
                False: np.empty((0, 2), dtype=np.float64),
                True: np.empty((0, 2), dtype=np.float64),
            }
            self.single_loading = False
            self.image_cache.clear()
            self._log(f"Loading {self.group.scan_name}:{self.group.serial} (19 beams)", "loading", True)
        loop = asyncio.get_running_loop()
        try:
            aggregate = await loop.run_in_executor(
                self.executor,
                lambda: load_beam_averaged_tod_with_auto_masks(
                    self.group,
                    AutoMaskDirectories(self.args.one_channel_dir, self.args.sat1380_dir),
                    block_records=self.args.block_records,
                    progress=lambda message: self._log(message, "loading", True),
                    require_all_masks=self.args.require_all_auto_masks,
                ),
            )
            with self.lock:
                if token != self.load_token:
                    return
                self.aggregate = aggregate
                self.frequency = aggregate.frequency_mhz
                self.mjd = aggregate.mjd
                self.document = load_mask_document(mask_path(self.args.mask_dir, self.group), self.group)
                self.document.auto_mask_files = aggregate.auto_mask_files
                self.local_medians = []
                for pol in (0, 1):
                    sample = aggregate.raw_mean[::max(1, aggregate.raw_mean.shape[0] // 400), ::max(1, aggregate.raw_mean.shape[1] // 1200), pol]
                    median = float(np.nanmedian(sample))
                    self.local_medians.append(median)
                    aggregate.raw_mean[:, :, pol] -= median
                    aggregate.auto_masked_mean[:, :, pol] -= median
                self.undo_stack.clear()
                self.dirty = False
                self.revision += 1
                self._log(f"Ready: {self.group.scan_name}:{self.group.serial}; drag to create a manual mask", "ready", False)
            self.schedule_spectrum()
        except Exception as error:
            self._log(f"ERROR: {error}", "error", False)
            traceback.print_exc()

    async def set_view(self, view_mode: str, beam: str | None = None) -> None:
        view_mode = str(view_mode).lower()
        if view_mode not in {"mean", "single"}:
            raise ValueError(f"Unknown view mode: {view_mode!r}")
        with self.lock:
            if self.aggregate is None:
                raise ValueError("Data are still loading")
            if view_mode == "mean":
                self.view_mode = "mean"
                self.image_cache.clear()
                self._log("Showing 19-beam mean", "ready", False)
                return
            beam = str(beam or self.selected_beam).upper()
            item = next((candidate for candidate in self.group.files if candidate.beam == beam), None)
            if item is None:
                raise ValueError(f"Beam {beam!r} is not available")
            self.selected_beam = beam
            if self.single is not None and self.single.beam == beam:
                self.view_mode = "single"
                self.image_cache.clear()
                self._log(f"Showing single beam {beam}", "ready", False)
                return
            group_key = self.group.key
            reference_frequency = self.frequency.copy()
            reference_mjd = self.mjd.copy()
            self.view_mode = "single"
            self.single_loading = True
            self._log(f"Loading single beam {beam}", "beam-loading", True)

        def read_single() -> tuple[SingleBeamTod, list[float]]:
            single = load_single_beam_tod_with_auto_masks(
                item,
                AutoMaskDirectories(self.args.one_channel_dir, self.args.sat1380_dir),
                block_records=self.args.block_records,
                require_all_masks=self.args.require_all_auto_masks,
            )
            if not np.allclose(single.frequency_mhz, reference_frequency, rtol=0.0, atol=1.0e-7):
                raise ValueError(f"Frequency axis mismatch for {beam}")
            if not np.allclose(single.mjd, reference_mjd, rtol=0.0, atol=1.0e-10):
                raise ValueError(f"MJD axis mismatch for {beam}")
            medians = []
            for pol in (0, 1):
                sample = single.raw[
                    ::max(1, single.raw.shape[0] // 400),
                    ::max(1, single.raw.shape[1] // 1200),
                    pol,
                ]
                median = float(np.nanmedian(sample))
                medians.append(median)
                single.raw[:, :, pol] -= median
            return single, medians

        try:
            single, medians = await asyncio.get_running_loop().run_in_executor(
                self.executor, read_single
            )
            with self.lock:
                if self.group.key != group_key:
                    return
                self.single = single
                self.single_medians = medians
                self.single_spectra = {
                    False: np.empty((0, 2), dtype=np.float64),
                    True: np.empty((0, 2), dtype=np.float64),
                }
                self.view_mode = "single"
                self.single_loading = False
                self.image_cache.clear()
                self._log(f"Showing single beam {beam}", "ready", False)
            self.schedule_spectrum()
        except Exception as error:
            with self.lock:
                self.single_loading = False
                self._log(f"SINGLE BEAM ERROR: {error}", "ready", False)
            raise

    def _push_undo(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.document))
        self.undo_stack = self.undo_stack[-50:]

    def edit(self, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.aggregate is None:
                raise ValueError("Data are still loading")
            mode = str(payload["mode"])
            pol = int(payload["pol"])
            targets = (0, 1) if bool(payload.get("both")) else (pol,)
            x0, x1 = sorted((float(payload["f0"]), float(payload["f1"])))
            start, stop = sorted((int(np.floor(payload["t0"])), int(np.ceil(payload["t1"]))))
            x0 = max(float(self.frequency[0]), x0)
            x1 = min(float(self.frequency[-1]), x1)
            start = max(0, start)
            stop = min(len(self.mjd), stop)
            if x1 <= x0 or stop <= start:
                raise ValueError("Empty selection")
            self._push_undo()
            if self.view_mode == "single":
                layers = self.document.beam_polarizations.setdefault(
                    self.selected_beam,
                    [PolarizationMask(), PolarizationMask()],
                )
                scope_label = self.selected_beam
            else:
                layers = self.document.polarizations
                scope_label = "Mean"
            for target in targets:
                item = layers[target]
                if mode == "frequency":
                    item.frequency_ranges_mhz.append([x0, x1])
                elif mode == "time":
                    item.time_ranges_records.append([start, stop])
                elif mode == "box":
                    item.boxes.append({"frequency_mhz": [x0, x1], "records": [start, stop]})
                elif mode == "delete":
                    item.frequency_ranges_mhz = [r for r in item.frequency_ranges_mhz if r[1] < x0 or r[0] > x1]
                    item.time_ranges_records = [r for r in item.time_ranges_records if r[1] <= start or r[0] >= stop]
                    item.boxes = [b for b in item.boxes if b["frequency_mhz"][1] < x0 or b["frequency_mhz"][0] > x1 or b["records"][1] <= start or b["records"][0] >= stop]
                else:
                    raise ValueError(f"Unknown edit mode: {mode}")
            normalize_document(self.document)
            self.document.done = False
            self.dirty = True
            self.revision += 1
            self._log(f"Manual {mode} edit added to {scope_label} ({'both pols' if len(targets) == 2 else f'pol {pol}'})", "ready", False)
        self.schedule_spectrum()

    def save_summary(self) -> dict[str, object]:
        with self.lock:
            return mask_document_region_summary(copy.deepcopy(self.document))

    def undo(self) -> None:
        with self.lock:
            if not self.undo_stack:
                raise ValueError("Nothing to undo")
            self.document = self.undo_stack.pop()
            self.dirty = True
            self.revision += 1
            self._log("Undid the last manual edit", "ready", False)
        self.schedule_spectrum()

    def revert(self) -> None:
        with self.lock:
            self.document = load_mask_document(mask_path(self.args.mask_dir, self.group), self.group)
            if self.aggregate is not None:
                self.document.auto_mask_files = self.aggregate.auto_mask_files
            self.undo_stack.clear()
            self.dirty = False
            self.revision += 1
            self._log("Reverted to the saved manual mask", "ready", False)
        self.schedule_spectrum()

    async def save(self) -> list[str]:
        with self.lock:
            if self.aggregate is None:
                raise ValueError("No loaded data")
            document = copy.deepcopy(self.document)
            document.done = True
            document.auto_mask_files = self.aggregate.auto_mask_files
            group = self.group
            frequency = self.frequency.copy()
            mjd = self.mjd.copy()
            self._log("Saving manual JSON and 19 per-beam NPZ products", "saving", True)
        json_target = mask_path(self.args.mask_dir, group)

        def write() -> list[Path]:
            save_mask_document(json_target, document)
            return save_beam_mask_products(self.args.mask_dir, group=group, frequency_mhz=frequency, mjd=mjd, document=document, json_path=json_target)

        try:
            paths = await asyncio.get_running_loop().run_in_executor(self.executor, write)
            with self.lock:
                self.document = document
                self.dirty = False
                self.revision += 1
                self._log(f"Saved {json_target.name} + {len(paths)} beam NPZ files", "ready", False)
            return [str(json_target), *map(str, paths)]
        except Exception as error:
            self._log(f"SAVE ERROR: {error}", "error", False)
            raise

    def schedule_spectrum(self) -> None:
        with self.lock:
            if self.aggregate is None:
                return
            revision = self.revision
            document = copy.deepcopy(self.document)
            raw_values = self.aggregate.raw_mean
            auto_values = self.aggregate.auto_masked_mean
            frequency = self.frequency
            single = self.single

        async def calculate() -> None:
            await asyncio.sleep(0.18)
            def calculate_all() -> tuple[dict[bool, np.ndarray], dict[bool, np.ndarray] | None]:
                mean_result = {
                    False: masked_time_average(raw_values, frequency, document),
                    True: masked_time_average(auto_values, frequency, document),
                }
                single_result = None
                if single is not None:
                    single_result = {
                        False: masked_time_average(
                            single.raw,
                            frequency,
                            document,
                            beam=single.beam,
                        ),
                        True: masked_time_average(
                            single.raw,
                            frequency,
                            document,
                            additional_mask=single.union_mask,
                            beam=single.beam,
                        ),
                    }
                return mean_result, single_result

            mean_result, single_result = await asyncio.get_running_loop().run_in_executor(
                self.executor, calculate_all
            )
            with self.lock:
                if revision == self.revision:
                    self.spectra = mean_result
                    if single is self.single and single_result is not None:
                        self.single_spectra = single_result
                    self.spectrum_revision += 1

        asyncio.create_task(calculate())

    def spectrum_payload(
        self,
        apply_auto: bool,
        view_mode: str = "mean",
        beam: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            view_mode = str(view_mode).lower()
            if view_mode == "single":
                requested_beam = str(beam or self.selected_beam).upper()
                if self.single is None or self.single.beam != requested_beam:
                    return {"revision": self.spectrum_revision, "apply_auto": bool(apply_auto), "view_mode": "single", "view_label": requested_beam, "frequency": [], "pol0": [], "pol1": []}
                selected = self.single_spectra[bool(apply_auto)]
                medians = self.single_medians
                view_label = requested_beam
            else:
                selected = self.spectra[bool(apply_auto)]
                medians = self.local_medians
                view_mode = "mean"
                view_label = "19-beam Mean"
            if not len(self.frequency) or not len(selected):
                return {"revision": self.spectrum_revision, "apply_auto": bool(apply_auto), "view_mode": view_mode, "view_label": view_label, "frequency": [], "pol0": [], "pol1": []}
            stride = max(1, len(self.frequency) // 2600)
            frequency = self.frequency[::stride]
            spectra = selected[::stride].copy()
            spectra[:, 0] += medians[0]
            spectra[:, 1] += medians[1]
            return {
                "revision": self.spectrum_revision,
                "apply_auto": bool(apply_auto),
                "view_mode": view_mode,
                "view_label": view_label,
                "frequency": frequency.tolist(),
                "pol0": [float(value) if np.isfinite(value) else None for value in spectra[:, 0]],
                "pol1": [float(value) if np.isfinite(value) else None for value in spectra[:, 1]],
            }

    def render(
        self, params: dict[str, Any]
    ) -> tuple[bytes, float, float, float, float]:
        with self.lock:
            if self.aggregate is None:
                raise ValueError("Data are still loading")
            pol = int(params["pol"])
            f0, f1 = sorted((float(params["f0"]), float(params["f1"])))
            t0, t1 = sorted((float(params["t0"]), float(params["t1"])))
            x0 = max(0, int(np.searchsorted(self.frequency, f0, side="left")))
            x1 = min(len(self.frequency), int(np.searchsorted(self.frequency, f1, side="right")))
            y0, y1 = max(0, int(np.floor(t0))), min(len(self.mjd), int(np.ceil(t1)))
            x1, y1 = max(x0 + 1, x1), max(y0 + 1, y1)
            apply_auto = bool(int(params.get("apply_auto", 1)))
            show_auto = bool(int(params.get("show_auto", 1)))
            width, height = int(params.get("width", 1000)), int(params.get("height", 300))
            contrast = int(params.get("contrast", 0)) % 4
            auto_width = max(0, min(int(params.get("auto_width", 1)), 4))
            vmin = float(params["vmin"]) if "vmin" in params else None
            vmax = float(params["vmax"]) if "vmax" in params else None
            view_mode = str(params.get("view_mode", "mean")).lower()
            beam = str(params.get("beam", self.selected_beam)).upper()
            if view_mode == "single":
                if self.single is None or self.single.beam != beam:
                    raise ValueError(f"Single beam {beam} is not loaded")
                values = self.single.raw[:, :, pol]
                value_mask = self.single.union_mask[:, :, pol] if apply_auto else None
                auto = self.single.union_mask[:, :, pol] if show_auto else None
                auto_total_beams = 1
            else:
                view_mode = "mean"
                beam = ""
                values = self.aggregate.auto_masked_mean[:, :, pol] if apply_auto else self.aggregate.raw_mean[:, :, pol]
                value_mask = None
                auto = self.aggregate.union_flagged_beam_count[:, :, pol] if show_auto else None
                auto_total_beams = 19
            key = (self.revision, view_mode, beam, pol, x0, x1, y0, y1, width, height, apply_auto, show_auto, contrast, auto_width, vmin, vmax)
            cached = self.image_cache.get(key)
            if cached is not None:
                return cached
        result = render_view_image(
            values,
            auto,
            y0=y0,
            y1=y1,
            x0=x0,
            x1=x1,
            width=width,
            height=height,
            contrast=contrast,
            auto_width=auto_width,
            value_mask=value_mask,
            auto_total_beams=auto_total_beams,
            vmin=vmin,
            vmax=vmax,
        )
        with self.lock:
            if len(self.image_cache) >= 20:
                self.image_cache.pop(next(iter(self.image_cache)))
            self.image_cache[key] = result
        return result


class BaseHandler(tornado.web.RequestHandler):
    @property
    def state(self) -> NativeMaskState:
        return self.settings["mask_state"]

    def write_json(self, value: Any, status: int = 200) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps(value, allow_nan=True))


class IndexHandler(BaseHandler):
    def get(self) -> None:
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store, max-age=0")
        self.write(self.settings["ui_html"])


class StateHandler(BaseHandler):
    def get(self) -> None:
        self.write_json(self.state.public_state())


class SpectrumHandler(BaseHandler):
    def get(self) -> None:
        self.write_json(
            self.state.spectrum_payload(
                bool(int(self.get_argument("apply_auto", "1"))),
                self.get_argument("view_mode", "mean"),
                self.get_argument("beam", None),
            )
        )


class ViewHandler(BaseHandler):
    async def get(self) -> None:
        try:
            data, low, high, auto_low, auto_high = await asyncio.get_running_loop().run_in_executor(self.state.render_executor, self.state.render, {key: self.get_argument(key) for key in self.request.arguments})
            self.set_header("Content-Type", "image/jpeg")
            self.set_header("Cache-Control", "no-store")
            self.set_header("X-Color-Low", f"{low:.12g}")
            self.set_header("X-Color-High", f"{high:.12g}")
            self.set_header("X-Auto-Color-Low", f"{auto_low:.12g}")
            self.set_header("X-Auto-Color-High", f"{auto_high:.12g}")
            self.write(data)
        except Exception as error:
            self.send_error(503, reason=str(error))


class ActionHandler(BaseHandler):
    async def post(self) -> None:
        try:
            payload = json.loads(self.request.body or b"{}")
            action = payload.get("action")
            result: dict[str, Any] = {}
            if action == "edit":
                self.state.edit(payload)
            elif action == "undo":
                self.state.undo()
            elif action == "revert":
                self.state.revert()
            elif action == "save":
                result["paths"] = await self.state.save()
            elif action == "save_summary":
                result["summary"] = self.state.save_summary()
            elif action == "set_view":
                await self.state.set_view(
                    str(payload.get("view_mode", "mean")),
                    payload.get("beam"),
                )
            elif action in {"previous", "next", "load"}:
                if action == "previous":
                    index = self.state.index - 1
                elif action == "next":
                    index = self.state.index + 1
                else:
                    index = int(payload["index"])
                asyncio.create_task(self.state.load(index))
            else:
                raise ValueError(f"Unknown action: {action!r}")
            result["state"] = self.state.public_state()
            self.write_json(result)
        except Exception as error:
            self.write_json({"error": str(error), "state": self.state.public_state()}, 400)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="nfile, group key, or SCAN:NFILE")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--mask-dir", type=Path, default=DEFAULT_MASK_DIR)
    parser.add_argument("--one-channel-dir", type=Path)
    parser.add_argument("--sat1380-dir", type=Path)
    parser.add_argument("--require-all-auto-masks", action="store_true")
    parser.add_argument("--block-records", type=int, default=64)
    parser.add_argument("--port", type=int, default=8992)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--ui-html", type=Path, default=Path(__file__).with_name("crafts_tod_native_ui.html"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state = NativeMaskState(args)
    app = tornado.web.Application(
        [(r"/", IndexHandler), (r"/api/state", StateHandler), (r"/api/spectrum", SpectrumHandler), (r"/api/view.jpg", ViewHandler), (r"/api/action", ActionHandler)],
        mask_state=state,
        ui_html=args.ui_html.read_text(encoding="utf-8"),
        compress_response=True,
    )
    app.listen(args.port, address=args.address)
    print(f"Native UI listening on http://{args.address}:{args.port}/", flush=True)
    asyncio.get_event_loop().create_task(state.load())
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
