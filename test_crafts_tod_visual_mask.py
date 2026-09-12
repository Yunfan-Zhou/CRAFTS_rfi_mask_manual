from __future__ import annotations

from types import SimpleNamespace

import matplotlib.pyplot as plt
import numpy as np

from crafts_tod_visual_mask import (
    CraftsTodMaskEditor,
    _console_page_html,
    configure_webagg,
    contrast_limits_from_drag,
    status_message,
    web_status_payload,
)


def test_right_drag_adjusts_both_polarization_limits() -> None:
    start = ((-1.0, 1.0), (-2.0, 2.0))
    shifted = contrast_limits_from_drag(start, 50.0, 0.0, 100.0, 100.0)
    assert np.allclose(shifted[0], (0.0, 2.0))
    assert np.allclose(shifted[1], (0.0, 4.0))

    widened = contrast_limits_from_drag(start, 0.0, 25.0, 100.0, 100.0)
    assert widened[0][1] - widened[0][0] > 2.0
    assert widened[1][1] - widened[1][0] > 4.0


def test_webagg_port_validation() -> None:
    original_backend = plt.get_backend
    original_values = {
        key: plt.rcParams[key]
        for key in (
            "webagg.address", "webagg.port", "webagg.port_retries",
            "webagg.open_in_browser",
        )
    }
    try:
        plt.get_backend = lambda: "WebAgg"
        configure_webagg(8991)
        assert plt.rcParams["webagg.address"] == "127.0.0.1"
        assert plt.rcParams["webagg.port"] == 8991
        assert plt.rcParams["webagg.port_retries"] == 1
        assert not plt.rcParams["webagg.open_in_browser"]
    finally:
        plt.get_backend = original_backend
        plt.rcParams.update(original_values)


def test_webagg_rejects_unsafe_port() -> None:
    with np.testing.assert_raises(ValueError):
        configure_webagg(80)


def test_web_console_contains_live_status_and_mouse_feedback() -> None:
    html = _console_page_html([1], "")
    assert "服务器运行状态" in html
    assert "/status.json" in html
    assert "mousedown" in html
    assert "正在拖动选区" in html
    assert "join('\\n')" in html
    assert "motion_notify" in html
    assert "setTimeout(pollStatus, 1500)" in html
    assert "AbortController" not in html


def test_status_payload_records_busy_state_and_log() -> None:
    status_message("unit-test loading", phase="loading", busy=True)
    payload = web_status_payload()
    assert payload["busy"] is True
    assert payload["phase"] == "loading"
    assert payload["message"] == "unit-test loading"
    assert payload["lines"][-1].endswith("unit-test loading")


def test_shift_scroll_zooms_both_waterfall_record_axes() -> None:
    figure, panels = plt.subplots(3, 1)
    editor = CraftsTodMaskEditor.__new__(CraftsTodMaskEditor)
    editor.fig = figure
    editor.axes = [panels[0], panels[1]]
    editor.spectrum_axis = panels[2]
    editor.frequency = np.linspace(100.0, 110.0, 32)
    editor.full_xlim = (100.0, 110.0)
    editor.full_ylim = (0.0, 100.0)
    editor.shift_held = False
    editor.vertical_wheel_enabled = False
    for axis in editor.axes:
        axis.set_xlim(*editor.full_xlim)
        axis.set_ylim(*editor.full_ylim)

    event = SimpleNamespace(
        inaxes=editor.axes[0],
        xdata=105.0,
        ydata=50.0,
        button="up",
        key="shift",
    )
    editor.on_scroll(event)

    assert np.isclose(np.diff(editor.axes[0].get_ylim())[0], 80.0)
    assert np.allclose(editor.axes[0].get_ylim(), editor.axes[1].get_ylim())
    assert np.allclose(editor.axes[0].get_xlim(), editor.full_xlim)
    plt.close(figure)


def test_tracked_shift_and_vertical_wheel_fallback_zoom_records() -> None:
    figure, panels = plt.subplots(3, 1)
    editor = CraftsTodMaskEditor.__new__(CraftsTodMaskEditor)
    editor.fig = figure
    editor.axes = [panels[0], panels[1]]
    editor.spectrum_axis = panels[2]
    editor.frequency = np.linspace(100.0, 110.0, 32)
    editor.full_xlim = (100.0, 110.0)
    editor.full_ylim = (0.0, 100.0)
    editor.shift_held = True
    editor.vertical_wheel_enabled = False
    for axis in editor.axes:
        axis.set_xlim(*editor.full_xlim)
        axis.set_ylim(*editor.full_ylim)

    event = SimpleNamespace(
        inaxes=editor.axes[0],
        xdata=105.0,
        ydata=50.0,
        button="up",
        key=None,
    )
    editor.on_scroll(event)
    assert np.isclose(np.diff(editor.axes[0].get_ylim())[0], 80.0)

    editor.set_ylim(*editor.full_ylim)
    editor.shift_held = False
    editor.vertical_wheel_enabled = True
    editor.on_scroll(event)
    assert np.isclose(np.diff(editor.axes[0].get_ylim())[0], 80.0)
    plt.close(figure)
