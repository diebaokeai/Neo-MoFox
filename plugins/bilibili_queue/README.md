# bilibili_queue · 直播弹幕排队（bot 侧接入）

把 B 站直播弹幕接到**独立排队姬应用** [`live_queue`](../../live_queue/README.md) 上：

```
观众发弹幕「排队」 ──► 本插件识别意图 ──HTTP──► live_queue（排队姬）
舰长发弹幕「下一个」 ──► 本插件校验权限 ──HTTP──►   ├─ 排队姬窗口（OBS 窗口采集）
QQ 群 /排队        ──► 本插件转发     ──HTTP──►   ├─ OBS 浏览器源叠加页
                                                └─ 控制面板
```

**本插件不存队列状态。** 队列、窗口、页面、HTTP 接口全部由 `live_queue` 提供，所以排队姬
能脱离 bot 单独使用；bot 开着时又能接管它。这样只有一份队列状态，永远不会出现「两边不一致」。

---

## ⚠️ 先看这两条

### 1. B 站发不了弹幕

`bilibili_live_adapter` 是**只入站不出站**的适配器，代码里写得很明确：

> B 站不允许第三方 bot 发弹幕，整体丢弃出站。

所以「入队成功，回复你排在第 3 位」这种弹幕**发不出去**——这是平台限制，不是 bug。
观众看到的是你画面上的排队姬窗口 / 叠加页。插件默认把结果写进日志
（`[排队] 已加入「排位」，你是第 1 位`），支持出站的平台（如 QQ）才需要打开
`behavior.reply_in_stream`。

### 2. 排队姬没在跑 = 弹幕排队无效

弹幕只是「请求」，真正排队的是排队姬。默认情况下插件会在需要时**自动把它拉起来**
（`app.auto_start`），日志里会写清楚：

```
排队姬窗口已启动（pid=12345），叠加页 http://127.0.0.1:17650/overlay
排队服务就绪：独立 app 已在运行（live_queue 1.0.0）
```

如果日志出现「排队服务暂不可用」，按提示处理（多半是端口被占或密钥不匹配）。

---

## 前置条件

1. 已装好并启用 `bilibili_live_adapter`（B 站直播开放平台凭证 + 主播身份码）；
2. 有 `live_queue`（排队姬）——就是本仓库根目录下的 `live_queue/`。插件会自动拉起它，
   也可以自己先启动（见它的 README）；
3. 窗口版需要 PySide6，默认会随插件依赖自动安装。

---

## 指令表

### 观众弹幕关键词（可在配置里改）

| 弹幕 | 效果 |
| --- | --- |
| `排队` / `排个队` / `上车` / `报名` | 只发关键词：回复「请写明要排哪个队，例如「排队 排位」」（不会进默认队） |
| `排队 排位` / `排队：排位` / `排队排位` | 进「排位」队（别名由排队姬解析，如 `排位赛` → `排位`） |
| `退队` / `下车` / `取消排队` | 退出自己所在的队列 |
| `排队情况` / `排队查询` / `队列状态` | 查概况 |

匹配是**最长优先**的，所以「排队情况」不会被入队词「排队」抢走。
带分隔符（空格 / `：` / `,` / `-` 等）才算「明确写了队名」。**排队必须写明一个已存在的队名**：
没有写队名（「排队」）或队不存在（「排队 新队」）都会被拒绝并提示，排队姬**不会自己建队**
（新队只能由主播在控制面板 / 排队姬窗口里新建）。「排队一下」这种没分隔符、又不对应任何已有
队列的，也会回一句「请写明要排哪个队」，不会被理解成队名「一下」。

### 舰长弹幕命令（按大航海等级授权）

> B 站开放平台的弹幕包里**只有 `guard_level`，没有房管标记**，所以只能按舰长等级授权。
> 注意 B 站 `guard_level` 数字越小等级越高：`1=总督`、`2=提督`、`3=舰长`。

| 弹幕 | 效果 |
| --- | --- |
| `下一个 [队名]` / `下一位` / `叫号` | 叫号出队 |
| `结束 [队名]` / `结束当前` | 结束当前服务对象 |
| `放回 [队名]` | 把当前叫号的人放回队首 |
| `清空 [队名]` | 清空队列 |
| `暂停 [队名]` / `恢复 [队名]` | 暂停 / 恢复接受排队 |
| `查看队列` | 列出全部队列 |
| `开队 <队名>` / `删队 <队名>` | 建队 / 删队 |
| `重置队列` | 清空所有队列 |

