# live_queue · 排队姬

**独立运行的直播排队应用**：多命名队列 + 桌面窗口 + OBS 浏览器源叠加页 + HTTP 控制接口。

它**不依赖任何机器人框架**——整个目录拷到别的机器上，只要有 Python 和（可选的）PySide6
就能跑。把弹幕送进来的有两条路（二选一或都开着，见「和 bot 同时开着会不会重复排队？」）：

```
观众发弹幕「排队」 ──► bilibili_queue 插件（跑在 bot 里）──┐
                  └──► bilibili_bridge.py（独立桥）      ──┤──HTTP──► live_queue ──► 窗口 / OBS 叠加页
主播点窗口上的按钮 / 控制面板 ───────────────────────────────┘
```

---

## 快速开始

### 1. 装依赖

```powershell
pip install PySide6-Essentials      # 只要窗口才需要；不要窗口可以跳过
```

### 2. 启动

三种方式等价，任选：

```powershell
# ① 启动器（Windows，双击也行，最省事）
live_queue\start.bat

# ② 脚本方式
python live_queue/app.py

# ③ 模块方式
python -m live_queue.app
```

> `live_queue/start.bat` 优先用本仓库的 `.venv`（那里有 PySide6，能开窗口），找不到就退回
> PATH 上的 `python`，所以把 `live_queue/` 整个目录拷到别处也能用；从任意目录用绝对路径
> 调 `app.py` 同样可以。
> 它自身只写 ASCII 注释——cmd 是按 OEM 代码页读批处理文件的，UTF-8 中文注释会被错误解码、
> 甚至被当成命令执行。

### 2.1 独立启动器与桌面快捷方式

不想每次手动敲命令，可以直接双击 **`live_queue\start_standalone.bat`**，或者用它建一个桌面快捷方式
（本仓库已经在本机桌面放了一个「排队姬」快捷方式，指向这个 bat）。它比 `start.bat` 多三件事：

- 排队姬**已经在跑**（比如机器人已经把它拉起来了）：只打开浏览器控制面板，不会复开第二个实例；
- 端口被**别的程序**占用：明确告诉你是哪个端口被占、该改哪里，而不是丢一句绑定失败；
- **没装 PySide6**：自动以无窗口模式启动，并帮你打开浏览器控制面板（想开窗口就
  `pip install PySide6-Essentials`）。

命令行也可以直接用：

```powershell
python live_queue/start_standalone.py            # 等价于 start_standalone.bat
python live_queue/start_standalone.py --port 18080
```

启动后会打印：

```
排队姬已启动（live_queue 1.0.0）
  OBS 浏览器源 → http://127.0.0.1:17650/overlay
  控制面板     → http://127.0.0.1:17650/panel
  数据文件     → .../live_queue/data/queues.json
  写操作仅限本机（未配置 server.token）
```

同时会弹出**排队姬窗口**（`window.enabled=false` 时不会）。

### 3. 接到直播画面上（二选一）

| 方式 | 怎么加 | 特点 |
| --- | --- | --- |
| **窗口采集**（推荐给直播姬/OBS） | 添加「窗口采集 / Window Capture」源，选标题为 **排队姬** 的窗口 | 不透明深色面板，窗口上直接有叫号按钮 |
| **浏览器源** | 添加「浏览器源 / Browser Source」，URL 填 `http://127.0.0.1:17650/overlay` | 背景透明，更适合叠在画面角落；纯展示 |

### 4. 直接用

- 窗口上：点「叫号 ▶」叫下一位、「暂停」停止收人、「清空」清场；
- 或者浏览器打开 `http://127.0.0.1:17650/panel` 用控制面板；
- 或者让机器人接管（见下）。

---

## 常用启动方式

```powershell
python live_queue/app.py                     # 窗口 + HTTP 服务（默认）
python live_queue/app.py --ui off            # 只要服务（headless，适合服务器/远程）
python live_queue/app.py --ui on             # 必须有窗口，没有 PySide6 就直接报错退出
python live_queue/app.py --port 18080        # 换端口
python live_queue/app.py --host 0.0.0.0      # 允许局域网访问（记得配 token！）
python live_queue/app.py --token 一个足够长的随机串   # 写操作要求密钥
python live_queue/app.py --data D:\queue.json        # 换数据文件位置
python live_queue/app.py --selftest          # 只检查配置与数据，不监听端口
python live_queue/start_standalone.py        # 独立启动器：已在运行就只打开控制面板
```

