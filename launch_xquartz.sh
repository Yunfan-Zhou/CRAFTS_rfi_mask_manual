#!/usr/bin/env bash
set -euo pipefail

target="${1:-Dec+2654_22_09:0002}"
case "$target" in
  *[!A-Za-z0-9+_.:-]*)
    echo "Invalid target: $target" >&2
    exit 2
    ;;
esac

open -a XQuartz
xquartz_display=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  xquartz_display="$(launchctl getenv DISPLAY)"
  if [[ -n "$xquartz_display" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "$xquartz_display" ]]; then
  echo "XQuartz did not publish DISPLAY; open XQuartz and retry." >&2
  exit 1
fi
export DISPLAY="$xquartz_display"

remote_app="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_mask_manual_x11"
input_root="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/gzn_20260831/five_stripes_current_modules_0001_0010_v2"
mask_root="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_manual_masks"

exec ssh -Y crafts \
  "mkdir -p '$mask_root' && cd '$remote_app' && exec env MPLBACKEND=TkAgg PYTHONUNBUFFERED=1 /home/zhouyunfan/anaconda3/bin/python crafts_tod_visual_mask.py '$target' --input-dir '$input_root' --mask-dir '$mask_root' --require-all-auto-masks --block-records 64"
