# CRAFTS TOD Manual Mask

This tool groups CRAFTS TOD FITS files by scan name and four-digit file serial,
then averages every available beam before showing two polarization waterfalls.
Masks are stored as small JSON sidecars and the FITS files are never modified.
The third panel shows the two polarization spectra after masking, calculated by
averaging the remaining valid records at every frequency.

The expected filename pattern is:

```text
<scan>_arcdrift-M<beam>_<window>_<serial>_<product>.fits
```

For example, the M01, M05, and M09 files below are one list entry:

```text
Dec+2654_22_09_arcdrift-M01_W_0007_T_swr_bpr.fits
Dec+2654_22_09_arcdrift-M05_W_0007_T_swr_bpr.fits
Dec+2654_22_09_arcdrift-M09_W_0007_T_swr_bpr.fits
```

## Run

Activate the local base environment first:

```bash
source ~/conda.sh
conda activate base
cd /Users/zz/work/FAST/crafts/RFI_flag/bpr/crafts_tod_manual_mask
```

Print the grouped list:

```bash
python crafts_tod_visual_mask.py --list
```

Open an entry using only its one-based list number:

```bash
python crafts_tod_visual_mask.py 1
```

The default input directory is the parent `bpr` directory. It can be changed
without editing the code:

```bash
python crafts_tod_visual_mask.py --input-dir /path/to/bpr 1
```

## Controls

- `Freq` or `f`: drag a frequency interval that applies to all records.
- `Time` or `t`: drag a record interval that applies to all frequencies.
- `Box` or `b`: drag a local time-frequency rectangle.
- `Delete` or `d`: remove mask items intersecting the dragged rectangle.
- `both pols`: apply the next edit to both polarizations; off by default.
- `Undo` or `u`: undo the last edit in the current entry.
- `Revert`: reload the last saved JSON mask.
- `Save` or `s`: save the JSON sidecar and mark the list entry `done`.
- `Prev`/`Next` or `p`/`n`: move between list entries after saving/reverting.
- `Pan` or `g`: enable left-drag frequency panning.
- Mouse wheel: zoom around the cursor.
- `Shift` + mouse wheel over a waterfall: zoom the record axis around the cursor;
  both polarization waterfalls remain synchronized.
- `vertical wheel` or `v`: reliable vertical-wheel mode for GUI backends that do
  not report the Shift modifier; while enabled, an ordinary wheel zooms records.
- `,`/`.`: move by half of the visible frequency window.
- `[`/`]`: move by 8 MHz.
- `0`: restore the full frequency range.
- `Contrast` or `c`: cycle through four robust display stretches.
- Right-drag: adjust both polarization color scales together. Horizontal motion
  shifts the color center; vertical motion changes the displayed dynamic range.
- `q`: close the window.

Red overlays are masks. The underlying waterfall remains visible so an RFI
feature can still be inspected after it is selected. Edits in one panel affect
only that polarization unless `both pols` is checked.

## Output And Reuse

Masks are written under `crafts_tod_manual_mask/masks/` as compact JSON files.
Frequency intervals are in MHz and record intervals use half-open Python slices.
The same mask applies to every beam that contributed to the displayed average.

The mask can be expanded or applied in later processing without loading the GUI:

```python
from crafts_tod_mask import apply_mask, load_mask_document

document = load_mask_document(mask_json_path, group)
masked_tod = apply_mask(tod_time_frequency_pol, frequency_mhz, document)
```

Use `build_mask(...)` when a Boolean mask is preferred over NaN-filled data.

## Validation

Check that all beams in every group have matching time/frequency axes:

```bash
python crafts_tod_visual_mask.py --validate
```

Run the small mask-model tests:

```bash
python -m pytest -q test_crafts_tod_mask.py
```
