from __future__ import annotations

from pathlib import Path

import numpy as np

from crafts_tod_mask import (
    MaskDocument,
    PolarizationMask,
    apply_mask,
    build_mask,
    discover_groups,
    load_mask_document,
    mask_is_done,
    masked_time_average,
    parse_tod_filename,
    save_mask_document,
)


def test_parse_and_group_sample_files(tmp_path: Path) -> None:
    names = [
        "Dec+2654_22_09_arcdrift-M09_W_0007_T_swr_bpr.fits",
        "Dec+2654_22_09_arcdrift-M01_W_0007_T_swr_bpr.fits",
        "Dec+2654_22_09_arcdrift-M05_W_0007_T_swr_bpr.fits",
    ]
    for name in names:
        (tmp_path / name).touch()

    parsed = parse_tod_filename(tmp_path / names[0])
    assert parsed is not None
    assert parsed.scan_name == "Dec+2654_22_09"
    assert parsed.beam == "M09"
    assert parsed.serial == "0007"

    groups = discover_groups(tmp_path)
    assert len(groups) == 1
    assert groups[0].beams == ("M01", "M05", "M09")


def test_expand_and_apply_independent_polarization_masks() -> None:
    frequency = np.arange(100.0, 106.0)
    document = MaskDocument(
        scan_name="test",
        serial="0001",
        beam_files={},
        polarizations=[
            PolarizationMask(
                frequency_ranges_mhz=[[101.0, 102.0]],
                boxes=[{"frequency_mhz": [104.0, 105.0], "records": [1, 3]}],
            ),
            PolarizationMask(time_ranges_records=[[2, 4]]),
        ],
    )

    mask0 = build_mask(frequency, 4, document, 0)
    mask1 = build_mask(frequency, 4, document, 1)
    assert mask0[:, 1:3].all()
    assert mask0[1:3, 4:6].all()
    assert not mask0[0, 0]
    assert mask1[2:4].all()
    assert not mask1[:2].any()

    data = np.ones((4, 6, 2), dtype=np.float32)
    masked = apply_mask(data, frequency, document)
    assert np.isnan(masked[:, :, 0]).sum() == mask0.sum()
    assert np.isnan(masked[:, :, 1]).sum() == mask1.sum()
    assert np.isfinite(data).all()


def test_mask_json_round_trip_and_done_state(tmp_path: Path) -> None:
    filename = "Dec+2654_22_09_arcdrift-M01_W_0008_T_swr_bpr.fits"
    (tmp_path / filename).touch()
    group = discover_groups(tmp_path)[0]
    document = MaskDocument.empty(group)
    document.polarizations[0].frequency_ranges_mhz = [[1380.2, 1380.1]]
    document.polarizations[1].time_ranges_records = [[20, 10]]
    document.done = True
    output = tmp_path / "mask.json"

    save_mask_document(output, document)
    restored = load_mask_document(output, group)

    assert mask_is_done(output)
    assert restored.polarizations[0].frequency_ranges_mhz == [[1380.1, 1380.2]]
    assert restored.polarizations[1].time_ranges_records == [[10, 20]]


def test_masked_time_average_keeps_unmasked_box_samples() -> None:
    frequency = np.array([100.0, 101.0, 102.0])
    data = np.zeros((4, 3, 2), dtype=np.float32)
    data[:, :, 0] = np.arange(4, dtype=np.float32)[:, None]
    data[:, :, 1] = (10.0 + np.arange(4, dtype=np.float32))[:, None]
    document = MaskDocument(
        scan_name="test",
        serial="0001",
        beam_files={},
        polarizations=[
            PolarizationMask(
                frequency_ranges_mhz=[[102.0, 102.0]],
                boxes=[{"frequency_mhz": [101.0, 101.0], "records": [0, 2]}],
            ),
            PolarizationMask(time_ranges_records=[[0, 2]]),
        ],
    )

    spectra = masked_time_average(data, frequency, document)

    assert np.isclose(spectra[0, 0], 1.5)
    assert np.isclose(spectra[1, 0], 2.5)
    assert np.isnan(spectra[2, 0])
    assert np.allclose(spectra[:, 1], 12.5)
