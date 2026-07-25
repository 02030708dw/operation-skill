# Operation Skills

面向 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的自动化操作 Skill 集合。

本仓库用于沉淀可复用的设备操作流程、辅助脚本和故障排查经验。目前主要面向魔云腾（MYT）Android 云手机，后续会持续增加更多日常运营与批量设备操作 Skill。

运营人员请阅读：[Windows/macOS 运营操作手册](docs/运营操作手册.md)。

> [!IMPORTANT]
> 部分 Skill 会执行真实的外部操作。使用前请阅读对应目录中的 `SKILL.md`，先运行预演或连接检查，再明确授权执行。不要将账号、密码、验证码、Token、真实 IP 等敏感信息提交到仓库。

## 仓库结构

仓库遵循 Hermes Tap 的默认扫描结构：所有 Skill 都放在根目录的 `skills/` 下，每个 Skill 使用独立目录并至少包含一份 `SKILL.md`。

```text
operation-skill/
├── skills/
│   ├── facebook-daily-like/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   └── references/
│   ├── facebook-daily-comment/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   └── references/
│   ├── facebook-post-publish/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   ├── tests/
│   │   └── references/
│   ├── facebook-followed-video-download/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   ├── examples/
│   │   └── references/
│   ├── cloudflare-r2-video-upload/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   ├── tests/
│   │   └── references/
│   ├── myt-cloud-phone-video-upload/  # 旧命令兼容别名
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   └── references/
│   ├── myt-cloud-phone-file-upload/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   ├── tests/
│   │   └── references/
│   └── philippines-lottery-result-media/
│       ├── SKILL.md
│       ├── assets/
│       ├── scripts/
│       ├── tests/
│       └── references/
└── README.md
```

## 获取 Skill

### 方法一：订阅 Hermes Tap（推荐）

添加本仓库后，Hermes 会按默认 `skills/` 路径发现其中所有 Skill：

```powershell
hermes skills tap add 02030708dw/operation-skill
```

搜索并安装需要的 Skill：

```powershell
hermes skills search facebook-daily-like
hermes skills search facebook-daily-comment
hermes skills search facebook-post-publish
hermes skills search facebook-followed-video-download
hermes skills search cloudflare-r2-video-upload
hermes skills search myt-cloud-phone-file-upload
hermes skills search philippines-lottery-result-media
hermes skills install 02030708dw/operation-skill/facebook-daily-like
hermes skills install 02030708dw/operation-skill/facebook-daily-comment
hermes skills install 02030708dw/operation-skill/facebook-post-publish
hermes skills install 02030708dw/operation-skill/facebook-followed-video-download
hermes skills install 02030708dw/operation-skill/cloudflare-r2-video-upload
hermes skills install 02030708dw/operation-skill/myt-cloud-phone-file-upload
hermes skills install 02030708dw/operation-skill/philippines-lottery-result-media
```

安装后，如果 Hermes 会话已经打开，执行：

```text
/reload-skills
```

随后便可通过斜杠命令加载：

```text
/facebook-daily-like
/facebook-daily-comment
/facebook-post-publish
/facebook-followed-video-download
/cloudflare-r2-video-upload
/myt-cloud-phone-file-upload
/philippines-lottery-result-media
```

检查和更新通过 Hermes 安装的 Skill：

```powershell
hermes skills check
hermes skills update
```

查看或移除已经订阅的 Tap：

```powershell
hermes skills tap list
hermes skills tap remove 02030708dw/operation-skill
```

### 方法二：直接安装单个 Skill

不订阅整个仓库，只安装一个 Skill：

```powershell
hermes skills install 02030708dw/operation-skill/skills/facebook-daily-like
hermes skills install 02030708dw/operation-skill/skills/facebook-daily-comment
hermes skills install 02030708dw/operation-skill/skills/facebook-post-publish
hermes skills install 02030708dw/operation-skill/skills/facebook-followed-video-download
hermes skills install 02030708dw/operation-skill/skills/cloudflare-r2-video-upload
hermes skills install 02030708dw/operation-skill/skills/myt-cloud-phone-file-upload
hermes skills install 02030708dw/operation-skill/skills/philippines-lottery-result-media
```

