# 更新记录（CHANGELOG）

版本号 = git tag。每个 tag 都指向一个能直接跑起来的源码状态（`py dsh_lanes.py --help` / `py dsh_lanes_gui.py`）。
exe 不进仓库（`.gitignore` 排除了 `dist/`），要分享请把 `dist\*-onedir.zip` 挂到 Release 附件。

---

## v0.3.0 — 2026-09-30

### 新增：一条 lane 内部支持多个 profile（多环境）

dsh 本来就支持 profile（`dsh --profile <名>`，profile 目录在 `$DSH_HOME/profiles/<名>/`），
但 v0.2 的启动器完全没有用到——一条 lane 只会启动默认的 `web`。这一版把 profile 提升为
启动器的核心概念：

- **卡片上直接列出 lane 的全部 profiles**（磁盘真值 ∪ lanes.json 登记），每个环境一行，
  独立的打开 / 停止 / 日志 / 移除；同一 lane 的多个 profile 可以**同时**运行（端口不同）。
  列表可收起 / 展开（有 profile 运行时自动展开，全部停止自动收起，手动切换会被记住），
  展开/收起动画逐帧渲染（每帧强制立即重排版，不攒帧、不闪屏；帧数可在
  `dsh_lanes_gui.py` 的 `_ANIM_FRAMES` 调）。
- **「＋ 添加 Profile」**两种方式：干净环境（只有官方 web 组合，目录形状镜像 dsh-app-boot
  的 `initProfile`），或 `--from <现有>` **整目录复制**一个现有 profile（含已装插件；
  junction 按副本路径重建，pnpm 账本里指回源 profile 的路径就地改写）。
- `profile-rm` 移除非运行中的 profile；默认 profile（web）不可删。
- 会话 / 凭据 / 设置是 HOME 级共享的，插件组合与补丁层按 profile 独立——
  **一个版本号的 dsh 就能开出多环境，不必为每个环境复制一整套 dsh**。

### 新增：端口实时控制，不再固定

- 以前 lane 的端口写死在 lanes.json；现在**每次启动现场挑** `port_range` 里第一个
  既没登记也没人监听的端口（实时读 TCP 表）。
- 想要指定端口：GUI 的 profile 行端口框现场填一个，或 CLI `--port N`。
  **偏好的端口被占用时自动改口**并写明原因，不再直接失败——只有"接管"这一种情况例外
  （见下）。
- 接管（adopt）来的 lane 保留一个端口偏好（默认 3080）：打开时若那个端口上正跑着一个
  DSH 实例、且没有任何别的运行态认领它，仍然**接管**而不是绕开；接管判定现在会先核对
  "这个端口是不是兄弟 profile 或别的 lane 的实例"，避免认错。

### 取消：主要版本自动继承 API key → 改为按条目复制

v0.2 的「primary / sync-key / 新建 lane 自动复制主要版本的 key」整组移除。替代品是
**按提供商条目复制**，方向反过来了：不再"自动把某个版本的 key 塞给新环境"，而是
"哪个环境要用哪几条，由用户勾选决定"：

- `keys <lane>`：列出这个 HOME 凭据里的全部条目（`.credentials.yaml` 的 refs 段）；
- `copy-key <源> <目标> [--only 条目,条目]`：只合并勾选的条目，其它内容一字不动
  （原文件先备份成 `.credentials.yaml.bak`）；
- GUI 每张卡片新增「凭据…」：选源 lane → 勾选提供商条目 → 选目标 lane → 复制。
- 兼容：旧 lanes.json 里的 `primary` 字段被忽略；旧 lane 数据里 lane 级的 `port`
  自动迁移成 web profile 的端口偏好。

### 新增：磁盘上没登记的 lane 自动认领

lanes.json 只记"登记表"，而创建 / 复制出来的东西都在磁盘上。登记表丢过（换目录跑、
exe 与源码各存一份配置、手删过）之后，磁盘上的 homes / versions / clones 就成了"孤儿"——
界面上看不见，也没法从这里打开。现在启动器会**自动把它们认回来**：

- **副本对**：`clones\<名>` 安装树 + `homes\<名>` HOME 成对出现（clone 的固定布局）→ 直接认；
- **日志配对**：`homes\<名>` 未登记 → 从启动器自己的启动日志头（`===== … $ node …bin.js
  web --port N`）里找回它用过的安装树和端口 → 认回来，并把端口记成 web profile 的偏好
  （手动启动的实例也能被自愈逻辑按端口认回）；
- **孤儿安装树**（只有 `versions\<版本>`、配不到 HOME）→ 不自动认，列在
  「本机各套安装」里等手动认领（「加入 lane 列表」会建议用根目录下的新 HOME）。

认领只补登记，不动任何目录；名字撞了以登记表为准。GUI 在启动和手动刷新（F5）时各扫一次，
CLI 每条命令开始前扫一次。

### 修复：补丁层把端口"钉死"导致 --port 失效

实测（global 及其副本）：web profile 的用户层里有一段工具写入的 `webserver` 覆盖行，
`config.port: 3080` 是**字面量**——用户层在组合顺序里压过 bundle 层的
`port: !!js ctx.webStartup.port ?? 3080`，于是命令行 `--port` 被静默忽略：端口不受控，
两条 lane 还会抢同一个端口。现在：

