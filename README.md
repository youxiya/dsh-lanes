# DSH 多版本启动器（dsh-lanes）

在 Windows 上**同时装、同时跑多套 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）**：
每套一个独立的安装目录、一个独立的 `DSH_HOME`；同一条 lane 内部还可以开**多个 profile**（多套插件组合＝多环境），
端口**不固定**，每次启动自动避开被占用的。

它要回答的问题很具体：**「先在一个旁边的版本上把那套东西试一遍，再决定要不要动我日常用的那一套。」**

- 纯 Python 标准库 + Tkinter，**不依赖任何第三方包**，不需要 `npx` / `pnpm`
- **它不是 dsh 插件**：不注入 dsh 进程、不改 dsh 自己的源代码，只在外面读文件、探端口、起子进程

> 平台：目前只在 **Windows** 上实测过（用到 `GetExtendedTcpTable`、目录 junction、`os.startfile`）。
> 开发与实测环境：Windows + Python 3.13。

---

## 1. 它解决什么

| 能力 | 命令 | 说明 |
| --- | --- | --- |
| 查有哪些版本 | `versions` | 直接问 npm registry 要 dist-tag 与全部版本（纯 HTTP，不装包） |
| **创建**指定版本 | `create <lane> <版本\|tag>` | 用 `npm install --prefix` 装进独立目录，**不覆盖你正在用的 dsh** |
| **打开**指定版本 | `open <lane[\|版本]>[/profile]` | 用它自己的 `DSH_HOME` 和端口起一个 profile，并打开带 token 的正确地址 |
| **多环境（profiles）** | `profiles` / `profile-add` / `profile-rm` | 一条 lane（同版本、同 HOME）里开出多个环境：各自一套插件组合，**不必复制多套 dsh** |
| **读 / 搬插件** | `plugins` / `plugin-transfer` | 看某个环境装了哪些插件（名称、实际版本、行 id、开关状态），并把它**搬到别的 profile / 别的版本**（重装同版本，或离线直拷）；界面上的「插件库」 |
| 看 / 停 / 排障 | `list` `stop` `logs` `doctor` | 运行状态、优雅停止、日志尾部、环境自检 |
| **看见"别的"实例** | `instances` | 列出**不是本启动器启动的** DSH 实例 + 本机所有安装树（npm 全局 / npx 缓存 / versions / clones 里没登记的） |
| **找回丢登记的 lane** | （自动） | 登记表丢过 / 换目录跑过，磁盘上成对出现的 homes+clones、以及日志里有线索的 homes 会自动重新登记；孤儿的安装树列在「本机各套安装」等手动认领 |
| **接管在用的那套** | `adopt <lane>` | 把本机已有的安装注册成 lane，沿用它的 `DSH_HOME`；HOME 里现成的 profiles 会一并显示 |
| **凭据（看 / 改 / 增删）** | `keys` / `set-key` / `rm-key` / `copy-key` | 列出每个 HOME 凭据里的**条目**，带上 dsh 里显示的**服务商名字**；点了才取明文值，可改 / 删 / 新增；复制可以整份搬或只挑几条 |
| **自定义服务商（写入）** | `provider-list` / `provider-add` | 直接在启动器里加 / 改 dsh「设置 → 模型」里的自定义服务商（写 profile 补丁层的 `llm-pi-ai`，顺手把 key 存进凭据）|
| **复制一整套** | `clone <源> <新>` | 安装树 + `DSH_HOME`（会话、设置、凭据、已装插件）整套复制成副本；`--profiles` 可以只带其中几个环境 |
| **升到指定版本** | `upgrade <lane> <版本>` | 就地升级 + 核对 + 启动冒烟，起不来自动退回；`--rollback` 回上一版 |
| 核对隔离 | `verify <lane>` | 逐条比对链接、版本、pnpm 账本、本地依赖，并体检 profile 补丁层 |
| 备份对话 | `chat-backup` / `chat-restore` | 会话整棵备份（逐文件指纹）+ 恢复（先校验快照、再另存现状） |
| **配置备份 / 迁移** | `cfg-backup` / `cfg-restore` | 轻量 zip：插件清单（不含本体）+ 会话 + 设置 + 本地插件；新建 lane 导入后**自动联网装回插件**；可查看/增删包内插件 |
| 删除一套 | `delete <lane>` | 登记 + 运行态 + 安装树 + `DSH_HOME`；手打名称二次确认 |

`lane` ＝ 一条「版本通道」的名字，例如 `main` / `next` / `last`。
lane 内部的多环境靠 **profile**（见第 4 节）。

> v0.3 起移除了「主要版本（primary）」：不再有"新建 lane 自动继承它的 API key"，
> 取而代之的是 `keys` / `copy-key`（和界面上的「凭据…」）——**哪些提供商条目复制到哪个
> 环境，由你说了算**。

---

## 2. 环境要求

1. **Windows**（见上文）。
2. **Python 3.10+**，并确保 `py` 或 `python` 在 PATH 里（窗口版用 Tkinter，官方安装包自带）。
   开发与实测环境是 **Python 3.13**；更早的版本没实测过，代码里没有第三方依赖。
3. **Node.js + npm**：dsh 本体是 npm 包，`create` / `upgrade` 会调用它来安装。启动器自己不打包 node。

什么都不用 `pip install`。

---

## 3. 快速开始

**图形界面（推荐）**

1. 双击 `start_gui.bat`（开的是**新版 Web 界面**：本机 HTTP 服务 + Edge 无边框窗口）。
2. 点右上角「**＋ 新建版本**」：选一个版本（对话框会列出 npm 上现成的版本与 dist-tag），起个名字（就是 lane 名）。
3. 卡片上是这条 lane 的 **profiles 列表**：每个环境一行，端口框**留空＝启动时自动挑空闲端口**，
   也可以现场填一个（被占用会自动改口）。点「打开」就起一个带 token 地址的 DSH web。