### 方法三：使用 Git 拉取整个仓库

适合查看源码、参与开发或同时维护多个 Skill：

```powershell
git clone https://github.com/02030708dw/operation-skill.git
cd operation-skill
```

以后同步最新内容：

```powershell
git pull
```

如需手动安装，将 `skills/<skill-name>/` 的完整目录复制到当前 Hermes 配置使用的本地 `skills` 目录，并在运行中的 Hermes 会话执行 `/reload-skills`。

## Skill 一览

| Skill | 版本 | 用途 | 支持平台 | 使用说明 |
|---|---:|---|---|---|
| `facebook-daily-like` | 2.4.0 | 通过 MYT HTTP API 并发操作云手机，为 Facebook 动态按指定数量点赞 | Windows、Linux、macOS | [查看 SKILL.md](skills/facebook-daily-like/SKILL.md) |
| `facebook-daily-comment` | 2.8.0 | 通过 MYT HTTP API 并发操作云手机，为 Facebook 动态发表指定数量和内容的评论 | Windows、Linux、macOS | [查看 SKILL.md](skills/facebook-daily-comment/SKILL.md) |
| `facebook-post-publish` | 1.2.0 | 在 MYT 云手机的 Facebook 中发布纯文字、图片或视频帖子，并严格校验图库素材类型 | Windows、Linux、macOS | [查看 SKILL.md](skills/facebook-post-publish/SKILL.md) |
| `facebook-followed-video-download` | 1.0.0 | 扫描获准访问的 Facebook 来源，按来源下载新增视频、去重并生成报告 | Windows、Linux、macOS | [查看 SKILL.md](skills/facebook-followed-video-download/SKILL.md) |
| `cloudflare-r2-video-upload` | 1.0.0 | 将本地视频安全上传到 Cloudflare R2，支持预演、并发、自动分片、去重检查和报告 | Windows、Linux、macOS | [查看 SKILL.md](skills/cloudflare-r2-video-upload/SKILL.md) |
| `myt-cloud-phone-file-upload` | 2.0.0 | 将用户指定的单个文件或目录内全部文件并发上传到魔云腾云手机，保持相对子目录并验证每个文件 | Windows、Linux、macOS | [查看 SKILL.md](skills/myt-cloud-phone-file-upload/SKILL.md) |
| `philippines-lottery-result-media` | 2.2.1 | 并发对比多个菲律宾彩票结果来源，为 2D、3D、4D、6D 生成高对比号码和时间字体、电影感动画、共享品牌素材和音乐的竖屏图片或视频 | Windows、Linux、macOS | [查看 SKILL.md](skills/philippines-lottery-result-media/SKILL.md) |

## Skill 使用说明

### `facebook-daily-like`

通过魔云腾（MYT）V1 HTTP API 操作一个或多个 Android 云手机，在已登录的 Facebook App 中查找未点赞的动态，并按用户本次指定的数量执行点赞。

主要特点：

- 支持 T1001、T1002 等多个云手机同时执行。
- 点赞数量没有固定默认值，必须由用户每次指定。
- 根据当前 UI XML 动态识别点赞按钮，不使用固定点赞坐标。
- 兼容新版中文 Facebook 的“赞按钮，双击并长按即可给评论留下心情”主帖入口，并按完整句型排除“赞某人的评论按钮”等评论点赞。
- 正常任务默认不设固定总时限；持续找到候选或验证点赞时会刷新进度，只有连续 120 秒无实质进展才触发看门狗超时。
- 默认执行预演；只有用户明确授权后才会真正点击。
- 不保存 Facebook 账号、密码、验证码或 Token。

#### 使用前准备