- `verify` 会体检这一项并给出文件与行内容；
- 启动器 open 时：钉住的端口**空闲就顺着用**（兼容写这块配置的工具，比如 remote-web-ui）；
  被占用、或你指定了别的端口时，自动写一个 `--patch` 覆盖层（组合顺序里排在两个用户层之后）
  让本次启动的端口真正生效，并在日志里写明覆盖了什么、为什么；
- 启动完成后运行态按**实际监听端口**记录（从抓到的 URL 校正），停止 / 认回不会找错。

### 顺带

- 运行态 / 日志按 profile 拆分：`run\<lane>__<profile>.json`、`logs\<lane>__<profile>.log`
  （旧的 `<lane>.json` / `<lane>.log` 仍按 web 读取）。
- `stop` 支持 `lane/profile`；`stop lane` 停这条 lane 的全部 profile。
- `open` / `list` / `logs` / `verify` / `plugins` / `market` 支持 profile 维度
  （`open lane/profile`、`verify --profile` 等）。
- `create` / `clone` 不再接收 `--port`（端口本来就是启动时的事）。

---

## v0.2.0 — 2026-09-28

### 新增：新建 / 升级可以自己选下载源（官方源 / 镜像源）

**为什么做这个**：官方当天发新版时，npmmirror 常常只同步了一半。实测 `@deepseek-ai/dsh@0.2.0-rc.1`
镜像已经有了，可它的 **12 个** `@deepseek-ai/dsh-*` 子包还没到（把 dsh 本体 + 两层依赖共 259 个包
逐个核对了一遍）。而旧版是"查版本走官方、`npm install` 走你 `.npmrc` 里的镜像"，两边不一致，
于是 npm 在第一个缺的子包上报 `ETARGET`（`No matching version found for
@deepseek-ai/dsh-cordis-client-runner@0.2.0-rc.1`）直接装不上。

现在的规矩：

1. **查版本用哪个源，装就用哪个源**（每次都显式给 npm `--registry`）；
2. 装的时候那个源要是还没同步全，**自动换另一个候选源重试一次**，并把"换了源"打进日志和界面。
   换源是安全的：这类失败发生在 npm 写盘之前（日志停在 `reify:loadTrees`），安装树不会被动过。

- **CLI**：`versions` / `create` / `upgrade` 新增 `--registry auto|official|mirror|URL`；
  `doctor` 会打印当前生效的源（含"装不上会退到哪个源"）。
- **GUI**：「新建版本」「升级版本」对话框里新增「下载源」三选一（自动 / 官方源 / 镜像源）＋
  「记住这个选择」勾选框（写进 `lanes.json`）；切换源会立刻重查一次版本列表；
  确认框会写明"从哪个源下载"；标题下面那一行也标明版本数字来自哪个源。
- **配置**：`lanes.json` 的 `registry` 语义扩展成"**首选源**"（查询与下载都用它），
  留空或 `auto` = 先官方、不行再镜像；填一个 http 地址（内网源）则不自动换源。

### 打包：改成 `--onedir`（`--onefile` 在本机起不来）

`--onefile` 每次启动都要解包到 `%TEMP%`，这一步在开发这台机器上被拦
（`Could not create temporary directory!` / `Failed to extract VCRUNTIME140.dll`）；
`--windowed` 的 GUI 版只弹一个标题为 "Error" 的对话框，双击看起来就是"什么都没发生"。
`--onedir` 不需要解包，CLI 与 GUI 都正常。**分享时请压缩整个文件夹**，不要只发 `.exe`。

`build_exe.bat` 也加固了：打包前先检查有没有正在运行的 exe（它锁着 `_internal\*.dll`，会让打包中途失败）。

### 修复：运行态写不进去不再把窗口搞成"关不掉的半成品"

- 症状：升级/刷新时弹 `PermissionError: ... run\<lane>.json`，PyInstaller 错误框 + Tk 窗口既不能用
  也关不掉，强行关掉后再启动又报一次。
- 根因：GUI 每轮刷新都会写"运行态"（自愈逻辑：端口上有实例就记下来），那条链路上有会抛异常的
  `write_text`，异常没人接。
- 修法：`write_run` 绝不抛异常（`OSError` → 只警告一次 + 返回 `False`）、`clear_run` 同样放宽；
  启动时检查 lane 根目录可写并说明后果；首次刷新包 `try/except`（失败也把窗口开出来）；
  `main()` 构造失败时弹一次框 + 关窗口 + 打 traceback，**绝不留半成品窗口**；
  CLI 双击（无参数）打印帮助后等一下回车，不再"一闪而过"。

### 其他

- GUI 标题行新增：npm 上各 dist-tag 的版本 + 「官方代码库 ↗」链接。
- 修掉一个隐藏问题：后台线程里读 Tk 变量（Tk 变量只能在主线程读）。

---

## v0.1.0 — 2026-09-27

首个版本。每套 DSH 独立安装树 + 独立 `DSH_HOME` + 独立端口，互不影响：

`create` / `open` / `list` / `instances` / `adopt` / `remove` / `primary` / `sync-key` / `delete` /
`clone`（整套复制，用来试新版/插件冲突）/ `verify`（核对隔离，`--fix` 就地修）/ `upgrade`
（核对 + 启动冒烟，起不来自动退回）/ `plugins`（只读清单）/ `market-*` / `chat-*` / `stop` / `logs` /
`doctor` / `versions`，以及同功能的图形界面（卡片式、5 秒刷新、日志面板、双击卡片即打开）。