4. 「**＋ 添加 Profile…**」可以给同一个版本开新环境：干净环境，或整目录复制现有 profile（含已装插件）。
5. 「停止」「日志」「凭据…」「服务商…」「升级版本…」「复制…」「插件…」「插件市场…」「配置文件…」「备份对话…」「删除这套 DSH」都在卡片上，
   右上角还有「插件库」（全局入口）。**每一个都是先算清"会发生什么"摆给你看，确认了才动手。**

> 两套界面（新版 Web / `start_classic.bat` 的经典界面）功能已经对齐，读写同一份配置，随时可以换着用。

**命令行**

```bat
py dsh_lanes.py versions
py dsh_lanes.py create next 0.1.7-rc.2
py dsh_lanes.py open next                      :: 默认 profile（web），端口自动挑
py dsh_lanes.py open next/test --port 3082     :: 指定 profile 与端口
py dsh_lanes.py profile-add next clean --from web
py dsh_lanes.py list
py dsh_lanes.py stop next
```

`start.bat open next` 是同一个入口（双击也能用，会 pause 住输出）。
完整命令见第 6 节，或 `py dsh_lanes.py --help`。

**不想装 Python？用打好的 exe**（自己打见第 10 节）：

| 目录里的 exe | 用途 |
| --- | --- |
| `dsh-lanes-web\dsh-lanes-web.exe` | 双击即用 = `start_gui.bat`（新版 Web 界面） |
| `dsh-lanes-gui\dsh-lanes-gui.exe` | 经典 tkinter 界面 = `start_classic.bat` |
| `dsh-lanes\dsh-lanes.exe` | 命令行版，参数与 `py dsh_lanes.py` 完全一致（例如 `dsh-lanes.exe doctor`） |

界面上的快捷键与入口：`F5` 刷新、`Ctrl+L` 清空日志、双击卡片＝打开默认 profile；
右上角「插件库」看/搬插件（默认是"全局入口"，卡片上的「插件…」带上那条 lane 作来源）、
主题按钮切换深浅色。新界面还认 `?debug=1`（右下角显示实时帧率与掉帧数）。

exe **自带 Python 运行时**，目标机器不用装 Python；但**仍然需要 Node.js + npm**（dsh 本体是 npm 包）。
exe 没签名，首次运行 Windows SmartScreen 可能拦一下：点「更多信息 → 仍要运行」。
**注意 exe 要连它所在文件夹一起用**（旁边有 DLL 与 `_internal`），单独拷 exe 出去跑不起来。

---

## 4. 隔离是怎么做到的

三条互不干扰的边界，缺一条就会变成"升级测试是假的"：

| 边界 | 做法 |
| --- | --- |
| **安装树** | `<root>\versions\<版本>` 或 `<root>\clones\<lane>`，用 `npm install --prefix` 装进去；不碰全局 `node_modules` |
| **DSH_HOME** | `<root>\homes\<lane>`：会话、设置、凭据、已装插件各一套（接管来的 lane 例外，它沿用原有的 HOME） |
| **端口** | **不固定**：每次启动现场挑 `port_range`（默认 3080–3129）里第一个空闲的；也可以当场指定一个，被占用会自动改口 |

### lane 内部的多环境：profile

一条 lane 只有一个 HOME，但 HOME 里可以有**多个 profile**（`<HOME>\profiles\<名>\`）：

- 每个 profile 有自己的**插件组合**（bundles / node_modules / 补丁层）——这就是"多环境"：
  例如 `web` 是日常全插件的一份，`test` 是复制出来准备折腾的一份，互不干扰；
- 同一 HOME 里的**会话、凭据、设置是共享的**（DSH 自己的设计），所以不用为每个环境重新填 key；
- 启动命令就是 `dsh --profile <名> --port <N>`，同一个版本可以**同时**开多个 profile（端口不同）；
- `desktop` 这个名字被 DSH Desktop（Electron 应用）独占，启动器不会列出/启动它；
- `profile-add <lane> <名>` 默认建"干净环境"（官方 web 组合，镜像 dsh 自己的初始化）；
  加 `--from <现有>` 则整目录复制一个现有 profile（含已装插件），pnpm 账本里指回源
  profile 的路径会就地改写。

复制一套时还有三个必须做对的细节，代码里都处理了（都是实测踩出来的）：

- `profiles\node_modules` 下的链接目标全是**绝对路径**，照抄会让副本与原件共享宿主代码（升副本连带改原件且不报错）→ 复制后按副本路径逐条重建。
- pnpm 账本里的 `virtualStoreDir` 记的是**原件**路径 → 不改就装不了插件，真按记录值来还会往原件里写。
- 本地 `file:` 依赖被记成相对路径，而**深度变了** → 副本要找绝对路径，`resolution.directory` 那处也必须一起换。

复制时可以只挑几个环境（命令行 `--profiles`，界面上是勾选框）：**没勾的 profile 目录压根不进副本**，
所以副本里没有它们（体积也只算勾中的那部分）。想再要一个，在副本上点「＋ 添加 Profile」重新建一个就行。

---

## 5. 写文件的边界（诚实说明）

启动器不注入 dsh 进程，但**确实会写下面这些地方**，都是你点什么才写什么：

- 自己的 `lanes.json`（登记表、窗口几何）——**源码运行**时在脚本目录，
  **exe 运行**时在 exe 所在目录（便携，拷走就能用）；那个目录不可写时自动退到 `%APPDATA%\dsh-lanes`
- lane 的安装树（创建 / 升级 / 复制 / 删除）
- lane 的 `DSH_HOME`（复制的 API key、对话恢复、运行态文件 `<root>\run\<lane>__<profile>.json`、
  备份目录 `<root>\backups\`、`profile-add` 新建的 profile 目录）
- profile 的**补丁层**（`cordis.patch.yml`）：插件开关（写真实行 id）、自定义服务商
  （`llm-pi-ai`）、插件加装 / 卸载时的清单登记、「插件市场」单独开关（`market-off` / `market-on`
  写的是一个**带起止标记**的块，`market-on` 可整段撤销）——每次写前留 `.bak`，写后用
  **dsh 自己的解析器**校验一次，不过就自动回滚
- 凭据文件 `.credentials.yaml` 的 `refs` 段（改 / 加 / 删条目、从别的 lane 导入）；
  只动点名的那几行，同样先备份、写后校验、不过回滚

**它不做的事**：不碰 dsh 自己的源代码；不替你做"这次升级安不安全"的判断；
不写任何不在上面清单里的文件（界面上「打开配置文件」只是只读预览 + `explorer` 定位）。

> 顺带一个行为保证：**写运行态失败不会让界面崩**。`<root>\run\<lane>.json` 只是"记住上次是谁起的"，
> 写不进去（目录只读、盘符被限制等）时只在日志里警告一次，卡片改用现场探测显示状态——
> 这条是踩过坑之后专门加固的：以前它会以未捕获异常的形式打崩窗口（起不来、也关不掉）。

---

## 6. 命令速查

```
py dsh_lanes.py doctor                    环境自检
py dsh_lanes.py versions [--all] [--registry auto|official|mirror]
py dsh_lanes.py create <lane> <ver> [--force] [--registry auto|official|mirror]
py dsh_lanes.py open <lane|ver>[/<profile>] [--profile P] [--port N] [--cwd DIR]
                                          [--no-browser] [--detach] [--timeout N]
