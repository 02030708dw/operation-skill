#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${YOUTUBE_DOWNLOADER_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/youtube-video-downloader/venv}"
python_bin="${PYTHON_BIN:-python3}"
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10 或更新版本，可用 PYTHON_BIN 指定")'

if [[ ! -x "$runtime_dir/bin/python" ]]; then
  "$python_bin" -m venv "$runtime_dir"
fi
"$runtime_dir/bin/python" -m pip install --disable-pip-version-check -r "$skill_dir/requirements.txt"
"$runtime_dir/bin/python" -c 'import yt_dlp, yt_dlp_ejs, imageio_ffmpeg; print("YouTube 下载依赖安装完成")'
echo "运行环境：$runtime_dir"
echo "下载脚本：$skill_dir/scripts/download.py"
if ! command -v node >/dev/null 2>&1 && ! command -v deno >/dev/null 2>&1; then
  echo '提示：运行前还需安装 Node.js 22+ 或受支持的 Deno，并将其加入 PATH。' >&2
fi
