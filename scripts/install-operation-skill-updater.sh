#!/usr/bin/env bash

set -euo pipefail

hermes_home="${HERMES_HOME:-${HOME}/.hermes}"
updater_home="${hermes_home}/operation-skill-updater"
base_url="${OPERATION_SKILL_BASE_URL:-}"

find_python() {
  for candidate in \
    "${hermes_home}/hermes-agent/venv/bin/python" \
    "$(command -v python3 2>/dev/null || true)"; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

python_bin="$(find_python || true)"
if [[ -z "${python_bin}" ]]; then
  echo "未找到 Hermes Python 或 python3，请先安装 Hermes。" >&2
  exit 1
fi

install_core="false"
adopt_existing_core="false"
uninstall="false"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --install-core)
      install_core="true"
      ;;
    --adopt-existing-core)
      adopt_existing_core="true"
      ;;
    --uninstall)
      uninstall="true"
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "${install_core}" == "true" && "${adopt_existing_core}" == "true" ]]; then
  echo "--install-core 与 --adopt-existing-core 不能同时使用。" >&2
  exit 2
fi

if [[ "${uninstall}" == "true" ]]; then
  if [[ "${install_core}" == "true" || "${adopt_existing_core}" == "true" ]]; then
    echo "--uninstall 不能与安装或采用参数同时使用。" >&2
    exit 2
  fi
  if [[ -f "${updater_home}/operation_skill_updater.py" ]]; then
    uninstall_status=0
    "${python_bin}" "${updater_home}/operation_skill_updater.py" \
      --hermes-home "${hermes_home}" uninstall-schedule || uninstall_status=$?
    if [[ "${uninstall_status}" -ne 0 ]]; then
      exit "${uninstall_status}"
    fi
  fi
  echo "已移除自动更新计划；日志、备份和 Skill 保持不变。"
  exit 0
fi

if [[ -z "${base_url}" ]]; then
  echo "请设置 OPERATION_SKILL_BASE_URL，例如 https://downloads.example.com/operation-skills/stable" >&2
  exit 1
fi
base_url="${base_url%/}"
"${python_bin}" - "${base_url}" <<'PY'
import os, sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
allowed = {"https"}
if os.environ.get("OPERATION_SKILL_UPDATER_ALLOW_FILE_URL") == "1":
    allowed.add("file")
if parsed.scheme not in allowed or (parsed.scheme == "https" and not parsed.netloc):
    raise SystemExit("OPERATION_SKILL_BASE_URL 只允许 HTTPS 地址。")
PY

download_with_limit() {
  local url="$1"
  local destination="$2"
  local maximum_bytes="$3"
  if [[ "${OPERATION_SKILL_UPDATER_ALLOW_FILE_URL:-}" == "1" ]]; then
    curl -fsSL --proto '=https,file' --proto-redir '=https,file' \
      --max-filesize "${maximum_bytes}" "${url}" -o "${destination}"
  else
    curl -fsSL --proto '=https' --proto-redir '=https' \
      --max-filesize "${maximum_bytes}" "${url}" -o "${destination}"
  fi
  local downloaded_bytes
  downloaded_bytes="$(wc -c < "${destination}" | tr -d '[:space:]')"
  if [[ "${downloaded_bytes}" -gt "${maximum_bytes}" ]]; then
    echo "下载内容超过允许大小: ${url}" >&2
    return 1
  fi
}

temporary_dir="$(mktemp -d)"
cleanup() {
  rm -rf "${temporary_dir}"
}
trap cleanup EXIT
download_with_limit "${base_url}/manifest.json" "${temporary_dir}/manifest.json" 1048576

updater_url="$("${python_bin}" - "${temporary_dir}/manifest.json" <<'PY'
import json, os, re, sys
from urllib.parse import urlparse

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("manifest 根节点必须是对象。")
if payload.get("schemaVersion") != 1:
    raise SystemExit("manifest schema 无效。")