py dsh_lanes.py list                      按 lane × profile 列出运行状态
py dsh_lanes.py instances                 列出所有 DSH 实例（含不是本启动器启动的）
py dsh_lanes.py adopt <lane> [--install 路径] [--port N] [--home 目录]
py dsh_lanes.py remove <lane> [--force]   只注销登记，不动安装树与 HOME
py dsh_lanes.py profiles <lane>           列出这条 lane 的全部 profile（环境）
py dsh_lanes.py profile-add <lane> <名> [--from 现有profile]
                                          干净环境（默认）或整目录复制现有 profile
py dsh_lanes.py profile-rm <lane> <名>    移除一个 profile（web 不可删）
py dsh_lanes.py keys <lane>               列出凭据条目（带服务商显示名 / 谁在用它）
py dsh_lanes.py set-key <lane> <条目> [--value V|--from-env E]
                                           改 / 加一条凭据；缺 --value 就安全地从 stdin 读
py dsh_lanes.py rm-key <lane> <条目,条目>  删凭据条目
py dsh_lanes.py copy-key <源> <目标> [--only 条目,条目]
                                           按条目复制 API key；缺省全部
py dsh_lanes.py provider-list <lane> [--profile web]
                                           列这个 profile 里手写的自定义服务商
py dsh_lanes.py provider-add <lane> <route> --api openai-completions --base-url URL
                                    [--display 名字] [--key sk-...] [--model m1,m2]
                                    [--profile web] [--overwrite]
                                           写进补丁层的 llm-pi-ai，key 存进凭据
py dsh_lanes.py plugin-add <lane> <包名[@版本]> [--profile web]
py dsh_lanes.py plugin-rm  <lane> <包名> [--profile web] [--clean-rows]
                                           加装 / 卸载这个 profile 的插件（走 dsh 自己的命令）
py dsh_lanes.py delete <lane> [--stop] [--keep-home] [--keep-install] [--home-too] [--yes]
py dsh_lanes.py clone <源 lane> <新 lane> [--profiles web,test] [--port N]
                                          只带这几个 profile（缺省＝源 HOME 里全部）
py dsh_lanes.py upgrade <lane> <版本|tag> [--stop] [--dry-run] [--no-boot-check]
                                          [--registry auto|official|mirror]
py dsh_lanes.py upgrade <lane> --rollback
py dsh_lanes.py verify <lane> [--profile P] [--fix]
py dsh_lanes.py plugins <lane> [--profile P] [--dump]   插件清单（只读）
py dsh_lanes.py plugin-transfer <源[/profile]> <目标[/profile]> 插件名… [--mode install|copy] [--dry-run]
                                          把插件搬到别的 profile / 别的版本（--dry-run 只看计划）
py dsh_lanes.py market <lane> [--profile P] [--offline] 插件市场现状
py dsh_lanes.py market-off <lane> [--no-boot-check] / market-on <lane>
py dsh_lanes.py market-update <lane> [--version V] [--anyway] [--no-boot-check] / market-rollback <lane>
py dsh_lanes.py chat-backup <lane> [--name 后缀] / chat-list <lane>
py dsh_lanes.py chat-restore <lane> [名称] [--stop] [--yes] [--force] / chat-rm <lane> <名称> [--yes]
py dsh_lanes.py cfg-backup <lane> [--name 后缀] [--no-sessions|--no-attachments|--no-settings]
                                          [--credentials] [--data 目录,目录] [--profiles web,test]
py dsh_lanes.py cfg-list / cfg-show <备份名> / cfg-plugins <备份名> / cfg-rm <备份名>
py dsh_lanes.py cfg-plugin-add <备份名> <profile> pkg@spec[pkg@spec…] [--from-lane lane]
py dsh_lanes.py cfg-plugin-rm <备份名> 包名… [--profile P]
py dsh_lanes.py cfg-restore <备份名> [新lane] [--version V] | [--lane 已有lane]
                                          [--no-sessions|--no-settings|--no-credentials|--no-data] [--yes]