- 安装 Python 3.9 或更高版本。
- Hermes 所在电脑能够访问 MYT 控制器。
- Facebook 已在目标云手机中人工登录完成。
- 在本机配置 `MYT_HOST`，其值为 MYT 控制器的主机名或 IP。

首次使用建议先检查连接：

```text
/facebook-daily-like 检查 T1001 和 T1002 的连接，不要点赞
```

#### 预演

预演会同时启动目标云手机的任务，滚动并识别可点赞按钮，但不会点击：

```text
/facebook-daily-like 为 T1001 和 T1002 各查找 8 篇可点赞动态，只预演，不要执行
```

#### 执行点赞

用户必须明确填写设备、每台点赞数量和执行意图：

```text
/facebook-daily-like 给 T1001 和 T1002 各点赞 8 篇，立即执行
```

脚本会为每台目标云手机建立独立并发任务，全部任务结束后分别汇总点赞数量、搜索轮数和错误。

#### 常见注意事项

- `--count` 没有默认值；缺少点赞数量时不会执行。
- 正常执行保持 `--max-runtime 0`，不要因为数量较大而添加 80 或 120 秒硬时限；`--stall-timeout 120` 只处理持续无进展。
- Hermes 外层命令超时至少使用 `max(600, 120 + 每台目标数量 × 90)` 秒；多设备并发时按单台数量计算。
- 首次登录、短信验证、双因素验证和安全检查必须人工完成。
- 实际执行前建议先预演，确认 Facebook 页面语言和按钮识别正常。
- 详细参数、端口映射和排查方法请阅读 [`skills/facebook-daily-like/SKILL.md`](skills/facebook-daily-like/SKILL.md)。

### `facebook-daily-comment`

通过魔云腾（MYT）V1 HTTP API 操作一个或多个 Android 云手机，在已登录的 Facebook App 中查找动态，并按用户本次指定的数量和内容发表评论。

主要特点：

- 支持 T1001、T1002 等多个云手机同时执行。
- 评论数量和内容没有默认值，必须由用户每次明确指定。
- 根据当前 UI XML 动态识别评论按钮、输入框和发送按钮，不使用固定坐标。
- 默认执行预演；只有用户明确授权后才会输入并发送评论。
- 默认记录已评论帖子的指纹，并扫描可见的相同评论，降低重复评论风险。
- 页面异常时可自动恢复；正常任务不设固定总时限，仅在连续 120 秒无实质进展时停止。
- 评论内容默认仅支持 1 至 200 个可打印 ASCII 字符。
- 不保存 Facebook 账号、密码、验证码或 Token。

#### 使用前准备

- 安装 Python 3.9 或更高版本。
- Hermes 所在电脑能够访问 MYT 控制器。
- Facebook 已在目标云手机中人工登录完成。
- 在本机配置 `MYT_HOST`，其值为 MYT 控制器的主机名或 IP。

首次使用建议先检查连接：

```text
/facebook-daily-comment 检查 T1001 和 T1002 的连接，不要评论
```

#### 预演

预演会同时启动目标云手机的任务并识别评论入口，但不会输入或发送评论：

```text
/facebook-daily-comment 为 T1001 和 T1002 各查找 3 篇可评论动态，评论内容为 Nice post!，只预演，不要执行
```

#### 执行评论

用户必须明确填写设备、每台评论数量、评论内容和执行意图：

```text
/facebook-daily-comment 给 T1001 和 T1002 各评论 3 篇，内容为 Nice post!，立即执行
```

脚本会为每台目标云手机建立独立并发任务，全部任务结束后分别汇总已评论数量、发送点击次数、恢复次数、跳过的重复帖子、剩余数量、搜索轮数和错误。部分完成时只能按汇总中的剩余数量补跑，避免重复评论。

#### 常见注意事项