队名留空 = 操作默认队。**普通观众发「下一个」不会被当成命令吞掉**，会照常进入 AI 对话
流程（这几个词都是常见口语，不能无条件拦截）。

### 聊天平台命令（QQ 等）

| 命令 | 权限 | 效果 |
| --- | --- | --- |
| `/排队` | 所有人 | 未写队名时提示要写明队名（不会自动建队） |
| `/排队 加入 <队名>` | 所有人 | 进指定队 |
| `/排队 退出 [队名]` | 所有人 | 退出队列 |
| `/排队 列表` / `/排队 我的` | 所有人 | 查看队列 / 查看自己的位置 |
| `/排队管理 下一个 [队名]` | OPERATOR | 叫号 |
| `/排队管理 结束 / 放回 / 清空 / 暂停 / 恢复 [队名]` | OPERATOR | 对应管理操作 |
| `/排队管理 队列` | OPERATOR | 列出队列 |
| `/排队管理 重置 [delete]` | OPERATOR | 清空全部队列（带 `delete` 连队列一起删） |
| `/排队管理 建队 <队名> [展示名] [上限]` | OPERATOR | 建队 |
| `/排队管理 删队 <队名>` | OPERATOR | 删队 |
| `/排队管理 移除 <用户ID> [队名]` | OPERATOR | 移出某人 |
| `/排队管理 帮助` | OPERATOR | 命令帮助 |

命令前缀由框架决定（默认 `/`）。管理命令走框架的权限体系（`owner_list` / 权限组），
所以适用于 QQ；B 站直播间请用上面的舰长弹幕命令。

---

## 配置

配置文件：`config/plugins/bilibili_queue/config.toml`（首次启动自动生成）。

这里**只配两件事**：弹幕怎么接、排队姬在哪。队列语义（默认队名、别名、人数上限、重复
策略、超时……）在排队姬那边（`live_queue/config.json`），不在这重复一份。

| 配置节 | 关键项 |
| --- | --- |
| `[plugin]` | `enabled` 总开关 |
| `[keywords]` | `join` / `leave` / `status` 三组关键词、`ignore_case` |
| `[admin]` | `min_rank` 弹幕管理最低等级（`anyone` / `captain` / `admiral` / `governor`）、`allow_danmaku_commands`、`command_prefix` |
| `[behavior]` | `platforms` 允许排队的平台、`block_message_from_llm` 命中后是否拦截弹幕、`reply_in_stream` 是否尝试出站回复、`reply_cooldown` 回复节流、`queue_status_command` |
| `[app]` | `host` / `port` / `token`（与排队姬对齐）、`auto_start` 自动拉起、`start_timeout`、`no_window` 只开服务不开窗口、`executable` 自定义解释器、`extra_args` 附加参数、`stop_on_unload` 是否随 bot 关闭排队姬（默认 false） |
| `[debug]` | `log_danmaku` 逐条弹幕日志、`log_commands` 操作日志 |

> `app.no_window = true` 适合「排队姬跑在没屏幕的机器上，只给 OBS 浏览器源用」的场景。

---

## 给其他插件调用

服务签名 `bilibili_queue:service:queue`：

```python
from src.app.plugin_system.api import service_api

queue = service_api.get_service("bilibili_queue:service:queue")

result = await queue.join(user_id="123", nickname="阿狸", queue="排位")
# {"ok": True, "status": "joined", "queue": "排位", "position": 1, "ticket": 1, ...}

snapshot = await queue.snapshot()          # 状态快照（纯 dict）
called = await queue.call_next("排位")      # 叫号，called["entry"] 是被叫到的人
```

返回值就是排队姬接口的结果 dict（含 `ok` / `status` / `message`），**都是内置类型**，
不需要 import 本插件的任何模块。

---

## 排查

