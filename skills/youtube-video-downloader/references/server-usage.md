# Linux 服务器使用

支持 Linux/macOS、Python 3.10+。Windows 暂不支持，脚本使用 Unix 文件锁。

## 安装

在服务器的 `operation-skill` 仓库根目录运行；如果通过 Hermes 安装，把 `skill_dir` 改为实际加载的技能目录。

```bash
skill_dir="$PWD/skills/youtube-video-downloader"
bash "$skill_dir/scripts/setup.sh"
youtube_python="${YOUTUBE_DOWNLOADER_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/youtube-video-downloader/venv}/bin/python"
"$youtube_python" "$skill_dir/scripts/download.py" --help
```

`setup.sh` 使用 `python3 -m venv`，支持通过 `PYTHON_BIN=/path/to/python3.11` 选择解释器。Debian/Ubuntu 缺少 venv 或 FFmpeg 时，由管理员安装 `python3-venv`、`ffmpeg`。另需 Node.js 22+ 或受 yt-dlp 支持的 Deno，并确保后台任务的 PATH 也能找到它。

默认优先使用系统 FFmpeg，或用 `--ffmpeg /usr/bin/ffmpeg` 指定。没有系统 FFmpeg 时使用 imageio-ffmpeg 自带的二进制；平台没有可用二进制时应安装系统 FFmpeg。脚本不会写入系统 Python 的 bin 目录。

## 单条测试

```bash
"$youtube_python" "$skill_dir/scripts/download.py" \
  'https://www.youtube.com/shorts/0ixRDhyAsMY' \
  --output "$HOME/videos/youtube"
```

## 频道批量

先读取频道前 10 条列表，再下载：

```bash
"$youtube_python" "$skill_dir/scripts/download.py" \
  'https://www.youtube.com/@Wimbledon/shorts' \
  --limit 10 --list-only --output "$HOME/videos/youtube"

"$youtube_python" "$skill_dir/scripts/download.py" \
  'https://www.youtube.com/@Wimbledon/shorts' \
  --limit 10 --output "$HOME/videos/youtube"
```

`--limit` 是本次检查的列表条目数，包含已下载条目。增加为 `--limit 50` 可检查更多；仅在明确需要全量时使用 `--all`。重复任务必须复用 `--output`，才能读取同一份去重记录。默认按实际画面高度筛选 `height<=1080`；竖屏 1080×1920 如需保留，可加 `--height 1920`，但不会放大低分辨率源。

## 需要登录时

本地测试使用过 Mac 的普通 Chrome 登录状态；Git 中不包含它，服务器不会自动得到这个状态。Cookie 文件应由管理员通过自己的受控方式配置，不能粘贴到聊天或提交 Git。示例路径是占位约定，不代表文件已经存在：

```bash
# 仅在已配置有效文件后执行
chmod 600 "$HOME/.config/youtube/cookies.txt"
"$youtube_python" "$skill_dir/scripts/download.py" \
  'https://www.youtube.com/shorts/0ixRDhyAsMY' \
  --cookies "$HOME/.config/youtube/cookies.txt" \
  --output "$HOME/videos/youtube"
```

批量命令也可添加同样的 `--cookies` 参数。若服务器本身有已登录的普通 Chrome，并且任务运行用户能够解密该配置，则可使用 `--browser chrome:Default`，不要同时传两种认证参数。无痕窗口不能被这个参数直接读取。详见 [authentication.md](authentication.md)。

服务器出口、Cookie 有效期和 YouTube 验证会影响结果；本地成功不保证服务器成功。遇到登录验证或限流，脚本会停止，不能将只读到视频标题计作下载成功。

## 产物与退出状态

- `<output>/<频道ID>/发布日期_标题_[视频ID].mp4`：媒体文件。
- `<output>/.download-archive.txt`：成功的视频 ID。
- `<output>/.reports/*.json`：结果、路径、尺寸、失败原因和计数。
- `<output>/.tools/`：需要时放置 FFmpeg 入口。
- 退出码 `0`：本次处理无下载失败（也可能全部跳过或列表为空）；`1`：下载/列表失败；`2`：参数或依赖配置错误；`130`：手动中断。

安装依赖、输出目录和 Cookie 应属于实际运行任务的用户。不要将 `.venv`、媒体或 Cookie 放入托管技能目录。该技能也可脱离 HM 独立运行。

## HM VN 后台集成

VN 开启 `HM_YOUTUBE_ENABLED` 并配置区域 `googleAccount` 后，由既有 `hm_server_worker.py` 领取 YouTube 任务。HM 适配器为 `scripts/hm_youtube_ingest.py`；依赖同一发布包中的 `facebook-video-ingest` 回传协议和浏览器公共模块。

运营人员在后台 Google 登录弹窗内完成官方页面登录。服务器在独立 Google 账号目录保存会话，登录维护与下载互斥，并发为 1；不会读取本机 Chrome。`LOGGED_IN` 表示已验证登录，只有真实文件下载成功才标记 `AVAILABLE / PASSED`。重新登录需重新验证下载；Cookie 存在本身不构成成功证据。

后台默认取频道返回的前 10 条（不是 10 条新增视频），过滤超过 20 分钟的内容，竖屏最大高度 1920。下载结果逐条回传，固定报告及每视频回执用于回传失败恢复，视频 ID 用于去重。认证或限流失败停止本轮，页面显示原因；不自动审核发布。

### 经授权从独立 Chrome 资料迁移

Google 拒绝服务器浏览器登录时，管理员可在用户明确同意后，从仅登录目标账号的独立 Chrome 资料导出 Google / YouTube Netscape Cookie，并通过私有传输送入 VN。不要使用含其他账号的常用资料。导出文件不能包含其他域名，权限必须为 `0600`，不进入 Git 或日志。

在配置了 `HM_TENANT_CONFIG` 的 HM Worker 内，以 `hermes` 用户执行：

```bash
python /opt/data/skills/youtube-video-downloader/scripts/import_google_session.py \
  --cookies /opt/data/private-import/google-vn.txt
```

导入命令仅接受 VN，持有区域账号维护锁，使用临时资料验证 YouTube 登录并关闭、重新打开浏览器复验。成功后替换 VN 会话，保留原资料备份；下载仍为 `NOT_TESTED`。需要验证、失败或超时会停止，原会话不被覆盖。处理完应删除传输用的临时文件；实际运行会话留在私有区域目录。