py dsh_lanes.py stop [lane|lane/profile ...]   缺省停全部；lane＝它的全部 profile
py dsh_lanes.py logs <lane[/profile]> [--lines N]
py selftest.py [lane ...]                 全链路复检：open → 认证握手 → 重复 open → stop
```

`--root <目录>` 可以临时覆盖 lane 根目录。

---

## 7. 配置：`lanes.json`

- 首次 `create`（或在窗口里新建版本）时**自动生成**，形状见 `lanes.example.json`。
- 它记的是**你这台机器**的路径与登记表，属于本机配置——**不要提交、不要分享**（`.gitignore` 里已经排除）。
- 关键字段：`root`（lane 根目录）、`port_range`、`default_cwd`（决定会话归属，DSH 按工作目录分项目）、
  `allow_scripts`（npm 11 起安装脚本要显式授权）、`registry`（下载源，见下）、`lanes`（登记表）。
- 端口**不写进配置**：每次启动现场挑空闲端口。两个例外都记成 web profile 的偏好：
  接管来的 lane（默认 3080，为了把"你正在用的那套"认回来）、自动认领的 lane（用启动日志里
  最后一次的端口）。偏好被占用时照样自动改口。
- lane 的 `profiles` 字段只是提示（登记时间、可选端口偏好）：**profile 的真值在磁盘上**
  （`<HOME>\profiles\` 下有哪些目录），手动建过的 profile 也会被列出、直接 open 就能用。

### 下载源：`registry`（新建 / 升级时可逐次选择）

`registry` 取值：留空或 `auto`（默认，先官方再镜像）、`official`（`registry.npmjs.org`）、
`mirror`（`registry.npmmirror.com`），也可以直接写一个 http 地址（内网源；这种情况下不会自动换源）。

两条规矩：

1. **查版本和下载一定用同一个源。** 以前查版本走官方、下载走 npm 默认源（你 `.npmrc` 里的镜像），
   两边不一致就出事：官方当天发的新版，镜像常常只同步了一半——`@deepseek-ai/dsh@0.2.0-rc.1`
   镜像已经有了，可它的 12 个 `@deepseek-ai/dsh-*` 子包还没到（当时逐个核对了 259 个包），
   于是 npm 在第一个缺的子包上报 `ETARGET` 装不上。现在用的是同一个源。
2. **缺东西会自动换另一个源重试一次**（命令行加 `--registry`，GUI 在「新建版本 / 升级版本」
   对话框里有「下载源」三选一，勾「记住」就写进 `lanes.json`）。换源是安全的：这类失败发生在
   npm 写盘之前（日志停在 `reify:loadTrees`，安装树没被动过），实测过。日志里会写明换了哪个源。

命令行临时改一次：`py dsh_lanes.py upgrade next 0.2.0-rc.1 --registry mirror`。

---

## 8. 已知限制 / 有意不做

- **只测过 Windows。**
- **不给"升级安不安全"的结论。** 工具只能按预设逻辑工作；组合树、插件 peer 闸门这些是宿主内部事实，
  宿主一变，工具输出的"安全/不安全"就是拿过期规则替你决定，比不给更危险。
  它只做确凿能验证的部分：装没装上、起没起来（HTTP 200 + cookie 握手）、文件指纹对不对、能不能退回。
- **接管来的外部安装（npm 全局那一份）不参与 `upgrade`**：那等于直接改你正在用的那一份。
- **不做通用插件开关**（理由见第 5 节）。
- **跨 lane 的对话记忆不互通**：会话文件按 lane（HOME）隔离；同一 lane 里的多个 profile **共享**
  会话（HOME 级设计）。跨版本要"记得"的东西应该放在记忆层（比如一个跨会话的记忆插件 / 记忆库），
  而不是会话文件层——会话格式在版本之间会变代际，强行共享迟早出问题。
- **用自动端口的 profile，运行态文件丢了就认不回来**：端口是现场挑的，重启后可能变；
  偏好端口（接管 lane 的 3080）才有"按端口重新接管"的自愈。
- **market-off / on / update 目前只作用于默认 profile（web）**；`plugins` / `market` / `verify`
  可以用 `--profile` 指定其它 profile 查看。

---

## 9. 排障

| 症状 | 先看 |
| --- | --- |
| 不知道环境缺什么 | `py dsh_lanes.py doctor` |
| 起了但浏览器打不开 | `py dsh_lanes.py logs <lane>`（找 `dsh web: <url>` 那行） |
| 端口被别的东西占了 | `py dsh_lanes.py instances` |
| 创建/复制过的 dsh 在窗口里看不见 | lanes.json 只记登记表。重新打开窗口（或 F5）会自动认领磁盘上成对的产物；孤儿的安装树在「本机各套安装」里点「加入 lane 列表」 |
| 指定了端口却没生效 | 补丁层里可能有工具写入的 `webserver` 端口字面量（压过 --port）：`verify <lane>` 会提示；启动器 open 时会按需自动覆盖 |
| 副本一装插件就崩 | `py dsh_lanes.py verify <lane>`；再 `verify <lane> --fix` |
| 怀疑 profile 补丁层坏了 | `verify` 会顺手体检：调宿主自带那份 js-yaml 真解析一遍，报错原话带 `行:列` |
| 用 exe 起不来又没报错 | `--windowed` 的 GUI exe 没有控制台：改用 `dsh-lanes.exe doctor`（同目录）看报错 |
| 新建 / 升级报 npm `ETARGET` / `notarget` | 那个源还没同步到这一版：加 `--registry official`（或 `mirror`）换一个源再试，或等几小时。工具默认就会自动换另一个源重试一次 |

---

## 10. 自己打包成 exe

```bat
py -m pip install pyinstaller      :: 只需一次
build_exe.bat                      :: 双击也行
```

产物（`dist\`，**是文件夹不是单个文件**）：

| 目录 | 打包参数 | 实测大小（zip 后） |
| --- | --- | --- |
| `dist\dsh-lanes\` | `--onedir --console` | 8.2 MB |
| `dist\dsh-lanes-gui\` | `--onedir --windowed` | 11.5 MB |
| `dist\dsh-lanes-web\` | `--onedir --windowed` + `--add-data "web;web"` | 约 12 MB |

**为什么要有命令行版**：`--windowed` 的窗口版 exe 没有控制台，早期崩溃是**静默**的。
出问题时 `dsh-lanes.exe doctor` 是唯一能看到报错的入口。

**Web 前端版的 exe**：界面文件（`web\` 下的 html/css/js）必须一起打进去，所以那条命令带
`--add-data "web;web"`——冻结后脚本从 `sys._MEIPASS` 下找这些文件。少了它会明确报
"找不到界面文件"，而不是给你一个白窗口。

**为什么默认 `--onedir` 而不是 `--onefile`**（实测踩过，不是偏好问题）：

`--onefile` 每次启动都要把自己解包到 `%TEMP%`。在开发这台机器上这一步会被拦掉：

```
[PYI-28584:ERROR] Could not create temporary directory!
（GUI 版没有控制台，只弹一个标题为 "Error" 的对话框 —— 双击看起来就是"什么都没发生/起不来"）
```

换成 `--onedir`（exe 旁边放 DLL 和 `_internal` 文件夹，**不需要解包**）后，CLI 与 GUI 都正常启动。
所以要分享的话：**把整个文件夹打包成 zip 发出去，别只拷 exe**（exe 离开那些文件跑不起来）。

**冻结后有两个行为会变**（源码里 `FROZEN` 分支，就这两处）：

- **配置位置**：`lanes.json` 跟着 **exe 所在目录**走。因为冻结后 `__file__` 指向 exe 自己的目录树，
  而 `--onefile` 那种临时解包目录一退出就没了。该目录不可写（例如塞进 `Program Files`）时退回 `%APPDATA%\dsh-lanes`。
- **默认工作区**：源码运行时取"脚本上两级目录"（也就是你放 dsh 项目的那个文件夹），
  冻结后那个位置没有意义，改用用户主目录。

**想要图标**：加 `--icon your.ico`（仓库里没带图标）。
**想试单文件**：把 `--onedir` 换回 `--onefile`，但先在你自己的机器上双击验证能不能起来。

仓库的 `.gitignore` 已经排除 `build/`、`dist/`、`*.spec`：**exe 不要提交进仓库**，
要分享就发到 GitHub 的 **Releases**（附件，把 zip 挂上去）。
打包好的两个 zip 就放在 `dist\`：`dsh-lanes-onedir.zip`（CLI）、`dsh-lanes-gui-onedir.zip`（GUI）。

---

## 11. 界面

两套界面共用同一份 `lanes.json`、同一批核心函数，可以同时开着互不打架
（状态以运行态文件与 TCP 表为准）。

### 11.1 默认：Web 前端（`dsh_lanes_web.py` + `web/`）

**为什么换**：上一版是 tkinter 自绘，动画每帧都要"改控件尺寸 + 强制重排版"
（展开动画的注释里写着单帧 ≈9ms），而且换主题＝**销毁重建全部控件**、刷新＝**删卡片重画**
—— 所以只有"闪"没有过渡。现在渲染交给浏览器，后端只做三件事：起一个只在 `127.0.0.1` 上的
小 HTTP 服务、把已核实过的核心函数接到 API 上、用 Edge/Chrome 的 `--app` 模式开一个无边框窗口指向它。

| 环节 | 做法 |
| --- | --- |
| 界面 | `web\index.html` + `style.css` + `app.js`（原生 JS，无构建步骤、无新依赖） |
| 实时 | SSE（`/api/events`）推日志 / 状态 / 任务 / npm 查询；日志攒 50ms **批量**发（npm 一秒上百行） |
| 状态 | ticker 每 2.5s 算一次快照，**签名变了才推**（和经典界面同一套判断） |
| 任务 | 后端起线程跑核心命令，stdout 重定向进日志（装 npm 时实时可见）；结束再推一条 `job_done`，开着的窗口靠它重读自己那份数据 |
| 窗口 | Edge `--app=` + 独立 `--user-data-dir`（`%LOCALAPPDATA%\dsh-lanes\webview-profile`，不动你日常那个浏览器配置） |
| 退出 | 关窗口 → SSE 断开 → 6s 后进程退出；**绝不顺手停掉正在跑的 DSH** |

**那些表单窗口**（新建版本 / 添加 Profile / 凭据 / 服务商 / 配置文件 / 插件库 / 插件市场 / 备份对话 / 复制 / 升级 / 删除）
是 `<dialog>` —— 浏览器自带 top layer，所以窗口里再弹一层确认框天然可堆叠，也不用自己写 z-index。
每个窗口的构成都一样：**只读接口先把"会发生什么"算出来 → 摆给你看 → 你确认了才起后端任务**。
底部按钮的可用性跟着表单实时重算（例子：删除那一步必须**手打 lane 名**才亮起「删除」；
它正在跑还得先勾「先停止它」）。动手之后进度去右边的日志面板看，和命令行是一条路。

「流畅 + 有过渡」是这么来的（正对着上一版那两个毛病）：

- **换主题**：改 `<html data-theme>`，颜色全走 CSS 变量 + `transition`；
  两层极光背景（浅 / 深）靠不透明度交叉淡入，而不是把控件全拆了重建。
- **列表刷新**：keyed diff 原地改节点（lane 按名字、profile 按名字），只增删真正变化的行 ——
  刷新时滚动位置和展开状态都不受影响（实测：正在输入的端口框不会被回写覆盖）。
- **展开 / 收起**：`grid-template-rows: 0fr ↔ 1fr`，浏览器自己过渡。
- **动画只碰 `transform` / `opacity`**（合成器线程）；日志那种大滚动区**不挂 `backdrop-filter`**
  （每帧重新模糊一整片文本太贵），改成接近不透明的底。玻璃感留给面板与卡片。
- `?debug=1` 在右下角显示实时帧率与掉帧数，可以直接看见帧数。

参数：`--classic` 转交经典界面、`--stay` 只起服务（用系统浏览器访问）、`--port N` 指定端口、
`--config PATH` 换一份配置（自动化测试用，**不碰你的 `lanes.json`**）、`--selfcheck` 后端自检、
`--dev` 开 DevTools 并把日志同时打到 stderr。

**安全**（本地服务该做的都做了）：只监听 `127.0.0.1`；启动生成一次性令牌，首屏 `?token=` 校验后
下发 `HttpOnly; SameSite=Strict` cookie；校验 `Host` 必须是本机地址（防 DNS rebinding）；
写操作要求 JSON 类型并校验来源；**不返回任何 CORS 头**；静态文件走白名单、不拼接用户输入做路径；
「凭据…」列表只送**条目名 + dsh 里的显示名**（谁在用它）到页面；**明文密钥只有你点「显示 / 编辑」
那一下才取**（走独立接口，一次一条，只在本机回环上），平时不进页面、不进日志。
同理，「服务商…」与「配置文件…」也是按需读、按需定位（`explorer /select,` 打开的那一下才碰文件系统）。

### 11.2 经典界面（`start_classic.bat` 或 `py dsh_lanes_web.py --classic`）

两套配色（**浅色 / 深色**），右上角一键切换，选择记在 `lanes.json` 的 `ui.theme`。

「磨砂玻璃」是怎么做出来的（tkinter 没有系统级亚克力/模糊）：

- **背景是一张极光渐变图**（几个柔和色斑叠在竖向渐变上），按窗口尺寸渲染。
  渲染在**后台线程**做（按半分辨率算完再 zoom(2)：相邻像素只差 1 级，看不出块），
  算完回主线程建图，所以拖动窗口时界面不卡。
- **每块面板/卡片都是"玻璃"**：画布底铺的是**主背景图里它所处的那一块**（把整张图摆到
  负偏移让画布自己裁），所以面板四个圆角外侧露出的是真实背景，和旁边无缝接上；
  圆角矩形再填一层"磨砂色"（主题底色混一点背景平均色）。卡片另加一层软阴影。
- 左栏（lane 列表）本身是**透明玻璃窗**：不铺磨砂层，直接露出背景，卡片浮在上面，
  卡片之间的缝里看到的就是那张彩色背景。滚动时背景像壁纸一样钉在窗口上。
- 圆角是 Canvas 上用**真圆弧**采样画的（`round_rect`）。注意别用 `smooth=True` 那种
  "四角各点两下"的写法：Tk 的平滑多边形是样条，曲线不穿过控制点，整块会被往里吸，
  直边中间凹进去一两个像素，底下那层背景就从缝里漏出来（深色主题下是个明显的黑块）。

两套主题的差别不只是反色：深色主题的卡片是 `#181e2b` 一类的深灰蓝，
背景光斑更饱和；浅色主题的卡片接近白但带一点背景色的倾向。