> **单实例保护**：用文件锁实现（默认在系统临时目录，按「端口 + 挂载」区分）。已经有一个
> 连同一个 bot 的窗口在跑时，再启动会立刻退出，不会出现两个窗口。同一台机器跑两个 bot
> 时两个窗口互不影响。
>
> 这里刻意**没有**用「占一个本地端口」那套常见做法：Windows 上会有端口被系统保留的情况
> （返回 `WinError 10013`，明明没人监听也绑不上），文件锁没有这个问题，而且进程无论怎么
> 退出（包括被强杀）操作系统都会自动释放锁，不会留下僵尸锁。

---

## 配置

首次启动会生成 `live_queue/config.json`（就放在应用目录下，方便整包拷走）。手改坏了也
不会起不来：类型对不上、字段缺失、多写了不认识的键，都会退回默认值。

```jsonc
{
  "version": 1,
  "server": {
    "host": "127.0.0.1",
    "port": 17650,
    "token": "",              // 留空 = 只允许本机写；跨机器控制时两边都配同一个
    "flush_interval": 5.0,    // 脏数据落盘间隔（秒）
    "purge_interval": 30.0    // 排队超时检查间隔（秒）
  },
  "queue": {
    "default_queue": "默认",   // 控制台「手动加人」留空时进哪个队（观众排队必须写明队名）
    "max_queues": 8,
    "capacity": 0,            // 每队默认人数上限，0 = 不限
    "duplicate_policy": "reject",   // reject / move_to_end / allow
    "auto_create_queue": false,     // 是否允许排一个还不存在的队；默认关，不会自己建队
    "require_explicit_queue": true, // 观众排队必须写明一个已存在的队名（不回落默认队）
    "entry_timeout": 0.0,           // 排队超时自动出队秒数，0 = 永不超时
    "aliases": ["排位赛=排位", "上分=排位", "娱乐局=娱乐"]
  },
  "window": {
    "enabled": true,
    "title": "排队姬",         // OBS 窗口列表里就按这个名字找
    "width": 460,
    "height": 640,
    "always_on_top": true,
    "show_buttons": true,     // false = 纯展示（观众看不到操作入口）
    "opacity": 1.0
  },
  "overlay": {
    "title": "排队中",
    "show_title": false,
    "queues": [],             // 只显示这些队列；留空显示全部
    "max_rows": 8,            // 每队最多显示几个人
    "poll_interval_ms": 1000,
    "show_current": true,
    "show_wait_time": true,
    "accent_color": "#6fd3ff",
    "text_color": "#f2f6ff",
    "panel_bg": "rgba(14, 16, 26, 0.72)",
    "panel_border": "rgba(120, 200, 255, 0.35)",
    "current_bg": "rgba(111, 211, 255, 0.16)",
    "title_size": 22,
    "name_size": 20
  }
}
```

> `queue` 这一节是**队列语义的唯一来源**。接机器人时不要在机器人那边再配一遍——两份配置
> 必然漂移。机器人只需要知道「排队姬在哪、密钥是什么」。

---

## HTTP 接口

根地址 `http://{host}:{port}`。

### 页面

| 路径 | 用途 |
| --- | --- |
| `/` `/overlay` | OBS 浏览器源用的透明叠加页 |
| `/panel` | 主播控制面板 |

### 只读（不鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{ok, app, version, revision, uptime, queue_count, total_waiting}` |
| GET | `/api/state` | 队列快照；支持 `?rev=`（增量，未变化时只回 `changed:false`）、`?queue=` 过滤、`?limit=` 截断 |
| GET | `/api/config` | 叠加页外观配置 |
| GET | `/api/queues` | 队列名列表 |
| POST | `/api/whoami` | `{can_write, token_required}`，用来判断手里的密钥能不能写 |

### 写操作（需要权限）

配了 `server.token` 时必须带请求头 `X-API-Key`（或 `?token=`）；没配则只允许本机来源。