- 除连接检查外，缺少评论数量或内容时不会执行。
- 首次登录、短信验证、双因素验证和安全检查必须人工完成。
- 中文、Emoji 和换行默认不受 Android `input text` 支持，请使用 ASCII 评论内容。
- 实际执行前建议先预演；排障时不要同时使用详细诊断和真实执行模式。
- 正常执行保持 `--max-runtime 0`；外层命令超时至少设置为 `max(600, 120 + 每台目标数量 × 90)` 秒。
- `sent_taps` 不代表评论成功；出现 `unverified-send` 时必须先人工核对，不能直接补跑。
- 自动恢复或防重复跳过不会改变补跑原则：仅对未完成设备按汇总中的 `remaining` 执行。
- 详细参数、恢复规则和排查方法请阅读 [`skills/facebook-daily-comment/SKILL.md`](skills/facebook-daily-comment/SKILL.md)。

### `facebook-post-publish`

控制魔云腾云手机中的 Facebook，通过首页 `+` → `帖子` → `图库` 发布纯文字、图片、视频，或文字加单个媒体文件。媒体从 `/sdcard/upload` 中选择。

主要特点：

- 默认只预演；只有用户明确要求立即执行时才点击发布。
- 支持只发图片、只发视频，不强制要求文字。
- 先在设备文件列表中按扩展名确认媒体类型；图库不显示文件名时，视频只选择带 `00:10` 等时长标记的缩略图，图片只选择不带时长的缩略图。
- 如果图库保留了上一次错误选择，会先取消错误素材再选择目标素材。
- 找不到可验证的视频时安全停止，绝不会退回选择左上角图片。
- 多台设备并发执行，并通过内容指纹防止未确认发布后自动重复发送。

预演只发视频：

```text
/facebook-post-publish 给 T1001 只发视频，不要发布
```

立即发布指定视频：

```text
/facebook-post-publish 给 T1001 发布 /sdcard/upload/result.mp4，不加文字，立即执行
```

如果 `/sdcard/upload` 下存在多个同类型文件，应指定完整文件名、关键词，或明确要求最新文件。详细选择规则和风险控制请阅读 [`skills/facebook-post-publish/SKILL.md`](skills/facebook-post-publish/SKILL.md)。

### `facebook-followed-video-download`

从用户配置的 Facebook Page、创作者、Reels、watch 或视频链接中查找新视频，按来源保存到独立目录，并通过 URL 与 `yt-dlp` 双重归档避免重复下载。

主要特点：

- 支持 Windows、Linux 和 macOS，不包含固定用户名、盘符或 Chrome 路径。
- 默认只预演；只有用户明确要求下载时才使用真实执行模式。
- `--count` 表示每个来源本次最多下载多少个，`0` 表示不限制。
- 每次真实执行生成 Markdown、JSON 和原始日志报告。
- 不保存账号、密码、Token 或 cookie 内容；可选 cookie 只能通过本地文件路径引用。

#### 使用前准备

- 安装 Python、Node.js、npm、Google Chrome/Chromium 和 `yt-dlp`。
- 在 Skill 的 `scripts/` 目录运行一次 `npm install`。
- 只添加公开内容或用户明确获准下载的 Facebook 来源。

初始化来源文件：

```text
/facebook-followed-video-download 初始化来源配置
```

添加来源：

```text
/facebook-followed-video-download 添加来源 creator-one，地址为 https://www.facebook.com/example/reels/
```

#### 预演

```text
/facebook-followed-video-download 查找每个来源最新 3 个视频，详细预演，不要下载
```

#### 执行下载

```text
/facebook-followed-video-download 下载每个来源最新 3 个视频，立即执行
```

首次全量导入可明确指定“全部”或“不限制数量”。大型任务正常运行时不应被短时间限制误判为超时；应等待同一进程结束，避免重复启动下载。

详细参数、安全边界和故障处理请阅读 [`skills/facebook-followed-video-download/SKILL.md`](skills/facebook-followed-video-download/SKILL.md)。

### `cloudflare-r2-video-upload`

将单个视频或整个本地目录递归上传到 Cloudflare R2 对象存储，保留相对目录结构，并对大视频自动使用 multipart 分片传输。