**为什么还留着它**：同一个壳的另一种画法 —— 万一浏览器起不来（或你更习惯 tkinter 的手感），
它是个现成的退路。两边的功能已经对齐，读写同一份 `lanes.json`，起停的也是同一批安装树与 HOME。

## 12. 插件：读取已装插件 · 跨 profile / 跨版本迁移

界面：「插件库」（右上角，全局入口）或卡片上的「插件…」（带上这条 lane 作来源）。
读的是**文件真值**（不跑 `--dump-config`），所以是即时的：包名、**实际装上的版本**、
行 id、开关状态、是否登记为组合包、是不是纯客户端插件。

搬运用两种方式，代价和可信度完全不同，界面上选：

| 方式 | 做什么 | 代价 |
| --- | --- | --- |
| **重新安装**（默认） | 读源里插件的**实际版本号**，在目标 profile 用 dsh 自己的 `plugin` 命令点名到那个版本装一遍 | 需要能连上源；跨版本依赖对不上时会**明确报错**（这是要的结果） |
| **离线直拷** | 把该插件在源里的**依赖闭包**整棵复制过去，链接按目标路径重建 | 不需要网络/node；但 pnpm 账本（`pnpm-lock.yaml`）不会同步，之后在目标里装别的东西时 pnpm 可能要重新解析一次 |