| 路径 | 请求体 | 说明 |
| --- | --- | --- |
| `/api/join` | `{user_id, nickname, platform, queue, queue_explicit, extra, guild, event_id}` | 入队 |
| `/api/leave` | `{user_id, queue, queue_explicit, event_id}` | 退队（`queue` 留空 = 退出全部） |
| `/api/add` | `{queue, nickname}` | 手动加人（服务端生成占位 ID） |
| `/api/call_next` | `{queue}` | 叫号 |
| `/api/finish` | `{queue}` | 结束当前 |
| `/api/requeue` | `{queue}` | 当前放回队首 |
| `/api/clear` | `{queue}` | 清空队列 |
| `/api/pause` | `{queue, paused}` | 暂停 / 恢复 |
| `/api/capacity` | `{queue, capacity}` | 改人数上限（0 = 不限） |
| `/api/create` | `{queue, display_name, capacity}` | 建队 |
| `/api/delete` | `{queue}` | 删队 |
| `/api/reset` | `{delete}` | 重置全部队列（`delete=true` 连队列一起删） |
| `/api/remove` | `{queue, user_id}` | 移出某人 |
| `/api/move_front` | `{queue, user_id}` | 插到队首 |
| `/api/flush` | `{}` | 立刻落盘 |

所有写接口都返回**同一个形状**：`{ok, status, message, ...}`。`message` 是给人和给日志看的
中文提示，`status` 是机器可读的状态码（`joined` / `already_in` / `queue_paused` /
`queue_required` / `queue_not_found` / `app_unavailable` …）。

### 幂等去重（event_id）

`join` / `leave` 可以带一个 `event_id`（B 站弹幕就是 `msg_id`）：

- 同一个 `event_id` 在 `queue.event_dedupe_seconds`（默认 600 秒）内**只处理一次**，第二次返回
  `status="duplicate_event"` 且不改动任何队列；
- 这是为了「bot 的 `bilibili_live_adapter` 和独立弹幕桥 `bilibili_bridge.py` 同时在线」这种情况：
  B 站开放平台本身是**单会话**（同一房间第二个 `/v2/app/start` 会报 7002「房间重复游戏」），
  所以同一时刻通常只有一方能收到弹幕；万一切换窗口里两边都拿到同一条，这一层去重保证
  **不会重复排队**；
- 第二道保险是身份：两边都用 `open_id` 作为 `user_id`，配合 `duplicate_policy="reject"`，
  同一个人在同一队列里永远只占一个位置。

### 队名解析规则

**排队必须写明一个已经存在的队名**：排队姬不会自己建队（新队只能在控制面板或排队姬窗口里手动新建）。
`queue_explicit` 表示调用方是否明确写了队名：

| 请求 | 结果 |
| --- | --- |
| `queue="排位"`（队已存在） | 正常入队（`queue_explicit` 真假都认，所以「排队排位」也能用） |
| `queue="排位赛"`（命中别名） | 映射到「排位」后入队 |
| `queue=""` | `queue_required`：请写明要排哪个队 |
| `queue="新队", queue_explicit=true` | `queue_not_found`：没有这个队，**不会自动新建** |
| `queue="一下", queue_explicit=false` | 队不存在 → `queue_required`（「排队一下」不该被当成队名） |
| 控制台 `/api/add` 留空队名 | 进 `default_queue`（该队必须已存在；只影响主播手动加人） |

```bash
# 例子：把观众加进「排位」队
curl -X POST http://127.0.0.1:17650/api/join \
  -H "Content-Type: application/json" \
  -d '{"user_id":"open-1","nickname":"阿狸","queue":"排位","queue_explicit":true}'
```

---

## 与机器人（mofox）配合

机器人侧的插件是 `plugins/bilibili_queue`，它**不再自己存队列**，只做「弹幕 → 这个接口」
的翻译。两边要对上两件事：

1. **地址**：插件的 `[app] host/port` 与这里的 `server.host/port` 一致（默认都是
   `127.0.0.1:17650`）；
2. **密钥**：如果这里配了 `server.token`，插件的 `[app] token` 要填同一个值。
   插件启动时会主动探测写权限，密钥不对会**在日志里直接告诉你怎么改**。

插件默认会在发现排队姬没在跑时**自动把它拉起来**（`[app] auto_start`），所以通常你只要
启动 bot 就行。反过来说：**关掉排队姬 = 弹幕排队失效**（窗口关闭时会确认一次）。

---

## 不跑机器人：独立弹幕桥

不想常驻整个 MoFox 时，用「排队姬 + 弹幕桥」就够了：

```powershell
python live_queue/bilibili_bridge.py --selftest   # 不连网，先看配置与关键词解析
python live_queue/bilibili_bridge.py              # 正式跑（读 live_queue/bridge_config.json）
```

