#!/usr/bin/env bash
set -euo pipefail

scan="Dec+2654_22_09"
nfile="${1:-0002}"
port="${2:-8988}"

if [[ "$nfile" =~ ^[0-9]+$ ]]; then
  printf -v nfile "%04d" "$((10#$nfile))"
else
  echo "用法: ./run_dec2654_web_ui.sh [nfile，例如 2 或 0002] [本地端口，默认8988]" >&2
  exit 2
fi
if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
  echo "端口必须是 1024--65535 的整数。" >&2
  exit 2
fi

remote_app="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_mask_manual_x11"
input_dir="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/gzn_20260831/five_stripes_current_modules_0001_0010_v2/server_data/ZD2022_1_2/Dec+2654_22_09__20230716"
mask_dir="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_manual_masks/Dec+2654_22_09__20230716"
runtime_dir="/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/crafts_rfi_webagg_runtime"
remote_python="/home/zhouyunfan/anaconda3/bin/python"
target="${scan}:${nfile}"
url="http://127.0.0.1:${port}/"
remote_pidfile="${runtime_dir}/webagg_${port}.pid"

if curl -fsS --max-time 1 "$url" >/dev/null 2>&1; then
  echo "本地端口 ${port} 已被占用；可指定另一个端口，例如：" >&2
  echo "  ./run_dec2654_web_ui.sh ${nfile} 8990" >&2
  exit 1
fi

echo "正在服务器读取 ${target}；19 beam 首次载入约需 2--3 分钟。"
echo "载入完成后会自动在默认浏览器打开 ${url}"
echo "结束时回到本终端按 Ctrl-C。"

if [[ "${CRAFTS_WEB_UI_DRY_RUN:-0}" == "1" ]]; then
  echo "启动参数检查通过。"
  exit 0
fi

if ! ssh crafts \
  "'$remote_python' -c \"import socket; s=socket.socket(); s.bind(('127.0.0.1', $port)); s.close()\""; then
  echo "服务器端口 ${port} 已被占用；请换一个端口，例如 8990 或 8991。" >&2
  exit 1
fi

ssh -tt -o ExitOnForwardFailure=yes -o LogLevel=QUIET \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
  -L "${port}:127.0.0.1:${port}" crafts \
  "mkdir -p '$mask_dir' '$runtime_dir/mplconfig' '$runtime_dir/tmp' && cd '$remote_app' && echo \$\$ > '$remote_pidfile' && exec env MPLBACKEND=WebAgg MPLCONFIGDIR='$runtime_dir/mplconfig' TMPDIR='$runtime_dir/tmp' PYTHONUNBUFFERED=1 '$remote_python' crafts_tod_visual_mask.py '$target' --input-dir '$input_dir' --mask-dir '$mask_dir' --require-all-auto-masks --block-records 64 --web-port '$port'" &
ssh_pid=$!

cleanup() {
  trap - EXIT INT TERM HUP
  if kill -0 "$ssh_pid" 2>/dev/null; then
    kill -HUP "$ssh_pid" 2>/dev/null || true
    wait "$ssh_pid" 2>/dev/null || true
  fi
  ssh -o BatchMode=yes -o ConnectTimeout=10 crafts \
    "if test -f '$remote_pidfile'; then pid=\$(cat '$remote_pidfile'); cmd=\$(ps -p \"\$pid\" -o args= 2>/dev/null || true); case \"\$cmd\" in *crafts_tod_visual_mask.py*'--web-port $port'*) kill \"\$pid\" 2>/dev/null || true; rm -f '$remote_pidfile';; '') rm -f '$remote_pidfile';; *) echo '保留不匹配的远端进程:' \"\$cmd\" >&2;; esac; fi" \
    >/dev/null || true
}
trap cleanup EXIT INT TERM HUP

ready=0
for ((attempt = 1; attempt <= 240; attempt++)); do
  if ! kill -0 "$ssh_pid" 2>/dev/null; then
    wait "$ssh_pid"
  fi
  if curl -fsS --max-time 1 "$url" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if ((ready == 0)); then
  echo "4分钟内未检测到 Web UI；请查看上面的服务器输出。" >&2
  exit 1
fi

open "$url"
echo "Web UI 已打开。关闭浏览器页面不会自动停止服务器；完成后请在这里按 Ctrl-C。"
wait "$ssh_pid"
