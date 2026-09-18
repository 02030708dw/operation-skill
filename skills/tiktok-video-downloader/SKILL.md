---
name: tiktok-video-downloader
description: Download TikTok public videos from single video links or bounded profile batches, locally or in an isolated server container, with resumable files, deduplication and JSON results. Use for TikTok 视频抓取、主页批量下载、服务器下载测试; not publishing or account management.
---

# TikTok 公开视频下载

使用本目录的 `scripts/download.py`。默认匿名；不读取浏览器 Cookie、账号资料、netrc 或 yt-dlp 命令行配置。首轮不提供认证参数。遇到明确登录要求、权限不足或验证时保留真实原因，不自行导入账号或切换代理。

## 本地使用

需要 Python 3.10+。从技能目录运行：

```bash
bash scripts/setup.sh
~/.local/share/operation-skill/tiktok-video-downloader/venv/bin/python scripts/download.py 'https://www.tiktok.com/@anunya.pangya/video/7686495975761939733'
~/.local/share/operation-skill/tiktok-video-downloader/venv/bin/python scripts/download.py 'https://www.tiktok.com/@anunya.pangya' --list-only --limit 10
~/.local/share/operation-skill/tiktok-video-downloader/venv/bin/python scripts/download.py 'https://www.tiktok.com/@anunya.pangya' --limit 10 --output /absolute/path/TikTok
```

`PYTHON_BIN` 可指定安装解释器；`TIKTOK_DOWNLOADER_VENV` 可指定虚拟环境。依赖固定在 `requirements.txt`，含官方支持的 curl_cffi 和自带 FFmpeg 的 imageio-ffmpeg；已有系统 FFmpeg 时优先使用。

主页默认处理平台返回的前 10 条（包括重复条目），不是下载 10 条新视频，也不承诺按发布日期排序。`--limit` 范围 1–1000；不要自行扩大用户要求的数量。支持官方 `/@账号`、`/@账号/video/ID` 链接；图文和直播单独记为不支持。

## 结果与恢复

- 串行下载 yt-dlp 选择的最高可用画质及声音；保留源编码，不转成低画质。
- 文件为 `账号/发布日期_标题_[视频ID].mp4`；无发布日期时用 `unknown-date`。
- `.download-archive.json` 按视频 ID 去重，记录相对文件路径、SHA-256 及音视频验证结果。文件缺失或哈希变化不会直接跳过。
- 完整文件经 FFmpeg 全程解码、时长及音视频检查后才归档。分片和 `.pending/` 用于续传；下载后归档前中断也可恢复。失败文件保留为 `.invalid-*` 供检查。
- `.reports/*.json` 每次独立报告。`downloaded`、`skipped`、`login-required`、`failed`、`unsupported`、`listed` 分开计数；主页无法枚举时 `discovered`、`unattempted` 为 null，不能说该账号没有视频。
- 登录要求继续处理其他条目；限流或安全验证立即停止批次。网络错误每次请求最多补试一次。原始页面、Cookie 和带签名媒体 URL 不入报告。
- 退出码：0 完成或列出成功；1 部分完成、受限或失败；2 参数/环境/目录锁错误；130 中断。不要以退出码 0 或只解析出格式代替实际文件验收。

服务器构建、持久化和复现步骤见 [服务器使用说明](references/server-usage.md)。本技能独立运行，不修改四区后台、账号状态或共享 Worker，也不自动创建定时任务。
