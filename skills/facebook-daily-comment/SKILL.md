---
name: facebook-daily-comment
description: Comment on Facebook feed posts through the MYT API.
version: 2.3.0
author: Local user with Hermes Agent
platforms: [windows, linux, macos]
required_environment_variables:
  - name: MYT_HOST
    prompt: MYT controller IP address or hostname
    help: Enter only the host, for example myt-controller.local, without http:// or a port.
    required_for: connecting to the MYT controller
prerequisites:
  commands: [python]
metadata:
  hermes:
    tags: [Automation, Android, Facebook, MYT, Comment]
    requires_tools: [terminal]
---

# Facebook Daily Comment Skill

通过魔云腾（MYT）V1 HTTP API，在一个或多个 Android 云手机的 Facebook 动态中寻找帖子，并按用户指定的数量和内容发表评论。此 Skill 只操作已登录的 Facebook App，不保存账号、密码，也不自动处理验证码或安全验证。

发表评论属于真实外部写操作。除非用户在当前请求中明确要求立即执行，否则只运行预演；只有脚本带 `--execute` 时才会输入并发送评论。

## When to Use

- 用户要求在 MYT 云手机的 Facebook 动态中发表评论。
- 用户要求同时操作 T1001、T1002 或其他 MYT 实例。
- 用户需要检查评论按钮、输入框、发送按钮或 MYT API 连接问题。

## Prerequisites

- Hermes 会话提供 `terminal` 工具。
- Python 3.9 或更高版本可通过 `python` 调用。
- Hermes 所在电脑能访问 MYT 控制器。
- Facebook 已在目标 Android 实例中人工登录完成。
- 将 MYT 控制器主机名或 IP 存入 `MYT_HOST`。不要把社交账号凭据写入 Skill。

可选环境变量：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `MYT_DEVICE_IDS` | `T1001,T1002` | 目标实例；也可直接填写端口 |
| `MYT_MAX_CYCLES` | `30` | 每个实例最多查找轮数 |
| `MYT_BASE_PORT` | `10005` | T1001 对应的基础端口 |
| `MYT_PORT_STRIDE` | `3` | 相邻实例端口步长 |
| `MYT_TIMEOUT` | `15` | 单个 HTTP 请求超时秒数 |
| `MYT_MAX_RUNTIME` | `105` | 每台设备的最长执行秒数，确保常见的 120 秒外层超时前返回汇总 |

默认端口规则：`T100N -> MYT_BASE_PORT + (N - 1) * MYT_PORT_STRIDE`。默认配置下 T1001 为 10005，T1002 为 10008。自定义映射时可在 `--devices` 中直接填写数字端口。

评论数量和评论内容没有默认值。除 `--check` 外，必须使用 `--count` 和 `--comment` 明确提供。

## How to Run

Hermes 加载 Skill 后会注入 `[Skill directory: <绝对路径>]`。使用该路径替换以下命令中的 `<SKILL_DIR>`。

只检查目标设备连接：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001,T1002 --check
```

预演评论按钮识别，不输入、不发送：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001,T1002 --count 3 --comment ":good"
```

用户明确授权后执行：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001,T1002 --count 3 --comment ":good" --execute
```

多个设备默认同时执行，每台目标云手机使用独立并发任务。

脚本默认在每台设备运行 105 秒时主动停止并输出部分结果。Hermes 调用 `terminal` 时应把外层命令超时设为至少 120 秒；不要仅通过增加 HTTP `--timeout` 来延长总执行时间。

## Quick Reference

| 目的 | 参数 |
|---|---|
| 指定控制器 | `--host myt-controller.local` |
| 指定实例 | `--devices T1001,T1002` |
| 直接指定端口 | `--devices 10005,10008` |
| 每台评论 N 篇 | `--count N`（必须填写） |
| 指定评论内容 | `--comment ":good"`（必须填写） |
| 限制查找轮数 | `--max-cycles 10` |
| 限制单台总耗时 | `--max-runtime 105` |
| UI dump 自动重试次数 | `--ui-dump-retries 4` |
| 只测连接 | `--check` |
| 不导航到 Facebook | `--no-launch`（仅确认当前就在 Feed 时使用） |
| 强制重启 Facebook | `--force-restart`（仅排障时使用） |
| 真正发送评论 | `--execute` |
| 输出详细诊断 | `--verbose` |

退出码：`0` 表示全部达到目标或预演成功，`1` 表示至少一台未达到目标，`2` 表示配置或参数错误。

## Procedure

1. 读取用户明确指定的设备、每台评论数量和评论内容，不使用旧版固定值。
2. 若缺少数量或内容，先向用户询问；不要自行生成或猜测评论。
3. 若 `MYT_HOST` 未配置，要求用户在本机配置；不要在聊天中索取密码或验证码。
4. 先使用 `--check` 并发确认目标端口可访问。
5. 用户未明确要求真实发送时，执行不带 `--execute` 的预演。
6. 用户明确要求执行时才添加 `--execute`。
7. 所有设备同时启动独立任务。每台设备会：
   - 默认通过 `fb:///` 深链导航到 Facebook Feed，不会强制停止 App；只有用户明确排障时才使用 `--force-restart`。
   - 启动后先验证 UI 已可读取；空或损坏的 UI dump 默认自动重试 4 次。
   - 识别当前页面：Feed、评论详情、Facebook 其他页面或其他 App。
   - 若停留在评论详情，只对已确认的评论层安全返回，每次返回后重新识别；禁止连续盲按返回键。
   - 若 `--no-launch` 时检测到桌面或其他 App，立即返回 `screen-state-error`，不持续滚动等待。
   - 滚动动态并取得当前 `uiautomator` XML。
   - 只接受精确的中文简体、中文繁体或英文评论按钮标签，排除“给评论留下心情”等反应按钮说明。
   - 进入评论区后轮询 UI，等待输入框完成加载。
   - 输入用户提供的评论内容。
   - 再次轮询 UI，动态识别并点击发送按钮。
   - 返回动态并滚动跳过刚处理的帖子。