| 现象 | 原因与处理 |
| --- | --- |
| 弹幕排队没反应 | 看日志有没有「排队服务暂不可用」。多半是排队姬端口/密钥和插件 `[app]` 不一致 |
| 日志说「端口上有服务在响应，但它不是 live_queue」 | 那个端口被别的程序占了，换 `[app] port` 与排队姬的 `server.port` |
| 日志说「密钥不匹配」 | 让 `[app] token` 与排队姬的 `server.token` 一致（或两边都留空只用本机访问） |
| 舰长命令没反应 | 检查 `[admin] min_rank`；`captain` 表示舰长（guard_level 1/2/3）都可以 |
| 观众说「我发了排队但没反应」 | 正常——B 站发不出弹幕，观众看的是你画面上的队列 |
| 队列显示还是旧的 | 叠加页窗口 1 秒刷新一次；浏览器源注意缓存 |
| 窗口没出现 | 没装 PySide6；`uv pip install PySide6-Essentials` 后重载插件 |
| 不想让 bot 拉起排队姬 | `[app] auto_start = false`，自己先启动它 |
| bot 重启把排队姬也关了 | 默认不会；除非你打开了 `[app] stop_on_unload` |

---

## 目录结构

```
plugins/bilibili_queue/
├── manifest.json            插件元数据（组件声明、依赖）
├── plugin.py                插件入口：生命周期、后台预热（拉起排队姬）
├── config.py                插件配置模型（6 个配置节）
├── core/
│   ├── envelope.py          统一消息信封 → 弹幕字段
│   ├── keywords.py          弹幕文本 → 意图（最长优先匹配）
│   ├── messages.py          回复文案（纯函数）
│   ├── settings.py          扁平化运行参数
│   ├── client.py            访问排队姬的 HTTP 客户端
│   ├── supervisor.py        排队姬进程的探测 / 拉起 / 关闭
│   └── runtime.py           上面的组合，组件统一入口
└── components/
    ├── queue_service.py      service:queue          对外服务
    ├── danmaku_handler.py    event_handler          弹幕入队（BEFORE_MESSAGE_RECEIVED）
    ├── queue_command.py      command:排队           观众命令
    ├── queue_admin_command.py command:排队管理       管理命令
    └── queue_common.py       命令发送者信息提取
```

测试：`test/plugins/bilibili_queue/`（约 230 个）与 `test/live_queue/`（约 210 个）：

```powershell
uv run pytest test/plugins/bilibili_queue test/live_queue -q
```

---

## 设计取舍（给后来改代码的人）

1. **队列状态放在独立应用里，插件不存副本**。这是这次重构的核心：两份状态必然漂移，
   而排队姬又必须能脱离 bot 单独使用。
2. **在 `BEFORE_MESSAGE_RECEIVED` 处理弹幕**而不是 `ON_MESSAGE_RECEIVED`：前者能读到
   `guard_level`、能干净拦截不进 LLM，也没有「同优先级订阅顺序」的竞态。
3. **处理器自己 try/except**：框架会把处理器异常转成 `PASS`（放行给 LLM），所以任何异常
   都必须自己兜住并记日志。
4. **返回的 `params` 原样返回**：key 集合必须与入参完全一致，否则处理结果被丢弃。
5. **排队姬不可用时仍然拦截队列关键词**：「排队」是命令，丢给 LLM 只会得到莫名其妙的回复；
   管理命令无权限时才放行。
6. **`QueueRuntime.call()` 永远返回 dict**：把「app 不可用 / 调用失败」合成为
   `ok=False` 的结果，组件里就不用到处写 try/except。
7. **就绪检查带 30 秒缓存**：否则每条弹幕都要先打一次 `/api/health`。调用失败时立刻让
   缓存失效，所以排队姬崩了也能自动恢复。
8. **拉起 app 放在后台任务里**：首次启动要加载 Qt，十几秒；插件加载是串行的，卡住它等于
   拖慢整个 bot 启动。
9. **日志一律用 `logger.error(msg, exc_info=True)`，不要用 `logger.exception()`**：
   本框架的 `Logger`（`src/kernel/logger/logger.py`）**没有** `exception` 方法，
   写了会在运行时抛 `AttributeError`，恰好把真正的异常盖掉。
10. **别名配置用 `list[str]` 而不是 `dict`**：框架渲染 TOML 时会把 dict 的键写成不带引号
    的裸键（`{ 上分 = "排位" }`），中文键在 TOML 里非法，写出来的配置文件下次读不回来。