主要特点：

- 默认仅预演，只有明确要求上传时才执行。
- 支持多个文件并发上传和单个大文件分片并发。
- 上传前通过对象键和文件大小判断是否已存在。
- 同名同大小对象自动跳过；同名不同大小对象默认停止冲突，不会擅自覆盖。
- 上传完成后重新读取远端对象大小进行验证。
- 凭据仅从本机 `CLOUDFLARE_R2_*` 环境变量读取，不写入命令、报告或仓库。

#### 使用前准备

- 在 Cloudflare R2 创建仅限目标存储桶的 Object Read & Write S3 API 凭据。
- 在 Hermes 使用的本机环境中配置 `CLOUDFLARE_R2_ACCOUNT_ID`、`CLOUDFLARE_R2_ACCESS_KEY_ID`、`CLOUDFLARE_R2_SECRET_ACCESS_KEY` 和 `CLOUDFLARE_R2_BUCKET`。
- 安装 Python 依赖 `boto3`。

检查连接：

```text
/cloudflare-r2-video-upload 检查 R2 配置和存储桶连接，不要上传
```

预演：

```text
/cloudflare-r2-video-upload 检查 C:\Users\me\Desktop\Facebook 下准备上传的前 10 个视频，R2 前缀为 facebook，不要上传
```

执行：

```text
/cloudflare-r2-video-upload 把 C:\Users\me\Desktop\Facebook 下全部视频上传到 R2 的 facebook 前缀，并发 3 个，立即执行
```

大型任务正常运行时不应被短时间限制误判为超时。终端暂时停止等待时，应继续监控同一进程，不能重复启动上传。

详细配置、安全规则和冲突处理请阅读 [`skills/cloudflare-r2-video-upload/SKILL.md`](skills/cloudflare-r2-video-upload/SKILL.md)。

### `myt-cloud-phone-file-upload`

通过魔云腾（MYT）HTTP API，把用户本次明确填写的单个文件，或者目录下全部普通文件上传到一个或多个 Android 云手机。路径没有默认值，Skill 不会自行扫描电脑或沿用上一次路径。

主要特点：

- 每次上传都必须由用户填写本地文件或目录路径以及目标设备。
- 默认只预演；只有用户明确要求上传时才会执行真实写入。
- T1001、T1002 等多台云手机并发上传，不需要逐台等待。
- 目录会递归收集全部普通文件，不按扩展名过滤；MP4、PNG、音频、JSON 等都会上传，并保持相对子目录。
- 使用临时文件上传，校验完整字节数后再移动到最终文件名。
- 同名同大小文件自动跳过；同名不同大小默认报告冲突，只有明确授权才覆盖。
- 上传完成后触发 Android 媒体扫描，让相册及相关应用发现新媒体文件。
- 实际上传落盘目录使用已在真机验证的 `/sdcard/upload`，不再错误检查 `/sdcard/Download`。
- 大文件传输期间持续打印进度，不使用 80 秒之类的短任务总时限。

使用前需要在 Hermes 的本机环境配置 `MYT_HOST`。连接检查不需要视频路径：

```text
/myt-cloud-phone-file-upload 检查 T1001 和 T1002 的连接，不要上传
```

预演时必须填写视频路径：

```text
/myt-cloud-phone-file-upload 把 F:\lottery\2d\2026-07-24 下全部文件上传到 T1001，只预演，不要执行
```

实际上传：

```text
/myt-cloud-phone-file-upload 把 F:\lottery\2d\2026-07-24 下全部文件上传到 T1001，立即执行
```

详细参数、端口映射、目录规则、验证和故障处理请阅读 [`skills/myt-cloud-phone-file-upload/SKILL.md`](skills/myt-cloud-phone-file-upload/SKILL.md)。

旧命令 `/myt-cloud-phone-video-upload` 保留为兼容别名，也会调用相同的文件及目录上传能力；新任务统一使用 `/myt-cloud-phone-file-upload`。

