# 服务器使用与验收

## 从提交版本安装

在 operation-skill 仓库先完成测试并用中文说明提交。部署包从该提交生成，勿复制混有未提交文件的工作目录：

```bash
git archive --format=tar.gz --output=/tmp/tiktok-skill.tar.gz HEAD skills/tiktok-video-downloader
```

将包传到服务器专用安装目录。更新已有安装前备份旧源码、数据和镜像版本；首次安装没有旧版本可备份。下载目录不放入 Git，也不挂载现有 Facebook、Google、Chrome 账号目录。

在解包出的技能目录构建（将标签替换为实际提交号）：

```bash
docker build -t hm-tiktok-downloader:COMMIT .
```

默认 Python 3.11-slim。`--build-arg BASE_IMAGE=...` 支持服务器已有、提供 Python 3.10+ 及 venv 的可信 Linux 镜像。它只作为独立新镜像的基础，不更新已有容器。镜像安装独立 Chromium 和字体，依赖在 `/opt/tiktok/venv` 单独安装。匿名浏览器资料仅在专用数据目录中临时创建，结束清理。Docker 中以非 root、无额外 capabilities 运行；`TIKTOK_CHROMIUM_NO_SANDBOX=1` 仅用于容器中的 Chromium 启动，不应对本机 Chrome 默认启用。FFmpeg 来自固定 imageio-ffmpeg wheel；有系统 FFmpeg 则优先使用。该基础镜像不得包含账号资料。

## 非 root 运行与持久化

以下示例在专用安装目录运行；Docker 需要当前用户的相应权限：

```bash
sudo install -d -o 10001 -g 10001 -m 750 data
docker run --rm --name tiktok-single-test --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src="$PWD/data",dst=/data hm-tiktok-downloader:COMMIT 'https://www.tiktok.com/@anunya.pangya/video/7686495975761939733' --output /data
docker run --rm --name tiktok-list-test --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src="$PWD/data",dst=/data hm-tiktok-downloader:COMMIT 'https://www.tiktok.com/@anunya.pangya' --list-only --limit 10 --output /data
docker run --rm --name tiktok-batch-test --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src="$PWD/data",dst=/data hm-tiktok-downloader:COMMIT 'https://www.tiktok.com/@anunya.pangya' --limit 10 --output /data
```

容器默认不传任何账号或 Cookie 环境变量，只有专用 `/data` 挂载。运行完成自动删除容器，数据、归档及报告仍在 `data`。不要添加自动重启策略或定时任务。退出码 1 时先看报告；验证和限流后不要立即循环重试。

匿名仍会有平台向本次 HTTP 会话发放的临时 Cookie；这不代表用户登录，也不从外部浏览器导入身份。

## 本地与服务器结果不一致时

先对齐 yt-dlp、curl_cffi 版本，禁用用户配置和导入 Cookie，分别验证主页枚举。服务器默认请求报 `PROFILE_ID_UNAVAILABLE` 时，可显式选择已验证的请求配置：

```bash
docker run --rm --name tiktok-compatible-batch --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src="$PWD/data",dst=/data hm-tiktok-downloader:COMMIT 'https://www.tiktok.com/@anunya.pangya' --impersonate chrome-131:macos-14 --limit 10 --output /data
```

`--impersonate` 设置 curl_cffi 请求头及 TLS 特征，不启动真实 Chrome，不读取浏览器资料，也不改变网络出口。报告 `requestClient` 会保留该值。枚举通过后仍须验收实际下载、解码和去重；不能仅凭一次成功认定始终可用。已有明确限流或验证阻断时停止，不能轮换该参数继续尝试。

## 验收

1. 运行单条，检查报告 `decodePassed=true`、文件大小、时长、分辨率及音视频编码。
2. 分别记录主页 `--list-only` 与实际前 10 条下载结果，枚举成功不代表下载成功。
3. 无限流/验证阻断时重复同一批次，已有文件应为 `skipped`，文件和归档数量不变。主页内容变化时按视频 ID 对照。
4. 记录提交号、镜像 ID、宿主机目录、报告路径和逐条结果。若平台阻断，如实保留错误码；不能把未生成文件的条目标为成功。
5. `docker ps` 确认测试容器已退出，共享 Worker 的容器 ID/启动时间没有变化。

本地测试：

```bash
~/.local/share/operation-skill/tiktok-video-downloader/venv/bin/python -m unittest discover -s tests -v
```

yt-dlp 官方依赖说明：https://github.com/yt-dlp/yt-dlp#dependencies

主页枚举仍受平台阻断时，记录原因并停止上线验收，不能用本地枚举链接代替服务器主页验收。
