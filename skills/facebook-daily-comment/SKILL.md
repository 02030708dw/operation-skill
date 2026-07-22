---
name: facebook-daily-comment
description: Comment on Facebook feed posts through the MYT API.
metadata:
  version: "2.8.0"
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
- Python 3.9 或更高版本可通过 `python` 调用（Linux/macOS 可按本机环境改用 `python3`）。
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
| `MYT_STALL_TIMEOUT` | `120` | 连续没有实质进展时的停止秒数；每次推进流程都会重新计时 |
| `MYT_DEDUPE_STORE` | `~/.hermes/state/facebook-daily-comment-dedupe.json` | 已评论帖子指纹记录 |
| `MYT_DEDUPE_TTL_DAYS` | `14` | 帖子指纹保留天数 |

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
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001,T1002 --count 3 --comment "good"
```

用户明确授权后执行：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001,T1002 --count 3 --comment "good" --execute --max-runtime 0
```

多个设备默认同时执行，每台目标云手机使用独立并发任务。

脚本默认不限制每台设备的正常总执行时间，只在连续 120 秒没有实质进展时停止。找到新帖子、找到输入框、识别发送键或验证评论成功都会刷新无进展计时。Hermes 调用 `terminal` 时，外层工具超时必须至少设置为 `max(600, 120 + 每台目标数量 × 90)` 秒；多台设备并发时按单台数量计算，不要按设备数量相加。

## Quick Reference

| 目的 | 参数 |
|---|---|
| 指定控制器 | `--host myt-controller.local` |
| 指定实例 | `--devices T1001,T1002` |
| 直接指定端口 | `--devices 10005,10008` |
| 每台评论 N 篇 | `--count N`（必须填写） |
| 指定评论内容 | `--comment "good"`（必须填写） |
| 连续无成功评论的查找轮数 | `--max-cycles 30` |
| 可选单台硬时限 | `--max-runtime N`（默认 `0`，仅显式传入非零值才启用） |
| 无进展超时 | `--stall-timeout 120` |
| UI dump 自动重试次数 | `--ui-dump-retries 4` |
| Feed 加载检查 | `--feed-ready-retries 5 --feed-ready-wait 1.5` |
| 空页面/非 Feed 快速退出 | `--not-feed-limit 4 --no-button-limit 8` |
| 发送后验证 | `--post-send-verify-retries 3 --post-send-verify-wait 1.5` |
| 图标型发送键 | 默认开启；`--no-send-icon-fallback` 可关闭纸飞机图标 fallback |
| 键盘 Enter | 不用于发送；Facebook 评论框中通常只是换行 |
| 防重复评论 | 默认开启；`--no-dedupe` 可关闭帖子指纹去重 |
| 可见评论防重 | 默认开启；`--no-visible-dedupe` 可关闭同文字扫描 |
| 去重记录文件 | `--dedupe-store ~/.hermes/state/facebook-daily-comment-dedupe.json` |
| 去重保留天数 | `--dedupe-ttl-days 14` |
| 自动恢复次数 | `--max-recoveries 2` |
| 恢复时关闭 Facebook | `--recovery-close-method both`（最近任务滑掉 + force-stop） |
| 最近任务滑掉坐标 | `--recents-swipe 360,900,360,120,500` |
| 关闭自动恢复 | `--no-auto-recover` |
| 只测连接 | `--check` |
| 不导航到 Facebook | `--no-launch`（仅确认当前就在 Feed 时使用） |
| 强制重启 Facebook | `--force-restart`（仅排障时使用） |
| 真正发送评论 | `--execute` |
| 输出详细诊断 | `--verbose`（与 `--execute` 互斥） |

退出码：`0` 表示全部达到目标或预演成功，`1` 表示至少一台未达到目标，`2` 表示配置或参数错误。

## Procedure