8. 最后一条评论发出后立即返回成功，不再执行返回动态页的网络收尾。
9. 等待所有并发任务结束，再分别汇总 `commented/target`、`remaining`、查找轮数和错误，并输出机器可读的 `RESULT_JSON`。

## Partial Completion and Recovery

部分完成的评论必须计入用户本次要求。若某台设备目标为 3、汇总显示 `commented=2/3 remaining=1`：

1. 不要把 `--count 3` 再执行一次。
2. 只对未完成设备补跑 `--count 1`。
3. 补跑默认使用 Feed 深链，不添加 `--no-launch`；它会导航回 Facebook Feed，但不会强制停止 App。
4. 已完成的其他设备不得再次包含在 `--devices` 中。
5. 若多个设备的 `remaining` 不同，按剩余数量分组或分别执行。

正确补跑示例：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001 --count 1 --comment ":good" --execute
```

脚本会输出 `REMAINING: T1001=1` 和明确的 `RETRY RULE`。只有用户或操作者已确认云手机当前就在 Feed 时才使用 `--no-launch`。Hermes 必须服从剩余数量提示，不能以“完成原始目标”为由重跑原始总数。自动恢复仍属于真实外部写操作；若日志无法确认已发送数量，应停止并请求用户核对，而不是猜测。

诊断页面状态时必须去掉 `--execute`。禁止为了查看 verbose 日志再次执行真实评论命令。使用：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001 --count 1 --comment ":good" --verbose
```

禁止绕过 Skill 临时拼接带 MYT 主机地址的 Python/HTTP 返回键脚本。页面恢复由本脚本的状态识别负责；若仍失败，应停止并让用户人工确认画面。

## Text Input Limits

MYT 的 Android `input text` 不保证直接输入 Unicode。新版脚本默认只接受 1–200 个可打印 ASCII 字符，并对远端 shell 参数安全引用；空格会转换为 Android 支持的 `%s`。

- 可用示例：`:good`、`Nice post!`、`Thanks for sharing.`
- 默认不支持：中文、Emoji、换行。
- 如需中文或 Emoji，应先在云手机部署支持 Unicode 输入的 ADB 输入法，再单独扩展脚本；不要通过拼接 shell 命令绕过校验。

## Pitfalls

- **不要自动登录**：首次登录、短信验证、双因素验证和异常登录提示必须人工处理。详见 [references/facebook-comment-guide.md](references/facebook-comment-guide.md)。
- **不要使用固定坐标**：评论按钮、输入框和发送按钮都必须来自当前 UI XML。
- **避免重复评论**：发送后脚本会返回动态并连续滚动多次，但页面刷新仍可能改变位置；先用较小数量验证。
- **评论数量和内容缺失**：除 `--check` 外，缺少 `--count` 或 `--comment` 会安全退出。
- **评论按钮误判**：只匹配 `评论`、`評論`、`评论按钮`、`評論按鈕`、`Comment` 或 `Comment button` 的完整标签，不再使用“包含评论”规则。
- **停留在评论详情**：脚本会识别底部评论输入框并安全关闭评论层；不会把详情页当作 Feed 持续滚动。
- **停留在手机桌面**：`--no-launch` 会立即返回 `screen-state-error`。重新执行剩余数量时去掉 `--no-launch`，让 Feed 深链恢复 Facebook。
- **UI dump 为空**：每次 dump 默认自动重试 4 次。连接检查成功不代表 Facebook UI 已加载；Feed 深链比强制重启更稳定。
- **输入框加载较慢**：脚本默认重试输入框 4 次、发送按钮 3 次。识别失败时先用 `--verbose` 预演，不要立刻重复真实发送。
- **外层命令超时**：脚本有 105 秒内部截止时间并会先打印部分汇总。若日志已经出现 `send tap issued (N/N)`，不要因外层出现 exit 124 而重新执行，否则可能重复评论。
- **错误补跑**：绝不能用原始 `--count` 补跑部分完成设备。只使用汇总中的 `remaining`；默认不要加 `--no-launch`。
- **危险诊断**：`--verbose` 排障不得同时携带 `--execute`，也不要使用临时 HTTP 脚本连续发送返回键。
- **Facebook 文案变化**：识别失败时使用 `--verbose` 查看节点描述，再有针对性地扩展精确标签。
- **并发日志**：每行带设备编号；日志交错表示多台设备正在同时运行。
- **平台规则**：自动评论可能受 Facebook 规则或账号风控限制。控制频率、使用真实合规内容，并由用户承担执行决策。

## Verification

- `--check` 对全部目标设备显示 `connection: OK`。
- 预演显示 `DRY RUN` 和评论按钮坐标，不输入或发送文字。
- 实际执行汇总中每台设备显示 `commented=<目标数>`。
- `status=time-limit-reached` 表示达到内部时限；只按 `remaining` 补跑未完成设备，不要整批重跑。
- `status=screen-state-error` 表示页面不适合安全操作；根据错误去掉 `--no-launch` 或人工确认页面，不要继续发送。
- 若未达到目标，保留端口、轮次和错误，使用 `--verbose` 诊断，不要盲目提高轮数或重复发送。