两种方式都会补三样东西，缺一样插件不会真的生效：

1. 目标 profile 的 `package.json` 里要有这个依赖；
2. `dsh.profile.bundles` 里要登记它（**只装不登记，宿主根本不会加载它**）；
3. 补丁层里针对这些插件**行 id** 的覆盖条目要搬过去（否则"源里关掉的插件"搬过去会变回开启）。

几个实测细节：

- **能点名到具体版本就一定点名**：`^1.2.0` 换个时间会解析成另一个版本，那就不叫迁移了。
- 但 `github:` / `git+https://` / `file:` 这类 spec **原样搬**：实测 `dsh-liquid-glass` 写的是
  `github:xingyingyuzhui/...`，它装的版本在 npm 上未必存在（这正是用 git 装的原因），
  改写成版本号会 404。`file:` 的相对路径会换成**指向源的绝对路径**（目标 profile 深度不一样）。
- 离线直拷**不覆盖目标里已有的东西**（覆盖 `.pnpm` 条目会连累别的包），已经存在的会列出来
  并核对版本，让你自己判断。
- 目标里没有的 profile 会自动按官方 web 组合建一个干净环境再搬。
- 改动前 `package.json` / `cordis.patch.yml` 都会留一份 `.bak`。
- **两边同版本的自动过滤**：搬一次既费时间，又可能把目标里已经调好的东西（软链、本地改动）
  盖掉，所以计划里直接标成「自动过滤」并说明理由；源里只有清单登记、其实没装上的也一样过滤。

左边每个插件旁边还有**就地管理**的按钮：**启用 / 禁用**（写它真实的行 id，写后宿主校验、
不过自动回滚）、**卸载**（`dsh plugin remove` → 摘依赖与 `dsh.profile.bundles` 登记 → 清补丁行）、
下面一条**加装**（填包名或 `@scope/name@版本`）。官方组合包、宿主基础设施、插件市场一律拒绝动。

命令行：

```bat
py dsh_lanes.py plugin-transfer global-copy/web global-copy/web-1 billion-context dsh-ops
py dsh_lanes.py plugin-transfer global-copy/web global-text/web dsh-ops --mode copy
py dsh_lanes.py plugin-transfer global-copy/web global-copy/web-1 all --dry-run
py dsh_lanes.py plugin-off global-copy/web dsh-ops      # 关一个（写真实行 id）
py dsh_lanes.py plugin-add global-copy/web @liustack/modsearch --profile web
py dsh_lanes.py plugin-rm  global-copy/web @liustack/modsearch --profile web --clean-rows
```

---

## 13. 凭据与自定义服务商

### 凭据（`.credentials.yaml` 的 `refs` 段）

一条 HOME 的凭据就是 `refs` 段里的一批「条目名 → 密钥」。界面上「凭据…」不再只是清单：

- 每条**带 dsh 里的显示名**（从 profile 补丁层反查 `displayName` / 插件名），
  一眼看出「这条是干嘛的」；
