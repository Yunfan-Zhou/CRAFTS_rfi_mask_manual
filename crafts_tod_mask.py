"""I/O and compact mask representation for CRAFTS TOD FITS files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Callable, Iterable

import numpy as np
from astropy.io import fits


FILENAME_RE = re.compile(
    r"^(?P<scan>.+?)_arcdrift-(?P<beam>M\d{2})_"
    r"(?P<window>[^_]+)_(?P<serial>\d{4})(?P<tail>_.+)\.fits$",
    re.IGNORECASE,
)
MASK_FORMAT = "crafts-tod-manual-mask-v1"


@dataclass(frozen=True)
class TodFile:
    path: Path
    scan_name: str
    beam: str
    window: str
    serial: str
    tail: str


@dataclass(frozen=True)
class TodGroup:
    scan_name: str
    serial: str
    window: str
    tail: str
    files: tuple[TodFile, ...]

    @property
    def key(self) -> str:
        return f"{self.scan_name}__{self.serial}"

    @property
    def beams(self) -> tuple[str, ...]:
        return tuple(item.beam for item in self.files)


@dataclass
class PolarizationMask:
    frequency_ranges_mhz: list[list[float]] = field(default_factory=list)
    time_ranges_records: list[list[int]] = field(default_factory=list)
    boxes: list[dict[str, list[float] | list[int]]] = field(default_factory=list)


@dataclass
class MaskDocument:
    scan_name: str
    serial: str
    beam_files: dict[str, str]
    polarizations: list[PolarizationMask] = field(
        default_factory=lambda: [PolarizationMask(), PolarizationMask()]
    )
    done: bool = False
    format: str = MASK_FORMAT
    saved_at_utc: str = ""
    frequency_unit: str = "MHz"
    time_unit: str = "record"
    interval_convention: str = "frequency closed; record intervals half-open [start, stop)"

    @classmethod
    def empty(cls, group: TodGroup) -> "MaskDocument":
        return cls(
            scan_name=group.scan_name,
            serial=group.serial,
            beam_files={item.beam: str(item.path.resolve()) for item in group.files},
        )


def parse_tod_filename(path: str | Path) -> TodFile | None:
    path = Path(path)
    match = FILENAME_RE.match(path.name)
    if match is None:
        return None
    values = match.groupdict()
    return TodFile(
        path=path,
        scan_name=values["scan"],
        beam=values["beam"].upper(),
        window=values["window"],
        serial=values["serial"],
        tail=values["tail"],
    )


def discover_groups(input_dir: str | Path) -> list[TodGroup]:
    """Group files by scan name and four-digit file serial across all beams."""
    input_dir = Path(input_dir)
    grouped: dict[tuple[str, str, str, str], list[TodFile]] = {}
    for path in sorted(input_dir.glob("*.fits")):
        item = parse_tod_filename(path)
        if item is None:
            continue
        key = (item.scan_name, item.serial, item.window, item.tail)
        grouped.setdefault(key, []).append(item)

    result: list[TodGroup] = []
    for (scan_name, serial, window, tail), files in grouped.items():
        files = sorted(files, key=lambda item: item.beam)
        result.append(
            TodGroup(
                scan_name=scan_name,
                serial=serial,
                window=window,
                tail=tail,
                files=tuple(files),
            )
        )
    return sorted(result, key=lambda group: (group.scan_name, int(group.serial)))


def mask_path(mask_dir: str | Path, group: TodGroup) -> Path:
    safe_scan = re.sub(r"[^A-Za-z0-9+_.-]+", "_", group.scan_name)
    return Path(mask_dir) / f"{safe_scan}_{group.serial}_manual_mask.json"


def _merge_float_ranges(ranges: Iterable[Iterable[float]]) -> list[list[float]]:
    normalized = sorted(
        ([min(float(a), float(b)), max(float(a), float(b))] for a, b in ranges),
        key=lambda item: item[0],
    )
    merged: list[list[float]] = []
    for lo, hi in normalized:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return merged


def _merge_record_ranges(ranges: Iterable[Iterable[int]]) -> list[list[int]]:
    normalized = sorted(
        ([min(int(a), int(b)), max(int(a), int(b))] for a, b in ranges),
        key=lambda item: item[0],
    )
    merged: list[list[int]] = []
    for start, stop in normalized:
        if stop <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, stop])
        else:
            merged[-1][1] = max(merged[-1][1], stop)
    return merged


def normalize_document(document: MaskDocument) -> MaskDocument:
    while len(document.polarizations) < 2:
        document.polarizations.append(PolarizationMask())
    document.polarizations = document.polarizations[:2]
    for pol_mask in document.polarizations:
        pol_mask.frequency_ranges_mhz = _merge_float_ranges(
            pol_mask.frequency_ranges_mhz
        )
        pol_mask.time_ranges_records = _merge_record_ranges(
            pol_mask.time_ranges_records
        )
        normalized_boxes = []
        for box in pol_mask.boxes:
            freq_lo, freq_hi = box["frequency_mhz"]
            rec_start, rec_stop = box["records"]
            rec_start, rec_stop = sorted((int(rec_start), int(rec_stop)))
            if rec_stop <= rec_start:
                continue
            normalized_boxes.append(
                {
                    "frequency_mhz": [
                        min(float(freq_lo), float(freq_hi)),
                        max(float(freq_lo), float(freq_hi)),
                    ],
                    "records": [rec_start, rec_stop],
                }
            )
        pol_mask.boxes = normalized_boxes
    return document


def save_mask_document(path: str | Path, document: MaskDocument) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.saved_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    normalize_document(document)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(document), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_mask_document(path: str | Path, group: TodGroup) -> MaskDocument:
    path = Path(path)
    if not path.exists():
        return MaskDocument.empty(group)
    values = json.loads(path.read_text(encoding="utf-8"))
    if values.get("format") != MASK_FORMAT:
        raise ValueError(f"Unsupported mask format in {path}: {values.get('format')!r}")
    polarizations = [PolarizationMask(**item) for item in values.pop("polarizations", [])]
    document = MaskDocument(polarizations=polarizations, **values)
    if document.scan_name != group.scan_name or document.serial != group.serial:
        raise ValueError(f"Mask file {path} does not match {group.key}")
    document.beam_files = {
        item.beam: str(item.path.resolve()) for item in group.files
    }
    return normalize_document(document)


def mask_is_done(path: str | Path) -> bool:
    try:
        return bool(json.loads(Path(path).read_text(encoding="utf-8")).get("done"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _select_hdus(hdul: fits.HDUList) -> tuple[int, int, int]:
    arrays = [(index, hdu, getattr(hdu, "data", None)) for index, hdu in enumerate(hdul)]
    arrays = [(index, hdu, data) for index, hdu, data in arrays if data is not None]

    tod_candidates = [item for item in arrays if np.ndim(item[2]) == 3]
    if not tod_candidates:
        raise ValueError("No three-dimensional TOD image was found")
    tod_index, _, tod = max(tod_candidates, key=lambda item: np.size(item[2]))

    one_dimensional = [item for item in arrays if np.ndim(item[2]) == 1]
    frequency = next(
        (item for item in one_dimensional if "freq" in item[1].name.lower()),
        None,
    )
    time = next(
        (
            item
            for item in one_dimensional
            if any(token in item[1].name.lower() for token in ("mjd", "time", "date"))
        ),
        None,
    )
    if frequency is None:
        frequency = next(
            (item for item in one_dimensional if len(item[2]) in tod.shape),
            None,
        )
    if time is None:
        remaining = [item for item in one_dimensional if frequency is None or item[0] != frequency[0]]
        time = next((item for item in remaining if len(item[2]) in tod.shape), None)
    if frequency is None or time is None:
        raise ValueError("Could not identify the frequency and time vectors")
    return frequency[0], time[0], tod_index


def _read_axes(path: Path) -> tuple[np.ndarray, np.ndarray, tuple[int, ...], tuple[int, int, int]]:
    with fits.open(path, memmap=True) as hdul:
        freq_index, time_index, tod_index = _select_hdus(hdul)
        frequency = np.asarray(hdul[freq_index].data, dtype=np.float64)
        time = np.asarray(hdul[time_index].data, dtype=np.float64)
        unit = str(hdul[freq_index].header.get("BUNIT", "")).strip().lower()
        if unit in {"hz", "hertz"} or np.nanmedian(np.abs(frequency)) > 1.0e5:
            frequency = frequency / 1.0e6
        shape = tuple(int(value) for value in hdul[tod_index].data.shape)
    return frequency, time, shape, (freq_index, time_index, tod_index)


def _tod_axes(shape: tuple[int, ...], ntime: int, nfreq: int) -> tuple[int, int, int]:
    time_axes = [index for index, length in enumerate(shape) if length == ntime]
    freq_axes = [index for index, length in enumerate(shape) if length == nfreq]
    for time_axis in time_axes:
        for freq_axis in freq_axes:
            if time_axis == freq_axis:
                continue
            remaining = ({0, 1, 2} - {time_axis, freq_axis}).pop()
            if shape[remaining] >= 2:
                return time_axis, freq_axis, remaining
    raise ValueError(
        f"TOD shape {shape} cannot be matched to time={ntime}, frequency={nfreq}, pol>=2"
    )


def validate_group(group: TodGroup) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int]]:
    if not group.files:
        raise ValueError(f"No beam files in group {group.key}")
    reference_freq, reference_time, reference_shape, _ = _read_axes(group.files[0].path)
    _tod_axes(reference_shape, len(reference_time), len(reference_freq))
    for item in group.files[1:]:
        frequency, time, shape, _ = _read_axes(item.path)
        _tod_axes(shape, len(time), len(frequency))
        if frequency.shape != reference_freq.shape or not np.allclose(
            frequency, reference_freq, rtol=0.0, atol=1.0e-7, equal_nan=True
        ):
            raise ValueError(f"Frequency axis mismatch: {item.path}")
        if time.shape != reference_time.shape or not np.allclose(
            time, reference_time, rtol=0.0, atol=1.0e-10, equal_nan=True
        ):
            raise ValueError(f"Time axis mismatch: {item.path}")
    return reference_freq, reference_time, (
        len(reference_time),
        len(reference_freq),
        2,
    )


def load_beam_averaged_tod(
    group: TodGroup,
    block_records: int = 128,
    progress: Callable[[str], None] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load two polarizations and average matching TOD pixels across available beams."""
    frequency, time, output_shape = validate_group(group)
    total = np.zeros(output_shape, dtype=np.float32)
    count = np.zeros(output_shape, dtype=np.uint8)

    for beam_number, item in enumerate(group.files, start=1):
        if progress is not None:
            progress(f"Loading {item.beam} ({beam_number}/{len(group.files)}): {item.path.name}")
        with fits.open(item.path, memmap=True) as hdul:
            freq_index, time_index, tod_index = _select_hdus(hdul)
            del freq_index, time_index
            raw = hdul[tod_index].data
            source_axes = _tod_axes(raw.shape, len(time), len(frequency))
            canonical = np.moveaxis(raw, source_axes, (0, 1, 2))
            for start in range(0, len(time), block_records):
                stop = min(start + block_records, len(time))
                block = np.asarray(canonical[start:stop, :, :2], dtype=np.float32)
                valid = np.isfinite(block)
                total[start:stop] += np.where(valid, block, 0.0)
                count[start:stop] += valid

    np.divide(total, count, out=total, where=count > 0)
    total[count == 0] = np.nan
    del count

    if frequency[0] > frequency[-1]:
        frequency = frequency[::-1].copy()
        total = total[:, ::-1, :].copy()
    return frequency, time, total