1. 读取用户明确指定的设备、每台评论数量和评论内容，不使用旧版固定值。
2. 若缺少数量或内容，先向用户询问；不要自行生成或猜测评论。
3. 若 `MYT_HOST` 未配置，要求用户在本机配置；不要在聊天中索取密码或验证码。
4. 先使用 `--check` 并发确认目标端口可访问。
5. 用户未明确要求真实发送时，执行不带 `--execute` 的预演；详细诊断只添加 `--verbose`，不得同时添加 `--execute`。
6. 用户明确要求执行时才添加 `--execute`，并保持 `--max-runtime 0`，不要根据评论数量擅自设置固定总时限。
7. 所有设备同时启动独立任务。每台设备会：
   - 默认通过 `fb:///` 深链导航到 Facebook Feed，不会强制停止 App；只有用户明确排障时才使用 `--force-restart`。
   - 启动后先验证 UI 已可读取；空或损坏的 UI dump 默认自动重试 4 次。
   - 若 Facebook 页面半载入、非 Feed、连续找不到评论按钮、找不到输入框、找不到发送按钮，或已验证评论后无法返回 Feed，会触发自动恢复。
   - 正常评论流程没有固定 80 秒总时限；每次选中有效帖子、找到输入框、找到发送键和验证发送成功都会刷新无进展计时，直到达到用户要求的数量。
   - 只有连续没有流程进展、连续多轮找不到可用评论，或停在非 Feed/无关页面并且恢复失败时，才停止并返回对应错误状态。
   - 自动恢复默认最多 2 次：打开最近任务并按 `--recents-swipe` 滑掉当前 App，再 `force-stop` Facebook，重新用 Feed 深链打开，并按已验证的剩余数量继续。
   - 识别当前页面：Feed、评论详情、Facebook 其他页面或其他 App。
   - 若停留在评论详情，只对已确认的评论层安全返回，每次返回后重新识别；禁止连续盲按返回键。
   - 若 `--no-launch` 时检测到桌面或其他 App，立即返回 `screen-state-error`，不持续滚动等待。
   - 滚动动态并取得当前 `uiautomator` XML。
   - 只接受精确的中文简体、中文繁体或英文评论按钮标签，排除“给评论留下心情”等反应按钮说明。
   - 对每个评论按钮附近的稳定帖子文字生成帖子指纹；若同设备同评论内容已处理过该帖子，直接跳过并滚动。若当前 UI 没有足够文字，只生成诊断指纹，不写入长期去重记录，避免仅凭坐标误判不同帖子。
   - 进入评论区后轮询 UI，等待输入框完成加载。
   - 输入前扫描当前可见留言；若已经看到完全相同的评论文本，跳过该帖子，不再发送。
   - 输入用户提供的评论内容。
   - 再次轮询 UI，先确认评论文字仍在输入框中，再按完整标签精确识别 `发送/送出/send/post` 等发送按钮。输入框即使包含 `Nice post!` 或 `send` 等文字也永远不会被当成发送键；若没有文字按钮，则在输入框右侧识别小型纸飞机图示 fallback，并排除相机、加号、表情、贴图等非发送按钮。不要用键盘 Enter 发送；该 Facebook 评论框中 Enter 通常只是换行。
   - 点击识别到的发送按钮。
   - 点击发送后重新读取 UI；只有页面回到 Feed、评论输入层关闭、发送按钮消失，或评论框不再保持可发送状态时，才把 `commented` 加 1。
   - 若无法确认发送成功，返回 `status=unverified-send`，记录 `sent_taps` 但不增加 `commented`，并停止该设备，避免重复评论。
   - 返回动态并滚动跳过刚处理的帖子。
8. 最后一条评论验证通过后立即返回成功，不再执行返回动态页的网络收尾。
9. 等待所有并发任务结束，再分别汇总 `commented/target`、`sent_taps`、`unverified`、`recoveries`、`duplicates_skipped`、`remaining`、查找轮数和错误，并输出机器可读的 `RESULT_JSON`。

## Partial Completion and Recovery

部分完成的评论必须计入用户本次要求。若某台设备目标为 3、汇总显示 `commented=2/3 remaining=1`：

1. 不要把 `--count 3` 再执行一次。
2. 只对未完成设备补跑 `--count 1`。
3. 补跑默认使用 Feed 深链，不添加 `--no-launch`；它会导航回 Facebook Feed，但不会强制停止 App。
4. 已完成的其他设备不得再次包含在 `--devices` 中。
5. 若多个设备的 `remaining` 不同，按剩余数量分组或分别执行。