if payload.get("repository") != "02030708dw/operation-skill" or payload.get("channel") != "main":
    raise SystemExit("manifest 来源或通道无效。")
if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("commit", ""))):
    raise SystemExit("manifest commit 无效。")
release_sequence = payload.get("releaseSequence")
if isinstance(release_sequence, bool) or not isinstance(release_sequence, int) or release_sequence <= 0:
    raise SystemExit("manifest releaseSequence 无效。")
updater = payload.get("updater")
if not isinstance(updater, dict):
    raise SystemExit("manifest updater 无效。")
if not re.fullmatch(r"[0-9a-f]{64}", str(updater.get("sha256", ""))):
    raise SystemExit("manifest updater SHA-256 无效。")
updater_size = updater.get("size")
if isinstance(updater_size, bool) or not isinstance(updater_size, int) or not 0 < updater_size <= 2 * 1024 * 1024:
    raise SystemExit("manifest updater 大小无效。")
url = str(updater.get("url", ""))
parsed = urlparse(url)
allowed = {"https"}
if os.environ.get("OPERATION_SKILL_UPDATER_ALLOW_FILE_URL") == "1":
    allowed.add("file")
if parsed.scheme not in allowed or (parsed.scheme == "https" and not parsed.netloc):
    raise SystemExit("更新器只允许 HTTPS 发布地址。")
print(url)
PY
)"
download_with_limit "${updater_url}" "${temporary_dir}/operation_skill_updater.py" 2097152

expected_sha="$("${python_bin}" - "${temporary_dir}/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["updater"]["sha256"])
PY
)"
expected_size="$("${python_bin}" - "${temporary_dir}/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["updater"]["size"])
PY
)"
release_sequence="$("${python_bin}" - "${temporary_dir}/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["releaseSequence"])
PY
)"
actual_sha="$("${python_bin}" - "${temporary_dir}/operation_skill_updater.py" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
)"
actual_size="$(wc -c < "${temporary_dir}/operation_skill_updater.py" | tr -d '[:space:]')"
if [[ "${expected_size}" -ne "${actual_size}" ]]; then
  echo "更新器大小校验失败。" >&2
  exit 1
fi
if [[ "${expected_sha}" != "${actual_sha}" ]]; then
  echo "更新器 SHA-256 校验失败。" >&2
  exit 1
fi

manage_core="false"
if [[ "${install_core}" == "true" || "${adopt_existing_core}" == "true" ]]; then
  manage_core="true"
fi
bootstrap_args=(
  "${python_bin}" "${temporary_dir}/operation_skill_updater.py"
  --hermes-home "${hermes_home}"
  --manifest-url "${base_url}/manifest.json"
  bootstrap-install --manifest-file "${temporary_dir}/manifest.json"
)
if [[ "${manage_core}" == "true" ]]; then
  bootstrap_args+=(--manage-core)
fi
"${bootstrap_args[@]}"

# Finish the foreground update before registering the LaunchAgent: RunAtLoad
# starts another updater process as soon as the schedule is registered.
run_args=("${python_bin}" "${updater_home}/operation_skill_updater.py" \
  --hermes-home "${hermes_home}" --idle-timeout 0 run)
if [[ "${install_core}" == "true" ]]; then
  run_args+=(--install-core)
fi
if [[ "${adopt_existing_core}" == "true" ]]; then
  run_args+=(--adopt-existing-core --adopt-release-sequence "${release_sequence}")
fi
# Register the retry schedule even when the first foreground run reports a
# recoverable failure (for example, a bridge refresh that was left pending).
run_status=0
"${run_args[@]}" || run_status=$?
schedule_status=0
"${python_bin}" "${updater_home}/operation_skill_updater.py" \
  --hermes-home "${hermes_home}" install-schedule || schedule_status=$?
if [[ "${schedule_status}" -ne 0 ]]; then
  exit "${schedule_status}"
fi
if [[ "${run_status}" -ne 0 ]]; then
  exit "${run_status}"
fi
echo "运营 Skill 自动更新已安装。"
