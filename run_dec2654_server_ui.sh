#!/usr/bin/env bash
set -euo pipefail

scan="Dec+2654_22_09"
nfile="${1:-0002}"
if [[ "$nfile" =~ ^[0-9]+$ ]]; then
  printf -v nfile "%04d" "$((10#$nfile))"
else
  echo "用法: bash run_dec2654_server_ui.sh [nfile编号，例如 2 或 0002]" >&2
  exit 2
fi

remote_app="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_mask_manual_x11"
input_dir="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/gzn_20260831/five_stripes_current_modules_0001_0010_v2/server_data/ZD2022_1_2/Dec+2654_22_09__20230716"
mask_dir="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_manual_masks/Dec+2654_22_09__20230716"
remote_python="/home/zhouyunfan/anaconda3/bin/python"
target="${scan}:${nfile}"

echo "服务器数据目录: $input_dir"
echo "正在列出可用的 _T nfile 组..."
ssh crafts \
  "cd '$remote_app' && '$remote_python' crafts_tod_visual_mask.py --input-dir '$input_dir' --mask-dir '$mask_dir' --list"

echo
echo "即将打开: $target"
echo "首次读取 19 beams 和自动 RFI mask 预计需要约 2–3 分钟。"

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
  echo "XQuartz 没有提供 DISPLAY；请打开 XQuartz 后重新运行。" >&2
  exit 1
fi
export DISPLAY="$xquartz_display"

exec ssh -Y crafts \
  "mkdir -p '$mask_dir' && cd '$remote_app' && exec env MPLBACKEND=TkAgg PYTHONUNBUFFERED=1 '$remote_python' crafts_tod_visual_mask.py '$target' --input-dir '$input_dir' --mask-dir '$mask_dir' --require-all-auto-masks --block-records 64"