正确补跑示例：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001 --count 1 --comment "good" --execute --max-runtime 0
```

脚本会输出 `REMAINING: T1001=1` 和明确的 `RETRY RULE`。只有用户或操作者已确认云手机当前就在 Feed 时才使用 `--no-launch`。Hermes 必须服从剩余数量提示，不能以“完成原始目标”为由重跑原始总数。自动恢复仍属于真实外部写操作；若日志无法确认已发送数量，应停止并请求用户核对，而不是猜测。

若某台设备显示 `status=unverified-send`，表示发送按钮确实点过，但脚本没有足够 UI 证据确认评论已经发布。此时不要直接按 `remaining` 自动补跑；先人工查看该云手机最新帖子评论。若评论已出现，把它从剩余数量中扣掉；若没有出现，再补跑剩余数量。

若汇总显示 `recoveries=1` 或更高，表示脚本曾自动关掉并重开 Facebook 后继续执行。只要最终 `status=ok commented=<目标数>`，无需额外补跑。若达到 `--max-recoveries` 后仍失败，只按汇总中的 `remaining` 补跑，不要重跑原始总数。

若汇总显示 `duplicates_skipped=1` 或更高，表示脚本检测到同一帖子或可见的同文字评论，已跳过这些帖子。跳过不计入 `commented`，也不应补成重复评论；只按 `remaining` 继续找新的帖子。

**补跑卡住恢复**：若补跑时脚本点击评论按钮后卡住（无 `send tap issued` 输出），设备可能留在前一次中断的评论详情页，导致输入框检测失败。此时补跑应添加 `--force-restart` 强制重启 Facebook，而非继续使用普通 Feed 深链。示例如下：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001 --count 1 --comment "good" --execute --force-restart --max-runtime 0
```

诊断页面状态时必须去掉 `--execute`。禁止为了查看 verbose 日志再次执行真实评论命令。使用：

```powershell
python "<SKILL_DIR>\scripts\facebook_daily_comment.py" --devices T1001 --count 1 --comment "good" --verbose
```

禁止绕过 Skill 临时拼接带 MYT 主机地址的 Python/HTTP 返回键脚本。页面恢复由本脚本的状态识别负责；若仍失败，应停止并让用户人工确认画面。

## Text Input Limits

MYT 的 Android `input text` 不保证直接输入 Unicode。新版脚本默认只接受 1–200 个可打印 ASCII 字符，并对远端 shell 参数安全引用；空格会转换为 Android 支持的 `%s`。

- 可用示例：`good`、`Nice post!`、`Thanks for sharing.`
- 默认不支持：中文、Emoji、换行。
- 如需中文或 Emoji，应先在云手机部署支持 Unicode 输入的 ADB 输入法，再单独扩展脚本；不要通过拼接 shell 命令绕过校验。

## Pitfalls