也可以直接双击 `live_queue/start_bridge.bat`。凭证写在 `live_queue/bridge_config.json`
（首次运行生成模板；本仓库这份已经从 bot 的 `bilibili_live_adapter` 抄好了同一套凭证）。

| 桥能做 | 桥不做（平台 / 职责限制） |
| --- | --- |
| 观众弹幕「排队 王棋」「退队」→ 入队 / 退队 | **回弹幕**：B 站开放平台禁止第三方 bot 发弹幕 |
| 「排队情况」查询 → 写日志（结果看叠加页 / 控制面板） | 房管命令（下一个 / 清空 / 暂停）：留给 bot 与排队姬窗口 |

### 和 bot 同时开着会不会重复排队？

不会，三层保证：

1. **平台单会话锁**：B 站开放平台同一房间同时只允许一个长连，第二个 `start_app` 会拿到
   7002「房间重复游戏」。桥遇到 7002 会**退避待命**并在日志里写明原因，不和 bot 抢会话；
   想用桥接就先停掉 bot 的 `bilibili_live_adapter`；
2. **应用侧幂等**：桥与 bot 每次都带 `event_id`（平台 `msg_id`），排队姬在
   `queue.event_dedupe_seconds`（默认 600 秒）内只处理一次，重复的那次返回 `duplicate_event`；
3. **身份统一**：两边都用 B 站 `open_id` 当 `user_id`，配合 `duplicate_policy="reject"`，
   同一个人在同一队列里永远只占一个位置。

桥退出（Ctrl+C）时会调 `/v2/app/end` 释放会话；下次启动也会先清理上次遗留的 `game_id`
（记在 `live_queue/data/bridge_session.json`），避免撞 7002 的长锁。

---

## 放在服务器上跑（headless）

没有屏幕的机器上可以只跑服务，用浏览器源叠加页 + 控制面板操作：

```powershell
python live_queue/app.py --ui off --host 0.0.0.0 --token 你的长随机串
```

此时 OBS / 浏览器用 `http://<服务器IP>:17650/overlay`。**开放到局域网时一定要配
`--token`**，否则同网段任何人都能清空你的队列。

---

## 数据与备份

- 队列状态：`live_queue/data/queues.json`（原子写入：先写临时文件再替换，写一半断电也不会
  留下半截 JSON）；
- 内存即时生效，落盘默认有 5 秒防抖，退出前会强制写一次；
- 写盘失败只记日志并保持「脏」标记，下一轮重试，不会打断直播中的排队操作；
- 想清空历史：删掉那个 JSON 文件即可。

---

## 排查

| 现象 | 原因与处理 |
| --- | --- |
| 启动报「端口已被占用」 | 已经有一个 live_queue 在跑（或别的程序占了这个端口）。换端口，或先关掉它 |
| 启动报「端口被系统保留」 | Windows 的 Hyper-V/WSL 会保留一些端口区间，这种端口即使没人监听也绑不上。用 `netsh int ipv4 show excludedportrange protocol=tcp` 看，然后换端口 |
| 没弹窗口 | 没装 PySide6。`pip install PySide6-Essentials`，或者用 `--ui off` 只跑服务 |
| 窗口显示「未连接」 | 这只发生在窗口连的是 bot（旧架构）；现在窗口和队列在同一进程里，正常不会出现。若出现，看控制台报错 |
| OBS 窗口采集是黑的 | 别用「游戏采集」；窗口采集模式选「Windows 10 (1903 and up)」或「自动」。窗口本身不透明，不该全黑 |
| 局域网连不上 | 启动时加了 `--host 0.0.0.0` 吗？防火墙放行了吗？配 token 了吗？ |
| 叠加页显示旧数据 | 浏览器源缓存：URL 后面加时间戳，或勾选「关闭时刷新浏览器」 |

---

## 设计说明

- **单一数据源**：队列状态只在这个进程里（内存）和这个 JSON 里，机器人不存副本，
  所以不存在「两边不一致」；
- **线程模型**：窗口在 Qt 主线程，HTTP 服务用 `ThreadingHTTPServer`（每请求一线程），
  两者都经由 `QueueHub` 访问队列，锁集中在那一个类里；
- **状态推送到窗口**：窗口不轮询自己的 HTTP，而是由 `QueueHub` 的变更回调发 Qt 信号
  （跨线程队列投递），只留 1 秒定时器刷新「已等待多久」这类会自己变化的内容；
- **零第三方依赖**（除可选的 PySide6）：HTTP 用标准库 `http.server`，打包和部署都简单。