- 点「显示 / 编辑」才取**明文值**（独立接口、一次一条、只在本机回环上），改完保存；
- 「删除」单条；「新增 / 覆盖」一个条目；「从这条 lane 导入」把别的 lane 整份合并过来
  （**修坏掉的凭据文件就走这条**）；
- 一律先备份 `.credentials.yaml.bak`，写后用**宿主自己的解析器**校验，不过自动回滚；
  文件本来就已经坏的，直接拦住不写。

命令行：`keys <lane> [--show]`（显示名 / 明文）、`set-key <lane> <条目> --value sk-...`
（或 `--from-env MY_ENV`，别把密钥留在命令行历史里）、`rm-key <lane> <条目,条目>`。

> 为什么以前会把文件写坏：旧实现用**逐行正则**抓条目，多行单引号值（超长 JSON，如
> `CODEARTS_ACCESS_TOKEN`）只抓到首行，写回时引号没闭合，YAML 直接 `MISSING_CHAR`。
> 现在按 YAML 标量规则跨行取完整值，并且**只改 `refs` 段里点名的那几行**。

### 自定义服务商（写 dsh「设置 → 模型」的同一处）

dsh 的模型设置页允许手写服务商：route（小写短名）+ 协议 + `baseURL` + 模型清单 + 密钥引用。
它落在 profile 补丁层的 `llm-pi-ai` → `config.providers.<route>`，密钥本体进 `.credentials.yaml`
的 `<ROUTE>_API_KEY`。本工具的「服务商…」按**同一套契约**读写它（route 正则、keyRef 命名、
协议取值都抄 dsh），所以能和那个页面互认：

- 列出这个 profile 已有的手写服务商（显示名 / route / 协议 / baseURL / keyRef / 模型）；
- 新增或更新一条：默认**拒绝覆盖**同名 route（怕冲掉调好的地址与模型），勾了
  「允许覆盖已存在的同名 route」才替换；
- 只填显示名 / 地址不改 key 时，原来的 `apiKeyEnv` 会保留，不会被抹掉。

命令行：`provider-list <lane> [--profile P]`、
`provider-add <lane> <route> --api openai-completions --base-url URL [--display 名字] [--key sk-...] [--model m1,m2] [--overwrite]`。

---

## 14. 配置备份 / 迁移（轻量 zip）

`clone` 是把**安装树 + HOME 整棵复制**（一套 800MB～1GB，慢）；`chat-backup` 只备份会话。
配置备份是第三种方式：**只打包"装了什么、说了什么、怎么配置"，不打包插件本体**，
得到一个几十 MB 的 zip；恢复到一条**新建 lane** 时按清单逐个联网装回插件（版本精确到备份时的值）。

### 备份里有什么

单个 zip，存 `<根目录>\backups\config\dshcfg-<lane>-<时间戳>[-后缀].zip`，内部：

| 内容 | 打不打 | 说明 |
| --- | --- | --- |
| `manifest.json` | 必有 | 格式标识、来源 lane/dsh 版本、每个 profile 的插件清单（pkg/spec/version/bundles/官方否/行 id/关闭状态/本地依赖）、内容索引、每个文件的 sha256 |
| `profiles/<p>/package.json`、`cordis.patch.yml`、`pnpm-workspace.yaml`、`compatibility.json` | 必有 | 插件依赖、bundles 登记、开关补丁、构建许可 |
| `sessions/**` | 默认打 | 会话记录（`.zstd` 已压缩，zip 里用 STORED 不重复压）；`--no-sessions` 可关 |
| `attachments/**` | 默认打 | 会话附件；`--no-attachments` 可关 |
| `home/` 顶层设置文件 | 默认打 | `settings.yaml.imported`、`dsh-service-config.json`、`pet.json`、`studymate-config.yaml` 等小文件（单个 ≤2MB） |
| `creds/.credentials.yaml` | **默认不打** | 含 API key，敏感；要打显式加 `--credentials` |
| `local/<key>/**` | 自动 | 只收集清单里**真实引用到**、且磁盘上存在的本地插件（`file:` / `link:`），不引用的整目录不打 |
| `data/<数据目录>/**` | 手动 | 体积大的插件数据目录（skills、whale-roles 等）默认不打，用 `--data 名,名` 挑 |

对比：源 lane HOME 1.2GB（其中 `profiles/*/node_modules` 占 837MB 插件本体），配置备份约 106MB。

### 命令

```bat
py dsh_lanes.py cfg-backup 1018-web --name before-reinstall
py dsh_lanes.py cfg-list                       列出所有备份（来源 / 插件数 / 会话数 / 大小 / 是否含凭据）
py dsh_lanes.py cfg-show dshcfg-1018-web-...   看备份里的插件清单、会话、设置、数据目录
py dsh_lanes.py cfg-plugins dshcfg-...         只列包内插件（可机读）
py dsh_lanes.py cfg-plugin-rm dshcfg-... dsh-pocket dsh-ops      从备份里删插件（清单+bundles+开关）
py dsh_lanes.py cfg-plugin-add dshcfg-... web dsh-notify-me@1.7.0
py dsh_lanes.py cfg-plugin-add dshcfg-... web dsh-notify-me --from-lane global   从本机某 lane 采集版本/开关
py dsh_lanes.py cfg-restore dshcfg-... my-new-lane              新建 lane（默认装备份记录的 dsh 版本）并恢复
py dsh_lanes.py cfg-restore dshcfg-... --lane existing-lane     导入已有 lane
```

### 恢复时做了什么

1. **新建 lane**：走 `create` 装一份 dsh（默认版本取备份里记录的 `source.dshVersion`，可用 `--version` 改）；
   目标 lane 正在运行会先全部停掉。
2. 每个 profile 不存在就按官方模板建干净环境。
3. **先合并 `pnpm-workspace.yaml` 的 `allowBuilds`**（见下），再装插件。
4. 还原清单里的本地 `file:` 插件：HOME 内引用还原到原相对位置；HOME 外的引用统一改写到
   目标 HOME `local-plugins/from-backup/<key>` 并改写 spec。