### `philippines-lottery-result-media`

并发获取和对比多个菲律宾彩票结果来源，为 PCSO 2D（EZ2）、3D（Swertres）、4D 和 6D 生成 1080×1920 竖屏结果图或带音乐的 MP4 视频。

主要特点：

- 顶层 Skill 不再绑定 2D，通过 `--game 2d|3d|4d|6d` 选择彩种。
- `pcsoresults.org` 与 `lottopcso.com` 默认并发抓取；一个来源先公布有效结果时即可生成。
- 两个来源的同期开奖数据一致时标记为 `confirmed`；仅单一来源可用时标记为 `single-source`；号码冲突时默认停止。
- 2D、3D、4D、6D 彩种 logo 和品牌 logo 统一放在 `assets/logos/`，背景音乐统一放在 `assets/audio/`。
- 默认只预演，不写入图片、视频或归档；只有用户明确要求生成时才使用真实执行模式。
- 2D、3D 支持 `2pm`、`5pm`、`9pm` 和三时段汇总；4D、6D 使用单期开奖结果布局。
- 默认使用 `cinematic` 动画：慢推镜头、动态光扫、粒子漂浮、呼吸光、暗角和淡入淡出。
- 图片使用蓝紫渐变背景、玻璃面板、发光数字球，以及带渐变、阴影、描边和光晕的标题字体。
- 开奖号码使用白色超粗字体、深色粗描边和轻微投影，保证在金色数字球上清晰可读。
- 2PM、5PM、9PM 时间标签同样使用白色超粗字体和深蓝粗描边，去除影响辨识度的深色渐变重影。
- 使用“最新”模式生成时，会先删除同一彩种、同一日期目录中由本脚本生成的旧 PNG/MP4，再写入最新素材；其他文件不会被删除。
- 如需保留以前的生成文件，可以明确要求“保留历史素材”，对应 `--keep-previous`。
- 性能较低的电脑可以使用 `subtle` 轻量动画，或使用 `none` 完全关闭动画。
- 支持 Windows、Linux 和 macOS 字体回退，并自动定位 FFmpeg。
- 支持为每个来源分别载入离线 HTML，便于复现解析和数据冲突问题。

首次使用先安装依赖并检查素材：

```powershell
python -m pip install -r skills/philippines-lottery-result-media/scripts/requirements.txt
python skills/philippines-lottery-result-media/scripts/philippines_lottery_result_media.py --check
```

Hermes 预演：

```text
/philippines-lottery-result-media 对比多个数据源，预演最新 2D 开奖结果，不要生成
```

生成图片和视频：

```text
/philippines-lottery-result-media 对比多个数据源，生成最新 3D 开奖结果视频，立即执行
```

结果来源属于第三方页面，发布前应与 PCSO 官方公布的信息核对。Skill 不会自动上传或发布生成文件。

详细参数、数据源规则、彩种配置和共享素材说明请阅读 [`skills/philippines-lottery-result-media/SKILL.md`](skills/philippines-lottery-result-media/SKILL.md)。

## 添加新的 Skill

后续新增 Skill 时：

1. 在 `skills/` 下创建与 Skill 名称一致的目录，名称使用小写字母、数字和连字符。
2. 添加包含 `name`、`description` 等标准 frontmatter 的 `SKILL.md`。
3. 将可执行工具放入 `scripts/`，详细资料放入 `references/`，模板放入 `templates/`。
4. 使用环境变量或本地配置保存机器相关参数，不要提交凭据、真实 IP 或个人数据。
5. 测试脚本、预演模式和失败退出行为。
6. 在本 README 的“Skill 一览”和“Skill 使用说明”中增加对应条目。
7. 更新版本号后再提交。

建议每个 Skill 的使用说明至少包含：

- 解决什么问题。
- 适合在什么情况下使用。
- 依赖和本地配置。
- 最小调用示例。
- 是否会产生真实外部操作。
- 验证结果及常见故障处理。