- **不要自动登录**：首次登录、短信验证、双因素验证和异常登录提示必须人工处理。详见 [references/facebook-comment-guide.md](references/facebook-comment-guide.md)。
- **不要使用固定坐标**：评论按钮、输入框和发送按钮都必须来自当前 UI XML。
- **避免重复评论**：发送后脚本会返回动态并连续滚动多次，但页面刷新仍可能改变位置；先用较小数量验证。
- **同帖防重**：脚本会保存同设备、同评论内容、同帖子指纹的记录，默认 14 天内不再评论同帖。只有包含稳定帖子文字的可靠指纹才会持久化，纯坐标诊断指纹不会写入。不要删除 `--dedupe-store`，除非明确要清空历史防重记录。
- **同文字可见防重**：进入评论区后若已经看见完全相同的评论文本，会跳过该帖。对于 `good` 这类非常通用的评论，可能会跳过别人已经写过 `good` 的帖子；这比重复评论更安全。如确认要关闭这层扫描，可加 `--no-visible-dedupe`，但不建议大批量执行时关闭。
- **评论数量和内容缺失**：除 `--check` 外，缺少 `--count` 或 `--comment` 会安全退出。
- **评论按钮误判**：只匹配 `评论`、`評論`、`评论按钮`、`評論按鈕`、`Comment` 或 `Comment button` 的完整标签，不再使用“包含评论”规则。
- **停留在评论详情**：脚本会识别底部评论输入框并安全关闭评论层；不会把详情页当作 Feed 持续滚动。
- **停留在手机桌面**：`--no-launch` 会立即返回 `screen-state-error`。重新执行剩余数量时去掉 `--no-launch`，让 Feed 深链恢复 Facebook。
- **UI dump 为空**：每次 dump 默认自动重试 4 次。连接检查成功不代表 Facebook UI 已加载；Feed 深链比强制重启更稳定。
- **页面加载不全或卡在空白/非 Feed**：新版会先做 Feed readiness 检查；若连续处于 `facebook-other` 或连续多个周期找不到评论按钮，会返回 `screen-state-error` 或 `no-comment-buttons`，不要盲目重跑 `--execute`。
- **输入框加载较慢**：脚本默认重试输入框 4 次、发送按钮 3 次。识别失败时先用 `--verbose` 预演，不要立刻重复真实发送。
- **自动恢复边界**：页面未到发送阶段的异常会自动关掉并重开 Facebook 后继续。若已经尝试点击发送但无法验证，状态会变成 `unverified-send` 并停止，不能自动恢复继续。
- **最近任务滑掉不准**：不同云手机的最近任务卡片坐标可能不同。默认 `--recovery-close-method both` 会同时滑掉最近任务并 `force-stop` Facebook；若滑动方向不对，调整 `--recents-swipe x1,y1,x2,y2,duration_ms`。
- **恢复次数用尽**：`--max-recoveries` 默认 2 次。超过后会返回失败汇总和 `remaining`，不要反复用原始 `--count` 重跑。
- **发送点击不等于成功**：`send tap issued` 只表示点过发送按钮。只有 `send verified` 或汇总中的 `commented=<目标数>` 才能当作成功。
- **评论内容含 send/post**：发送按钮使用完整标签匹配并排除输入节点，`Nice post!`、`Please send it` 等评论内容不会再把输入框误识别为发送按钮。`--verbose` 会打印最终发送目标的描述和坐标。
- **外层命令超时**：正常大任务不再受 80 秒内部总时限限制。Hermes 必须按 `max(600, 120 + 每台目标数量 × 90)` 秒设置 terminal 工具超时，避免外层提前产生 exit 124。若外层仍被中断且 `sent_taps` 大于 `commented`，先人工核对设备，不要直接重跑。
- **脚本卡住（点击评论按钮后无反应）**：设备若被前一次中断的执行留在评论详情页，脚本可能点击评论按钮后找不到输入框（`input field not found`）并卡住。使用 `--force-restart` 可强制重启 Facebook 恢复，比单纯重试更可靠。若多次卡住，优先用 `--force-restart` 补跑，不要反复执行不带重启的普通重试。
- **大批量评论性能**：只要流程持续推进，脚本会继续执行到目标数量；不要因为运行超过 80 秒就拆分或补跑。若出现 `unverified-send`，仍必须先人工确认，因为它表示发送结果不确定，而不是任务量较大。
- **错误补跑**：绝不能用原始 `--count` 补跑部分完成设备。只使用汇总中的 `remaining`；默认不要加 `--no-launch`。
- **危险诊断**：`--verbose` 排障不得同时携带 `--execute`，也不要使用临时 HTTP 脚本连续发送返回键。
- **Facebook 文案变化**：识别失败时使用 `--verbose` 查看节点描述，再有针对性地扩展精确标签。
- **并发日志**：每行带设备编号；日志交错表示多台设备正在同时运行。
- **平台规则**：自动评论可能受 Facebook 规则或账号风控限制。控制频率、使用真实合规内容，并由用户承担执行决策。

## Verification

- `--check` 对全部目标设备显示 `connection: OK`。
- 预演显示 `DRY RUN` 和评论按钮坐标，不输入或发送文字。
- 实际执行汇总中每台设备显示 `commented=<目标数>` 且 `status=ok`，才算完成。`sent_taps` 只是发送按钮点击次数，不代表成功。
- `recoveries=N` 表示该设备自动关掉并重开 Facebook 的次数；最终成功时可以接受，不需要补跑。
- `duplicates_skipped=N` 表示跳过了疑似已评论或已有同文字评论的帖子；这些帖子不得用补跑追回。
- `status=unverified-send` 表示发送按钮已点击但 UI 没能确认发布；人工核对该云手机后再决定是否补跑，避免重复评论。
- `status=stalled-timeout` 表示连续超过 `--stall-timeout` 没有实质进展，通常是找不到评论、页面无关或 UI/API 卡住。
- `status=search-timeout` 表示连续 `--max-cycles` 轮没有完成新的已验证评论；已经完成的数量仍会保留。
- `status=time-limit-reached` 只会在用户显式设置非零 `--max-runtime` 时出现；默认不会因正常任务执行较久而触发。
- `status=screen-state-error` 表示页面不适合安全操作；根据错误去掉 `--no-launch` 或人工确认页面，不要继续发送。
- `status=no-comment-buttons` 表示 Facebook 已打开但连续多个周期没有可评论按钮；先人工检查页面是否加载完整，或用不带 `--execute` 的 `--verbose` 诊断。
- 若未达到目标，保留端口、轮次和错误，使用 `--verbose` 诊断，不要盲目提高轮数或重复发送。
