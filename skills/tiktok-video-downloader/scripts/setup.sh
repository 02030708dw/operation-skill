#!/usr/bin/env bash
set -euo pipefail
skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${TIKTOK_DOWNLOADER_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/tiktok-video-downloader/venv}"
python_bin="${PYTHON_BIN:-python3}"
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10+")'
if [[ ! -x "$runtime_dir/bin/python" ]]; then "$python_bin" -m venv "$runtime_dir"; fi
"$runtime_dir/bin/python" -m pip install --disable-pip-version-check -r "$skill_dir/requirements.txt"
"$runtime_dir/bin/python" -c 'import yt_dlp, curl_cffi, imageio_ffmpeg; print("TikTok 下载依赖就绪")'
printf 'Python: %s/bin/python\n' "$runtime_dir"
