---
name: facebook-daily-like
description: Use MYT HTTP API to like Facebook feed posts.
metadata:
  version: "2.2.1"
  author: Local user with Hermes Agent
  platforms: [windows, linux, macos]
  prerequisites:
    commands: [python]
  required_environment_variables:
    - name: MYT_HOST
      prompt: MYT controller IP address or hostname
      help: Enter only the host, for example myt-controller.local, without http:// or a port.
      required_for: connecting to the MYT controller
  hermes:
    tags: [Automation, Android, Facebook, MYT]
    requires_tools: [terminal]
---

# Facebook Daily Like Skill

通过魔云腾（MYT）V1 HTTP API 在一个或多个 Android 实例的 Facebook 动态中寻找未点赞帖子，并按指定数量点赞。此 skill 只处理已登录的 Facebook App，不保存账号、密码，也不自动绕过验证码或安全验证。

真实点赞属于外部写操作。除非用户在当前请求中明确要求立即执行，否则先运行预演；只有带 `--execute` 才会发送点击命令。

## When to Use

- 用户要求在魔云腾设备上查找或点赞 Facebook 动态。
- 用户要求给 T1001、T1002 或其他 MYT 实例批量点赞。
- 用户要诊断 MYT API、Facebook 登录态、UI dump 或点赞按钮识别问题。

## Prerequisites

- Hermes 会话提供 `terminal` 工具。
- Python 3.9 或更高版本可通过 `python` 调用（Linux/macOS 可按本机环境改用 `python3`）。
- Hermes 所在电脑能访问 MYT 控制器。
- Facebook 已在目标 Android 实例中人工登录完成。
- 将控制器主机名或 IP 存入 `MYT_HOST`。不要把凭据、验证码或 token 写进本 skill。

可选环境变量：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `MYT_DEVICE_IDS` | `T1001,T1002` | 目标实例；也可传端口 |
| `MYT_MAX_CYCLES` | `40` | 每个实例最多滚动/识别轮数 |
| `MYT_BASE_PORT` | `10005` | T1001 对应的基础端口 |
| `MYT_PORT_STRIDE` | `3` | 相邻实例端口步长 |
| `MYT_TIMEOUT` | `15` | 单个 HTTP 请求超时秒数 |
| `MYT_MAX_RUNTIME` | `80` | 每台设备最长执行秒数，确保外层终端超时前返回汇总 |

设备端口规则默认为：`T100N -> MYT_BASE_PORT + (N - 1) * MYT_PORT_STRIDE`。例如默认配置下 T1001 为 10005，T1002 为 10008。若设备编号不符合该规则，直接在 `--devices` 中填写端口。

## How to Run

Hermes 加载 skill 时会在正文后注入 `[Skill directory: <绝对路径>]`。使用该绝对路径替换下列命令中的 `<SKILL_DIR>`，不要猜测 Hermes 的安装位置。

先检查配置和连接，不进行点赞：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_like.py" --check
```

预演：启动 Facebook、滚动并识别第一个可点赞按钮，但不点击。`--count` 没有默认值，必须使用用户本次指定的数量：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_like.py" --devices T1001,T1002 --count <用户填写的数量>
```