def build_mask(
    frequency_mhz: np.ndarray,
    ntime: int,
    document: MaskDocument,
    polarization: int,
) -> np.ndarray:
    """Expand one polarization's compact ranges into a time-frequency mask."""
    if polarization not in (0, 1):
        raise ValueError("polarization must be 0 or 1")
    pol_mask = document.polarizations[polarization]
    mask = np.zeros((ntime, len(frequency_mhz)), dtype=bool)
    for lo, hi in pol_mask.frequency_ranges_mhz:
        mask[:, (frequency_mhz >= lo) & (frequency_mhz <= hi)] = True
    for start, stop in pol_mask.time_ranges_records:
        mask[max(0, start) : min(ntime, stop), :] = True
    for box in pol_mask.boxes:
        lo, hi = box["frequency_mhz"]
        start, stop = box["records"]
        freq_selection = (frequency_mhz >= lo) & (frequency_mhz <= hi)
        mask[max(0, int(start)) : min(ntime, int(stop)), freq_selection] = True
    return mask


def apply_mask(
    tod: np.ndarray,
    frequency_mhz: np.ndarray,
    document: MaskDocument,
    copy: bool = True,
) -> np.ndarray:
    """Apply both polarization masks to canonical TOD shaped (time, frequency, pol)."""
    if tod.ndim != 3 or tod.shape[2] < 2:
        raise ValueError("TOD must have shape (time, frequency, polarization>=2)")
    result = np.array(tod, copy=True) if copy else tod
    for polarization in (0, 1):
        expanded = build_mask(frequency_mhz, tod.shape[0], document, polarization)
        result[:, :, polarization][expanded] = np.nan
    return result


def masked_time_average(
    tod: np.ndarray,
    frequency_mhz: np.ndarray,
    document: MaskDocument,
) -> np.ndarray:
    """Average unmasked records for each frequency and polarization.

    A fully frequency-masked channel becomes NaN. Time and box masks remove only
    their selected records, so the remaining samples still contribute.
    """
    if tod.ndim != 3 or tod.shape[2] < 2:
        raise ValueError("TOD must have shape (time, frequency, polarization>=2)")
    if tod.shape[1] != len(frequency_mhz):
        raise ValueError("TOD frequency axis does not match frequency_mhz")

    spectra = np.full((tod.shape[1], 2), np.nan, dtype=np.float64)
    for polarization in (0, 1):
        values = np.asarray(tod[:, :, polarization])
        mask = build_mask(frequency_mhz, tod.shape[0], document, polarization)
        valid = np.isfinite(values) & ~mask
        count = np.sum(valid, axis=0, dtype=np.int64)
        total = np.sum(values, axis=0, dtype=np.float64, where=valid)
        np.divide(total, count, out=spectra[:, polarization], where=count > 0)
    return spectra