5. 第三方插件逐个 `dsh plugin add <pkg>@<版本> --save-exact`（官方 `@deepseek-ai/` 组合包是宿主自带的，跳过）；
   registry 包用备份里的实际版本，`github:`/`git`/`https` 原样，`file:` 用新路径。
   **本地有同版本就离线装**：registry 包先 `--offline` 纯离线（pnpm 同盘共享内容寻址 store，
   本机其他 lane 装过相同版本，按内容哈希保证版本一致、零网络），缺了再 `--prefer-offline`
   联网补缺；日志逐个标注「离线复用本地缓存 / 联网下载 / 本地插件还原」。
6. `cordis.patch.yml` 剥掉 webserver 端口条目后与目标合并（新 lane 即等于原样）；
   bundles 用清单兜底登记。
7. 会话先把现状另存一份再覆盖、逐文件校验指纹；同名设置先留 `.bak`；
   凭据只**合并 refs 段**（不冲掉目标浏览器里已登录的会话）；插件数据目录合并不覆盖。

### 关于 `allowBuilds`（git-hosted 插件）

`dsh-providers-hub` 这类插件装在 git 仓库上，要靠 `pnpm-workspace.yaml` 的 `allowBuilds`
放行它的 `prepare` 构建，否则 pnpm 报 `ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED`。
pnpm 10.26+ 的 `allowBuilds` 键只接受**包名**或**包名@精确版本**，写
`"pkg@git+https://….git"` 会报 `ERR_PNPM_INVALID_VERSION_UNION`，而且这一个坏键会让之后
**所有** `pnpm add` 都失败。备份和恢复时会把这类键自动规范化成裸包名（如 `dsh-providers-hub: true`），
所以备份的旧写法不影响恢复。

### 界面入口

Web 前端顶部「配置备份」、每张卡片的「配置备份…」；经典界面卡片上的「配置备份…」。
四个视图：备份列表 / 新建备份（勾选会话、附件、设置、凭据、数据目录）/ 查看（列出插件、勾选增删）/ 导入恢复（先出计划再执行）。
点「开始备份 / 开始恢复」后任务在后台跑、弹窗立即关闭，可随时回到主界面；进度看右侧「实时日志」
和顶部状态条，完成后右上角弹结果提示。

### 边界

- 恢复装插件**优先离线**：本机 pnpm store 里有同版本就不联网；store 缺的（新机器、没装过的插件）
  才需要联网下载，耗时取决于缺多少；跨机器迁移时 `file:` 外部引用会改写到目标 HOME，但 git 依赖要能访问对应仓库。
- 含凭据的 zip **不加密**，请自行保管。
- `cfg-list` 对读不出 manifest 的包会显示 `[损坏]`，不会拿去恢复。

---

## 15. 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。当前版本 **v0.6.4**（2026-10-18）：

- **新**：凭据面板从"只读清单"升级成**能看 / 改 / 删 / 新增 / 重新导入**（列表带 dsh 里的
  服务商显示名；明文值点了才取，只在本机回环）；新增**自定义服务商**面板与
  `set-key` / `rm-key` / `provider-list` / `provider-add` 命令。
- **新**：插件库就地**开关 / 卸载 / 加装**（`plugin-add` / `plugin-rm`）；**配置文件**面板
  （补丁层 / 凭据 / package.json / lock / workspace / modules 内容 + 一键定位）。
- **改**：插件迁移自动过滤「两边同版本」的条目（少一次无谓重装，不会覆盖目标里调好的东西）。
- **修**：凭据写坏 bug —— 多行单引号值以前被逐行正则只取首行、写回时引号未闭合，
  文件直接解析不过（`MISSING_CHAR`）。现在整套读写按 YAML 标量规则走，写后用宿主解析器
  校验、不过自动回滚；坏文件会被拦住并提示"从别的 lane 重新导入"。
- v0.6.3（2026-10-18）：

- **改**：备份/恢复点「开始」后在后台跑、弹窗立即关闭、可回主界面，完成弹结果提示；
  子进程不再弹黑窗（pythonw 静默）；新建 lane 实时显示 npm 下载进度（已下载 N 个包/速率）；
  恢复插件本地有同版本就离线复用（pnpm store 命中则零网络）。
- **修**：Web 顶部「配置备份」→「新建备份」报 `st is not defined`；
  `start_gui.bat` 启动前清理残留后端。
- v0.6.2（2026-10-18）：

- **新**：配置备份 / 迁移（`cfg-backup` / `cfg-restore` 等 8 个命令，Web 与经典界面同步）。
  轻量 zip 只装插件清单（不含本体）、会话、设置、本地插件；新建 lane 导入后自动联网装回插件；
  可查看备份内容、在备份里增删插件。备份/恢复时自动规范化 pnpm `allowBuilds` 的 git 键。
- v0.6.1（2026-10-18）：

- **修**：窗口里「删除这套 DSH」以前会崩（`RuntimeError: lost sys.stdin`）—— `--yes` 只挡住了
  报告的前半段，后面那段 `input()` 照样执行；带 HOME 的删除从来没成功过。
- **新**：复制时可以挑要带哪些 profile（命令行 `--profiles`，两套界面都是勾选框），
  没勾的目录不进副本，体积也跟着小。
- v0.6.0（2026-10-17）：**第二批迁完** —— 新建版本、添加 Profile、插件库、凭据、插件市场、
  备份对话、复制、升级、删除这套 DSH、核对隔离全部进了新界面。
- v0.5.0：**换前端** —— 界面改成本地 Web 前端（`dsh_lanes_web.py` + `web/`），
  只监听回环、带一次性令牌；换主题 / 刷新 / 展开收起都变成真过渡。
- v0.4.0：经典界面重做（两套主题 + 极光磨砂玻璃）、插件库（读取与迁移）。
- v0.3.0：一条 lane 内多个 profile、端口实时挑空闲、`keys` / `copy-key` 按条目复制。

---

## 16. 许可

MIT，见 [LICENSE](LICENSE)。