用户明确授权后执行：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_like.py" --devices T1001,T1002 --count <用户填写的数量> --execute
```

多个设备默认同时执行。脚本会为每台目标云手机建立独立并发任务，不需要额外并发参数。

## Quick Reference

| 目的 | 参数 |
|---|---|
| 指定控制器 | `--host myt-controller.local` |
| 指定实例 | `--devices T1001,T1002` |
| 直接指定端口 | `--devices 10005,10008` |
| 每台点赞 N 篇 | `--count N`（必须由用户填写） |
| 限制搜索轮数 | `--max-cycles 10` |
| 限制单台总耗时 | `--max-runtime 80` |
| UI dump 自动重试 | `--ui-dump-retries 4 --ui-dump-retry-wait 1` |
| Feed 载入检查 | `--feed-ready-retries 5 --feed-ready-wait 1.5` |
| 空页/非 Feed 快速恢复 | `--not-feed-limit 4 --no-button-limit 8` |
| 点赞后验证 | `--like-verify-retries 3 --like-verify-wait 1 --like-verify-radius 90` |
| 自动恢复次数 | `--max-recoveries 2` |
| 恢复时关闭 FB | `--recovery-close-method both`（最近任务滑掉 + force-stop） |
| 最近任务滑掉坐标 | `--recents-swipe 360,900,360,120,500` |
| 关闭自动恢复 | `--no-auto-recover` |
| 只测连接 | `--check` |
| 不启动 Facebook | `--no-launch` |
| 强制重启 Facebook | `--force-restart` |
| 真正点击 | `--execute` |
| 输出详细诊断 | `--verbose`（与 `--execute` 互斥） |

脚本退出码：`0` 表示全部达到目标，`1` 表示至少一台未达到目标，`2` 表示配置或参数错误。

## Procedure

1. 读取用户明确指定的设备和点赞数量；不要假设为 5，也不要自行扩大范围。
2. 用户没有提供点赞数量时，必须先询问，不能运行预演或真实点赞。
3. 若 `MYT_HOST` 未配置，要求用户在本机配置，不要在聊天中索取密码或验证码。
4. 先执行 `--check`，并发确认所有目标端口的 MYT API 可访问。
5. 若用户没有明确要求真实点赞，执行不带 `--execute` 的预演并报告发现结果；详细诊断只添加 `--verbose`，不得同时添加 `--execute`。
6. 若用户明确要求真实点赞，添加 `--execute`。
7. 所有目标设备同时启动独立任务。每台设备都会：
   - 启动 Facebook（除非传入 `--no-launch`）。
   - 启动后检查当前是否在 Facebook；页面未完全载入时继续由主循环滚动检查。
   - 向上滑动动态。
   - 获取 `uiautomator` XML；空或损坏的 UI dump 默认自动重试 4 次。
   - 若 Facebook 页面半载入、非 Feed、连续找不到可点赞按钮，或 UI dump 失败，会自动恢复。
   - 自动恢复默认最多 2 次：打开最近任务并按 `--recents-swipe` 滑掉当前 App，再 `force-stop` Facebook，重新用 Feed 深链打开，并按已验证的剩余数量继续。
   - 从可点击节点中识别中文简体、中文繁体或英文的未点赞按钮，并排除“赞某条评论”“Like a comment”等评论区反应按钮。
   - 即使当前可见帖子全部处于已点赞状态，也会把包含已按下/Unlike 控件的页面识别为 Feed，继续滚动寻找新帖子，不会误触发页面恢复。
   - 根据 XML 的 `bounds` 动态计算中心点；不使用固定点赞坐标。
   - 点击 Like 后重新读取 UI；只有原位置出现已按下/Unlike 状态，或页面仍明确处于 Feed 且未点赞 Like 按钮从原位置消失，才把 `liked` 加 1。跳到 Facebook 其他页面不算验证成功。
   - 若无法确认点击结果，返回 `status=unverified-like` 并停止该设备，避免重复点击造成取消点赞。
   - 达到数量或到达最大轮数后停止。
8. 等待所有并发任务结束，再汇总每台设备的 `liked/target`、`tap_sent`、`recoveries`、`remaining`、搜索轮数和错误。

## Pitfalls

- **不要自动登录**：首次登录、验证码、短信验证或异常登录提示必须由用户人工完成。详见 [references/facebook-login-guide.md](references/facebook-login-guide.md)。
- **不要使用旧账号信息**：旧版 skill 曾包含明文凭据；新版已移除。不要从历史文件或日志恢复它们。
- **语言变化**：默认识别 `赞`、`讚`、`Like`，并排除 `已按下`、`已赞`、`已讚`、`Unlike` 以及评论区的点赞/反应按钮。若 Facebook 改文案，先用 `--verbose` 查看候选节点，再修改脚本中的标签列表。
- **页面结构变化**：不要写死点赞按钮坐标。滑动坐标只负责滚动，点赞坐标必须来自当前 UI dump。
- **页面加载不全或卡住**：脚本会在连续非 Feed、连续无可点赞按钮或 UI dump 失败时自动关掉并重开 Facebook。若达到 `--max-recoveries` 仍失败，只按汇总中的 `remaining` 补跑，不要重跑原始 `--count`。
- **点赞点击不等于成功**：`tap sent` 只表示点过按钮。只有 `like verified` 或汇总中的 `status=ok liked=<目标数>` 才能当作成功。
- **无法验证点赞**：`status=unverified-like` 表示已经尝试点 Like，但 UI 不能确认结果。先人工查看设备，不要立即补跑，否则可能再次点击同一按钮导致取消点赞。
- **网络失败**：先确认电脑与 MYT 在可互通网络，再检查控制器 IP、设备端口和防火墙。
- **重复运行**：脚本只选择 UI 标记为未点赞的按钮，但应用文案变化可能导致误判；实际执行前先预演。
- **点赞数量缺失**：`--count` 没有默认值。除 `--check` 外，缺少该参数会安全退出。
- **并发日志**：每行都带设备编号。不要因为日志先后交错就误认为设备仍在串行执行。
- **计划任务**：先手工验证。定时执行时必须明确保存 `MYT_HOST`，并在任务命令中保留清晰日志；不要把社交账号密码放入计划任务。

## Verification

- `--check` 对所有目标设备显示 `OK`。
- 预演输出 `DRY RUN` 和识别到的按钮描述、坐标，但不会输出 `tap sent`。
- 实际执行后，每台设备汇总显示 `status=ok liked=<目标数>` 才算完成；`tap_sent` 只是点击次数，不代表成功。
- `recoveries=N` 表示该设备自动关掉并重开 Facebook 的次数；最终成功时可以接受，不需要补跑。
- `status=time-limit-reached` 表示达到内部时限；只按 `remaining` 补跑未完成设备，不要整批重跑。
- `status=screen-state-error` 或 `status=no-like-buttons` 表示页面不适合继续安全操作；先检查页面是否加载完整，再按 `remaining` 补跑。
- `status=unverified-like` 表示点击后结果无法确认；人工核对设备后再决定是否补跑。
- 若计数未达到目标，保留输出中的端口、轮次和错误信息，使用 `--verbose` 重跑诊断，不要盲目增加点击次数。
