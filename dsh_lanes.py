#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 多版本启动器（第一步：创建 / 打开指定版本）
================================================

纯 Python 标准库实现，不依赖任何第三方包，也不需要 pnpm / npx。

核心能力
--------
1. `versions` —— 查询 npm registry 上 @deepseek-ai/dsh 的全部版本与 dist-tag（纯脚本 HTTP）
2. `create`   —— 把指定版本装进独立目录（互不覆盖）并登记成一条 lane
3. `open`     —— 用该 lane 自己的 DSH_HOME / 端口启动一个 profile，并打开带 token 的正确 URL
4. `profiles` —— 一条 lane（一个 DSH_HOME）里管理多个 profile：各自一套插件组合，即多环境
5. `list` / `stop` / `logs` / `doctor` —— 查看、停止、排障

为什么每个 lane 要独立 DSH_HOME
--------------------------------
DSH 启动时会把 `$DSH_HOME/profiles/node_modules` 的模块回退链接重新指向**当次启动所用安装**的
依赖闭包（dsh-app-boot 的 healProfilesModuleFallback）。两套版本共用同一个 home，会随着谁启动
就把这份共享镜像往谁那边治一次。所以 lane 之间必须隔离安装目录 + 隔离 home。

lane 内部的多环境靠 **profile**：`dsh <profile>` 启动 `$DSH_HOME/profiles/<profile>`，
每个 profile 有自己的插件组合（bundles / node_modules / cordis.patch.yml），
但同一 HOME 里的会话、凭据、设置是共享的——于是**一个版本就能开多个环境**，
不必为每个环境复制一整套 dsh。端 口不固定：每次启动实时挑空闲端口（也可以现场指定）。

目录布局（默认 root = D:\\dsh-lanes，可在 lanes.json 改）
--------------------------------------------------------
    <root>/
      versions/<版本>/              npm --prefix 目标：node_modules/@deepseek-ai/dsh/lib/bin.js
      homes/<lane>/                 DSH_HOME：settings/sessions/credentials/profiles
      homes/<lane>/profiles/<名>/   一个 profile＝一个环境（插件组合独立，会话/凭据共享）
      run/<lane>__<profile>.json    运行态：pid / port / url（旧版 <lane>.json 读作 profile web）
      logs/<lane>__<profile>.log    启动与安装日志

用法
----
    py dsh_lanes.py doctor
    py dsh_lanes.py versions
    py dsh_lanes.py create next 0.1.7-rc.2
    py dsh_lanes.py open next                     # 启动默认 profile（web），端口自动挑
    py dsh_lanes.py open next/test --port 3082    # 启动指定 profile、现场指定端口
    py dsh_lanes.py profile-add next clean --from web   # 复制 web 的插件组合成新环境
    py dsh_lanes.py list
    py dsh_lanes.py logs next/test
    py dsh_lanes.py stop next                     # 停掉这条 lane 的全部 profile
    py dsh_lanes.py copy-key global next --only DEEPSEEK_API_KEY   # 按条目复制 API key
    py dsh_lanes.py delete next --stop  # 删除一套 DSH（登记 + 安装树 + HOME）
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import zipfile
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# ======================= 常量与默认配置 =======================

# 冻结成 exe（PyInstaller）之后 `__file__` 指向**临时解包目录**（_MEIPASS）：
# 配置必须跟着 exe 走，跟着临时目录走的话一退出就没了。
FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """本程序自己的目录：源码运行＝脚本目录；冻结后＝exe 所在目录。

    冻结后优先把 `lanes.json` 放在 exe 旁边（便携、拷走就能用）；
    那个目录不可写时（例如被塞进 Program Files）退到 `%APPDATA%\\dsh-lanes`，
    避免"第一次保存配置就崩"。
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    base = Path(sys.executable).resolve().parent
    try:
        probe = base / ".dsh-lanes-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "dsh-lanes" if appdata else base


SCRIPT_DIR = app_dir()
CONFIG_PATH = SCRIPT_DIR / "lanes.json"

# 提示文字里"下一步该敲什么"用的程序名：源码是 .py，冻结后是 .exe 自己的名字
ME_NAME = Path(sys.executable).name if FROZEN else Path(__file__).name
PKG = "@deepseek-ai/dsh"
PKG_URL_NAME = "@deepseek-ai%2Fdsh"

# 官方代码库。取自已装包 package.json 的 repository 字段
# （git+https://github.com/deepseek-ai/deepseek-harness.git，directory: apps/cli），
# 2026-09-27 用本机那份 0.1.7-rc.2 核对过。
REPO_URL = "https://github.com/deepseek-ai/deepseek-harness"

OFFICIAL_REGISTRY = "https://registry.npmjs.org"
MIRROR_REGISTRY = "https://registry.npmmirror.com"
REGISTRY_CANDIDATES = [
    OFFICIAL_REGISTRY,
    MIRROR_REGISTRY,
]

# 「用哪个源下载」：auto（默认）/ official / mirror / 直接一个 http 地址。
# 为什么要这个开关（2026-09-28 实测教训）：官方源当天发新版时，npmmirror 常常只同步了一半
# —— dsh@0.2.0-rc.1 它有了，可 12 个 @deepseek-ai/dsh-* 子包还没到（查了 259 个包，镜像缺 12）。
# 这时"查版本走官方、安装走镜像"必然在第一个缺的子包上 npm ETARGET 失败。所以两条规矩：
#   ① 查版本用哪个源，安装就用哪个源；
#   ② 装的时候如果那个源还没同步全，允许换另一个源重试一次。
REGISTRY_ALIASES = {
    "": "",
    "auto": "",
    "official": OFFICIAL_REGISTRY,
    "npmjs": OFFICIAL_REGISTRY,
    "npm": OFFICIAL_REGISTRY,
    "mirror": MIRROR_REGISTRY,
    "npmmirror": MIRROR_REGISTRY,
    "taobao": MIRROR_REGISTRY,
}
REGISTRY_CHOICES = ("auto", "official", "mirror")
REGISTRY_LABELS = {
    OFFICIAL_REGISTRY: "官方源 npmjs.org",
    MIRROR_REGISTRY: "镜像源 npmmirror.com",
}

# npm 11 起 install script 默认需要显式授权（RFC npm/rfcs#868）。
# 这张表必须写在**目标目录的 package.json 的 allowScripts 对象**里：
#   - 数组形式无效（会被当成键 "0"/"1"...），必须是 {"包名": true}
#   - 环境变量 npm_config_allow_scripts / --allow-scripts 在项目式安装里会被 npm 直接拒绝
DEFAULT_ALLOW_SCRIPTS = {
    "@deepseek-ai/dsh-subprocess-local": True,
    "koffi": True,
    "node-pty": True,
    "@google/genai": True,
    "protobufjs": True,
}

DEFAULT_CONFIG = {
    "root": "",
    "node": "",
    "npm_cli": "",
    "registry": "",
    "port_range": [3080, 3129],
    "default_cwd": "",
    "startup_timeout": 240,
    "open_browser": True,
    "allow_scripts": DEFAULT_ALLOW_SCRIPTS,
    "lanes": {},
}

# ======================= 终端输出 =======================


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"


def setup_terminal() -> None:
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def col(text: str, color: str = "") -> str:
    return f"{color}{text}{C.RESET}" if color else text


def info(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(col("  [OK] ", C.GREEN) + msg, flush=True)


def warn(msg: str) -> None:
    print(col("  [!!] ", C.YELLOW) + msg, flush=True)


def fail(msg: str) -> None:
    print(col("  [XX] ", C.RED) + msg, flush=True)


# ======================= 配置读写 =======================


def default_root() -> str:
    """默认 lane 根目录：优先 D 盘（固定盘、空间大），否则回落用户目录。"""
    for cand in ("D:\\dsh-lanes", "C:\\dsh-lanes"):
        drive = Path(cand).drive
        if drive and Path(drive + "\\").exists():
            return cand
    return str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "dsh-lanes")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as exc:
            fail(f"lanes.json 解析失败，改用默认配置：{exc}")
    if not cfg.get("root"):
        cfg["root"] = default_root()
    if not cfg.get("default_cwd"):
        if FROZEN:
            # 冻结后 __file__ 在临时解包目录里，"上两级"没有任何意义 → 用用户主目录
            cfg["default_cwd"] = str(Path.home())
        else:
            # 脚本位于 <工作区>\启动器\ 下，默认工作区取上两级
            try:
                cfg["default_cwd"] = str(Path(__file__).resolve().parents[2])
            except IndexError:
                cfg["default_cwd"] = str(Path.home())
    merged = dict(DEFAULT_ALLOW_SCRIPTS)
    merged.update(cfg.get("allow_scripts") or {})
    cfg["allow_scripts"] = merged
    cfg.setdefault("lanes", {})
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(cfg)
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def paths(cfg: dict) -> dict:
    root = Path(cfg["root"])
    return {
        "root": root,
        "versions": root / "versions",
        # clone 出来的安装树放这里，而不是 versions/<版本>：
        # versions 是按"版本"命名的，两条 lane 同版本会撞车，而且那样无法原地升级副本
        # （升级会把同版本的别的 lane 一起改掉）。
        "clones": root / "clones",
        "homes": root / "homes",
        "run": root / "run",
        "logs": root / "logs",
        # 备份放这里：chat/<lane>/<时间戳> = 对话记录快照，market/<lane>/<时间戳> = 插件市场更新前的快照
        "backups": root / "backups",
    }


def ensure_layout(cfg: dict) -> dict:
    p = paths(cfg)
    for key in ("root", "versions", "clones", "homes", "run", "logs", "backups"):
        p[key].mkdir(parents=True, exist_ok=True)
    return p


# ======================= 工具函数 =======================


def find_node(cfg: dict) -> str | None:
    if cfg.get("node") and Path(cfg["node"]).is_file():
        return cfg["node"]
    found = shutil.which("node")
    if found:
        return found
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe"),
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def find_npm_cli(cfg: dict, node: str | None) -> str | None:
    if cfg.get("npm_cli") and Path(cfg["npm_cli"]).is_file():
        return cfg["npm_cli"]
    if node:
        cand = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if cand.is_file():
            return str(cand)
    for cand in (
        r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js",
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "npm" / "bin" / "npm-cli.js"),
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def _read_tcp_table() -> dict[int, int] | None:
    """读一次 TCP LISTEN 表 → {端口: PID}。读不到返回 None（调用方决定怎么降级）。

    直接调 iphlpapi 的 GetExtendedTcpTable：不 spawn 子进程、不用管道，
    也不依赖 netstat / WMI（本机沙箱下这两条都是不可用的）。
    """
    if os.name != "nt":
        return {}
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    ERROR_INSUFFICIENT_BUFFER = 122
    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    ret = iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if ret != ERROR_INSUFFICIENT_BUFFER:
        return None
    buf = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if ret != 0:
        return None
    raw = buf.raw
    count = struct.unpack_from("<I", raw, 0)[0]
    rows: dict[int, int] = {}
    row_size = 24  # MIB_TCPROW_OWNER_PID = 6 个 DWORD
    for index in range(count):
        base = 4 + index * row_size
        if base + row_size > len(raw):
            break
        local_port = struct.unpack_from(">H", raw, base + 8)[0]  # 低 16 位、网络字节序
        pid = struct.unpack_from("<I", raw, base + 20)[0]
        rows[local_port] = pid
    return rows


_tcp_cache: dict = {"at": 0.0, "rows": None}
_tcp_lock = threading.Lock()


def tcp_listeners(ttl: float = 0.5, force: bool = False) -> dict[int, int]:
    """TCP LISTEN 端口 → 持有进程 PID（带 TTL 缓存）。

    为什么要缓存：GUI 每轮刷新会问很多次（每条 lane、实例发现、端口挑选），
    读一次表本身只要 0.3ms，但重复十几次也没必要。
    """
    with _tcp_lock:
        cached = _tcp_cache["rows"]
        if (
            not force
            and ttl > 0
            and cached is not None
            and time.monotonic() - _tcp_cache["at"] < ttl
        ):
            return dict(cached)
    rows = _read_tcp_table()
    if rows is None:
        with _tcp_lock:
            return dict(_tcp_cache["rows"] or {})  # 读失败就用上一次成功的结果
    with _tcp_lock:
        _tcp_cache["at"] = time.monotonic()
        _tcp_cache["rows"] = rows
    return dict(rows)


def invalidate_tcp_cache() -> None:
    """启动 / 停止 / 删除之后就地把缓存作废，别让界面看到过期状态。"""
    with _tcp_lock:
        _tcp_cache["at"] = 0.0


def listening_pid(port: int) -> int:
    """这个端口上有监听进程吗？有就返回它的 PID，否则 0。"""
    try:
        return int(tcp_listeners().get(int(port), 0))
    except (TypeError, ValueError):
        return 0


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已被占用。

    走 TCP LISTEN 表，**不用"试着连一下"**：本机实测连一个没有监听的端口不会被立刻
    拒绝，而是要等满 600ms 超时（安全软件把 SYN 丢了），每次判断都卡 0.6 秒——
    这是启动器界面卡顿的根因。读整张表只要 0.3ms，而且"端口被占了"的权威判据本来就是
    表里有没有 LISTEN 项，不是连得上连不上。
    """
    if int(port) in tcp_listeners():
        return True
    if _tcp_cache["rows"] is None and os.name == "nt":
        # 表读不到（极少见）→ 退回连接探测：慢，但绝不把已占用的端口误判成空闲
        try:
            with socket.create_connection((host, int(port)), timeout=0.6):
                return True
        except OSError:
            return False
    return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_uint32()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=_no_window_flags())
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


# ======================= 实例发现（纯 ctypes，不依赖 netstat / WMI） =======================

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_image(pid: int) -> str:
    """取进程可执行文件全路径；取不到返回空串（权限/沙箱限制都可能）。"""
    if os.name != "nt" or pid <= 0:
        return ""
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k.CloseHandle(handle)


def probe_dsh_like(port: int, host: str = "127.0.0.1") -> bool:
    """没带 token 的裸 GET 返回 401 —— 这正是 DSH 信任围栏的特征
    （dsh-client-connection：没有有效 cookie 一律 401）。用来把"别的程序占了这个端口"区分开。"""
    import http.client

    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        code = resp.status
        resp.read(64)
        conn.close()
        return code == 401
    except Exception:
        return False


def process_start_time(pid: int) -> str:
    """进程启动时间（本机时区字符串）。用于识别"这个实例是什么时候起来的"。"""
    if os.name != "nt" or pid <= 0:
        return ""
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    try:
        created, exited, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not k.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return ""
        ticks = (created.high << 32) | created.low
        unix = ticks / 1e7 - 11644473600  # FILETIME 纪元(1601) 与 Unix 纪元之差
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix))
    finally:
        k.CloseHandle(handle)


def find_installs(cfg: dict | None = None) -> list[dict]:
    """枚举本机所有 `@deepseek-ai/dsh` 安装树：npm 全局前缀 / npx 缓存 / PATH / 各 lane 版本目录。

    为什么要连 npx 缓存一起找：老启动器用的是 `npx @deepseek-ai/dsh web`，
    而 npx 装的是**缓存目录**里那份，与 `npm i -g` 的全局目录不是同一棵树——只看全局会漏。
    每条都带来源标签，方便判断"到底在用哪一份"。
    """
    results: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, source: str) -> None:
        key = str(path).lower()
        if key in seen:
            return
        manifest = path / "package.json"
        if not manifest.is_file():
            return
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("name") != PKG:
            return
        seen.add(key)
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest.stat().st_mtime))
        except OSError:
            mtime = ""
        results.append(
            {
                "version": data.get("version", "?"),
                "path": str(path),
                "source": source,
                "installedAt": mtime,
            }
        )

    appdata = os.environ.get("APPDATA")
    if appdata:
        add(Path(appdata) / "npm" / "node_modules" / "@deepseek-ai" / "dsh", "npm 全局")
    which = shutil.which("dsh")
    if which:
        try:
            add(Path(which).resolve().parent / "node_modules" / "@deepseek-ai" / "dsh", "PATH 上的 dsh")
        except OSError:
            pass
    add(Path(r"C:\Program Files\nodejs\node_modules\@deepseek-ai\dsh"), "nodejs 安装目录")

    local = os.environ.get("LOCALAPPDATA")
    if local:
        npx_dir = Path(local) / "npm-cache" / "_npx"
        if npx_dir.is_dir():
            try:
                for entry in npx_dir.iterdir():
                    add(entry / "node_modules" / "@deepseek-ai" / "dsh", "npx 缓存")
            except OSError:
                pass

    if cfg:
        p = paths(cfg)
        registered_trees: set[str] = set()
        for lane, data in cfg.get("lanes", {}).items():
            if not (isinstance(data, dict) and data.get("version")):
                continue
            # 注意：lane 目录自己的 package.json 是 npm --prefix 的前缀清单
            # （name = dsh-lane-<版本>），真正的 dsh 清单在嵌套的 node_modules 里。
            if data.get("installDir"):
                registered_trees.add(str(Path(data["installDir"]).resolve()).lower())
                continue
            tree = p["versions"] / str(data["version"])
            registered_trees.add(str(tree.resolve()).lower())
            add(tree / "node_modules" / "@deepseek-ai" / "dsh", f"lane {lane}")
        # versions/ 与 clones/ 里**没被任何 lane 登记**的树也列出来：
        # 手动拷进来的、登记表丢过的，都能在这里看到并手动认领
        for sub, label in ((p["versions"], "versions 目录（未登记）"),
                           (p["clones"], "clones 目录（未登记）")):
            if not sub.is_dir():
                continue
            try:
                entries = sorted(sub.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir() or str(entry.resolve()).lower() in registered_trees:
                    continue
                before = len(results)
                add(entry / "node_modules" / "@deepseek-ai" / "dsh", label)
                if len(results) > before:
                    results[-1]["tree"] = str(entry)

    return results


def find_global_install() -> dict | None:
    """只取 npm 全局那份（兼容旧调用）；找不到返回 None。"""
    for item in find_installs(None):
        if item["source"] == "npm 全局":
            return item
    return None


def discover_instances(cfg: dict) -> list[dict]:
    """列出"不是本启动器启动的" DSH web 实例（例如全局安装那套 3080）。

    收录条件：端口落在 port_range（DSH 约定 3080-3129）内，或带 401 围栏特征。
    """
    listeners = tcp_listeners()
    lo, hi = cfg.get("port_range", [3080, 3129])
    owned: dict[int, str] = {}
    for rec in all_run_records(cfg):
        port = int(rec.get("port") or 0)
        if port and rec.get("lane"):
            owned[port] = f"{rec['lane']}/{rec.get('profile') or DEFAULT_PROFILE}"

    found: list[dict] = []
    for port in sorted(listeners):
        pid = listeners[port]
        lane = owned.get(port)
        if lane and pid_alive(pid):
            continue  # 本启动器管理的，卡片区已经有了
        in_range = int(lo) <= port <= int(hi)
        image = process_image(pid)
        exe = Path(image).name if image else ""
        node_like = bool(re.search(r"node|dsh|harness", exe, re.I))
        # 先把明显无关的端口排除掉，再谈 HTTP 探测：
        # 探测是有超时的网络请求，如果对每个监听端口都探一遍，几十个端口就能拖住十几秒
        # （GUI 在 __init__ 里调它，会表现为"窗口一直不出来"）。
        if not in_range and not node_like:
            continue
        # 401 围栏也不是 DSH 独有（实测 VS Code 调试端口也 401），所以段外还要进程名像 node/dsh。
        dsh_like = probe_dsh_like(port)
        if not in_range and not (dsh_like and node_like):
            continue
        found.append(
            {
                "port": port,
                "pid": pid,
                "exe": exe or "?",
                "image": image,
                "dsh_like": dsh_like,
                "started": process_start_time(pid),
                "url": f"http://127.0.0.1:{port}",
            }
        )
    return found


# ======================= 自动认领：磁盘上没登记的 lane =======================
#
# lanes.json 只记"登记表"，而创建 / 复制出来的东西都在磁盘上（homes、versions、clones、
# 启动日志）。登记表丢过（换目录跑、手删、exe 与源码各存一份）之后，磁盘上的东西就成了
# "孤儿"——界面上看不见，也没法从这里打开。这一段负责把它们**自动认回来**：
#
#   ① 副本对：`clones/<名>` 安装树 + `homes/<名>` HOME 成对出现（clone 的固定布局）→ 直接认；
#   ② 日志配对：`homes/<名>` 未登记 → 从启动器自己的启动日志头（`===== … $ node …bin.js
#      web --port N`）里找回它用过的安装树和端口 → 认回来，并把端口记成 web profile 的偏好
#      （这样手动启动的实例也能被自愈逻辑按端口认回）；
#   ③ 孤儿安装树（没有任何 HOME 配对，比如只有 versions/<版本>）→ 不自动认（不知道 HOME 是谁），
#      列在「本机各套安装」里等手动认领。
#
# 认领只做一件事：往 lanes.json 里补登记。不动任何目录，名字撞了就跳过（登记表优先）。

LANE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def _lane_clues_from_log(path: Path) -> dict | None:
    """从一条启动日志里取回最近一次启动的线索：入口脚本 / 端口 / profile。

    日志头是启动器自己写的：`===== 时间 $ node …bin.js [web|--profile P] --port N … (cwd=…)`。
    从**最新的一条**往回找（日志是追加的，旧的端口早就不是现在的了）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if "$ " not in line or "bin.js" not in line:
            continue
        header = line.split("$ ", 1)[1].split("(cwd=", 1)[0].strip()
        tokens = header.split()
        try:
            idx = next(i for i, t in enumerate(tokens) if t.endswith("bin.js"))
        except StopIteration:
            continue
        rest = tokens[idx + 1:]
        port = None
        for j, tok in enumerate(rest):
            if tok == "--port" and j + 1 < len(rest) and rest[j + 1].isdigit():
                port = int(rest[j + 1])
                break
        return {"entry": tokens[idx], "port": port}
    return None


def _install_tree_of_entry(entry_text: str) -> Path | None:
    """从入口脚本路径反推安装树：兼容 prefix 树与接管型包目录两种写法。"""
    p = Path(entry_text)
    parts = p.parts
    tail = ("node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
    if len(parts) >= len(tail) and tuple(parts[-len(tail):]) == tail:
        return Path(*parts[: len(parts) - len(tail)])
    pkg = p.parent.parent  # 接管型：<包目录>\lib\bin.js
    if pkg.name == "dsh" and pkg.parent.name == "@deepseek-ai":
        return pkg
    return None


def find_lost_lanes(cfg: dict) -> list[dict]:
    """找出磁盘上有、登记表里没有的 lane 痕迹（不改任何东西，只报告）。"""
    p = ensure_layout(cfg)
    registered = {n for n, d in cfg.get("lanes", {}).items() if isinstance(d, dict)}
    out: list[dict] = []
    seen_names: set[str] = set()

    # ① 副本对（clone 的固定布局，不需要日志就能认）
    if p["clones"].is_dir():
        try:
            entries = sorted(p["clones"].iterdir())
        except OSError:
            entries = []
        for entry in entries:
            name = entry.name
            if not entry.is_dir() or name in registered or not LANE_NAME_RE.match(name):
                continue
            home = p["homes"] / name
            if not home.is_dir():
                continue
            version = _manifest_version(entry / "node_modules" / "@deepseek-ai" / "dsh")
            clue = _lane_clues_from_log(p["logs"] / f"{name}.log")
            clue = clue or _lane_clues_from_log(p["logs"] / f"{name}__{DEFAULT_PROFILE}.log")
            seen_names.add(name)
            out.append({
                "name": name, "home": home, "install": entry, "version": version,
                "port": (clue or {}).get("port"), "kind": "clone", "confident": True,
                "why": f"clones\\{name} 安装树 + homes\\{name} HOME 成对（clone 布局）",
            })

    # ② HOME 在、登记表没有 → 从启动日志里找回安装树与端口
    if p["homes"].is_dir():
        try:
            entries = sorted(p["homes"].iterdir())
        except OSError:
            entries = []
        for entry in entries:
            name = entry.name
            if (not entry.is_dir() or name in registered or name in seen_names
                    or not LANE_NAME_RE.match(name)):
                continue
            install = None
            version = None
            port = None
            logs = sorted(p["logs"].glob(f"{name}*.log")) if p["logs"].is_dir() else []
            for log in reversed(logs):  # 新日志优先
                clue = _lane_clues_from_log(log)
                if not clue:
                    continue
                tree = _install_tree_of_entry(clue["entry"])
                if tree is None:
                    continue
                # 两种安装树写法都要认：接管型（树根本身就是 dsh 包目录）与
                # prefix 型（树根是 npm --prefix 前缀，真正的 dsh 清单在嵌套的 node_modules 里）
                if _manifest_name(tree) == PKG:
                    install, version = tree, _manifest_version(tree)
                elif _manifest_name(tree / "node_modules" / "@deepseek-ai" / "dsh") == PKG:
                    install = tree
                    version = _manifest_version(tree / "node_modules" / "@deepseek-ai" / "dsh")
                if install is not None:
                    port = clue.get("port")
                    break
            confident = install is not None
            seen_names.add(name)
            out.append({
                "name": name, "home": entry, "install": install, "version": version,
                "port": port, "kind": "created", "confident": confident,
                "why": (f"homes\\{name} 在磁盘上，启动日志里找回了它的安装树"
                        if confident else
                        f"homes\\{name} 在磁盘上，但日志里没找到配对的安装树（不自动认领）"),
            })
    return out


def adopt_lost_lanes(cfg: dict) -> list[dict]:
    """把**配对明确**的未登记 lane 自动补进登记表。返回这次认领了哪些。

    什么时候会用到：lanes.json 被删过 / 换了目录跑（exe 与源码各存一份配置）/ 手动清理过，
    而创建、复制出来的 homes / versions / clones 都还在磁盘上。认领是纯登记：
    不动任何目录，端口线索记成 web profile 的偏好，好让手动启动的实例也能被认回。
    """
    adopted: list[dict] = []
    for item in find_lost_lanes(cfg):
        if not item.get("confident") or not item.get("name") or item["name"] in cfg.get("lanes", {}):
            continue
        data: dict = {
            "version": item.get("version") or "?",
            "home": str(item["home"]),
            "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "由启动器自动重新认领（磁盘上发现未登记的 lane）",
        }
        if item.get("kind") == "clone":
            data["installDir"] = str(item["install"])
            data["kind"] = "clone"
        elif item.get("install") is not None:
            standard = paths(cfg)["versions"] / str(item.get("version"))
            try:
                nonstandard = item["install"].resolve() != standard.resolve()
            except OSError:
                nonstandard = True
            if nonstandard:
                data["installDir"] = str(item["install"])
        if item.get("port"):
            data["port"] = int(item["port"])
        cfg["lanes"][item["name"]] = data
        adopted.append(item)
    if adopted:
        save_config(cfg)
    return adopted


# ======================= profile：一条 lane 里的多个环境 =======================
#
# lane ＝ 一个版本（一棵安装树 + 一个 DSH_HOME）；profile ＝ 这个 HOME 里的一个环境：
# `$DSH_HOME/profiles/<名>/`（各自的插件组合 / 补丁层 / node_modules）。
# 会话、凭据、设置在 HOME 级共享——所以一个版本就能开出多个环境，不必复制多套 dsh。
#
# profile 的**真值在磁盘**（HOME/profiles/ 下有哪些目录）；lanes.json 里 lane 的
# "profiles" 字段只存提示（可选的端口偏好、登记时间）。旧配置没有 profiles 字段时，
# 一律按 {"web": {端口提示＝旧 lane 端口}} 看，行为与 v0.2 兼容。
#
# 保留名与规则照抄 dsh-app-boot（loadProfile / resolveProfileDir / PROFILE_TEMPLATES）：
# · `desktop` 由 DSH Desktop（Electron）独占，命令行起不了；
# · 官方模板 web / acp / headless / sdk / sdk-minimal 首次启动会自动初始化；
# · 自定义 profile 需要目录里先有 package.json——profile-add 就是替你把这个建好。

DEFAULT_PROFILE = "web"
RESERVED_PROFILES = {"desktop"}
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 与 dsh-app-boot 的 PROFILE_TEMPLATES 一致（2026-09-30 对照 0.1.7-rc.2 安装树核对）。
SHIPPED_TEMPLATES: dict[str, list[str]] = {
    "web": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"],
    "acp": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app"],
    "headless": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"],
    "sdk": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-sdk-app"],
    "sdk-minimal": ["@deepseek-ai/dsh-sdk-minimal"],
}

PROFILE_PATCH_TEMPLATE = (
    "# Your patch layer for this dsh profile, applied after every bundle layer:\n"
    "# a top-level YAML array of loader patch entries (id-targeted config\n"
    "# overrides, disables, and insert lists; `!!js` expressions allowed).\n"
    "[]\n"
)
PROFILE_PNPM_WORKSPACE = "packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n"


def init_profile_dir(dir: Path, bundles: list[str]) -> None:
    """按 dsh-app-boot `initProfile` 的形状初始化 profile 目录（存在的一律不覆盖）。

    官方组合包不用预装：宿主启动时会从当次安装树解析（resolveBundleDir）；
    这里只补 profile 自己的三件套——清单（含 bundles）、空补丁层、pnpm 工作区设置。
    """
    dir = Path(dir)
    dir.mkdir(parents=True, exist_ok=True)
    manifest = dir / "package.json"
    if not manifest.is_file():
        manifest.write_text(
            json.dumps(
                {
                    "name": f"dsh-profile-{dir.name}",
                    "private": True,
                    "dependencies": {},
                    "dsh": {"profile": {"bundles": list(bundles)}},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    patch = dir / "cordis.patch.yml"
    if not patch.is_file():
        patch.write_text(PROFILE_PATCH_TEMPLATE, encoding="utf-8")
    workspace = dir / "pnpm-workspace.yaml"
    if not workspace.is_file():
        workspace.write_text(PROFILE_PNPM_WORKSPACE, encoding="utf-8")


def validate_profile_name(name: str) -> None:
    """profile 名能不能用。规则与宿主 resolveProfileDir 对齐，再加保留名。"""
    if not name or not PROFILE_NAME_RE.match(name) or name in (".", "..", "node_modules"):
        raise ValueError(f"profile 名不合法：{name!r}（字母/数字开头，只含字母 数字 . _ -）")
    if name in RESERVED_PROFILES:
        raise ValueError(f"profile「{name}」由 DSH Desktop（Electron 应用）独占，命令行不能启动它")


def disk_profiles(home) -> list[str]:
    """HOME/profiles/ 下真实存在的 profile（有 package.json 的目录；排除保留名）。"""
    root = Path(home) / "profiles"
    out: list[str] = []
    if not root.is_dir():
        return out
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir() or entry.name in RESERVED_PROFILES or entry.name == "node_modules":
            continue
        if (entry / "package.json").is_file():
            out.append(entry.name)
    return out


def lane_profile_hints(cfg: dict, lane: str) -> dict[str, dict]:
    """lanes.json 里登记的 profile 提示；旧配置（无 profiles 字段）迁移成 web 视图。"""
    data = cfg.get("lanes", {}).get(lane) or {}
    profiles = data.get("profiles")
    if isinstance(profiles, dict) and profiles:
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in profiles.items()}
    # 旧数据：lane 级的 port 就是 web profile 的端口偏好（接管 lane 靠它认回 3080）
    out: dict[str, dict] = {DEFAULT_PROFILE: {}}
    if data.get("port"):
        out[DEFAULT_PROFILE]["port"] = int(data["port"])
    return out


def lane_profiles_view(cfg: dict, lane: str) -> dict[str, dict]:
    """这条 lane 可用的全部 profile ＝ 配置登记 ∪ 磁盘真实存在的。

    返回 {名字: {"port": 端口偏好或 None, "createdAt": …, "onDisk": 是否在磁盘上,
    "registered": 是否登记进 lanes.json}}。磁盘是真值：手动建过的 profile 也会出现，
    直接 open 就能用。
    """
    data = cfg.get("lanes", {}).get(lane) or {}
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    view: dict[str, dict] = {}
    for name, hint in lane_profile_hints(cfg, lane).items():
        view[name] = {
            "port": hint.get("port"),
            "createdAt": hint.get("createdAt"),
            "onDisk": False,
            "registered": True,
        }
    for name in disk_profiles(home):
        if name in view:
            view[name]["onDisk"] = True
        else:
            view[name] = {"port": None, "createdAt": None, "onDisk": True, "registered": False}
    return view


def default_profile_of(view: dict[str, dict]) -> str:
    """这条 lane 的默认 profile：web 优先，否则按名字排序取第一个。"""
    if DEFAULT_PROFILE in view:
        return DEFAULT_PROFILE
    return sorted(view)[0] if view else DEFAULT_PROFILE


def profile_hint_port(cfg: dict, lane: str, profile: str) -> int | None:
    """这个 profile 的端口偏好（仅提示；启动时被占用会自动改口）。"""
    return lane_profiles_view(cfg, lane).get(profile, {}).get("port")


def set_profile_hint(cfg: dict, lane: str, profile: str, remove: bool = False, **fields) -> None:
    """把 profile 提示写回 lanes.json（第一次写时顺手把旧 lane 端口迁到 web 名下）。"""
    data = cfg["lanes"][lane]
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        if data.get("port"):
            profiles[DEFAULT_PROFILE] = {"port": int(data["port"])}
    if remove:
        profiles.pop(profile, None)
    else:
        entry = dict(profiles.get(profile) or {})
        entry.update(fields)
        profiles[profile] = entry
    data["profiles"] = profiles
    save_config(cfg)


def run_file(cfg: dict, lane: str, profile: str) -> Path:
    """运行态文件：<root>/run/<lane>__<profile>.json。"""
    return paths(cfg)["run"] / f"{lane}__{profile}.json"


def legacy_run_file(cfg: dict, lane: str) -> Path:
    """v0.2 及以前的运行态文件（无 profile 概念），读取时视作 profile web。"""
    return paths(cfg)["run"] / f"{lane}.json"


def read_run(cfg: dict, lane: str, profile: str = DEFAULT_PROFILE) -> dict | None:
    """读运行态。旧的 `run/<lane>.json`（v0.2 及以前，无 profile 概念）读作 profile web。"""
    f = run_file(cfg, lane, profile)
    if not f.exists() and profile == DEFAULT_PROFILE:
        f = legacy_run_file(cfg, lane)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        data.setdefault("profile", profile)
        return data
    return None


_WRITE_WARNED: set[str] = set()


def lane_root_writable(cfg: dict) -> tuple[bool, str]:
    """lane 根目录能不能写。返回 (能不能写, 说明文字)。

    运行态、克隆、删除、升级都要写这个目录；不能写时不代表不能用——
    只是"窗口下次打开不会记得这条 lane"，所以只提示，不拦。
    """
    root = Path(cfg.get("root") or "")
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(root)
    except OSError as exc:
        return False, f"{root}（{exc}）"


def write_run(cfg: dict, lane: str, profile: str, data: dict) -> bool:
    """写运行态文件。**绝不抛异常**：写不进去只警告一次，功能降级。

    为什么这么写：这个文件只是"记住上一次是谁起的"，不是必要数据；而它在 GUI 每次刷新的
    链路上（lane_states → reconcile_lane）。一旦它抛异常，整个窗口会在 __init__ 里死掉——
    2026-09-27 就是这么崩的：受限环境里 `D:\\dsh-lanes\\run\\npx.json` 不许写，
    结果窗口起不来、也关不掉（没有 mainloop 的半成品窗口）。
    """
    try:
        p = ensure_layout(cfg)
        payload = dict(data)
        payload["lane"] = lane
        payload["profile"] = profile
        (p["run"] / f"{lane}__{profile}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except OSError as exc:
        key = f"{lane}/{profile}|{exc}"
        if key not in _WRITE_WARNED:
            _WRITE_WARNED.add(key)
            warn(f"运行态写不进去（{paths(cfg)['run']}）：{exc}")
            warn("  这条 lane 照样能用：状态每次现场探测，只是下次打开窗口时不会自动认回来。")
        return False


def clear_run(cfg: dict, lane: str, profile: str | None = None) -> None:
    """清运行态。profile=None ＝ 这条 lane 的全部 profile（含 v0.2 的无名旧文件）。"""
    if profile:
        candidates: list[Path] = [run_file(cfg, lane, profile)]
        if profile == DEFAULT_PROFILE:
            candidates.append(legacy_run_file(cfg, lane))
    else:
        candidates = sorted(paths(cfg)["run"].glob(f"{lane}__*.json"))
        candidates.append(legacy_run_file(cfg, lane))
    for f in candidates:
        try:
            f.unlink()
        except OSError:
            pass


def all_run_records(cfg: dict) -> list[dict]:
    """扫一遍 run 目录，返回所有运行态记录（lane / profile / port / pid …）。

    run 目录里就几十个小文件，全扫只要几毫秒；多 profile 之后端口不再固定，
    "哪些端口是我们自己的实例"只能靠这里的记录回答，不能从 lane 配置反推。
    """
    out: list[dict] = []
    run_dir = paths(cfg)["run"]
    try:
        files = sorted(run_dir.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        rec = dict(data)
        stem = f.stem
        if not rec.get("lane"):
            rec["lane"] = stem.split("__", 1)[0]
        if not rec.get("profile"):
            # 旧命名 <lane>.json（无 __）读作 web；新命名按文件名补齐
            rec["profile"] = stem.rsplit("__", 1)[1] if "__" in stem else DEFAULT_PROFILE
        out.append(rec)
    return out


def sync_lane_versions(cfg: dict) -> list[dict]:
    """把「接管 / 副本」lane 登记的版本对齐到安装树的真实版本。

    为什么只对带 installDir 的 lane 做：这类 lane 的安装树在登记之外（npm 全局 /
    clones 目录），用户完全可以绕过启动器升级它（`npm i -g` / 手动 npm install）——
    升级后登记还是旧版本，界面上显示 0.1.7、实际跑的是 0.2。发现不一致就以安装树
    为准改登记（不动任何文件）。常规 lane 不做这件事：它们的版本号就是安装树的
    定位器（versions/<版本>），反向同步会让登记指向错误的位置；它们的不一致由
    require_version_installed 明确报错。
    """
    changed: list[dict] = []
    for lane, data in cfg.get("lanes", {}).items():
        if not isinstance(data, dict) or not data.get("installDir"):
            continue
        install = Path(data["installDir"])
        if _manifest_name(install) == PKG:
            actual = _manifest_version(install)          # 接管型：installDir 就是 dsh 包目录
        else:
            actual = installed_version(install)          # 副本型：prefix 树
        if actual and str(actual) != str(data.get("version")):
            changed.append({"lane": lane, "from": str(data.get("version")), "to": str(actual)})
            data["version"] = str(actual)
    if changed:
        save_config(cfg)
    return changed


def port_claimed_by_other(cfg: dict, lane: str, profile: str, port: int) -> bool:
    """这个端口有没有被**别的** (lane, profile) 的存活实例认领着。

    用途：打开时发现"偏好的端口上已经有个 DSH"，要先确认它不是兄弟 profile
    或别的 lane 的实例，才允许走"接管"——否则会把他人的实例记到自己名下。
    """
    for rec in all_run_records(cfg):
        if int(rec.get("port") or 0) != int(port):
            continue
        if rec.get("lane") == lane and rec.get("profile") == profile:
            continue
        if pid_alive(int(rec.get("pid") or 0)):
            return True
    return False


def lane_any_running(cfg: dict, lane: str) -> dict | None:
    """这条 lane 有**任何一个** profile 在跑吗？返回第一个找到的运行态记录。

    升级 / 删除 / 复制这类"动整条 lane"的操作要用它——它们关心的不是某个
    profile，而是"这个 HOME / 这棵安装树现在有没有进程在用"。
    """
    view = lane_profiles_view(cfg, lane)
    order = [DEFAULT_PROFILE] + sorted(name for name in view if name != DEFAULT_PROFILE)
    order = [name for name in order if name in view]
    for prof in order:
        rt = lane_runtime(cfg, lane, prof)
        if rt:
            return rt
    return None


def lane_runtime(cfg: dict, lane: str, profile: str = DEFAULT_PROFILE) -> dict | None:
    """返回仍在运行的运行态记录；进程已死则顺手清理。

    被"接管"的实例（不是本启动器启动的）要额外核对进程启动时间：
    PID 会被系统回收，只靠 pid_alive 会把一个不相干的新进程误判成"还在跑"。
    """
    data = read_run(cfg, lane, profile)
    if not data:
        return None
    pid = int(data.get("pid", 0))
    if not pid_alive(pid):
        clear_run(cfg, lane, profile)
        return None
    expect = data.get("procStartedAt")
    if data.get("adopted") and expect:
        actual = process_start_time(pid)
        if actual and actual != expect:
            clear_run(cfg, lane, profile)
            return None
    return data


def reconcile_lane(cfg: dict, lane: str, profile: str = DEFAULT_PROFILE) -> dict | None:
    """刷新时的自愈：没有存活运行态、但这个 profile 的端口上是个 DSH 实例 → 自动重新接管。

    为什么要这一步：你用别的方式（老的启动.bat / npx）重启那套 DSH 时，
    运行态里记的 PID 就过期了。没有自愈的话卡片会一直显示"已停止"，
    直到你手动点一次「打开」。这里让它自己认回来。

    成本控制：先查 TCP 表里这个端口有没有监听（0.1ms，一表管所有 lane）、
    再看进程名像不像 node/dsh，最后才做一次带超时的 401 围栏探测。
    用自动端口的 profile 没有固定端口可查——重启后端口会变，这种只能等"打开"时接管。
    """
    run = lane_runtime(cfg, lane, profile)
    if run:
        return run
    port = profile_hint_port(cfg, lane, profile)
    if not port:
        return None
    pid = listening_pid(port)
    if not pid or not pid_alive(pid):
        return None
    if not re.search(r"node|dsh|harness", process_image(pid), re.I):
        return None
    if not probe_dsh_like(port):
        return None
    started = process_start_time(pid) or time.strftime("%Y-%m-%d %H:%M:%S")
    write_run(
        cfg,
        lane,
        profile,
        {
            "lane": lane,
            "version": (cfg.get("lanes", {}).get(lane) or {}).get("version", "?"),
            "port": port,
            "pid": pid,
            "url": f"http://127.0.0.1:{port}",
            "adopted": True,
            "procStartedAt": started,
            "startedAt": started,
            "note": "自动重新接管了该端口上的实例（不是本启动器启动的）",
        },
    )
    return read_run(cfg, lane, profile)


def lane_states(cfg: dict, ttl: float = 0.5) -> dict[str, dict[str, dict | None]]:
    """一次算出**所有** lane × profile 的运行态，复用同一份 TCP 表。

    GUI 一轮刷新里要问好几次"这个 profile 在跑吗"（算签名、建卡片、更新计数）。
    每次都重新 reconcile 的话，同一份事实要算三遍；这里一次算完交给调用方复用。
    """
    tcp_listeners(ttl=ttl)  # 预热：后面所有 listening_pid 直接命中缓存
    out: dict[str, dict[str, dict | None]] = {}
    for name, data in cfg.get("lanes", {}).items():
        if not isinstance(data, dict):
            continue
        out[name] = {
            prof: reconcile_lane(cfg, name, prof)
            for prof in lane_profiles_view(cfg, name)
        }
    return out


# ======================= 版本查询（纯脚本 HTTP） =======================


def registry_label(reg: str) -> str:
    """给人看的源名字（认不出的原样返回，便于自定义地址）。"""
    return REGISTRY_LABELS.get(reg, reg or "（未指定）")


def registry_plan(cfg: dict, choice: str | None = None) -> list[str]:
    """按顺序该试哪些源：**第一个是首选，后面是失败后的退路**。

    choice 省略时看 lanes.json 的 registry。取值：auto / official / mirror / 一个 http 地址，
    大小写不敏感，空串＝auto。自定义地址不安排退路（可能是内网源，不该偷偷去公网问）。
    """
    raw = str(choice if choice is not None else cfg.get("registry") or "").strip()
    key = raw.lower()
    if key in REGISTRY_ALIASES:
        chosen = REGISTRY_ALIASES[key]
        if not chosen:
            return list(REGISTRY_CANDIDATES)
        return [chosen] + [r for r in REGISTRY_CANDIDATES if r != chosen]
    if raw.startswith("http"):
        return [raw.rstrip("/")]
    raise ValueError(
        f"认不出的下载源 {raw!r}：可用 auto / official / mirror，或一个 http(s) 地址"
    )


def registry_plan_after(reg: str, cfg: dict, choice: str | None = None) -> list[str]:
    """已经知道 `reg` 能答话时，把它排第一，其余候选跟在后面当退路。

    查版本和装版本必须用**同一个源**（2026-09-28 那次 ETARGET 就是这么来的），所以查到版本的
    那个源要一路带到安装那一步。
    """
    return [reg] + [r for r in registry_plan(cfg, choice) if r != reg]


def registry_choice_of(cfg: dict) -> str:
    """lanes.json 里存的值 → 界面上的三选一（auto / official / mirror）。"""
    key = str(cfg.get("registry") or "").strip().lower()
    if key in ("official", "npmjs", "npm"):
        return "official"
    if key in ("mirror", "npmmirror", "taobao"):
        return "mirror"
    return "auto"


def registry_value_of(choice: str) -> str:
    """界面上的三选一 → 写进 lanes.json 的值（auto 存空串；自定义地址原样存）。"""
    return REGISTRY_ALIASES.get(str(choice or "").strip().lower(), str(choice or "").strip())


def fetch_index(cfg: dict, name: str = PKG,
                registries: list[str] | None = None) -> tuple[dict, str]:
    """查询 registry，返回 (packument, 实际使用的 registry)。

    官方 registry 在本机会握手超时（schannel/网络原因），所以首个候选只等 8 秒就换下一个。
    `name` 默认是 dsh 本体；插件市场更新时用它查 `dshmarket`。
    `registries` 传了就按它来（升级时会把"查到版本的那个源"排到最前）。
    """
    regs = list(registries) if registries else registry_plan(cfg)
    quoted = urllib.parse.quote(name, safe="")
    last: Exception | None = None
    for index, reg in enumerate(regs):
        url = f"{reg.rstrip('/')}/{quoted}"
        timeout = 8 if index == 0 else 25
        try:
            # 用完整 packument（而非 abbreviated）以拿到 time 字段里的发布日期
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "dsh-lanes-launcher",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp), reg
        except Exception as exc:  # noqa: BLE001
            last = exc
            warn(f"registry 不可用 {reg}：{exc}")
    raise RuntimeError(f"所有 registry 都查询失败：{last}")


def tail_lines(path: Path, count: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


def wait_authenticated(url: str, timeout: float = 30.0, interval: float = 0.6) -> tuple[bool, str]:
    """按浏览器的方式验证服务可用：带 cookie jar 走一次 token → cookie → 200 握手。

    dsh-client-connection 的规则是：根路径带正确 token 的 GET 会下发 cookie 并重定向到 ./，
    之后靠 cookie 放行，否则 401。所以不带 cookie jar 的裸请求必然 401——
    这个探针同时验证了三件事：进程活着、token 是**本次**的、静态资源可服务。
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    deadline = time.time() + timeout
    last = "未知"
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=8) as resp:
                if resp.status == 200 and len(jar) > 0:
                    return True, f"HTTP {resp.status} + cookie"
                last = f"HTTP {resp.status}（未下发 cookie）"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(interval)
    return False, last


def version_key(v: str):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", v)
    if not m:
        return ((0, 0, 0), 1, ())
    base = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = m.group(4)
    if pre is None:
        return (base, 1, ())  # 正式版 > 预发布版
    parts = []
    for seg in pre.split("."):
        parts.append((1, int(seg), "") if seg.isdigit() else (0, 0, seg))
    return (base, 0, tuple(parts))


def sorted_versions(packument: dict) -> list[str]:
    return sorted(packument.get("versions", {}).keys(), key=version_key)


def resolve_tag(packument: dict, spec: str) -> str:
    tags = packument.get("dist-tags", {})
    if spec in tags:
        return tags[spec]
    if spec in packument.get("versions", {}):
        return spec
    # 允许 "0.1.7" 这类前缀写法
    matches = [v for v in packument.get("versions", {}) if v.startswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return sorted(matches, key=version_key)[-1]
    raise KeyError(spec)


# ======================= 子进程：无管道执行（写日志 + 可选跟随） =======================


def _no_window_flags() -> int:
    """短命令（npm/pnpm/node/taskkill 等）：不弹控制台窗口。

    关键：start_gui.bat 用 pythonw（无控制台）启动后端，此时控制台子进程默认会
    新开一个 conhost 黑窗、一闪而过——装插件、跑 node 时就看到 node 窗口不停闪。
    CREATE_NO_WINDOW 让子进程不分配控制台（输出仍可走我们重定向的文件/管道）。
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def _new_group_flags() -> int:
    """长驻服务（dsh web）：独立进程组（方便整组 kill_tree）+ 同样不弹控制台窗口。"""
    flags = _no_window_flags()
    if os.name == "nt":
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


def follow_file(path: Path, stop: threading.Event, on_line=None, start_offset: int = 0) -> threading.Thread:
    """跟随一个正在被写入的日志文件，边打印边交给 on_line。

    start_offset 很关键：日志是追加写的，同一个 lane 反复启动会让旧进程的
    `dsh web: ...?token=` 行还留在文件里。从 0 读就会抓到**上一次的旧 token**
    （token 是每进程的），表现为浏览器 401。所以必须从"本次启动前"的偏移开始读。
    """

    def worker() -> None:
        while not path.exists() and not stop.is_set():
            time.sleep(0.05)
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            try:
                fh.seek(start_offset)
            except OSError:
                pass
            def emit(piece: str) -> None:
                # on_line 返回 True 表示"这行我自己处理了，别再原样打印"
                # （用于把 npm 几百行 http 日志压成几行下载进度）。
                if on_line and on_line(piece):
                    return
                sys.stdout.write(piece)
                sys.stdout.flush()

            while True:
                line = fh.readline()
                if line:
                    emit(line)
                    continue
                if stop.is_set():
                    rest = fh.read()
                    if rest:
                        emit(rest)
                    return
                time.sleep(0.15)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def run_logged(cmd: list[str], cwd: Path, env: dict, log_path: Path, follow: bool = True,
               on_line=None) -> int:
    """执行命令：stdout/stderr 直接落到日志文件（不用管道），可选实时跟随打印。

    on_line(line)：跟随线程每行回调；返回 True 表示这行由回调自行处理、不再原样打印
    （用于把 npm 冗长的 http 日志压缩成下载进度）。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    thread = None
    with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(cmd)} (cwd={cwd})\n")
        logf.flush()
        offset = logf.tell()
        if follow:
            thread = follow_file(log_path, stop, on_line=on_line, start_offset=offset)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=_new_group_flags(),
        )
        code = proc.wait()
        if thread is not None:
            stop.set()
            thread.join(timeout=3)
    return code


# ======================= npm 安装 =======================


_NPM_HTTP_RE = re.compile(r"\bhttp\s+fetch\b", re.IGNORECASE)
_NPM_TGZ_RE = re.compile(r"/-/(?P<pkg>[^/\s)]+?\.tgz)")


def _npm_progress_reporter(prefix: str = "      "):
    """把 npm `--loglevel=http` 的逐包下载日志压成"已下载 N 个包（速率）"。

    原始 http 行仍完整写进日志文件（方便排查），只是不原样刷到界面；非 http 行
    （npm warn / deprecated / error / 最后的 added N packages / 阶段信息）照常透传。
    npm 在非 TTY（输出重定向给文件）时不画进度条，这是能看到"在下载、没卡死"的办法。
    """
    state = {"n": 0, "last_t": time.monotonic(), "start": time.monotonic()}
    lock = threading.Lock()

    def on_line(line: str) -> bool:
        s = line.rstrip()
        if not _NPM_HTTP_RE.search(s):
            return False  # 非 http 行：原样透传（警告/错误/阶段/汇总）
        m = _NPM_TGZ_RE.search(s)
        if not m:
            return True  # metadata 请求之类的 http 行：太吵，吞掉
        with lock:
            state["n"] += 1
            n = state["n"]
            now = time.monotonic()
            pkg = m.group("pkg")
            if len(pkg) > 48:
                pkg = pkg[:45] + "..."
            if n == 1:
                info(col(f"{prefix}⬇ 开始下载依赖包（首个：{pkg}）…", C.GRAY))
            elif n % 20 == 0 or now - state["last_t"] >= 1.5:
                state["last_t"] = now
                rate = n / max(now - state["start"], 0.001)
                info(col(f"{prefix}⬇ 已下载 {n} 个包（约 {rate:.1f} 包/秒，最近：{pkg}）", C.GRAY))
        return True

    return on_line


def build_npm_env(cfg: dict, node: str) -> dict:
    env = os.environ.copy()
    # 关键：项目式安装里 npm 会直接拒绝来自 CLI/env 的 allow-scripts
    # （EALLOWSCRIPTS），必须删掉环境变量，改由目标 package.json 的 allowScripts 承担。
    for key in [k for k in env if k.lower().startswith("npm_config_allow_scripts")]:
        env.pop(key, None)
    node_dir = str(Path(node).parent)
    path = env.get("PATH", "")
    if node_dir and node_dir not in path:
        env["PATH"] = node_dir + os.pathsep + path
    return env


def write_lane_package_json(vdir: Path, version: str, allow_scripts: dict) -> None:
    manifest_path = vdir / "package.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    manifest.setdefault("name", f"dsh-lane-{version}")
    manifest["private"] = True
    manifest.setdefault("version", "0.0.0")
    manifest["description"] = f"DSH lane install tree for @deepseek-ai/dsh@{version}"
    manifest["allowScripts"] = dict(allow_scripts)
    vdir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def installed_version(vdir: Path) -> str | None:
    manifest_path = vdir / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
    except Exception:
        return None


def bin_js(vdir: Path) -> Path:
    return vdir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"


def npm_install_tree(cfg: dict, tree: Path, version: str, log_path: Path,
                     registries: list[str] | None = None, follow: bool = True,
                     what: str = "安装") -> str:
    """在 `tree` 里装 `PKG@version`，返回**真正装成的那个源**。

    两条规矩都来自 2026-09-28 那次失败的实测：
      1. **每次都显式给 `--registry`**：查版本用哪个源，装就用哪个源。以前不给，
         npm 就退回你 `.npmrc` 里的镜像，于是"查到 0.2.0-rc.1、镜像上却缺它的子包"→ ETARGET；
      2. 失败原因若是"这个源上没有这个版本/它的依赖"（npm 的 ETARGET / notarget），
         自动换下一个候选源重试一次 —— 官方当天发新版时镜像常常只同步一半，而失败发生在
         npm 写盘之前（日志里停在 reify:loadTrees，实测安装树没被动过），换源重试是安全的。
    """
    node = find_node(cfg)
    npm_cli = find_npm_cli(cfg, node)
    if not node or not npm_cli:
        raise RuntimeError("找不到 node 或 npm-cli.js，请检查 Node.js 安装（可用 doctor 子命令）")
    env = build_npm_env(cfg, node)
    regs = list(registries) if registries else registry_plan(cfg)
    last = ""
    for index, reg in enumerate(regs):
        cmd = [
            node,
            npm_cli,
            "install",
            "--prefix",
            str(tree),
            f"{PKG}@{version}",
            "--registry",
            reg,
            "--no-audit",
            "--no-fund",
            # http 级别才会逐包打印 fetch（非 TTY 下 npm 不画进度条）；
            # 原始行写进日志，界面端由 _npm_progress_reporter 压成"已下载 N 个包"。
            "--loglevel=http",
        ]
        info(col(f"正在{what} {PKG}@{version} → {tree}", C.BOLD))
        info(f"  下载源    : {registry_label(reg)}"
             + ("（首选）" if index == 0 else "（换源重试）"))
        if index == 0:
            info(col("  正在从 registry 下载并解包依赖（数百个包，首装约 1–3 分钟），"
                     "下面实时显示下载进度：", C.GRAY))
        info(col(f"（完整日志：{log_path}）", C.GRAY))
        code = run_logged(cmd, cwd=tree, env=env, log_path=log_path, follow=follow,
                          on_line=_npm_progress_reporter() if follow else None)
        got = installed_version(tree)
        if code == 0 and got == version:
            return reg
        last = f"npm 退出码 {code}，实际装到 {got!r}"
        tail = "\n".join(tail_lines(log_path, 80))
        nxt = regs[index + 1] if index + 1 < len(regs) else None
        if nxt and ("ETARGET" in tail or "notarget" in tail):
            warn(f"{registry_label(reg)} 上这个版本的依赖还不齐（npm ETARGET）——"
                 f"换 {registry_label(nxt)} 重试一次")
            info(col("      （这次失败发生在 npm 写盘之前，安装树没被动过）", C.GRAY))
            continue
        break
    raise RuntimeError(f"{what}失败（{last}）。完整日志：{log_path}")


def install_version(cfg: dict, version: str, follow: bool = True,
                    registries: list[str] | None = None) -> Path:
    """把指定版本装进 <root>/versions/<version>；已装且版本一致则直接复用。"""
    p = ensure_layout(cfg)
    vdir = p["versions"] / version
    existing = installed_version(vdir)
    if existing == version:
        ok(f"版本 {version} 已安装，直接复用：{vdir}")
        return vdir

    write_lane_package_json(vdir, version, cfg["allow_scripts"])
    log = p["logs"] / f"install-{version}.log"
    npm_install_tree(cfg, vdir, version, log, registries=registries, follow=follow)
    if not bin_js(vdir).is_file():
        raise RuntimeError(f"安装完成但入口缺失：{bin_js(vdir)}")
    ok(f"已安装 {PKG}@{version}")
    return vdir


# ======================= lane 管理 =======================


def preferred_ports(cfg: dict) -> set[int]:
    """全部 lane × profile 的端口偏好（接管 lane 的 3080、手动写进配置的提示）。"""
    used: set[int] = set()
    for data in cfg.get("lanes", {}).values():
        if not isinstance(data, dict):
            continue
        if data.get("port"):
            try:
                used.add(int(data["port"]))
            except (TypeError, ValueError):
                pass
        for hint in (data.get("profiles") or {}).values():
            if isinstance(hint, dict) and hint.get("port"):
                try:
                    used.add(int(hint["port"]))
                except (TypeError, ValueError):
                    pass
    return used


def pick_port(cfg: dict, exclude: set[int] | None = None) -> int:
    """挑一个空闲端口：读一次 TCP 表，再在 port_range 里找第一个既没登记也没人监听的。

    每次**现场**读表（带 0.5s 缓存）：端口偏好不再是铁定的，谁先起谁得，
    后起的自动避开——这是"端口不固定、自动避开占用"的落点。
    """
    lo, hi = cfg.get("port_range", [3080, 3129])
    used = preferred_ports(cfg)
    listening = set(tcp_listeners())
    for port in range(int(lo), int(hi) + 1):
        if exclude and port in exclude:
            continue
        if port in used or port in listening:
            continue
        return port
    raise RuntimeError(f"{lo}-{hi} 之间没有空闲端口")


def find_lane_by_version(cfg: dict, version: str) -> str | None:
    for name, lane in cfg["lanes"].items():
        if isinstance(lane, dict) and lane.get("version") == version:
            return name
    return None


# ======================= 凭据（API key）条目复制 =======================
#
# 设计说明（v0.3 起取消「主要版本自动继承」）：API key 属于哪个环境由用户自己决定，
# 启动器提供**按条目复制**——源 HOME 的 .credentials.yaml 里有哪些提供商条目
# （refs 段，如 DEEPSEEK_API_KEY），勾选其中几条复制到目标 HOME，其它一概不碰。

CRED_FILENAME = ".credentials.yaml"


def credentials_path(home) -> Path:
    return Path(home) / CRED_FILENAME


# 一条 refs 条目：`  KEY: value`。键可能是裸键 / 单引号 / 双引号。
_CRED_ENTRY_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<key>(?:[^\s:#][^:]*?)|'(?:[^']|'')*'|\"[^\"]*\")"
    r"\s*:\s*(?P<val>.*)$"
)

# 界面 / 命令里**手写**条目名时的合法形状（大写习惯，如 MY_API_KEY）。
CRED_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# YAML 双引号标量支持的转义（JSON 只覆盖其中一部分，所以自己解一遍）
_CRED_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", "\\": "\\", '"': '"', "/": "/",
    "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029", " ": " ",
}


def _cred_read_lines(path: Path) -> tuple[list[str], str]:
    """读成 (行列表, 行尾符)。不存在的文件 → ([], "\\n")。行尾符原样保留，写回不改口味。"""
    if not path.is_file():
        return [], "\n"
    text = path.read_text(encoding="utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    if lines and lines[-1] == "":
        lines.pop()
    return lines, eol


def _cred_open_quote(raw: str) -> str | None:
    """这段值是不是"引号开了还没合上"（＝后面还有续行）。返回引号字符或 None。"""
    text = raw.strip()
    if not text or text[0] not in "'\"":
        return None
    quote = text[0]
    body = text[1:]
    i = 0
    while i < len(body):
        ch = body[i]
        if quote == "'":
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    i += 2
                    continue
                return None
        else:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                return None
        i += 1
    return quote


def _cred_unescape_double(body: str) -> str:
    """双引号标量内容 → 原字符串。先试 JSON（dsh 写的值都是 JSON 形态），不行再按 YAML 转义表。"""
    try:
        return json.loads('"' + body + '"')
    except Exception:
        pass
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _CRED_ESCAPES:
                out.append(_CRED_ESCAPES[nxt])
                i += 2
                continue
            if nxt in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[nxt]
                try:
                    out.append(chr(int(body[i + 2:i + 2 + width], 16)))
                    i += 2 + width
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _cred_scalar(raw: str) -> str:
    """一段标量文本 → 字符串值（单/双引号、跨行折叠后的形态都认）。"""
    text = raw.strip()
    if not text:
        return ""
    if text[0] == "'":
        body = text[1:]
        if body.endswith("'"):
            body = body[:-1]
        return body.replace("''", "'")
    if text[0] == '"':
        body = text[1:]
        if body.endswith('"'):
            body = body[:-1]
        return _cred_unescape_double(body)
    cut = len(text)
    for match in re.finditer(r"\s#", text):
        cut = match.start()
        break
    return text[:cut].rstrip()


def _cred_key(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return _cred_scalar(text)
    return text


def _refs_section(lines: list[str]) -> tuple[int, int]:
    """`refs:` 段的行区间 [起始, 结束)；找不到 → (-1, -1)。

    只做定点文本定位：不引入 yaml 依赖，也不会动 `records:` 等其它段。
    """
    start = -1
    for i, line in enumerate(lines):
        if line.rstrip() == "refs:":
            start = i
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() and not line[:1].isspace():
            end = j          # 顶格的新段（records: … / version: …）＝ refs 结束
            break
    return start, end


def _ref_entries(lines: list[str], start: int, end: int) -> list[dict]:
    """refs 段里的条目：[{key, value, line_start, line_end}]。

    关键点：dsh 自己写长 token 时会**把一个单引号标量断成好几行**（中间还夹着 JSON 的
    逗号与空格）。按 YAML 折叠规则，行间断点折成一个空格 —— 所以这里把续行拼回一行再解。
    旧版逐行正则只取了第一行，导入时把那半行写回去 → 引号不闭合 → YAML 直接坏掉（那个 bug）。
    """
    entries: list[dict] = []
    if start < 0:
        return entries
    base: int | None = None
    i = start + 1
    while i < end:
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        match = _CRED_ENTRY_RE.match(raw)
        if not match:
            i += 1
            continue
        indent = len(match.group("indent"))
        if base is None:
            base = indent
        if indent != base:
            i += 1
            continue
        parts = [match.group("val")]
        quote = _cred_open_quote(match.group("val"))
        j = i + 1
        while quote and j < end and lines[j].strip():
            parts.append(lines[j].strip())
            quote = _cred_open_quote(" ".join(parts))
            j += 1
        entries.append({
            "key": _cred_key(match.group("key")),
            "value": _cred_scalar(" ".join(parts)),
            "line_start": i,
            "line_end": j,
        })
        i = j
    return entries


def read_credential_refs(home) -> dict[str, str]:
    """读出某个 DSH_HOME 里已存的密钥引用（refs 段，如 DEEPSEEK_API_KEY → 真值）。

    值是**解码后**的字符串：单/双引号剥掉、跨行折回一行。所以它可以直接写进界面、
    也可以原样再写回文件（写入时统一走 JSON 双引号标量，不会再半行半行地丢）。
    """
    path = credentials_path(home)
    if not path.is_file():
        return {}
    try:
        lines, _ = _cred_read_lines(path)
        start, end = _refs_section(lines)
        return {e["key"]: e["value"] for e in _ref_entries(lines, start, end)}
    except Exception:
        return {}


def credential_ref_value(home, name: str) -> str | None:
    """取一个条目的值（界面「显示」按钮用）；不存在 → None。"""
    return read_credential_refs(home).get(name)


def _cred_fmt_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_.-]+", key) else json.dumps(key, ensure_ascii=False)


def _cred_fmt_entry(key: str, value: str) -> str:
    """一条条目写成一行：值走 JSON 双引号标量（JSON 转义是 YAML 双引号转义的子集）。"""
    return f"  {_cred_fmt_key(key)}: {json.dumps(value, ensure_ascii=False)}"


def _cred_apply_lines(lines: list[str], start: int, end: int,
                      changes: dict[str, str | None]) -> list[str]:
    """按行重写 refs 段：changes 里 value=None 表示删除该条目。

    没被点名的条目**原样保留**（逐行搬，不重新序列化）—— 这是"只动该动的行"的落实处。
    """
    if start < 0:
        added = [_cred_fmt_entry(k, v) for k, v in changes.items() if v is not None]
        if not added:
            return lines
        idx = next((i + 1 for i, ln in enumerate(lines)
                    if ln.strip().startswith("version:")), 0)
        return lines[:idx] + ["refs:"] + added + lines[idx:]
    entries = {e["line_start"]: e for e in _ref_entries(lines, start, end)}
    appended = [_cred_fmt_entry(k, v) for k, v in changes.items()
                if v is not None and k not in {e["key"] for e in entries.values()}]
    out: list[str] = []
    i = 0
    total = len(lines)
    while i < total:
        if appended and i == end:
            out.extend(appended)
            appended = []
        entry = entries.get(i)
        if entry is not None:
            key = entry["key"]
            if key in changes:
                value = changes[key]
                if value is not None:
                    out.append(_cred_fmt_entry(key, value))
            else:
                out.extend(lines[i:entry["line_end"]])
            i = entry["line_end"]
            continue
        out.append(lines[i])
        i += 1
    if appended:
        out.extend(appended)
    return out


def _cred_write(path: Path, lines: list[str], eol: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(eol.join(lines) + eol, encoding="utf-8")


def _cred_backup(path: Path) -> str:
    if not path.is_file():
        return ""
    bak = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(path, bak)
        return str(bak)
    except OSError:
        return ""


def _cred_rebuild_section(lines: list[str], start: int, end: int,
                          refs: dict[str, str]) -> list[str]:
    """整个 refs 段**重写**一遍：所有条目重新序列化，段外的行一个字节不动。

    这是"修复"路径专用：文件已经坏掉时（未闭合的引号会把后续行都吞进那个标量），
    定点改行救不回来 —— 只能把 refs 段整体重铺一次干净的单行标量。
    """
    body = [_cred_fmt_entry(k, v) for k, v in refs.items()]
    if start < 0:
        idx = next((i + 1 for i, ln in enumerate(lines)
                    if ln.strip().startswith("version:")), 0)
        return lines[:idx] + ["refs:"] + body + lines[idx:]
    return lines[:start] + ["refs:"] + body + lines[end:]


def merge_credential_refs(path: Path, refs: dict[str, str],
                          rebuild: bool = False) -> list[str]:
    """把 refs 合并进凭据文件：缺则插入、有则更新，其它行一个字节不动。

    值一律写成 YAML 双引号标量。旧版把解析出来的半行原样写回，跨行 token 会被写坏；
    现在写出去的一行无论多长都合法，dsh 读回来与源值逐字相等。

    `rebuild=True`：整个 refs 段重铺（修复坏文件用），能读出来的条目会保留、
    点名的一律覆盖。返回**实际被改动**的键名列表（已经是同样值的不算）。
    """
    lines, eol = _cred_read_lines(path)
    # `version: 1` 这一行是必需的：0.1.7 的 credentials-local 会把没有它的文件判成
    # "pre-release flat layout" 并**直接拒绝加载**，然后一串等 `credentials` 服务的插件
    # 全都起不来（新建的 lane 会表现为"启动超时、没抓到 URL"）。
    # 必须**在读 refs 段之前**补这一行，否则行号会错位。
    if not any(line.strip().startswith("version:") for line in lines):
        lines.insert(0, "version: 1")
    start, end = _refs_section(lines)
    current = {e["key"]: e["value"] for e in _ref_entries(lines, start, end)}
    changes = {k: v for k, v in refs.items() if current.get(k) != v}
    if rebuild:
        merged = {**current, **refs}
        new_lines = _cred_rebuild_section(lines, start, end, merged)
        _cred_backup(path)
        _cred_write(path, new_lines, eol)
        return sorted(refs)
    if not changes:
        return []
    new_lines = _cred_apply_lines(lines, start, end, changes)
    _cred_backup(path)
    _cred_write(path, new_lines, eol)
    return [k for k in refs if k in changes]


def set_credential_ref(home, name: str, value: str) -> list[str]:
    """改/加一个条目（单条）。返回被改动的键名列表。"""
    return merge_credential_refs(credentials_path(home), {name: value})


def set_credential_refs(cfg: dict, lane: str, refs: dict[str, str],
                        verify: bool = True, force: bool = False,
                        rebuild: bool = False) -> dict:
    """写多个条目，并在写后用**宿主自己的解析器**校验一次；不过就回滚。

    为什么先拦：文件本身已经坏了的时候（就是用户遇到的那个 MISSING_CHAR），
    再往里写只会把水搅得更浑 —— 这时候要的是"重新导入/重建"，不是继续填。

    `rebuild=True` 就是那条"重建"路径：整个 refs 段重铺（能读出来的条目保留），
    所以坏文件也救得回来；照样写后校验、不过回滚。
    """
    home = lane_home(cfg, lane)
    path = credentials_path(home)
    out = {"ok": False, "path": str(path), "backup": "", "written": [],
           "verify": "skip", "verify_why": "", "error": "", "rollback": "",
           "rebuilt": bool(rebuild)}
    if path.is_file() and not force and not rebuild:
        verdict, why = judge_yaml_file(cfg, lane, path)
        if verdict == "bad":
            out["verify"], out["verify_why"] = verdict, why
            out["error"] = (f"当前凭据文件解析不过（{why}）—— 没有往里写。"
                            "先用「从别的 lane 重新导入」或手工修好，再改条目")
            return out
    try:
        out["written"] = merge_credential_refs(path, refs, rebuild=rebuild)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    bak = path.with_name(path.name + ".bak")
    out["backup"] = str(bak) if bak.is_file() else ""
    if not out["written"]:
        out["ok"] = True
        out["verify_why"] = "值本来就一致，文件一个字都没动"
        return out
    verdict, why = judge_yaml_file(cfg, lane, path) if verify else ("skip", "没做校验")
    out["verify"], out["verify_why"] = verdict, why
    if verdict == "bad":
        bak = path.with_name(path.name + ".bak")
        if bak.is_file():
            try:
                shutil.copy2(bak, path)
                out["rollback"] = f"校验不过（{why}）—— 已用 .bak 回滚"
            except OSError as exc:
                out["rollback"] = f"校验不过（{why}）—— 回滚也失败了：{exc}"
        else:
            out["rollback"] = f"校验不过（{why}）—— 没有备份可回滚"
        out["error"] = out["rollback"]
        return out
    out["ok"] = True
    return out


def delete_credential_refs(cfg: dict, lane: str, names: list[str],
                           verify: bool = True) -> dict:
    """删掉若干条目（写后用宿主解析器校验，不过就回滚）。"""
    home = lane_home(cfg, lane)
    path = credentials_path(home)
    out = {"ok": False, "path": str(path), "backup": "", "removed": [],
           "verify": "skip", "verify_why": "", "error": "", "rollback": ""}
    if not path.is_file():
        out["ok"] = True
        out["verify_why"] = "凭据文件还不存在，没有可删的条目"
        return out
    lines, eol = _cred_read_lines(path)
    start, end = _refs_section(lines)
    present = {e["key"] for e in _ref_entries(lines, start, end)}
    wanted = [n for n in names if n in present]
    if not wanted:
        out["ok"] = True
        out["verify_why"] = "这些条目本来就不在文件里"
        return out
    verdict, why = judge_yaml_file(cfg, lane, path)
    if verdict == "bad":
        out["verify"], out["verify_why"] = verdict, why
        out["error"] = (f"当前凭据文件解析不过（{why}）—— 没有动它。"
                        "先用「从别的 lane 重新导入」或手工修好，再删条目")
        return out
    new_lines = _cred_apply_lines(lines, start, end, {n: None for n in wanted})
    out["backup"] = _cred_backup(path)
    _cred_write(path, new_lines, eol)
    out["removed"] = wanted
    verdict, why = judge_yaml_file(cfg, lane, path) if verify else ("skip", "没做校验")
    out["verify"], out["verify_why"] = verdict, why
    if verdict == "bad":
        bak = path.with_name(path.name + ".bak")
        if bak.is_file():
            shutil.copy2(bak, path)
            out["rollback"] = f"校验不过（{why}）—— 已用 .bak 回滚"
        else:
            out["rollback"] = f"校验不过（{why}）—— 没有备份可回滚"
        out["error"] = out["rollback"]
        return out
    out["ok"] = True
    return out


# ======================= 自定义服务商（llm-pi-ai providers） =======================
#
# DSH 的「设置 → 模型」页允许手写一个服务商：route（小写短名）+ 协议 + baseURL +
# 模型清单 + 一个密钥引用。它把这条写进 profile 补丁层的 llm-pi-ai 条目
# （config.providers.<route>），密钥本体写进 .credentials.yaml 的 refs 段。
# 这里按**同一套契约**自己写一遍（route 正则、${ROUTE}_API_KEY 命名都抄 dsh），
# 界面因此能做到和那个页面一样；写后一样用宿主 js-yaml 校验，不过就回滚。

PKG_LLM_PI_AI = "@deepseek-ai/dsh-llm-pi-ai"
PROVIDER_ROUTE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
PROVIDER_APIS = ("openai-completions", "openai-responses", "anthropic-messages")

# route → 约定俗成的凭据条目名（与 dsh 自己的 deriveKeyRef 一字不差：
# `${route.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_API_KEY`）。
def provider_key_ref(route: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", route.upper()) + "_API_KEY"


_CRED_SAFE_PLAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./:+@,()\[\]{}-]*$")
_CRED_PLAIN_RESERVED = {"true", "false", "null", "~", "yes", "no", "on", "off"}

# 常见官方插件：补丁层里不写 apiKeyEnv 时它们用的默认条目 → 显示名
CRED_DEFAULT_SOURCES = {
    "dsh-llm-deepseek-api-key": ("DEEPSEEK_API_KEY", "DeepSeek（官方账号）"),
    "dsh-web-search-deepseek": ("DEEPSEEK_API_KEY", "联网搜索（DeepSeek）"),
}


def _yaml_scalar(text: str) -> str:
    """能裸写就裸写，否则走 JSON 双引号标量（合法 YAML）。"""
    if text and _CRED_SAFE_PLAIN_RE.match(text) and text.lower() not in _CRED_PLAIN_RESERVED:
        return text
    return json.dumps(text, ensure_ascii=False)


def validate_provider(route: str, api: str, base_url: str, models: list[dict]) -> str:
    """校验一个服务商定义；返回 "" 表示通过，否则是给人看的原因。"""
    if not PROVIDER_ROUTE_RE.match(route or ""):
        return "route 只能是小写字母/数字/连字符，且以字母开头（例：acme-gateway）"
    if api not in PROVIDER_APIS:
        return f"协议只能填 {'、'.join(PROVIDER_APIS)}"
    if not re.match(r"^https?://\S+$", (base_url or "").strip()):
        return "baseURL 必须是 http:// 或 https:// 开头的地址"
    if not models:
        return "至少要有一个模型（id 必填）"
    seen: set[str] = set()
    for index, model in enumerate(models, 1):
        mid = str(model.get("id") or "").strip()
        if not mid:
            return f"第 {index} 个模型缺 id"
        if mid in seen:
            return f"模型 id 重复：{mid}"
        seen.add(mid)
    return ""


def _patch_entry_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    """顶层条目的 (id, 起始行, 结束行)。id 取不到时是 ""。"""
    spans: list[tuple[str, int, int]] = []
    cur_start: int | None = None
    cur_id = ""
    for i, raw in enumerate(lines):
        if raw.startswith("- ") or raw.rstrip() == "-":
            if cur_start is not None:
                spans.append((cur_id, cur_start, i))
            match = ENTRY_ID_RE.match(raw.rstrip())
            cur_id = match.group(1) if match else ""
            cur_start = i
    if cur_start is not None:
        spans.append((cur_id, cur_start, len(lines)))
    return spans


def _entry_name(lines: list[str], start: int, end: int) -> str:
    for raw in lines[start:end]:
        match = re.match(r"^ {2}name:\s*(.+?)\s*$", raw)
        if match:
            return _cred_scalar(match.group(1))
    return ""


def _is_pi_ai_entry(lines: list[str], start: int, end: int, ident: str) -> bool:
    return ident == "llm-pi-ai" or _entry_name(lines, start, end) == PKG_LLM_PI_AI


def _providers_span(lines: list[str], estart: int, eend: int):
    """llm-pi-ai 条目里的 providers 段 → (providers 行, 追加点, [(route, 起, 止)]); 没有 → None。"""
    pline = -1
    for i in range(estart, eend):
        if re.match(r"^ {4}providers:\s*(#.*)?$", lines[i]):
            pline = i
            break
    if pline < 0:
        return None
    routes: list[tuple[str, int]] = []
    i = pline + 1
    while i < eend:
        raw = lines[i]
        if raw.strip() and not raw[:1].isspace():
            break
        indent = len(raw) - len(raw.lstrip())
        if raw.strip() and indent <= 4:
            break
        match = re.match(r"^ {6}([A-Za-z0-9_.-]+):\s*$", raw)
        if match:
            routes.append((match.group(1), i))
        i += 1
    insert_at = i
    spans = []
    for index, (route, lstart) in enumerate(routes):
        lend = routes[index + 1][1] if index + 1 < len(routes) else insert_at
        spans.append((route, lstart, lend))
    return pline, insert_at, spans


def _provider_lines(route: str, provider: dict) -> list[str]:
    out = [f"      {route}:"]
    if provider.get("displayName"):
        out.append(f"        displayName: {_yaml_scalar(str(provider['displayName']))}")
    if provider.get("apiKeyEnv"):
        out.append(f"        apiKeyEnv: {_yaml_scalar(str(provider['apiKeyEnv']))}")
    out.append(f"        api: {_yaml_scalar(str(provider['api']))}")
    out.append(f"        baseURL: {_yaml_scalar(str(provider['baseURL']))}")
    out.append("        models:")
    for model in provider.get("models") or []:
        mid = str(model.get("id") or "").strip()
        out.append(f"          - id: {_yaml_scalar(mid)}")
        name = str(model.get("name") or mid).strip() or mid
        out.append(f"            name: {_yaml_scalar(name)}")
        ctx = model.get("contextWindow")
        if isinstance(ctx, int) and ctx > 0:
            out.append(f"            contextWindow: {ctx}")
        max_tokens = model.get("maxTokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            out.append(f"            maxTokens: {max_tokens}")
    return out


def upsert_provider_in_patch(text: str, route: str, provider: dict,
                             overwrite: bool = False) -> tuple[bool, str, str, str]:
    """把一条服务商写进补丁层的 llm-pi-ai 条目 → (ok, why, 新文本, 动作)。

    动作：added / replaced / none（一字未改）/ refused。条目/段不存在时会照着
    dsh 自己写出来的层级（2/4/6/8 空格）补出来，不猜缩进。
    """
    lines = text.splitlines()
    listing = text.splitlines()
    target = next(((s, e) for ident, s, e in _patch_entry_spans(listing)
                   if _is_pi_ai_entry(listing, s, e, ident)), None)
    provider_lines = _provider_lines(route, provider)
    if target is None:
        entry = ["- id: llm-pi-ai", f"  name: {json.dumps(PKG_LLM_PI_AI)}", "  config:",
                 "    providers:", *provider_lines]
        # 用 append_patch_entries 而不是自己拼：它会先把 `[]` 占位符注释掉，
        # 否则"[] 后面再跟一个 - id:"＝一份文档里两个顶层元素，宿主直接报
        # "end of the stream or a document separator is expected"。
        ok_add, why_add, joined = append_patch_entries(text, entry)
        if not ok_add:
            return False, why_add, text, "refused"
        return True, "", joined, "added"
    estart, eend = target
    span = _providers_span(lines, estart, eend)
    if span is None:
        cfg_line = next((i for i in range(estart + 1, eend)
                         if re.match(r"^ {2}config:\s*(#.*)?$", lines[i])), -1)
        if cfg_line >= 0:
            at = cfg_line + 1
            while at < eend and (not lines[at].strip()
                                 or len(lines[at]) - len(lines[at].lstrip()) > 2):
                at += 1
            insert = ["    providers:", *provider_lines]
        else:
            at = eend
            insert = ["  config:", "    providers:", *provider_lines]
        new_lines = lines[:at] + insert + lines[at:]
        return True, "", "\n".join(new_lines) + "\n", "added"
    _pline, insert_at, route_spans = span
    existing = next((rs for rs in route_spans if rs[0] == route), None)
    if existing is not None:
        if not overwrite:
            return False, f"这个 profile 里已经有 route「{route}」了（要改就选覆盖）", text, "refused"
        rstart, rend = existing[1], existing[2]
        new_lines = lines[:rstart] + provider_lines + lines[rend:]
        return True, "", "\n".join(new_lines) + "\n", "replaced"
    new_lines = lines[:insert_at] + provider_lines + lines[insert_at:]
    return True, "", "\n".join(new_lines) + "\n", "added"


def lane_custom_providers(cfg: dict, lane: str, profile: str = DEFAULT_PROFILE) -> list[dict]:
    """这个 profile 补丁层里手写的服务商清单（只读）。"""
    path = plugin_patch_path(cfg, lane, profile)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ident, start, end in _patch_entry_spans(lines):
        if not _is_pi_ai_entry(lines, start, end, ident):
            continue
        span = _providers_span(lines, start, end)
        if span is None:
            continue
        for route, rstart, rend in span[2]:
            info = {"route": route, "profile": profile, "displayName": "",
                    "apiKeyEnv": "", "api": "", "baseURL": "", "models": []}
            for raw in lines[rstart:rend]:
                match = re.match(r"^ {8}(displayName|apiKeyEnv|api|baseURL):\s*(.+?)\s*$", raw)
                if match:
                    info[match.group(1)] = _cred_scalar(match.group(2))
                else:
                    mid = re.match(r"^ {10}- id:\s*(.+?)\s*$", raw)
                    if mid:
                        info["models"].append(_cred_scalar(mid.group(1)))
            out.append(info)
    return out


def _cred_sources_of_patch(path: Path, profile: str) -> list[tuple[str, dict]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    pairs: list[tuple[str, dict]] = []
    for ident, start, end in _patch_entry_spans(lines):
        name = _entry_name(lines, start, end)
        short = name.rsplit("/", 1)[-1] if name else (ident or "")
        if _is_pi_ai_entry(lines, start, end, ident):
            span = _providers_span(lines, start, end)
            for route, rstart, rend in (span[2] if span else []):
                ref = ""
                display = ""
                api = ""
                base_url = ""
                for raw in lines[rstart:rend]:
                    match = re.match(
                        r"^ {8}(displayName|apiKeyEnv|api|baseURL):\s*(.+?)\s*$", raw)
                    if match:
                        key, value = match.group(1), _cred_scalar(match.group(2))
                        if key == "apiKeyEnv":
                            ref = value
                        elif key == "displayName":
                            display = value
                        elif key == "api":
                            api = value
                        elif key == "baseURL":
                            base_url = value
                ref = ref or provider_key_ref(route)
                pairs.append((ref, {"kind": "provider", "displayName": display or route,
                                    "route": route, "profile": profile, "api": api,
                                    "baseURL": base_url}))
            continue
        ref = ""
        display = ""
        for raw in lines[start:end]:
            match = re.match(r"^\s+apiKeyEnv:\s*(.+?)\s*$", raw)
            if match and not ref:
                ref = _cred_scalar(match.group(1))
            match = re.match(r"^\s+displayName:\s*(.+?)\s*$", raw)
            if match and not display:
                display = _cred_scalar(match.group(1))
        default = CRED_DEFAULT_SOURCES.get(short)
        if not ref and default:
            ref = default[0]
        if not display and default:
            display = default[1]
        if ref:
            pairs.append((ref, {"kind": "plugin", "displayName": display or short or ref,
                                "route": "", "profile": profile, "api": "", "baseURL": ""}))
    return pairs


def lane_credential_sources(cfg: dict, lane: str) -> dict[str, list[dict]]:
    """凭据条目（refs 键）→ 谁在引用它（带 dsh 里设的显示名）。只读、失败一律降级成空。"""
    out: dict[str, list[dict]] = {}
    home = lane_home(cfg, lane)
    profiles: list[str] = []
    proot = home / "profiles"
    if proot.is_dir():
        profiles = sorted(p.name for p in proot.iterdir() if p.is_dir())
    for profile in profiles:
        try:
            pairs = _cred_sources_of_patch(plugin_patch_path(cfg, lane, profile), profile)
        except Exception:  # noqa: BLE001
            pairs = []
        for ref, item in pairs:
            out.setdefault(ref, []).append(item)
    return out


def write_custom_provider(cfg: dict, lane: str, profile: str, route: str, api: str,
                          base_url: str, models: list[dict], display_name: str = "",
                          api_key: str = "", store_key: bool = True,
                          overwrite: bool = False, verify: bool = True) -> dict:
    """新增/覆盖一个自定义服务商：写补丁层（+ 顺手把密钥写进凭据 refs），写完校验、不过回滚。"""
    out = {"ok": False, "route": route, "profile": profile, "action": "",
           "keyRef": "", "path": "", "backup": "", "verify": "skip", "verifyWhy": "",
           "credential": None, "error": "", "rollback": ""}
    why = validate_provider(route, api, base_url, models)
    if why:
        out["error"] = why
        return out
    if lane not in (cfg.get("lanes") or {}):
        out["error"] = f"没有 lane「{lane}」"
        return out
    allowed = lane_profiles_view(cfg, lane)
    if profile not in allowed:
        out["error"] = (f"profile「{profile}」不在允许范围内"
                        f"（{ '、'.join(sorted(allowed)) }）")
        return out
    path = plugin_patch_path(cfg, lane, profile)
    out["path"] = str(path)
    info = lane_profiles_view(cfg, lane).get(profile) or {}
    if not info.get("onDisk"):
        out["error"] = f"profile「{profile}」在磁盘上还没有（先启动一次让它初始化）"
        return out
    if path.is_file() and verify:
        # 补丁层的判据必须用 dsh 自己那套（JSON_SCHEMA + `!!js` 标量），
        # 否则文件里合法的 `!<tag:yaml.org,2002:js>` 表达式会被判成"解析不过"。
        home, pkg_dir, install_dir = lane_judge_paths(cfg, lane)
        verdict, why_judge = judge_patch_yaml(cfg, home, pkg_dir, path, install_dir)
        if verdict == "bad":
            out["verify"], out["verifyWhy"] = verdict, why_judge
            out["error"] = (f"补丁层现在解析不过（{why_judge}）—— 没敢往里写。"
                            "先用 `plugins <lane> --dump` 或插件总览把它修好")
            return out
    text = ensure_patch_array(path.read_text(encoding="utf-8") if path.is_file() else "")
    provider: dict = {"api": api, "baseURL": base_url.strip(),
                      "displayName": (display_name or "").strip(),
                      "models": [dict(m) for m in models]}
    key_ref = provider_key_ref(route)
    if api_key and store_key:
        provider["apiKeyEnv"] = key_ref
    elif store_key:
        # 不填 key 的"只改显示名 / 地址 / 模型"场景：别把原来绑好的密钥引用抹掉
        try:
            prior = next((p for p in lane_custom_providers(cfg, lane, profile)
                          if p.get("route") == route and p.get("apiKeyEnv")), None)
        except Exception:  # noqa: BLE001
            prior = None
        if prior:
            provider["apiKeyEnv"] = prior["apiKeyEnv"]
    ok_up, why_up, new_text, action = upsert_provider_in_patch(
        text, route, provider, overwrite)
    out["action"] = action          # refused / none 也要让调用方看得见（前端据此提示）
    if not ok_up:
        out["error"] = why_up
        return out
    out["keyRef"] = provider.get("apiKeyEnv") or "" if store_key else ""
    if action == "none":
        out["ok"] = True
        out["verifyWhy"] = "文件一个字都没动"
        return out
    backup_ok, backup_info = backup_patch_text(path, text)
    if not backup_ok:
        out["error"] = f"备份失败，没敢写：{backup_info}"
        return out
    out["backup"] = backup_info
    wrote, why_write = write_patch_text(path, new_text)
    if not wrote:
        out["error"] = f"写入失败：{why_write}"
        return out
    if verify:
        home, pkg_dir, install_dir = lane_judge_paths(cfg, lane)
        verdict, why_judge = judge_patch_yaml(cfg, home, pkg_dir, path, install_dir)
        out["verify"], out["verifyWhy"] = verdict, why_judge
        if verdict == "bad":
            rolled = False
            if Path(backup_info).is_file():
                try:
                    shutil.copyfile(backup_info, path)
                    rolled = True
                except OSError:
                    rolled = False
            out["rollback"] = (f"校验不过（{why_judge}）—— "
                               + ("已用 .bak 回滚" if rolled else "回滚失败"))
            out["error"] = out["rollback"]
            return out
    if api_key and store_key:
        cred = set_credential_refs(cfg, lane, {key_ref: api_key}, verify=verify)
        out["credential"] = {"ok": bool(cred.get("ok")), "ref": key_ref,
                             "error": cred.get("error", ""),
                             "verify": cred.get("verify", ""),
                             "verifyWhy": cred.get("verify_why", "")}
        if not cred.get("ok"):
            if Path(backup_info).is_file():     # 原子性：密钥没写上就别留半条服务商
                try:
                    shutil.copyfile(backup_info, path)
                    out["rollback"] = "凭据写入失败，补丁层已回滚"
                except OSError:
                    out["rollback"] = "凭据写入失败，补丁层回滚也没成功"
            out["error"] = cred.get("error") or "凭据写入失败"
            return out
    out["ok"] = True
    return out


def copy_credential_refs(cfg: dict, src: str, dst: str,
                         only: set[str] | None = None) -> dict:
    """把源 lane HOME 凭据里的**指定条目**复制到目标 lane HOME（只动 refs 段，不碰其它内容）。

    only=None ＝ 全部条目；否则只复制列出的键（提供商条目，如 DEEPSEEK_API_KEY）。
    返回可打印的结果说明，供 CLI / GUI 复用。
    """
    out = {
        "src": src,
        "dst": dst,
        "src_home": str(lane_home(cfg, src)),
        "dst_home": str(lane_home(cfg, dst)),
        "path": "",
        "available": [],
        "selected": [],
        "written": [],
        "unknown": [],
        "error": None,
        "skipped": None,
    }
    if src not in cfg.get("lanes", {}) or dst not in cfg.get("lanes", {}):
        out["error"] = "源或目标 lane 不存在"
        return out
    if src == dst:
        out["skipped"] = "源和目标是同一条 lane"
        return out
    src_home = lane_home(cfg, src)
    dst_home = lane_home(cfg, dst)
    try:
        if src_home.resolve() == dst_home.resolve():
            out["skipped"] = "两条 lane 用的是同一个 DSH_HOME，无需复制"
            return out
    except Exception:
        pass

    refs = read_credential_refs(src_home)
    out["available"] = sorted(refs)
    if not refs:
        out["error"] = (
            f"源 lane「{src}」的 {credentials_path(src_home)} 里没有 refs 段，没有可复制的条目"
        )
        return out
    selected = sorted(refs) if not only else [k for k in sorted(refs) if k in only]
    out["unknown"] = sorted(only - set(refs)) if only else []
    if not selected:
        out["error"] = "选中的条目在源凭据里一个都不存在"
        return out
    out["selected"] = selected
    out["path"] = str(credentials_path(dst_home))
    try:
        out["written"] = merge_credential_refs(
            credentials_path(dst_home), {k: refs[k] for k in selected}
        )
    except Exception as exc:  # noqa: BLE001 —— 单次写失败不该打断调用方
        out["error"] = f"写入失败：{type(exc).__name__}: {exc}"
    return out


def print_copy_result(res: dict) -> None:
    """把 copy_credential_refs 的结果打成控制台说明。"""
    if res.get("skipped"):
        info(f"      凭据      : 未复制 —— {res['skipped']}")
        return
    if res.get("error"):
        warn(f"      凭据      : 未复制 —— {res['error']}")
        if res.get("unknown"):
            info(col(f"                  其中源里没有的条目：{'、'.join(res['unknown'])}", C.GRAY))
        return
    extra = "" if res["written"] else "（值已一致，无需改动）"
    ok(f"      凭据      : 已从「{res['src']}」复制 {len(res['selected'])} 项到「{res['dst']}」"
       f"：{'、'.join(res['selected'])}{extra}")
    info(col(f"                  → {res['path']}", C.GRAY))


def resolve_target(cfg: dict, target: str) -> str:
    """把用户给的 lane 名或版本号统一成 lane 名；必要时自动登记一条 lane。"""
    if target in cfg["lanes"]:
        return target
    by_ver = find_lane_by_version(cfg, target)
    if by_ver:
        return by_ver
    # 是已安装的版本？自动登记
    vdir = paths(cfg)["versions"] / target
    if installed_version(vdir) == target:
        name = target
        cfg["lanes"][name] = {
            "version": target,
            "home": str(paths(cfg)["homes"] / name),
            "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "由 open 自动登记",
        }
        save_config(cfg)
        info(col(f"已自动登记 lane「{name}」（版本 {target}）", C.CYAN))
        return name
    raise KeyError(target)


def lane_install_dir(cfg: dict, lane: str) -> Path:
    """这条 lane 用哪棵安装树。

    - 常规 lane：`<root>/versions/<版本>`（npm --prefix 前缀目录，本启动器装的）
    - 接管的 lane（`installDir` 字段）：**dsh 包目录本身**（如
      `%APPDATA%\\npm\\node_modules\\@deepseek-ai\\dsh`），也就是 find_installs 报出来的那个路径。
      两种写法由 lane_entry_script() 统一解析。
    """
    lane_data = cfg["lanes"][lane]
    if lane_data.get("installDir"):
        return Path(lane_data["installDir"])
    return paths(cfg)["versions"] / str(lane_data["version"])


def lane_entry_script(cfg: dict, lane: str) -> Path:
    """兼容两种安装树写法，返回 `lib/bin.js` 的实际路径。"""
    base = lane_install_dir(cfg, lane)
    direct = base / "lib" / "bin.js"          # 接管型：base 就是 dsh 包目录
    if direct.is_file():
        return direct
    return bin_js(base)                        # 常规型：base 是 --prefix 前缀目录


def require_version_installed(cfg: dict, lane: str) -> tuple[str, Path, Path]:
    lane_data = cfg["lanes"][lane]
    version = str(lane_data["version"])
    vdir = lane_install_dir(cfg, lane)
    entry = lane_entry_script(cfg, lane)
    if not entry.is_file():
        if lane_data.get("installDir"):
            raise RuntimeError(
                f"lane「{lane}」指向的安装树里没有入口：{entry}\n"
                f"    （installDir = {vdir}）"
            )
        raise RuntimeError(
            f"lane「{lane}」的版本 {version} 尚未安装。先执行：\n"
            f"    py {ME_NAME} create {lane} {version}"
        )
    if not lane_data.get("installDir") and installed_version(vdir) != version:
        raise RuntimeError(
            f"lane「{lane}」的安装树版本与登记不符（登记 {version}，实际 {installed_version(vdir)}）"
        )
    home = Path(lane_data.get("home") or (paths(cfg)["homes"] / lane))
    return version, vdir, home


# ======================= 子命令 =======================


def cmd_doctor(cfg: dict, args) -> int:
    info(col("── 环境自检 ──", C.BOLD))
    node = find_node(cfg)
    info(f"  node        : {node or '未找到'}")
    if node:
        try:
            out = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=20,
                                 creationflags=_no_window_flags()).stdout.strip()
            ok(f"node 可执行（{out}）")
        except Exception as exc:
            fail(f"node 无法执行：{exc}")
    npm_cli = find_npm_cli(cfg, node)
    info(f"  npm-cli.js  : {npm_cli or '未找到'}")
    if node and npm_cli:
        try:
            out = subprocess.run(
                [node, npm_cli, "-v"], capture_output=True, text=True, timeout=30,
                creationflags=_no_window_flags(),
            ).stdout.strip()
            ok(f"npm 可执行（{out}）")
        except Exception as exc:
            fail(f"npm 无法执行：{exc}")
    info(f"  python      : {sys.version.split()[0]}")
    p = paths(cfg)
    info(f"  lane root   : {p['root']}")
    try:
        ensure_layout(cfg)
        probe = p["root"] / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok("lane root 可写")
    except Exception as exc:
        fail(f"lane root 不可写：{exc}")
    info(f"  默认工作区  : {cfg.get('default_cwd')}")
    info(f"  端口范围    : {cfg['port_range'][0]}-{cfg['port_range'][1]}")
    try:
        plan = registry_plan(cfg)
        extra = "" if len(plan) == 1 else f"（首选；装不上会退到 {registry_label(plan[1])}）"
        info(f"  下载源      : {registry_label(plan[0])}{extra}"
             f"   [lanes.json registry={cfg.get('registry')!r}]")
    except ValueError as exc:
        fail(f"下载源配置有问题：{exc}")
    try:
        regs = registry_plan(cfg, getattr(args, "registry", None))
        packument, reg = fetch_index(cfg, registries=regs)
        tags = ", ".join(f"{k}={v}" for k, v in sorted(packument.get("dist-tags", {}).items()))
        ok(f"registry 可达（{registry_label(reg)}）："
           f"{len(packument.get('versions', {}))} 个版本 / {tags}")
    except Exception as exc:
        fail(f"registry 不可达：{exc}")
    if CONFIG_PATH.exists():
        ok(f"配置文件：{CONFIG_PATH}")
    else:
        info(col(f"  配置文件    : 未创建（首次 create 时自动生成 {CONFIG_PATH}）", C.GRAY))
    return 0


def cmd_versions(cfg: dict, args) -> int:
    try:
        regs = registry_plan(cfg, getattr(args, "registry", None))
    except ValueError as exc:
        fail(str(exc))
        return 1
    packument, reg = fetch_index(cfg, registries=regs)
    tags = packument.get("dist-tags", {})
    versions = sorted_versions(packument)
    times = packument.get("time", {})
    info(col(f"── npm 上的 {PKG}（来源 {registry_label(reg)}）──", C.BOLD))
    info(col("  dist-tags：", C.CYAN) + ", ".join(f"{k}={v}" for k, v in sorted(tags.items())))
    info("")
    tag_of = {}
    for tag, ver in tags.items():
        tag_of.setdefault(ver, []).append(tag)
    limit = len(versions) if args.all else min(15, len(versions))
    shown = versions[-limit:]
    info(f"  {'版本':<16}{'标签':<12}{'发布日期':<14}本地")
    info("  " + "─" * 54)
    for v in reversed(shown):
        installed = installed_version(paths(cfg)["versions"] / v) == v
        date = (times.get(v) or "")[:10]
        info(f"  {v:<16}{','.join(tag_of.get(v, [])) or '-':<12}{date:<14}"
             + (col("已装", C.GREEN) if installed else col("-", C.GRAY)))
    if not args.all and len(versions) > limit:
        info(col(f"  … 共 {len(versions)} 个版本，加 --all 看全部", C.GRAY))
    return 0


def cmd_create(cfg: dict, args) -> int:
    p = ensure_layout(cfg)
    lane = args.lane
    choice = getattr(args, "registry", None)
    try:
        regs = registry_plan(cfg, choice)
    except ValueError as exc:
        fail(str(exc))
        return 1
    try:
        packument, reg = fetch_index(cfg, registries=regs)
        version = resolve_tag(packument, args.version)
    except Exception as exc:
        fail(f"无法解析版本 {args.version!r}：{exc}")
        return 1
    if version != args.version:
        info(col(f"  {args.version} → {version}", C.GRAY))
    if lane in cfg["lanes"] and not args.force:
        old = cfg["lanes"][lane]
        if old.get("version") != version:
            fail(
                f"lane「{lane}」已存在，版本 {old.get('version')}。"
                f"要改写请加 --force，或换个 lane 名。"
            )
            return 1
    vdir = install_version(cfg, version, follow=args.follow,
                           registries=registry_plan_after(reg, cfg, choice))
    home = paths(cfg)["homes"] / lane
    home.mkdir(parents=True, exist_ok=True)
    keep = cfg["lanes"].get(lane) if isinstance(cfg["lanes"].get(lane), dict) else {}
    cfg["lanes"][lane] = {
        "version": version,
        "home": str(home),
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if isinstance(keep.get("profiles"), dict):
        cfg["lanes"][lane]["profiles"] = keep["profiles"]
    save_config(cfg)

    info("")
    ok(f"lane「{lane}」就绪")
    info(f"      版本      : {version}")
    info(f"      下载源    : {registry_label(reg)}")
    info(f"      安装目录  : {vdir}")
    info(f"      DSH_HOME : {home}")
    info(col("      端口      : 启动时实时挑空闲端口（open --port N 可现场指定）", C.GRAY))
    info(col("      凭据      : 新 HOME 是空的。用 copy-key（或界面「凭据…」）"
             "从其它版本按条目复制 API key", C.GRAY))
    info("")
    info(col(f"  下一步： py {ME_NAME} open {lane}", C.CYAN))
    return 0


URL_RE = re.compile(r"dsh web:\s*(\S+)")


def parse_open_target(cfg: dict, target: str, profile_arg: str | None) -> tuple[str, str]:
    """把 `lane` / `lane/profile` / `版本`（＋可选 --profile）统一成 (lane, profile)。"""
    lane_part, sep, prof_part = target.partition("/")
    lane = resolve_target(cfg, lane_part)
    profile = (profile_arg or "").strip() or ((prof_part.strip() if sep else "") or DEFAULT_PROFILE)
    try:
        validate_profile_name(profile)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    return lane, profile


def detect_pinned_webserver_port(cfg: dict, lane: str,
                                 profile: str = DEFAULT_PROFILE) -> tuple[int, str, list[str]] | None:
    """补丁层里有没有把 webserver 端口**钉死**成字面量？返回 (端口, 来源文件, config 原文行)。

    实测场景（2026-09-30，global 及其副本）：web profile 的用户层里有一段工具写入的
    `webserver` 覆盖行，`config.port: 3080` 是字面量。用户层在组合顺序里压过 bundle 层的
    `port: !!js ctx.webStartup.port ?? 3080`，于是命令行 `--port` 被**静默忽略**——
    复制出来的环境端口不受控，两条 lane 还会抢同一个端口。两个补丁层都查（HOME 级的
    在组合顺序里更靠后、说了算），找到的以 HOME 级为准。
    """
    home = lane_home(cfg, lane)
    found = None
    for path in (profile_dir(home, profile) / PATCH_LAYER_FILE, Path(home) / PATCH_LAYER_FILE):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = _extract_webserver_row(text)
        if parsed:
            found = (parsed["port"], str(path), parsed["lines"])
    return found


def _extract_webserver_row(text: str) -> dict | None:
    """在补丁层文本里找 `- id: webserver` 条目及其 config 块（含字面量 port 才算钉住）。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^-\s*id:\s*['\"]?webserver['\"]?\s*(#.*)?$", line):
            continue
        indent = len(line) - len(line.lstrip())
        block: list[str] = []
        for nxt in lines[index + 1:]:
            if nxt.strip() and not nxt.lstrip().startswith("#"):
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent:
                    break
            block.append(nxt)
        port = None
        for raw in block:
            m = re.match(r"^\s*port:\s*['\"]?(\d+)['\"]?\s*(?:#.*)?$", raw)
            if m:
                port = int(m.group(1))
                break
        if port is not None:
            return {"port": port, "lines": block}
    return None


def write_port_override(cfg: dict, lane: str, profile: str,
                        pinned_rows: list[str], port: int) -> Path:
    """写一个 `--patch` 临时覆盖层，把 webserver 的 port 按本次启动的要求改掉。

    `--patch` 在组合顺序里排在两个用户层之后，所以能压过钉死的端口。config 其它键
    （host / compression …）从钉住的那行原样抄过来，只换 port——这样无论宿主的
    config 合并语义是按键合并还是整块替换，结果都一致。
    """
    path = paths(cfg)["logs"] / f".port-override-{lane}__{profile}.yml"
    body: list[str] = []
    for raw in pinned_rows:
        # 只抄 config 的键值行：`name:` 和 `config:` 这两行不带（config: 由下面统一补），
        # 否则会拼出 "config 里套 name / 两个 config:" 的坏 YAML
        if re.match(r"^\s*name:\s", raw) or re.match(r"^\s*config:\s*$", raw):
            continue
        m = re.match(r"^(\s*)port:\s*['\"]?\d+['\"]?\s*(?:#.*)?$", raw)
        body.append(f"{m.group(1)}port: {port}" if m else raw)
    text = (
        f"# dsh-lanes 临时覆盖（启动器自动生成，本次启动把 webserver 端口定为 {port}）\n"
        f"# 原因：你的补丁层把端口钉成了字面量，命令行 --port 会被它压住。\n"
        f"# 不想被覆盖：把启动端口填成钉住的值，或删掉补丁层里 webserver 行的 port。\n"
        "- id: webserver\n"
        "  config:\n" + "\n".join(body) + "\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def cmd_open(cfg: dict, args) -> int:
    try:
        lane, profile = parse_open_target(cfg, args.target, getattr(args, "profile", None))
    except KeyError:
        fail(f"找不到 lane 或已安装版本「{args.target.partition('/')[0]}」。先跑 versions / create。")
        return 1
    except ValueError as exc:
        fail(str(exc))
        return 1
    try:
        version, vdir, home = require_version_installed(cfg, lane)
    except RuntimeError as exc:
        fail(str(exc))
        return 1

    running = lane_runtime(cfg, lane, profile)
    if running:
        url = running.get("url") or f"http://127.0.0.1:{running.get('port')}"
        warn(f"lane「{lane}」的 profile「{profile}」已在运行"
             f"（pid {running.get('pid')}，端口 {running.get('port')}）")
        info(col(f"  {url}", C.CYAN))
        if cfg.get("open_browser", True) and not args.no_browser:
            webbrowser.open(url)
        return 0

    node = find_node(cfg)
    if not node:
        fail("找不到 node")
        return 1

    # ── 端口：不固定，启动时现场定 ──
    # 优先级：--port ＞ profile 的端口偏好（接管 lane 靠它认回 3080）＞ 实时挑空闲。
    # 偏好的端口被占用时**自动改口**（端口本来就是每启动一次现场挑的，不值得为它失败）；
    # 唯一的例外：默认 profile（web）+ 端口上是个 DSH 实例 + 没有别的运行态认领它
    # ——那多半是你用老方式启动的那一套，"接管"它而不是绕开它。
    #
    # 补丁层钉死端口的例外：有的工具会在补丁层里把 webserver 端口写死（用户层压过
    # bundle 层，命令行 --port 被静默忽略）。没指定端口时把钉住的值当第一偏好：
    # 空闲就顺着它用（兼容写这块配置的工具）；被占用就走下面的接管/改口逻辑，
    # 最终端口和钉住值不一致时，再写一个 --patch 覆盖层让本次启动说了算。
    try:
        pin = detect_pinned_webserver_port(cfg, lane, profile)
    except Exception:  # noqa: BLE001 —— 探测失败就当没钉住，不影响启动
        pin = None
    explicit_port = getattr(args, "port", None)
    pref = explicit_port or profile_hint_port(cfg, lane, profile)
    if pin and not explicit_port:
        pref = pref or pin[0]

    port: int | None = None
    if pref:
        pref = int(pref)
        holder = listening_pid(pref)
        if holder == 0:
            port = pref
        else:
            adoptable = (
                profile == DEFAULT_PROFILE
                and pid_alive(holder)
                and probe_dsh_like(pref)
                and not port_claimed_by_other(cfg, lane, profile, pref)
            )
            if adoptable:
                pid = holder
                started = process_start_time(pid) or time.strftime("%Y-%m-%d %H:%M:%S")
                url = f"http://127.0.0.1:{pref}"
                write_run(
                    cfg,
                    lane,
                    profile,
                    {
                        "version": version,
                        "port": pref,
                        "pid": pid,
                        "url": url,
                        "adopted": True,
                        "installedAt": cfg["lanes"][lane].get("installDir", ""),
                        "procStartedAt": started,
                        "startedAt": started,
                        "note": "接管了原本就在这个端口上运行的实例（不是本启动器启动的）",
                    },
                )
                info("")
                warn(f"端口 {pref} 上已经有一个 DSH 实例在跑（pid {pid}，启动于 {started}）")
                ok("已接管它，不会再起第二个进程；运行态与停止按钮都归本启动器管了")
                info(col(f"  {url}", C.BOLD + C.CYAN))
                if getattr(args, "adopt_only", False):
                    return 0
                if cfg.get("open_browser", True) and not args.no_browser:
                    # 这个实例的 token 只存在于它自己的 stdout 里，我们拿不到，
                    # 只能用裸地址——浏览器里若已有它的登录 cookie 就能直接进。
                    webbrowser.open(url)
                return 0
            image = process_image(holder) or "未知进程"
            warn(f"端口 {pref} 已被占用（pid {holder}，{Path(image).name}）——自动改用其它空闲端口")
            try:
                port = pick_port(cfg, exclude={pref})
            except RuntimeError as exc:
                fail(str(exc))
                return 1
    else:
        try:
            port = pick_port(cfg)
        except RuntimeError as exc:
            fail(str(exc))
            return 1

    # 最终端口和钉住值不一致 → 写临时覆盖层（--patch 在组合顺序里排在两个用户层之后）
    patch_args: list[str] = []
    if pin and port is not None and port != pin[0]:
        try:
            overlay = write_port_override(cfg, lane, profile, pin[2], int(port))
            patch_args = ["--patch", str(overlay)]
            warn(f"补丁层把 webserver 端口钉在 {pin[0]}（{pin[1]}）——本次启动已覆盖为 {port}")
            warn("  （想一直用钉住的端口：启动时把端口填成它；或删掉补丁层里 webserver 行的 port）")
        except OSError as exc:
            warn(f"端口覆盖层写不出来（{exc}）：本次启动可能仍落在钉住的 {pin[0]} 上")

    entry = lane_entry_script(cfg, lane)
    if not entry.is_file():
        fail(f"入口缺失：{entry}（建议重新 create，或用 adopt 指向正确的安装树）")
        return 1

    env = os.environ.copy()
    env["DSH_HOME"] = str(home)
    env.pop("DSH_WEB_URL", None)  # 避免继承别的实例的地址
    cwd = Path(args.cwd) if args.cwd else Path(cfg.get("default_cwd") or Path.home())
    if not cwd.is_dir():
        warn(f"工作区 {cwd} 不存在，改用 {Path.home()}")
        cwd = Path.home()

    p = ensure_layout(cfg)
    log = p["logs"] / f"{lane}__{profile}.log"
    # `dsh <名>` 就是 `dsh --profile <名>` 的缩写（bin.js 的展开规则）；这里写全称。
    # 启动器自己的旗标（--profile / --patch）必须排在应用参数（--port / --no-open）
    # 之前——pass-through 从第一个未知旗标开始，顺序错了 --patch 会被塞给 web 应用。
    cmd = [node, str(entry), "--profile", profile, *patch_args,
           "--port", str(port), "--no-open"]

    info(col(f"── 启动 lane「{lane}」· profile「{profile}」──", C.BOLD))
    info(f"      版本      : {version}")
    info(f"      DSH_HOME : {home}")
    info(f"      工作区    : {cwd}")
    info(f"      端口      : {port}" + (f"（偏好的 {pref} 被占，已自动改口）" if pref and port != pref else ""))
    info(f"      日志      : {log}")
    info("")

    url_box: dict[str, str] = {}
    pattern = URL_RE

    def on_line(line: str) -> None:
        m = pattern.search(line)
        if m and "url" not in url_box:
            url_box["url"] = m.group(1).strip()

    stop = threading.Event()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(cmd)} (cwd={cwd})\n")
        logf.flush()
        offset = logf.tell()  # 只认本次启动之后写的行（旧 token 不可用）
        thread = follow_file(log, stop, on_line, start_offset=offset)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=_new_group_flags(),
        )
        write_run(
            cfg,
            lane,
            profile,
            {
                "version": version,
                "port": port,
                "pid": proc.pid,
                "url": "",
                "cwd": str(cwd),
                "installDir": str(vdir),
                "procStartedAt": process_start_time(proc.pid),
                "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        # 就绪判据 = 从日志抓到 `dsh web: <带 token 的 URL>`。
        # 不能只看端口通：webserver 绑定早于 URL 打印，用裸地址访问会 401。
        deadline = time.time() + int(args.timeout or cfg.get("startup_timeout", 240))
        while time.time() < deadline and proc.poll() is None and "url" not in url_box:
            time.sleep(0.3)

        url = url_box.get("url")
        if url:
            # 按浏览器的方式确认服务真的可用：token → cookie → 200
            remaining = max(10.0, deadline - time.time())
            info(col("  正在验证登录握手（token → cookie）…", C.GRAY))
            good, detail = wait_authenticated(url, timeout=min(45.0, remaining))
            if not good:
                fail(f"服务起来了但认证握手失败（{detail}）：URL 里的 token 该进程不认。")
                fail(f"日志尾部（完整日志：{log}）：")
                for line in tail_lines(log, 15):
                    info(col("    " + line.rstrip(), C.GRAY))
                stop.set()
                thread.join(timeout=2)
                if proc.poll() is None:
                    kill_tree(proc.pid)
                clear_run(cfg, lane, profile)
                return 1
            data = read_run(cfg, lane, profile) or {}
            data["url"] = url
            # 以 URL 为准校正端口：万一宿主没按请求的端口监听（例如旧版组合里有
            # 覆盖层压住了 --port），运行态记录的必须是真的，否则停止/认回都会找错
            m_port = re.match(r"https?://[^/:]+:(\d+)(?:/|$)", url)
            if m_port and int(m_port.group(1)) != int(port):
                warn(f"实际监听端口是 {m_port.group(1)}，不是请求的 {port}——运行态按实际端口记录")
                data["port"] = int(m_port.group(1))
            write_run(cfg, lane, profile, data)
            info("")
            ok(f"lane「{lane}」的 profile「{profile}」已启动并验证通过（{detail}）")
            info(col(f"  {url}", C.BOLD + C.CYAN))
            if cfg.get("open_browser", True) and not args.no_browser:
                webbrowser.open(url)
        else:
            code = proc.poll()
            fail(f"启动超时或进程已退出（退出码 {code}）：没抓到 dsh 打印的 URL 行")
            fail(f"日志尾部（完整日志：{log}）：")
            for line in tail_lines(log, 15):
                info(col("    " + line.rstrip(), C.GRAY))
            stop.set()
            thread.join(timeout=2)
            if proc.poll() is None:
                kill_tree(proc.pid)
            clear_run(cfg, lane, profile)
            return 1

        if args.detach:
            info(col("  已转入后台（--detach）。停止： "
                     f"py {ME_NAME} stop {lane}/{profile}", C.GRAY))
            stop.set()
            thread.join(timeout=2)
            return 0

        info(col(f"  按 Ctrl+C 停止该 profile（{lane}/{profile}）。", C.GRAY))
        try:
            while proc.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            info("")
            warn("收到停止指令 (Ctrl+C)")
        finally:
            stop.set()
            thread.join(timeout=2)
            if proc.poll() is None:
                kill_tree(proc.pid)
            clear_run(cfg, lane, profile)
            ok("profile 已停止")
    return 0


def cmd_instances(cfg: dict, args) -> int:
    """列出本机上所有 DSH 实例，含不是本启动器启动的那些（例如全局安装的 3080）。"""
    info(col("── 未登记的实例（不是本启动器启动的）──", C.BOLD))
    found = discover_instances(cfg)
    if not found:
        info(col("  （没有发现。你在用的 3080 若确实在跑，说明进程枚举被限制了。）", C.GRAY))
    else:
        info(f"  {'端口':<7}{'PID':<9}{'进程':<16}{'启动时间':<21}特征")
        info("  " + "─" * 78)
        for item in found:
            feature = "像是 DSH 实例" if item["dsh_like"] else "端口被占用（不一定是 DSH）"
            info(
                f"  {item['port']:<7}{item['pid']:<9}{item['exe']:<16}"
                f"{item.get('started') or '?':<21}{feature}"
            )
            info(col(f"          {item['url']}", C.CYAN))

    info("")
    info(col("── 本机各套安装（到底是哪一份在跑）──", C.BOLD))
    installs = find_installs(cfg)
    if not installs:
        info(col("  （没找到任何 @deepseek-ai/dsh 安装树）", C.GRAY))
    else:
        info(f"  {'来源':<16}{'版本':<16}{'写入时间':<18}路径")
        info("  " + "─" * 96)
        for item in installs:
            info(
                f"  {item['source']:<16}{item['version']:<16}"
                f"{item.get('installedAt') or '?':<18}{item['path']}"
            )
    return 0


def cmd_adopt(cfg: dict, args) -> int:
    """把本机已有的一份安装（默认 npm 全局那份）注册成 lane，并沿用它的 DSH_HOME。

    这样就能用同一个启动器"打开 / 停止"你平时在用的那套，而不必再装一份。
    """
    lane = args.lane
    ensure_layout(cfg)
    if lane in cfg.get("lanes", {}) and not args.force:
        fail(f"lane「{lane}」已存在。要覆盖请加 --force。")
        return 1

    installs = find_installs(cfg)
    target = None
    if args.install:
        want = str(Path(args.install).resolve()).lower()
        target = next((i for i in installs if str(Path(i["path"]).resolve()).lower() == want), None)
        if target is None:
            manifest = Path(args.install) / "package.json"
            data = {}
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            if data.get("name") == PKG:
                target = {
                    "version": data.get("version", "?"),
                    "path": str(Path(args.install)),
                    "source": "指定路径",
                    "installedAt": "",
                }
        if target is None:
            fail(f"{args.install} 不是有效的 {PKG} 安装树（目录下要有 package.json）")
            return 1
    else:
        target = next((i for i in installs if i["source"] == "npm 全局"), None)
        if target is None:
            fail("找不到 npm 全局安装。用 --install <路径> 指定，或先跑 instances 看有哪些安装树")
            return 1

    home = (
        Path(args.home).expanduser()
        if args.home
        else Path(os.environ.get("USERPROFILE") or Path.home()) / ".dsh"
    )
    port = int(args.port) if args.port else 0
    if not port:
        port = 3080
        if port_in_use(port) and not probe_dsh_like(port):
            port = pick_port(cfg)
    cfg["lanes"][lane] = {
        "version": target["version"],
        "port": port,
        "home": str(home),
        "installDir": target["path"],
        "kind": "adopted",
        "source": target["source"],
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 端口记成默认 profile（web）的偏好：下次 open 先试它，好把已在跑的那套接管回来；
        # 被占且不是 DSH 时会自动改口，不会卡死
        "profiles": {DEFAULT_PROFILE: {"port": port, "createdAt": time.strftime("%Y-%m-%d %H:%M:%S")}},
    }
    save_config(cfg)
    info("")
    ok(f"已把「{target['source']}」注册为 lane「{lane}」")
    info(f"      版本      : {target['version']}")
    info(f"      安装目录  : {target['path']}")
    info(f"      DSH_HOME : {home}")
    info(f"      默认端口  : {port}（web profile 的偏好；被占用会自动改口）")
    info(col("      凭据      : 用它现有的 HOME，里面已有你的 API key；", C.GRAY))
    info(col("                需要复制给别的 lane 时用 copy-key（或界面「凭据…」）按条目复制", C.GRAY))
    warn("这条 lane 用的是你现有的 DSH_HOME（含已有会话 / 设置 / 插件），不是隔离 home。")
    profs = disk_profiles(home)
    if profs:
        info(col(f"      profiles  : HOME 里现成的环境：{'、'.join(profs)}"
                 f"（open {lane}/<名> 直接用，或 profile-add 再建）", C.GRAY))
    info("")
    info(
        col(
            f"  下一步： py {ME_NAME} open {lane}"
            "   （若该端口已有实例在跑，会自动接管它而不会再起一个）",
            C.CYAN,
        )
    )
    return 0


def cmd_list(cfg: dict, args) -> int:
    lanes = cfg.get("lanes", {})
    info(col("── lanes ──", C.BOLD))
    if not lanes:
        info(col("  （还没有 lane。用 create 建一条，例如：create next 0.1.7-rc.2）", C.GRAY))
        return 0
    info(f"  {'lane':<18}{'版本':<16}{'profile':<14}{'状态':<18}端口")
    info("  " + "─" * 84)
    for name, lane in lanes.items():
        if not isinstance(lane, dict):
            continue
        ver = lane.get("version", "?")
        states = lane_states(cfg).get(name) or {}
        view = lane_profiles_view(cfg, name)
        order = [DEFAULT_PROFILE] + sorted(p for p in view if p != DEFAULT_PROFILE)
        order = [p for p in order if p in view]
        for index, prof in enumerate(order):
            rt = states.get(prof)
            head = f"  {name:<18}{ver:<16}" if index == 0 else "  " + " " * 35
            prof_cell = f"{prof:<14}" if index == 0 else f"· {prof:<12}"
            if rt:
                state = col(f"运行中 {rt.get('pid')}", C.GREEN)
                port_text = f"{rt.get('port')}  {rt.get('url') or ''}"
            else:
                state = col("已停止", C.GRAY)
                hint = view[prof].get("port")
                port_text = f"偏好 {hint}" if hint else "自动挑空闲"
            info(f"{head}{prof_cell}{state:<28}{port_text}")
        if not view:
            info(f"  {name:<12}{ver:<16}{col('（还没有 profile，open 时会自动建 web）', C.GRAY)}")
    info("")
    info(col("  端口不固定：每次启动实时挑空闲端口；偏好端口被占用会自动改口。", C.GRAY))
    return 0


def _stop_pair(cfg: dict, lane: str, profile: str) -> bool:
    """停掉一个 (lane, profile)。返回是否真的停了东西。"""
    rt = read_run(cfg, lane, profile)
    pid = int(rt.get("pid", 0)) if rt else 0
    # 没有运行记录时，按这个 profile 的端口偏好把它找回来
    # （典型场景：用老方式启动的实例，或运行态文件被清过）
    if not (pid and pid_alive(pid)):
        port = profile_hint_port(cfg, lane, profile) or 0
        found = tcp_listeners().get(port, 0) if port else 0
        if found and pid_alive(found):
            if probe_dsh_like(port) or re.search(
                r"node|dsh|harness", process_image(found), re.I
            ):
                info(f"「{lane}/{profile}」没有运行记录，但端口 {port} 上有进程 {found}，按端口停止它")
                pid = found
            else:
                warn(f"端口 {port} 被一个不像 DSH 的进程占用（pid {found}），不擅自结束它")
    label = f"{lane}/{profile}"
    if pid and pid_alive(pid):
        started = process_start_time(pid)
        info(f"正在停止 {label} (pid {pid}{'，启动于 ' + started if started else ''}) …")
        kill_tree(pid)
        time.sleep(0.6)
        if pid_alive(pid):
            warn(f"{label} 的进程 {pid} 仍存活，请手动检查")
        else:
            ok(f"{label} 已停止")
        clear_run(cfg, lane, profile)
        return True
    if rt or profile_hint_port(cfg, lane, profile):
        warn(f"{label} 没有正在运行的进程")
    clear_run(cfg, lane, profile)
    return False


def cmd_stop(cfg: dict, args) -> int:
    """停止目标：缺省停全部；`stop lane` 停这条 lane 的全部 profile；`stop lane/profile` 停单个。"""
    targets = args.target or []
    if isinstance(targets, str):
        targets = [targets]
    pairs: list[tuple[str, str]] = []
    if not targets:
        for rec in all_run_records(cfg):
            key = (str(rec.get("lane")), str(rec.get("profile") or DEFAULT_PROFILE))
            if key not in pairs:
                pairs.append(key)
        for lane in cfg.get("lanes", {}):
            if not isinstance(cfg.get("lanes", {}).get(lane), dict):
                continue
            for prof in lane_profiles_view(cfg, lane):
                if (lane, prof) not in pairs:
                    pairs.append((lane, prof))
    else:
        for target in targets:
            lane_part, sep, prof_part = target.partition("/")
            if lane_part not in cfg.get("lanes", {}):
                try:  # 允许用版本号指 lane（与 open 一致）
                    lane_part = resolve_target(cfg, lane_part)
                except KeyError:
                    warn(f"没有 lane「{lane_part}」，跳过")
                    continue
            if sep and prof_part:
                try:
                    validate_profile_name(prof_part)
                except ValueError as exc:
                    warn(str(exc))
                    continue
                pairs.append((lane_part, prof_part))
            else:
                pairs.extend((lane_part, prof) for prof in lane_profiles_view(cfg, lane_part))
    any_stopped = False
    for lane, prof in pairs:
        if _stop_pair(cfg, lane, prof):
            any_stopped = True
    return 0 if any_stopped or not targets else 1


def cmd_remove(cfg: dict, args) -> int:
    """从登记表里移除一条 lane（不动安装树与 DSH_HOME）。"""
    lane = args.lane
    data = cfg.get("lanes", {}).get(lane)
    if not isinstance(data, dict):
        fail(f"没有 lane「{lane}」")
        return 1
    rt = lane_runtime(cfg, lane)
    if rt and not args.force:
        fail(f"lane「{lane}」正在运行（pid {rt.get('pid')}）。先 stop 它，或加 --force 只注销登记。")
        return 1
    del cfg["lanes"][lane]
    save_config(cfg)
    clear_run(cfg, lane)
    ok(f"已从登记表移除 lane「{lane}」")
    if data.get("installDir"):
        info("（接管型：只删了登记，它的安装树与 DSH_HOME 原样保留，随时可用 adopt 再加回来）")
    else:
        info(f"（安装树还在：{paths(cfg)['versions'] / str(data.get('version'))}；要省空间自行删目录）")
    return 0


def cmd_profiles(cfg: dict, args) -> int:
    """列出一条 lane 的全部 profile（磁盘真值 ∪ 登记提示）与各自运行状态。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    view = lane_profiles_view(cfg, lane)
    home = lane_home(cfg, lane)
    info(col(f"── lane「{lane}」的 profiles ──", C.BOLD))
    info(f"  DSH_HOME : {home}")
    info(col("  会话 / 凭据 / 设置是 HOME 级共享的；每个 profile 有自己的插件组合与补丁层", C.GRAY))
    if not view:
        info(col("  （还没有 profile。open 会自动初始化默认的 web；或用 profile-add 建）", C.GRAY))
        return 0
    states = lane_states(cfg).get(lane) or {}
    order = [DEFAULT_PROFILE] + sorted(name for name in view if name != DEFAULT_PROFILE)
    order = [name for name in order if name in view]
    for prof in order:
        info_col = view[prof]
        rt = states.get(prof)
        tags = []
        if prof == DEFAULT_PROFILE:
            tags.append("默认")
        if not info_col["onDisk"]:
            tags.append("尚未初始化（首次 open 时自动建）")
        if not info_col["registered"]:
            tags.append("磁盘上有，未登记")
        if rt:
            state = col(f"运行中 pid {rt.get('pid')}　端口 {rt.get('port')}", C.GREEN)
            url = rt.get("url") or ""
            if url:
                state += col(f"　{url}", C.CYAN)
        else:
            state = col("已停止", C.GRAY)
        tail = f"　（{'；'.join(tags)}）" if tags else ""
        info(f"  · {prof:<16}{state}{tail}")
    me = ME_NAME
    info("")
    info(col(f"  启动指定 profile： py {me} open {lane}/<名> [--port N]", C.CYAN))
    info(col(f"  新建（复制现有）： py {me} profile-add {lane} <新名> --from <现有>", C.CYAN))
    info(col(f"  新建（干净环境）： py {me} profile-add {lane} <新名>", C.CYAN))
    info(col(f"  移除　　　　　　： py {me} profile-rm {lane} <名>", C.CYAN))
    return 0


def cmd_profile_add(cfg: dict, args) -> int:
    """给一条 lane 新建 profile：干净环境（官方 web 组合）或整目录复制现有 profile。

    · 干净：按 dsh-app-boot initProfile 的形状建目录（package.json + cordis.patch.yml
      + pnpm-workspace.yaml），官方组合包由宿主启动时从安装树解析，不需要预装；
    · --from：整目录复制源 profile（含已装插件），junction 按副本路径重建，
      pnpm 账本里指回源 profile 的路径就地改写。
    """
    lane = args.lane
    name = args.name
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    try:
        validate_profile_name(name)
    except ValueError as exc:
        fail(str(exc))
        return 1
    view = lane_profiles_view(cfg, lane)
    if name in view and view[name]["onDisk"]:
        fail(f"profile「{name}」已经存在（{lane_home(cfg, lane) / 'profiles' / name}）")
        return 1

    home = lane_home(cfg, lane)
    dst = home / "profiles" / name
    me = ME_NAME
    if getattr(args, "from_profile", None):
        src_name = str(args.from_profile).strip()
        if src_name == name:
            fail("源 profile 和目标不能同名")
            return 1
        if src_name not in view or not view[src_name]["onDisk"]:
            fail(f"源 profile「{src_name}」在磁盘上不存在（现有：{'、'.join(sorted(view)) or '无'}）")
            return 1
        src = home / "profiles" / src_name
        info(col(f"── 复制 profile：{lane}/{src_name} → {lane}/{name} ──", C.BOLD))
        info(f"  源目录    {src}")
        info(f"  目标目录  {dst}")
        if lane_runtime(cfg, lane, src_name):
            warn(f"源 profile「{src_name}」正在运行：正在写入的文件可能复制到一半（其它内容不受影响）")
        stats = copy_tree(src, dst)
        rel = relink_junctions(src, dst, [(src, dst)])
        info(col(f"  复制 {stats['files']} 个文件 / {human_size(stats['bytes'])}，"
                 f"重建链接 {rel['made']}/{rel['total']} 个", C.GRAY))
        fixed = 0
        for relpath in ("node_modules/.modules.yaml", "pnpm-lock.yaml", "node_modules/.pnpm/lock.yaml"):
            path = dst / relpath
            try:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            replaced = text.replace(str(src), str(dst)).replace(str(src).replace("\\", "/"), str(dst).replace("\\", "/"))
            if replaced != text:
                try:
                    shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
                    path.write_text(replaced, encoding="utf-8")
                    fixed += 1
                except OSError as exc:
                    warn(f"  {relpath} 改写失败：{exc}")
        if fixed:
            info(col(f"  已把 {fixed} 个 pnpm 账本文件里指回源 profile 的路径改成本 profile 的", C.GRAY))
        if stats["errors"]:
            fail(f"有 {len(stats['errors'])} 个条目没能复制——这个副本不可信：")
            for path_text, reason in stats["errors"][:4]:
                info(col(f"      {path_text}：{reason}", C.GRAY))
            info(col(f"      清掉重来： py {me} profile-rm {lane} {name} --yes", C.CYAN))
            return 1
        ok(f"profile「{name}」已创建（复制自 {src_name}，含已装插件）")
    else:
        info(col(f"── 新建 profile：{lane}/{name}（干净环境，官方 web 组合）──", C.BOLD))
        init_profile_dir(dst, SHIPPED_TEMPLATES["web"])
        ok(f"profile「{name}」已创建（{dst}）")
        info(col("      里面还没有任何插件；启动后到 DSH 的插件市场里装，或：", C.GRAY))
        info(col(f"      py {me} profile-add {lane} {name} --from {DEFAULT_PROFILE} 可改成完整复制", C.GRAY))

    set_profile_hint(cfg, lane, name, createdAt=time.strftime("%Y-%m-%d %H:%M:%S"))
    info("")
    info(col(f"  启动： py {me} open {lane}/{name}        （端口自动挑空闲，--port 可现场指定）", C.CYAN))
    return 0


def cmd_profile_rm(cfg: dict, args) -> int:
    """移除一个 profile：删目录 + 运行态 + 登记提示。默认 profile（web）不删。"""
    lane = args.lane
    name = args.name
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    if name == DEFAULT_PROFILE:
        fail(f"profile「{name}」是默认 profile（插件市场 / 核对等功能都以它为默认对象），不允许移除")
        return 1
    if lane_runtime(cfg, lane, name):
        fail(f"profile「{lane}/{name}」正在运行，先停再删： py {ME_NAME} stop {lane}/{name}")
        return 1
    view = lane_profiles_view(cfg, lane)
    pdir = lane_home(cfg, lane) / "profiles" / name
    if name in view and not view[name]["onDisk"]:
        # 只是登记提示（没建到磁盘）——清登记就行
        set_profile_hint(cfg, lane, name, remove=True)
        clear_run(cfg, lane, name)
        ok(f"已移除登记提示「{lane}/{name}」（磁盘上本来就没有目录）")
        return 0
    if not pdir.is_dir():
        fail(f"profile「{name}」在磁盘上不存在：{pdir}")
        return 1
    size = dir_size(pdir)
    if not getattr(args, "yes", False):
        try:
            typed = input(f"  确认删除 profile「{lane}/{name}」（{human_size(size)}）？"
                          f"输入 y 回车确认，其它取消：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            typed = ""
        if typed not in ("y", "yes", "是"):
            info("已取消")
            return 0
    try:
        shutil.rmtree(pdir)
    except OSError as exc:
        fail(f"删除目录失败：{exc}")
        return 1
    set_profile_hint(cfg, lane, name, remove=True)
    clear_run(cfg, lane, name)
    ok(f"已移除 profile「{lane}/{name}」（{human_size(size)}）")
    info(col("  会话与凭据在 HOME 级，不受影响；这个 profile 里装的插件没了。", C.GRAY))
    return 0


def cmd_keys(cfg: dict, args) -> int:
    """列出一条 lane 的凭据条目（.credentials.yaml 的 refs 段，即各提供商的 key 引用）。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    home = lane_home(cfg, lane)
    path = credentials_path(home)
    refs = read_credential_refs(home)
    info(col(f"── lane「{lane}」的凭据条目 ──", C.BOLD))
    info(f"  {path}")
    if not refs:
        warn("  （没有 refs 段——这个 HOME 里还没有 API key）")
        return 0
    try:
        sources = lane_credential_sources(cfg, lane)
    except Exception:  # noqa: BLE001
        sources = {}
    for key in sorted(refs):
        users = sources.get(key) or []
        who = "、".join(u.get("displayName") or u.get("route") or u.get("profile") or "?"
                       for u in users)
        info(f"  · {key}" + (col(f"    ← {who}", C.GRAY) if who else ""))
    info("")
    if getattr(args, "show", False):
        for key in sorted(refs):
            info(col(f"  {key} = {refs[key]}", C.YELLOW))
        warn("  上面是明文密钥：只在本机看，别贴到别处。")
    info(col(f"  看明文： py {ME_NAME} keys {lane} --show", C.CYAN))
    info(col(f"  改 / 加一条： py {ME_NAME} set-key {lane} <条目名> --value sk-...", C.CYAN))
    info(col(f"  删条目： py {ME_NAME} rm-key {lane} <条目,条目>", C.CYAN))
    info(col(f"  复制到别的 lane： py {ME_NAME} copy-key {lane} <目标lane> [--only {sorted(refs)[0]}]", C.CYAN))
    return 0


def cmd_set_key(cfg: dict, args) -> int:
    """改 / 加一条凭据（写后宿主校验，不过自动回滚）。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    name = str(args.name or "").strip()
    if not CRED_KEY_RE.match(name):
        fail("条目名只能是字母/数字/_.-（大写习惯，如 MY_API_KEY）")
        return 1
    value = getattr(args, "value", None)
    env_name = str(getattr(args, "from_env", None) or "").strip()
    if value is None and env_name:
        value = os.environ.get(env_name)
        if value is None:
            fail(f"环境变量 {env_name} 是空的")
            return 1
    if value is None:
        fail("要写什么值？--value sk-... 或 --from-env MY_ENV（别把密钥留在命令行历史里）")
        return 1
    info(col(f"── 写凭据条目：{lane} / {name} ──", C.BOLD))
    res = set_credential_refs(cfg, lane, {name: str(value)}, verify=True)
    if not res.get("ok"):
        fail(f"  {res.get('error') or res.get('verify_why')}")
        if res.get("rollback"):
            warn(f"  {res['rollback']}")
        return 1
    if not res.get("written"):
        warn(f"  没改（{res.get('verify_why') or '值和原来一样'}）")
        return 0
    ok(f"  √ 已写入 {name}（{len(str(value))} 字符）")
    if res.get("backup"):
        info(col(f"  备份：{res['backup']}", C.GRAY))
    if lane_runtime(cfg, lane):
        warn("  这条 lane 有实例在跑：重启后才会用上新 key")
    return 0


def cmd_rm_key(cfg: dict, args) -> int:
    """删凭据条目（写后宿主校验，不过自动回滚）。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    names = [n.strip() for raw in (getattr(args, "names", None) or [])
             for n in str(raw).split(",") if n.strip()]
    if not names:
        fail("没写要删哪个条目")
        return 1
    info(col(f"── 删凭据条目：{lane} ──", C.BOLD))
    res = delete_credential_refs(cfg, lane, names, verify=True)
    if not res.get("ok"):
        fail(f"  {res.get('error') or res.get('verify_why')}")
        if res.get("rollback"):
            warn(f"  {res['rollback']}")
        return 1
    removed = res.get("removed") or []
    if removed:
        ok(f"  √ 已删 {'、'.join(removed)}")
    else:
        warn(f"  没有可删的条目（{res.get('verify_why') or '本来就不在'}）")
    if res.get("backup"):
        info(col(f"  备份：{res['backup']}", C.GRAY))
    if lane_runtime(cfg, lane):
        warn("  这条 lane 有实例在跑：重启后才会生效")
    return 0


def cmd_provider_list(cfg: dict, args) -> int:
    """列出某个 profile 补丁层里手写的自定义服务商。"""
    lane = args.lane
    prof = getattr(args, "profile", None) or PROFILE_NAME
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    info(col(f"── 自定义服务商：{lane}/{prof} ──", C.BOLD))
    try:
        providers = lane_custom_providers(cfg, lane, prof)
    except Exception as exc:  # noqa: BLE001
        fail(f"  读不出来：{type(exc).__name__}: {exc}")
        return 1
    if not providers:
        warn("  （没有手写服务商——dsh 设置里加的那些才在这里）")
        return 0
    for item in providers:
        title = item.get("displayName") or item.get("route")
        info(f"  · {title}" + col(f"  [{item.get('route')}]", C.GRAY))
        info(col(f"      api      {item.get('api') or '?'}", C.GRAY))
        info(col(f"      baseURL  {item.get('baseURL') or '?'}", C.GRAY))
        ref = item.get("apiKeyEnv") or provider_key_ref(item.get("route") or "")
        info(col(f"      keyRef   {ref}", C.GRAY))
        if item.get("models"):
            info(col(f"      models   {'、'.join(item['models'])}", C.GRAY))
    info("")
    info(col(f"  加 / 改一个： py {ME_NAME} provider-add {lane} <route> --api openai-completions "
             f"--base-url URL --display 名字 --key sk-... --model m1,m2", C.CYAN))
    return 0


def cmd_provider_add(cfg: dict, args) -> int:
    """写一个自定义服务商进补丁层（key 顺手存进凭据 refs）。"""
    lane = args.lane
    prof = getattr(args, "profile", None) or PROFILE_NAME
    route = str(args.route or "").strip()
    models = [{"id": m.strip()} for m in
              (getattr(args, "model", None) or "").split(",") if m.strip()]
    info(col(f"── 写自定义服务商：{lane}/{prof} / {route} ──", C.BOLD))
    res = write_custom_provider(
        cfg, lane, prof, route, str(getattr(args, "api", "") or ""),
        str(getattr(args, "base_url", "") or ""), models,
        display_name=str(getattr(args, "display", "") or ""),
        api_key=str(getattr(args, "key", "") or ""),
        overwrite=bool(getattr(args, "overwrite", False)), verify=True)
    if res.get("action") == "refused":
        fail(f"  {res.get('error')}")
        info(col(f"      要改就加 --overwrite", C.CYAN))
        return 1
    if not res.get("ok"):
        fail(f"  {res.get('error')}")
        return 1
    if res.get("action") == "none":
        warn("  文件一个字都没改（内容已经一样）")
        return 0
    ok(f"  √ 已{'覆盖更新' if res['action'] == 'replaced' else '新增'} route「{route}」")
    if res.get("keyRef"):
        ok(f"  密钥已存进凭据条目 {res['keyRef']}")
    info(col(f"  改动：{res.get('path')}", C.GRAY))
    if lane_runtime(cfg, lane, prof):
        warn("  这条 profile 正在运行：重启后才会让 dsh 读到新服务商")
    return 0


def cmd_copy_key(cfg: dict, args) -> int:
    """在 lane 之间按条目复制 API key：源 → 目标，--only 挑条目，缺省全部。"""
    src = args.source
    dst = args.dest
    for name in (src, dst):
        if name not in cfg.get("lanes", {}):
            fail(f"没有 lane「{name}」。先 create / adopt，或用 keys <lane> 看现有 lane。")
            return 1
    only = {x.strip() for x in (getattr(args, "only", None) or "").split(",") if x.strip()}
    refs = read_credential_refs(lane_home(cfg, src))
    if not refs:
        fail(f"源 lane「{src}」的 HOME 里没有凭据条目（keys {src} 可查看）")
        return 1
    if only:
        unknown = only - set(refs)
        if unknown:
            warn(f"源里没有这些条目（跳过）：{'、'.join(sorted(unknown))}")
        info(col(f"── 复制凭据条目：{src} → {dst} ──", C.BOLD))
        info(f"  已选条目  {'、'.join(sorted(only & set(refs))) or '（无）'}")
    else:
        info(col(f"── 复制凭据条目：{src} → {dst}（全部）──", C.BOLD))
        info(f"  源条目    {'、'.join(sorted(refs))}")
    res = copy_credential_refs(cfg, src, dst, only=only or None)
    print_copy_result(res)
    if res.get("written"):
        info(col("  注意：目标正在运行中的实例要重启一次才会用上新的 key。", C.GRAY))
        return 0
    return 0 if not res.get("error") else 1


def dir_size(path: Path, limit: int = 60000) -> int:
    """目录体积（字节）。entry 数超 limit 就提前收敛，避免扫一个大 HOME 卡住界面。"""
    total = 0
    seen = 0
    stack = [Path(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    seen += 1
                    if seen > limit:
                        return total
                    try:
                        if _is_reparse(entry.path):
                            continue  # junction / symlink：不算体积，也不跟进去（跟进去会重复计 600 遍）
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def human_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} GB"


# ======================= 复制一套 DSH（clone） =======================

REPARSE_FLAG = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT：junction 与 symlink 都带这个位


def _is_reparse(path: Path) -> bool:
    """这个条目是不是 junction / symlink。

    为什么专门写一个：Windows 上 **junction（目录联接）的 `os.path.islink()` 返回 False**，
    只查 islink 会把 600 个 junction 当成普通目录递归复制进去——既白复制 1GB 多，
    又会让副本和原件共享同一批宿主代码（后面那条才是要命的）。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if getattr(st, "st_file_attributes", 0) & REPARSE_FLAG:
        return True
    try:
        return bool(os.path.isjunction(path)) or os.path.islink(path)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _read_link_target(path: Path) -> str | None:
    """读链接目标（**不跟随**）。读不到返回 None。"""
    try:
        return os.readlink(path)
    except OSError:
        return None


def strip_win_prefix(text: str) -> str:
    """`\\\\?\\C:\\...` → `C:\\...`。

    os.readlink 读 junction 会带 `\\\\?\\` 前缀，而 PowerShell 的 LinkTarget 不带——
    两边不归一化就没法比较。
    """
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\"):]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\"):]
    return text


def _manifest_name(path) -> str | None:
    try:
        return json.loads((Path(path) / "package.json").read_text(encoding="utf-8")).get("name")
    except Exception:
        return None


def _manifest_version(path) -> str | None:
    try:
        return json.loads((Path(path) / "package.json").read_text(encoding="utf-8")).get("version")
    except Exception:
        return None


def copy_tree(src: Path, dst: Path, on_progress=None, skip: set | None = None) -> dict:
    """把 src 整棵复制到 dst。

    规则（三条，缺一条就会静默出错）：
      1. 普通文件/目录 → 复制，并保留时间戳；
      2. **junction / symlink 不复制内容、也不照抄链接**，只计数。
         照抄是错的：本机这些链接的目标都是绝对路径，照抄会让副本继续指向**原安装树**
         （`<HOME>/profiles/node_modules/*` → 全局那份 dsh）。那样"副本"其实和原件共享宿主代码，
         升级副本会连带影响原件，而且**不报任何错**——升级测试的结论就假了。
         正确做法是留空，由 DSH 启动时按"这次启动用的安装树"重建（`healProfilesModuleFallback`），
         事后用 `verify` 核对它到底指对了没有。
      3. 单条失败不中断整棵复制，但会记进 errors —— 有 error 的副本不可信，上层必须报错。
    """
    stats = {"files": 0, "dirs": 0, "bytes": 0, "links": 0, "errors": [], "skipped": []}
    stack = [(Path(src), Path(dst))]
    while stack:
        s, d = stack.pop()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            stats["errors"].append((str(d), str(exc)))
            continue
        stats["dirs"] += 1
        try:
            entries = list(os.scandir(s))
        except OSError as exc:
            stats["errors"].append((str(s), str(exc)))
            continue
        for entry in entries:
            sp = Path(entry.path)
            dp = d / entry.name
            if skip and sp in skip:
                # 明确跳过的子树（例如"只复制选中的 profile"里没选的那些目录）：
                # 既不建目录也不进去 —— 留个空壳会让 DSH 以为这个 profile 存在
                stats["skipped"].append(str(sp))
                continue
            if _is_reparse(sp):
                stats["links"] += 1
                continue
            try:
                is_dir = entry.is_dir()
            except OSError as exc:
                stats["errors"].append((str(sp), str(exc)))
                continue
            if is_dir:
                stack.append((sp, dp))
                continue
            try:
                shutil.copyfile(sp, dp)
                try:
                    shutil.copystat(sp, dp)  # 保留 mtime：会话文件的"最新时间"要能对上
                except OSError:
                    pass
                stats["bytes"] += entry.stat().st_size
                stats["files"] += 1
            except OSError as exc:
                stats["errors"].append((str(sp), str(exc)))
            if on_progress and stats["files"] and stats["files"] % 400 == 0:
                on_progress(stats)
    return stats


def remap_link_target(target: str, pairs: list[tuple]) -> str:
    """把链接目标里的"源安装树 / 源 HOME"前缀换成副本的对应路径。

    必须按**路径段**匹配：`...\\@deepseek-ai\\dsh` 是 `...\\@deepseek-ai\\dsh-acp` 的字符串前缀，
    但绝不是它的父目录。不做边界判断就会把 `dsh-acp` 重定向成 `<副本>\\-acp`——实测踩到过，
    后果是 230 个链接被误判成"源里已悬空"而跳过（不报错，副本悄悄少一堆包）。
    """
    norm = strip_win_prefix(target)
    low = _norm_win(norm)
    for src_prefix, dst_prefix in pairs:
        s = str(Path(src_prefix))
        sl = _norm_win(s)
        if low == sl:
            return str(dst_prefix)
        if low.startswith(sl + "\\"):
            rest = norm[len(s):].lstrip("\\/")
            return str(Path(dst_prefix) / rest)
    return norm


def make_junction(target, link) -> None:
    """建目录联接（junction）。junction 不需要管理员权限，也不用开发者模式。"""
    link = Path(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(link) or link.exists():
        return
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def junction_map(root) -> dict:
    """列出 root 下**所有** junction/symlink → {相对路径: 目标}（不跟进去、不递归链接内部）。"""
    root = Path(root)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for entry in entries:
            sp = Path(entry.path)
            if _is_reparse(sp):
                target = _read_link_target(sp)
                if target:
                    out[str(sp.relative_to(root))] = strip_win_prefix(target)
                continue
            try:
                if entry.is_dir():
                    stack.append(sp)
            except OSError:
                continue
    return out


def relink_junctions(src_root, dst_root, pairs: list[tuple]) -> dict:
    """把 src_root 下所有 junction/symlink，按**副本自己的路径**在 dst_root 重建一遍。

    为什么必须做，而且必须对**安装树和 HOME 都做**：
    · 这些链接的目标都是**绝对路径**。照抄会让副本继续指向原件——"副本"和原件共享同一批代码，
      升级副本会连带影响原件，而且**不报任何错**。
    · 反过来，只跳过不重建也不行：本机 `<prefix>\\node_modules\\<dep>` 就是一批 junction
      （依赖是被"提升"上去的），跳过等于副本**少了 230 个依赖包**，升级测试直接失真。
    · 留空等 DSH 自己建也不行：这一版 DSH 的 runtime 解析**不创建链接**
      （`dsh-app-boot` 文档原话："runtime 解析不创建链接"），实测确认它只建空目录。
      只有"profile 需要现场安装依赖"那条路径会顺手建，你的完整 profile 走不到那儿。

    源里本来就悬空的链接（目标早被新版本删掉、老链接没清）跳过并计数，不算错。
    """
    src_root = Path(src_root)
    dst_root = Path(dst_root)
    out = {"total": 0, "made": 0, "remapped": 0, "skipped": 0, "dangling": 0, "errors": []}
    if not src_root.is_dir():
        return out
    stack = [src_root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as exc:
            out["errors"].append((str(cur), str(exc)))
            continue
        for entry in entries:
            sp = Path(entry.path)
            try:
                dp = dst_root / sp.relative_to(src_root)
            except ValueError:
                continue
            if _is_reparse(sp):
                out["total"] += 1
                target = _read_link_target(sp)
                if not target:
                    out["skipped"] += 1
                    continue
                new_target = remap_link_target(target, pairs)
                if not Path(new_target).exists():
                    # 源里本来就是**悬空**链接：目标早就被新版本删掉了，而这一版 DSH 的
                    # runtime 解析不再重建链接，于是老链接一直留着没人清。复制不了，也不算错。
                    out["dangling"] += 1
                    continue
                if _norm_win(new_target) != _norm_win(strip_win_prefix(target)):
                    out["remapped"] += 1
                try:
                    make_junction(new_target, dp)
                    out["made"] += 1
                except Exception as exc:  # noqa: BLE001
                    out["skipped"] += 1
                    out["errors"].append((str(dp), f"{type(exc).__name__}: {exc}"))
                continue
            try:
                if entry.is_dir():
                    stack.append(sp)
            except OSError:
                continue
    return out


def _merge_relink(*results: dict) -> dict:
    total = {"total": 0, "made": 0, "remapped": 0, "skipped": 0, "dangling": 0, "errors": []}
    for item in results:
        for key in ("total", "made", "remapped", "skipped", "dangling"):
            total[key] += item[key]
        total["errors"].extend(item["errors"])
    return total


def lane_clone_plan(cfg: dict, src: str, new: str, sizes: bool = True,
                    profiles: list[str] | None = None) -> dict:
    """算出一条 lane 的复制方案：装到哪、从哪来、多大、空间够不够、有没有硬问题。

    `profiles` 只列要一起复制的 profile（HOME\\profiles\\<名>）；`None` ＝全部。
    为什么要能挑：一条 lane 的 HOME 里常常开着好几个环境，体积主要在 profile 的
    node_modules 上（实测一条 lane 的 HOME 是 628 MB），而"试升级"往往只关心其中一个。
    没选的 profile **不进副本**（不是复制完再删），所以体积也照着选中的算。
    """
    data = cfg["lanes"][src]
    p = ensure_layout(cfg)
    src_home = Path(data.get("home") or (p["homes"] / src))
    src_install = lane_install_dir(cfg, src)
    if _manifest_name(src_install) == PKG:
        src_pkg = src_install                       # 接管型：installDir 本身就是 dsh 包目录
        src_root = src_install                      # 复制它的全部内容
    else:
        src_pkg = src_install / "node_modules" / "@deepseek-ai" / "dsh"
        src_root = src_install                      # 常规型：整棵 prefix 一起搬（含提升到顶层的依赖）
    dst_install = p["clones"] / new
    dst_pkg = dst_install / "node_modules" / "@deepseek-ai" / "dsh"
    dst_root = dst_pkg if src_root == src_pkg else dst_install
    on_disk = sorted(disk_profiles(src_home))
    if profiles is None:
        chosen = list(on_disk)
    else:
        wanted = [str(name).strip() for name in profiles if str(name).strip()]
        chosen = [name for name in wanted if name in on_disk]
    plan = {
        "src": src,
        "src_version": str(data.get("version") or "?"),
        "src_home": src_home,
        "src_install": src_install,
        "src_pkg": src_pkg,
        "src_root": src_root,
        "home": p["homes"] / new,
        "install": dst_install,
        "pkg": dst_pkg,
        "root": dst_root,
        "profiles": on_disk,
        "profiles_selected": chosen,
        "profiles_skipped": [name for name in on_disk if name not in chosen],
        "home_size": None,
        "home_size_all": None,
        "install_size": None,
        "free": None,
        "problems": [],
    }
    if not src_home.is_dir():
        plan["problems"].append(f"源 lane 的 DSH_HOME 不存在：{src_home}")
    if not src_pkg.is_dir():
        plan["problems"].append(f"源 lane 的 dsh 包目录不存在（入口缺失）：{src_pkg}")
    if plan["home"].exists():
        plan["problems"].append(f"目标 DSH_HOME 已存在：{plan['home']}")
    if plan["install"].exists():
        plan["problems"].append(f"目标安装目录已存在：{plan['install']}")
    if profiles is not None:
        unknown = [str(name).strip() for name in profiles
                   if str(name).strip() and str(name).strip() not in on_disk]
        if unknown:
            plan["problems"].append(
                f"源 lane 的 HOME 里没有这些 profile：{'、'.join(unknown)}"
                f"（磁盘上现有：{'、'.join(on_disk) or '无'}）")
    if sizes:
        plan["install_size"] = dir_size(src_install)
        plan["home_size_all"] = dir_size(src_home)
        # 没选的 profile 不会进副本，体积从总量里扣掉（profiles 那棵子树是 HOME 的子集，
        # dir_size 不跟 junction，所以直接相减是准的）
        skipped_size = sum(dir_size(src_home / "profiles" / name)
                           for name in plan["profiles_skipped"])
        plan["home_size"] = max(0, plan["home_size_all"] - skipped_size)
        try:
            base = plan["install"] if plan["install"].exists() else Path(cfg["root"])
            plan["free"] = shutil.disk_usage(str(base)).free
        except OSError:
            plan["free"] = None
    return plan


def package_dir_of_entry(entry: Path) -> Path:
    """从入口脚本往上找 dsh 包目录（兼容"包目录"与"prefix 前缀目录"两种安装树写法）。"""
    cur = Path(entry).parent
    for _ in range(4):
        if _manifest_name(cur) == PKG:
            return cur
        cur = cur.parent
    return Path(entry).parent.parent


def count_sessions(home) -> int:
    root = Path(home) / "sessions"
    if not root.is_dir():
        return 0
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        total += sum(1 for name in filenames if name.endswith(".zstd"))
    return total


def profile_bundles(home, profile: str = "web") -> list[str] | None:
    manifest = Path(home) / "profiles" / profile / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    return list(((data.get("dsh") or {}).get("profile") or {}).get("bundles") or [])


def farm_targets(home) -> dict[str, str]:
    """读出"宿主依赖农场"（`<HOME>/profiles/node_modules`）里每个链接指向哪。

    这个目录是 DSH 启动时建的：把宿主自己的依赖用 junction 接进 profile，
    好让 profile 里装的插件能 require 到宿主的包。指向哪，决定插件**实际加载的是哪份宿主代码**——
    所以它是"隔离成立没有"的权威判据。
    """
    farm = Path(home) / "profiles" / "node_modules"
    out: dict[str, str] = {}
    if not farm.is_dir():
        return out
    for scope in farm.iterdir():
        if _is_reparse(scope):
            target = _read_link_target(scope)
            if target:
                out[scope.name] = strip_win_prefix(target)
            continue
        if not scope.is_dir():
            continue
        try:
            children = list(scope.iterdir())
        except OSError:
            continue
        for child in children:
            if _is_reparse(child):
                target = _read_link_target(child)
                if target:
                    out[f"{scope.name}/{child.name}"] = strip_win_prefix(target)
    return out


def _norm_win(path_text: str) -> str:
    return strip_win_prefix(str(path_text)).replace("/", "\\").rstrip("\\").lower()


def cmd_clone(cfg: dict, args) -> int:
    """把一条 lane 连安装树带 DSH_HOME **完整复制**成一条新 lane。

    为什么要"复制"而不是"新建"：新建出来的是空环境——没有你的会话、没有你装的插件、
    没有你的设置，根本测不出插件冲突。复制出来的是当前环境的快照，升级它就能回答
    "这批插件在新版本上还活着吗"，而原件一路不动（还能随时回退）。
    """
    try:
        src = resolve_target(cfg, args.source)
    except KeyError:
        fail(f"找不到 lane 或已安装版本「{args.source}」。先跑 list / instances 看看有哪些。")
        return 1
    new = args.lane
    if new == src:
        fail("源 lane 和目标 lane 不能同名")
        return 1
    if new in cfg["lanes"]:
        fail(f"lane「{new}」已存在。换个名字，或先 `delete {new}`。")
        return 1
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", new):
        fail("lane 名只允许字母 / 数字 / . _ - （1-32 字符）")
        return 1

    raw = getattr(args, "profiles", None)
    if isinstance(raw, str):
        wanted: list[str] | None = [part.strip() for part in raw.split(",") if part.strip()]
    elif raw:
        wanted = [str(part).strip() for part in raw if str(part).strip()]
    else:
        wanted = None

    plan = lane_clone_plan(cfg, src, new, sizes=True, profiles=wanted)
    if plan["profiles"] and not plan["profiles_selected"]:
        fail(f"一个 profile 都没选 —— 源 lane 的 HOME 里有 {'、'.join(plan['profiles'])}，"
             "至少留一个（不指定 --profiles 就是全复制）")
        return 1
    if plan["problems"]:
        for item in plan["problems"]:
            fail(item)
        return 1

    need = int(((plan["home_size"] or 0) + (plan["install_size"] or 0)) * 1.05)
    info(col(f"── 复制 lane「{src}」→「{new}」 ──", C.BOLD))
    info(f"  源安装树    : {plan['src_install']}")
    info(f"  源 DSH_HOME : {plan['src_home']}")
    info(f"  待复制      : 安装树 {human_size(plan['install_size'] or 0)}"
         f" + HOME {human_size(plan['home_size'] or 0)}"
         f" ≈ {human_size(need)}")
    if plan["profiles"]:
        info(f"  profiles    : 复制 {'、'.join(plan['profiles_selected']) or '（无）'}"
             + (f"　·　跳过 {'、'.join(plan['profiles_skipped'])}"
                f"（整份 HOME 是 {human_size(plan['home_size_all'] or 0)}）"
                if plan["profiles_skipped"] else "（全部）"))
    if plan["free"]:
        info(f"  目标盘剩余  : {human_size(plan['free'])}")
        if need > plan["free"]:
            fail(f"空间不足：需要约 {human_size(need)}，只剩 {human_size(plan['free'])}")
            return 1

    src_state = lane_any_running(cfg, src)
    if src_state:
        warn(f"源 lane「{src}」有 profile 正在运行（{src_state.get('profile')}）："
             "正在写入的那个会话文件可能复制到一半（其它内容不受影响）")

    started = time.time()
    last = {"t": 0.0}

    def progress(stats: dict, label: str) -> None:
        now = time.time()
        if now - last["t"] < 2.0:
            return
        last["t"] = now
        info(col(f"      …{label} {stats['files']} 个文件 / {human_size(stats['bytes'])}"
                 f"（跳过 {stats['links']} 个链接）", C.GRAY))

    info("")
    info(col("  [1/4] 复制安装树…", C.CYAN))
    st_install = copy_tree(plan["src_root"], plan["root"], on_progress=lambda s: progress(s, "安装树"))
    write_lane_package_json(plan["install"], plan["src_version"], cfg.get("allow_scripts") or {})

    info(col("  [2/4] 复制 DSH_HOME（会话 / 设置 / 已装插件）…", C.CYAN))
    # 没选中的 profile 不进副本：不是"复制完再删"，而是压根不进去（省时间也省空间）
    skip_dirs = {plan["src_home"] / "profiles" / name for name in plan["profiles_skipped"]}
    st_home = copy_tree(plan["src_home"], plan["home"],
                        on_progress=lambda s: progress(s, "HOME"), skip=skip_dirs)

    info(col("  [3/4] 重建 junction（安装树 + HOME，全部重定向到副本自己的路径）…", C.CYAN))
    pairs = [
        (plan["src_pkg"], plan["pkg"]),
        (plan["src_install"], plan["install"]),
        (plan["src_home"], plan["home"]),
    ]
    rel = _merge_relink(
        relink_junctions(plan["src_root"], plan["root"], pairs),
        relink_junctions(plan["src_home"], plan["home"], pairs),
    )
    info(col(f"      重建 {rel['made']}/{rel['total']} 个链接"
             f"（重定向 {rel['remapped']} 个，源里已悬空跳过 {rel['dangling']} 个）", C.GRAY))

    info(col("  [4/4] 登记新 lane…", C.CYAN))
    cfg["lanes"][new] = {
        "version": plan["src_version"],
        "home": str(plan["home"]),
        "installDir": str(plan["install"]),
        "kind": "clone",
        "clonedFrom": src,
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if plan["profiles_selected"]:
        # HOME 里**选中**的 profile 目录已经复制过来了，把名字也登记上（端口偏好不带：新环境重新挑）
        cfg["lanes"][new]["profiles"] = {
            name: {"createdAt": time.strftime("%Y-%m-%d %H:%M:%S")}
            for name in plan["profiles_selected"]
        }
    save_config(cfg)

    errors = list(st_install["errors"]) + list(st_home["errors"])
    links = st_install["links"] + st_home["links"]
    elapsed = time.time() - started
    info("")
    ok(f"lane「{new}」已就绪（复制自 {src}，版本 {plan['src_version']}）")
    info(f"      安装目录  : {plan['install']}")
    info(f"      DSH_HOME  : {plan['home']}")
    info(col("      端口      : 启动时实时挑空闲端口", C.GRAY))
    if plan["profiles_selected"]:
        info(col(f"      profiles  : {'、'.join(plan['profiles_selected'])}"
                 "（随 HOME 复制过来了）", C.GRAY))
    if plan["profiles_skipped"]:
        info(col(f"                  没复制的：{'、'.join(plan['profiles_skipped'])}"
                 "（需要时在新 lane 上点「＋ 添加 Profile」再建）", C.GRAY))
    info(f"      复制内容  : {st_install['files'] + st_home['files']} 个文件 / "
         f"{human_size(st_install['bytes'] + st_home['bytes'])}，用时 {elapsed:.1f}s")
    info(f"      会话文件  : {count_sessions(plan['home'])} 个"
         f"（源 lane {count_sessions(plan['src_home'])} 个）")
    info(f"      宿主农场  : 重建链接 {rel['made']}/{rel['total']} 个"
         f"，跳过 {links} 个原本的链接（由上面这一步按副本路径重连）")
    if rel["errors"]:
        warn(f"有 {len(rel['errors'])} 个农场链接没建起来（副本仍能跑，但依赖解析结构和原件不一致）")
        for path_text, reason in rel["errors"][:4]:
            info(col(f"      {path_text}：{reason}", C.GRAY))
        info(col(f"      核对： py {ME_NAME} verify {new}", C.CYAN))
    refs = read_credential_refs(plan["home"])
    info(f"      API key   : {'、'.join(sorted(refs)) if refs else '无（源 HOME 里就没有）'}")

    if errors:
        info("")
        fail(f"有 {len(errors)} 个条目没能复制——**这个副本不可信**，别拿它下结论。")
        for path_text, reason in errors[:6]:
            info(col(f"      {path_text}：{reason}", C.GRAY))
        if len(errors) > 6:
            info(col(f"      …还有 {len(errors) - 6} 条", C.GRAY))
        warn("如果源 lane 正在运行，先 `stop` 它再删掉这个副本重来：")
        info(col(f"      py {ME_NAME} delete {new} --stop", C.CYAN))
        return 1

    info("")
    info(col(f"  下一步①： py {ME_NAME} open {new} --no-browser", C.CYAN))
    info(col("          （副本有自己的安装树和 HOME，可以和原件同时开着）", C.GRAY))
    info(col(f"  下一步②： py {ME_NAME} verify {new}   # 核对隔离真的成立", C.CYAN))
    info(col(f"  下一步③： py {ME_NAME} upgrade {new} <版本>   # 升级这个副本，原件一点不动", C.CYAN))
    return 0


# ======================= 升级到指定版本 =======================
#
# 目标：把某一条 lane **精确**换成另一个版本，并且随时能退回去。
# 三种安装树的升级方式不同，按树在哪自动判：
#
#   tree     ：`<root>\versions\<版本>`（本启动器装的 next / stable）→ 装一棵新树 + 改登记。
#              旧树原地留着，所以"退回去"是**秒级**的（只改登记，不重装、不联网）。
#   clone    ：`<root>\clones\<lane>`（复制出来的副本）→ **就地** `npm install --prefix`。
#              路径不变，所以 HOME 里那几百条 junction 一条都不用重连（复制时领教过这点）。
#   external ：接管来的安装（npm 全局那份、Desktop 自带的 app.asar）→ **这一步不动它**。
#              改它等于改你日常真正在用的那一套，得单独一步、单独确认。
#
# 三道保险：①运行中拒绝升级（除非 `--stop`，升完还给你起回来）；
# ②升完核对（入口 / 实际版本 / 补丁层体检）；③**启动冒烟**——真起一次再停回去，
# 起不来**自动退回**到升级前的版本。


def lane_upgrade_plan(cfg: dict, lane: str) -> dict:
    """这条 lane 该怎么升级：树在哪、走哪种方式、旧版本能不能秒退。"""
    data = cfg["lanes"][lane]
    p = paths(cfg)
    install = data.get("installDir")
    if not install:
        vdir = p["versions"] / str(data.get("version") or "")
        return {
            "mode": "tree",
            "dir": vdir,
            "why": f"本启动器装的（{p['versions']}\\<版本>）：换版本＝换一棵树，旧树留着，退回是秒级的",
        }
    idir = Path(install)
    try:
        idir.relative_to(p["clones"])
        return {
            "mode": "clone",
            "dir": idir,
            "why": f"副本自己的安装树（{idir}）：就地升级，路径不变，HOME 里的链接一条都不用重连",
        }
    except ValueError:
        pass
    kind = "Desktop 自带的（app.asar）" if "app.asar" in str(idir) else "接管来的外部安装"
    return {"mode": "external", "dir": idir, "why": f"{kind}（{idir}）"}


def npm_install_in_place(cfg: dict, tree: Path, version: str, follow: bool = True,
                         registries: list[str] | None = None) -> str:
    """在**已有的**安装树里就地换版本（`npm install --prefix <tree> <pkg>@<version>`）。

    副本走这条路的关键理由是**路径不变**：DSH 的 HOME 里那些 junction 记的是绝对路径，
    换个目录就得逐条重连（复制那一步已经为此写了一整套 `relink_junctions`）；
    就地升级则一条都不用动。
    """
    p = ensure_layout(cfg)
    write_lane_package_json(tree, version, cfg.get("allow_scripts") or {})
    log = p["logs"] / f"upgrade-{tree.name}-{version}.log"
    return npm_install_tree(cfg, tree, version, log, registries=registries,
                            follow=follow, what="原地安装")


def upgrade_checks(cfg: dict, lane: str, version: str, tree: Path,
                   home: Path) -> tuple[list[str], list[str]]:
    """升完核对 → (致命问题, 只是提醒的问题)。

    分开的原因：补丁层体检报出的东西**不是这次升级造成的**（升级没碰过那一层），
    不该拿它去触发自动退回；只有"入口没了 / 版本不对 / 起不来"才是升级本身失败。
    """
    fatal: list[str] = []
    notes: list[str] = []
    entry = lane_entry_script(cfg, lane)
    if not entry.is_file():
        fatal.append(f"升级后找不到入口脚本：{entry}")
    actual = installed_version(tree)
    if actual != version:
        fatal.append(f"升级后安装树里的实际版本是 {actual!r}，不是 {version!r}")
    else:
        ok(f"安装树里的实际版本 = {actual}")
    if entry.is_file():
        pkg_dir = package_dir_of_entry(entry)
        for patch_path in (profile_dir(home) / PATCH_LAYER_FILE, Path(home) / PATCH_LAYER_FILE):
            if patch_path.is_file():
                report_patch_layer(cfg, home, pkg_dir, patch_path, notes, tree)
    farm = farm_targets(home)
    if farm:
        inside = sum(1 for t in farm.values() if _norm_win(t).startswith(_norm_win(str(tree))))
        info(f"  宿主农场  : {len(farm)} 条链接，指向本次安装树的 {inside} 条")
        if inside != len(farm):
            notes.append(
                f"宿主农场里有 {len(farm) - inside} 条链接还指着别的安装树——"
                "宿主启动时会自己按当次安装重指（下面的启动冒烟就会做这件事）"
            )
    return fatal, notes


def cmd_upgrade(cfg: dict, args) -> int:
    me = ME_NAME
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」（现有：{'、'.join(cfg.get('lanes', {})) or '无'}）")
        return 1
    data = cfg["lanes"][lane]
    p = ensure_layout(cfg)
    plan = lane_upgrade_plan(cfg, lane)

    info(col(f"── 升级 lane「{lane}」──", C.BOLD))
    info(f"  当前版本  : {data.get('version')}")
    info(f"  安装树    : {plan['dir']}")
    info(f"  升级方式  : {plan['why']}")

    if plan["mode"] == "external":
        fail(
            f"「{lane}」的安装树在本启动器目录之外 —— 改它等于改你日常真正在用的那一套，"
            "这一步有意不动它。"
        )
        info(col("      要动它得单独一步：先停实例 → 换那一份安装 → 起回来 → 确认能退回。", C.GRAY))
        info(col(f"      只想看状态： py {me} list / py {me} instances", C.CYAN))
        return 1

    # 目标版本：--rollback 时取上次升级前记下的版本
    rollback = bool(getattr(args, "rollback", False))
    spec = data.get("previousVersion") if rollback else getattr(args, "version", None)
    if not spec:
        fail("要写明目标版本（例如 0.1.7-rc.2 / latest），或用 --rollback 退回上一次的版本")
        return 1
    registry_note = ""
    packument = None
    choice = getattr(args, "registry", None)
    reg = ""
    try:
        regs = registry_plan(cfg, choice)
    except ValueError as exc:
        fail(str(exc))
        return 1
    try:
        packument, reg = fetch_index(cfg, registries=regs)
        registry_note = f"（来源 {registry_label(reg)}）"
    except Exception as exc:  # noqa: BLE001 —— 离线也可能只是"目标版本早装好了"
        if re.match(r"^\d+\.\d+\.\d+", str(spec)):
            version = str(spec)
            warn(f"连不上 registry（{exc}）——就按写死的 {version} 装，装不上会明确报错")
        else:
            fail(f"连不上 registry，也没法解析 {spec!r}：{exc}")
            return 1
    if packument is not None:
        try:
            version = resolve_tag(packument, str(spec))
        except Exception:  # noqa: BLE001
            fail(f"registry 上没有这个版本/tag：{spec!r}（可能打错了，或它还没发布）")
            known = sorted_versions(packument)
            if known:
                info(col(f"      已有的最近几版：{'、'.join(known[-6:])}", C.GRAY))
                info(col(f"      想按 tag 装： py {me} upgrade {lane} latest", C.CYAN))
            return 1
    tagnote = f"{spec} → {version} " if str(spec) != version else f"{version} "
    info(col(f"  目标版本  : {tagnote}{registry_note}", C.CYAN))

    # 查到版本用的是哪个源，就一路带到安装那一步（查/装必须同源）；它要是只同步了一半，
    # npm_install_tree 会自动换另一个候选源重试一次。
    install_regs = registry_plan_after(reg, cfg, choice) if reg else registry_plan(cfg, choice)

    current = str(data.get("version") or "")
    before = installed_version(plan["dir"]) or current
    if version == before:
        ok(f"lane「{lane}」已经就是 {version}，不用动")
        return 0

    running = lane_any_running(cfg, lane)
    was_running = bool(running)

    if getattr(args, "dry_run", False):
        info("")
        info(col("  --dry-run：只算不做。真执行会按这个顺序来：", C.CYAN))
        steps: list[str] = []
        if plan["mode"] == "tree":
            reuse = installed_version(p["versions"] / version) == version
            steps.append(f"装一棵新树 {p['versions'] / version}"
                         + ("（本地已有，直接复用、不联网）" if reuse else "（走 npm）"))
            steps.append(f"把登记改成 {version}；旧树 {plan['dir']} 留着 → 退回是秒级的")
        else:
            steps.append(f"就地 npm install --prefix {plan['dir']} {PKG}@{version}（路径不变）")
        steps.insert(0, f"从 {registry_label(install_regs[0])} 查版本并下载"
                        + (f"；那个源缺东西就换 {registry_label(install_regs[1])} 重试一次"
                           if len(install_regs) > 1 else ""))
        if running:
            steps.append(f"先停掉它（现在跑着：pid {running.get('pid')}，端口 {running.get('port')}）"
                         "—— 不带 --stop 的话真执行会被拒绝")
        steps.append("核对（入口 / 实际版本 / 补丁层）+ 启动冒烟（真起一次再停回去）")
        steps.append(f"起不来就自动退回 {before}")
        if was_running:
            steps.append("升完把它起回来（它升级前是运行中的）")
        for index, step in enumerate(steps, 1):
            info(f"    {index}) {step}")
        return 0

    if running and not getattr(args, "stop", False):
        fail(
            f"lane「{lane}」正在运行（pid {running.get('pid')}，端口 {running.get('port')}）——"
            "升级要换掉它的安装树。先停："
        )
        info(col(f"      py {me} stop {lane}          # 或加 --stop 让升级自己停，升完再起回来", C.CYAN))
        return 1

    if running:
        info(col("  先停掉它（--stop）…", C.CYAN))
        if cmd_stop(cfg, argparse.Namespace(target=[lane])) != 0:
            fail("停不掉，放弃升级")
            return 1
        info("")

    started = time.time()
    try:
        if plan["mode"] == "tree":
            vdir = install_version(cfg, version, registries=install_regs)
        else:
            npm_install_in_place(cfg, plan["dir"], version, registries=install_regs)
            vdir = plan["dir"]
    except Exception as exc:  # noqa: BLE001
        fail(f"安装失败：{exc}")
        info(col(f"      登记没动，lane「{lane}」还是 {before}（安装树可能被 npm 改了一半）", C.GRAY))
        info(col(f"      想干净重来： 再跑一次  py {me} upgrade {lane} {version}", C.CYAN))
        return 1

    data["version"] = version
    data["previousVersion"] = before
    data["upgradedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_config(cfg)
    home = Path(data.get("home") or (p["homes"] / lane))

    info("")
    info(col("  [核对]", C.CYAN))
    fatal, notes = upgrade_checks(cfg, lane, version, vdir, home)
    for item in fatal:
        fail(item)

    smoke_ok = True
    if not getattr(args, "no_boot_check", False):
        info("")
        info(col("  [启动冒烟] 真起一次再停回去（这一步会顺手把宿主农场重指到新安装树）…", C.CYAN))
        smoke_ok = boot_smoke(cfg, lane, timeout=180)
        if smoke_ok:
            ok("起来过，又停回去了")
        else:
            fail(f"起不来。日志：{lane_log_path(cfg, lane)}")

    if fatal or not smoke_ok:
        info("")
        warn(f"这次升级不成立 —— 自动退回 {before}…")
        try:
            if plan["mode"] == "tree":
                install_version(cfg, before, registries=install_regs)
            else:
                npm_install_in_place(cfg, plan["dir"], before, registries=install_regs)
        except Exception as exc:  # noqa: BLE001
            fail(f"自动退回也失败了：{exc}")
            info(col(f"      手动退回： py {me} upgrade {lane} {before}", C.CYAN))
            return 1
        data["version"] = before
        data["previousVersion"] = version
        data["upgradedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_config(cfg)
        ok(f"已退回 {before}（登记和安装树都改回来了）")
        if was_running:
            info(col("      它升级前是运行中的，正在起回来…", C.CYAN))
            cmd_open(cfg, argparse.Namespace(target=lane, port=None, cwd=None, timeout=None,
                                            no_browser=False, detach=True, adopt_only=False))
        return 1

    info("")
    for item in notes:
        warn(item)
    ok(f"lane「{lane}」已升到 {version}（原 {before}）")
    info(f"      用时      : {time.time() - started:.1f}s")
    info(f"      退回      : py {me} upgrade {lane} --rollback")
    info(f"      核对      : py {me} verify {lane}")
    if plan["mode"] == "tree":
        info(col(f"      旧树还在  : {plan['dir']}（所以退回不用重装）", C.GRAY))
    if was_running:
        info(col("      它升级前是运行中的，正在起回来…", C.CYAN))
        cmd_open(cfg, argparse.Namespace(target=lane, port=None, cwd=None, timeout=None,
                                         no_browser=False, detach=True, adopt_only=False))
    else:
        info(col(f"      打开      : py {me} open {lane}", C.CYAN))
    return 0


def cmd_verify(cfg: dict, args) -> int:
    """核对一条 lane 的"隔离"是不是真的成立。

    专治最阴的一类错误：副本看起来能跑，插件却悄悄 require 到了**另一个版本**的宿主代码——
    不报错、不崩溃，只是"升级测试"的结论变成假的。所以这里比的是权威判据：
    宿主依赖农场（`profiles/node_modules`）里每个链接**实际指向哪**。
    """
    lane = args.lane
    data = cfg.get("lanes", {}).get(lane)
    if not data:
        fail(f"没有 lane「{lane}」")
        return 1
    try:
        version, vdir, home = require_version_installed(cfg, lane)
    except RuntimeError as exc:
        fail(str(exc))
        return 1
    entry = lane_entry_script(cfg, lane)
    pkg_dir = package_dir_of_entry(entry)
    prof = getattr(args, "profile", None) or PROFILE_NAME
    info(col(f"── 核对 lane「{lane}」──", C.BOLD))
    info(f"  版本      : {version}")
    info(f"  安装树    : {vdir}")
    info(f"  dsh 包目录: {pkg_dir}")
    info(f"  DSH_HOME  : {home}")
    info(f"  profile   : {prof}")

    problems: list[str] = []
    try:
        entry.relative_to(vdir)
        ok("入口脚本在本 lane 自己的安装树里")
    except ValueError:
        problems.append(f"入口脚本不在本 lane 的安装树里：{entry}（vdir={vdir}）")
    actual = _manifest_version(pkg_dir)
    if actual and version and actual != version:
        problems.append(f"登记版本 {version} 与安装树里的实际版本 {actual} 不符")

    src = data.get("clonedFrom")
    src_home = None
    src_pkg = None
    src_install_dir = None
    if src and src in cfg["lanes"]:
        try:
            _v, src_install_dir, src_home = require_version_installed(cfg, src)
            src_pkg = package_dir_of_entry(lane_entry_script(cfg, src))
        except RuntimeError:
            src_home = None

    # 副本被 `upgrade` 升到别的版本之后，"按源 lane 逐条比对"就失去意义了：
    # 源还是旧版本，它的依赖闭包和副本的已经不同（新版会加/删包），比出来的"差异"
    # 全是版本差异、不是隔离问题 —— 会把人引到错的方向。这时只核对另一条更强的性质：
    # **每条链接都指向副本自己的安装树**。
    same_as_source = True
    if src and src in cfg["lanes"]:
        src_ver = str(cfg["lanes"][src].get("version") or "")
        if src_ver and version and src_ver != version:
            same_as_source = False
            warn(f"副本已是 {version}，源 lane「{src}」还是 {src_ver} —— 版本不同，"
                 "逐条比对源 lane 没有意义；改为核对「每条链接都指向本 lane 自己」")

    # 安装树里的 junction 也要逐条比：本机 <prefix>\node_modules\<dep> 就是一批 junction，
    # 少了它们副本就是"少了 230 个依赖包"，而这种缺失不报错、只是行为不一样。
    if src and src_install_dir and _manifest_name(src_install_dir) == PKG:
        src_root, dst_root = src_install_dir, pkg_dir
    else:
        src_root, dst_root = src_install_dir, vdir
    if src_root and Path(src_root).is_dir() and same_as_source:
        src_j = junction_map(src_root)
        my_j = junction_map(dst_root)
        live_j = {k: v for k, v in src_j.items() if Path(v).exists()}
        pairs_j = [(src_pkg, pkg_dir), (src_install_dir, vdir)]
        want_j = {k: remap_link_target(v, pairs_j) for k, v in live_j.items()}
        bad_j = sorted(
            k for k, want in want_j.items() if _norm_win(my_j.get(k, "")) != _norm_win(want)
        )
        info(f"  安装树链接: 源 {len(src_j)} 条（活 {len(live_j)}）/ 本 lane {len(my_j)} 条，"
             f"逐条一致 {len(want_j) - len(bad_j)} 条")
        if bad_j:
            problems.append(
                f"有 {len(bad_j)} 个安装树链接没对上（副本的依赖不完整或指回了原件）：{bad_j[:5]}"
            )

    mine = farm_targets(home)                        # {包名: 实际指向}
    source_links = farm_targets(src_home) if (src_home and same_as_source) else {}

    if getattr(args, "fix", False) and src_home and src_pkg:
        info(col("  --fix：按源 lane 重连 junction（安装树 + HOME）…", C.CYAN))
        pairs = [(src_pkg, pkg_dir), (src_install_dir, vdir), (src_home, home)]
        rel = _merge_relink(
            relink_junctions(src_install_dir, vdir, pairs),
            relink_junctions(src_home, home, pairs),
        )
        info(col(f"      重建 {rel['made']}/{rel['total']} 个链接（重定向 {rel['remapped']} 个）", C.GRAY))
        if rel["errors"]:
            for path_text, reason in rel["errors"][:4]:
                info(col(f"      {path_text}：{reason}", C.GRAY))
        mine = farm_targets(home)

    if source_links:
        # 有源可比（副本）：逐条比对 —— 副本每条链接都应等于"源的目标，把源安装树前缀换成副本的"
        # 源里已悬空的（目标不存在）不算差异：那种链接谁也建不出来，源自己也是坏的。
        live = {n: t for n, t in source_links.items() if Path(t).exists()}
        dangling = len(source_links) - len(live)
        # 前缀形态的安装树里，依赖是"提升"到 <install>\node_modules\<dep> 的（不在 dsh 包目录里），
        # 所以映射前缀必须和 clone 用同一组：包目录 → 整棵安装树 → HOME。
        pairs = [(src_pkg, pkg_dir), (src_install_dir, vdir), (src_home, home)]
        expected = {
            name: remap_link_target(target, pairs)
            for name, target in live.items()
        }
        missing = sorted(n for n in expected if n not in mine)
        bad = sorted(
            (n, mine[n], expected[n])
            for n in expected
            if n in mine and _norm_win(mine[n]) != _norm_win(expected[n])
        )
        extra = sorted(n for n in mine if n not in expected)
        info(f"  宿主农场  : 源「{src}」{len(source_links)} 条（其中源里已悬空 {dangling} 条）"
             f" / 本 lane {len(mine)} 条，逐条比对一致 {len(expected) - len(missing) - len(bad)} 条")
        if missing:
            problems.append(
                f"有 {len(missing)} 条农场链接没建起来（依赖解析结构和原件不一致）：{missing[:5]}"
            )
        if bad:
            problems.append(
                f"有 {len(bad)} 条农场链接指向了**别的安装树**——插件会加载到别的版本的宿主代码，"
                "这条 lane 的测试结论不可信"
            )
            for name, got, want in bad[:4]:
                info(col(f"      ✗ {name}  实际 → {got}", C.RED))
                info(col(f"               应为 → {want}", C.GRAY))
        if extra:
            warn(f"有 {len(extra)} 条链接是源 lane 没有的：{extra[:5]}")
        if not (missing or bad):
            ok("农场逐条与源 lane 对应，且全部指向本 lane 自己的安装树（隔离成立）")
    elif mine:
        # 没有源可比（普通 lane / 接管的 lane / 已升级到别的版本的副本）：
        # 只要求链接都指向本 lane 自己的安装树。
        # 注意"自己的安装树"有**两个合法根**：接管型里 vdir 就是 dsh 包目录，
        # 前缀型里依赖被提升到 `<安装树>\node_modules\<dep>`（不在 dsh 包目录下面）——
        # 只判 pkg_dir 会把前缀型的整片提升依赖误报成"指向别处"。
        own_roots = {_norm_win(str(vdir)), _norm_win(str(pkg_dir))}
        ours = lambda target: any(_norm_win(target).startswith(root) for root in own_roots)
        same = sum(1 for t in mine.values() if ours(t))
        info(f"  宿主农场  : {len(mine)} 条链接，其中指向本 lane 安装树的 {same} 条")
        outside = sorted((n, t) for n, t in mine.items() if not ours(t))
        if outside:
            warn(f"有 {len(outside)} 条指向别处（例如 DSH Desktop 自带那份），逐条看一眼：")
            for name, target in outside[:6]:
                info(col(f"      · {name} → {target}", C.GRAY))
        else:
            ok("农场里全部链接都指向本 lane 自己的安装树（隔离成立）")
    else:
        farm_dir = Path(home) / "profiles" / "node_modules"
        if farm_dir.exists():
            problems.append(f"宿主依赖农场里一条链接都没有：{farm_dir}")
        else:
            info(col(f"  宿主农场  : 不存在（{farm_dir}）——源 lane 没有农场时属正常", C.GRAY))

    # pnpm 账本（.modules.yaml）：复制出来的副本会带着**原件**的 virtualStoreDir。
    # 它平时不影响启动、也不报错，但一旦要用 pnpm 给这条 lane 装/更新插件，
    # pnpm 会直接拒绝干活（ERR_PNPM_UNEXPECTED_VIRTUAL_STORE），而且它指的是别条 lane 的目录。
    ledger = pnpm_ledger_state(cfg, lane, prof)
    if ledger["exists"]:
        info(f"  pnpm 账本 : store={ledger['store'] or '?'}")
        info(col(f"              virtualStore={ledger['virtual'] or '?'}", C.GRAY))
        if ledger["outside"]:
            if getattr(args, "fix", False):
                rep = repair_pnpm_virtual_store(cfg, lane, prof)
                if rep["changed"]:
                    info(col(f"      --fix：已改成 {rep['want']}", C.CYAN))
                    ledger = pnpm_ledger_state(cfg, lane, prof)
                else:
                    warn(f"      --fix 没能改动：{rep['error'] or '没找到该字段'}")
            if ledger["outside"]:
                problems.append(
                    "pnpm 账本里的 virtualStoreDir 指在本 lane 之外（"
                    f"{ledger['virtual']}）——用 pnpm 给这条 lane 装/更新插件会被它拒绝；"
                    "verify --fix 可就地修正"
                )
        else:
            ok("pnpm 账本里的 virtualStoreDir 指在本 lane 自己身上")
    else:
        info(col("  pnpm 账本 : 不存在（这个 profile 不是 pnpm 装的）", C.GRAY))

    # 本地插件（`file:` 依赖）：pnpm 把解析结果记成相对路径，换个深度就指不到地方。
    # 复制/搬动之后必然遇到，所以这里也核一遍（实测报错长这样：ENOENT scandir 'D:\...\Desktop\...'）。
    file_specs = pnpm_file_spec_state(cfg, lane, prof)
    for item in file_specs:
        if item["broken"]:
            if getattr(args, "fix", False):
                rep = repair_pnpm_file_specs(cfg, lane, prof)
                if rep["changed"]:
                    for line in rep["changed"]:
                        info(col(f"      --fix：{line}", C.CYAN))
                    file_specs = pnpm_file_spec_state(cfg, lane, prof)
                    break
            problems.append(
                f"本地依赖 {item['name']} 指不到地方：{item['lock'] or item['specifier']} → "
                f"{item['resolved']}（复制/搬动后深度变了，pnpm 账本里记的相对路径不再成立；"
                "verify --fix 可以换成绝对路径）"
            )
    if file_specs and not any(item["broken"] for item in file_specs):
        ok(f"本地依赖（file:）{len(file_specs)} 个都能指到地方")

    # 补丁层体检（2026-09-27 那次故障的防线）：真解析一遍 + 市场读不读得到。
    # 这是"下一次启动会不会直接挂"的唯一提前判据 —— 补丁层坏了，症状只会出现在启动那一刻。
    for patch_path in (profile_dir(home, prof) / PATCH_LAYER_FILE, Path(home) / PATCH_LAYER_FILE):
        if patch_path.is_file():
            report_patch_layer(cfg, home, pkg_dir, patch_path, problems, vdir)
        else:
            info(col(f"  补丁层    : 无（{patch_path}）", C.GRAY))

    # 钉死端口体检：补丁层里 webserver 的字面量端口会压过命令行 --port（用户层 > bundle 层）。
    # 启动器 open 时会按需写 --patch 覆盖，但手动跑 `dsh web --port N` 的人会被它静默忽略。
    try:
        pin = detect_pinned_webserver_port(cfg, lane, prof)
    except Exception:  # noqa: BLE001
        pin = None
    if pin:
        warn(f"  端口钉死  : 补丁层把 webserver 端口写死为 {pin[0]}（{pin[1]}）——"
             "手动 `dsh web --port N` 会被它忽略；用启动器 open 时会按需自动覆盖")

    if src and src in cfg["lanes"]:
        if src_home:
            a, b = count_sessions(home), count_sessions(src_home)
            line = f"  会话文件  : 本 lane {a} 个 / 源 lane「{src}」{b} 个"
            info(line)
            if a < b:
                warn("比源 lane 少（源 lane 在复制后又写过会话，或复制时有文件被占用）")
            ba, bb = profile_bundles(home, prof), profile_bundles(src_home, prof)
            if ba is not None and bb is not None:
                if ba == bb:
                    ok(f"插件清单一致（{len(ba)} 个组合包，含官方 {sum(1 for x in ba if x.startswith('@deepseek-ai/'))} 个）")
                else:
                    only_a = [x for x in ba if x not in bb]
                    only_b = [x for x in bb if x not in ba]
                    problems.append(f"插件清单与源 lane 不一致：多 {only_a} 少 {only_b}")
    info("")
    if problems:
        for item in problems:
            fail(item)
        return 1
    ok("核对通过")
    return 0


# ======================= 插件开关（一键禁用自己装的插件） =======================

PROFILE_NAME = "web"
BLOCK_BEGIN = "# >>> dsh-lanes 禁用第三方插件（启动器自动生成，勿手改；用 plugins-on 撤销）"
BLOCK_END = "# <<< dsh-lanes 禁用第三方插件结束"

# 插件市场（dshmarket）有自己的开关块：它是最需要"随手关掉/随手打开"的那一个
# （关掉别的插件容易，关掉"插件市场"本身以前得手改 YAML）。两个块互不干扰：
# plugins-on 只删上面那段，market-on 只删下面这段。
MARKET_PKG = "dshmarket"
MARKET_BLOCK_BEGIN = "# >>> dsh-lanes 插件市场开关（启动器自动生成，勿手改；用 market-on 撤销）"
MARKET_BLOCK_END = "# <<< dsh-lanes 插件市场开关结束"
# pnpm 的"新版本按住"一次性绕过：必须用 .npmrc 的拼法（`--config.minimumReleaseAge=0` 在
# pnpm 12.3+ 的原生 CLI 上会被静默忽略——不报未知选项，只是白写）。这是 dshmarket 自己的
# 源码里给的结论（lib/install.js 的 RELEASE_AGE_OVERRIDE），不是我猜的。
MARKET_BYPASS_FLAG = "--config.minimum-release-age=0"

DUMP_ID_RE = re.compile(r"^(\s*)-\s+id:\s*['\"]?([A-Za-z0-9_.@/:-]+)['\"]?\s*$")
DUMP_DISABLED_RE = re.compile(r"^\s*disabled:\s*(.*)$")
PATCH_ID_RE = re.compile(r"^\s*(?:-\s*)?id:\s*['\"]?([A-Za-z0-9_.@/:-]+)['\"]?\s*$")
UNMATCHED_RE = re.compile(r'patch:\s*entry\s+"?([^"\s]+)"?\s+not found')
SKIPPED_RE = re.compile(r'skipping profile bundle\s+"?([^":\s]+)"?', re.I)


def profile_dir(home, profile: str = PROFILE_NAME) -> Path:
    return Path(home) / "profiles" / profile


def read_profile_bundles(home, profile: str = PROFILE_NAME) -> list[str]:
    """profile 里启用了哪些组合包（顺序有意义：loader 是有序的）。"""
    manifest = profile_dir(home, profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list((((data.get("dsh") or {}).get("profile") or {}).get("bundles")) or [])


def is_official_bundle(pkg: str) -> bool:
    return pkg.startswith("@deepseek-ai/")


def patch_ids(text: str) -> list[str]:
    """取这个组合包**插入**的行 id（只认 `insert:` 块里的，覆盖行不算）。

    **行 id 不等于包名**——实测：`dshmarket` 的行 id 是 `dsh-market`，
    `beauticode-dsh` 是 `beauticode-bridge`，`dsh-plugin-browser` 是 `browser`。
    写错 id 不会报错，只会被静默跳过（`patch: entry X not found`）。

    为什么只取 insert：补丁文件里既有 `- insert:`（本包新增的行），也有裸的 `- id: X`
    （**覆盖别人的行**）。实测踩到：`dsh-mnemon` 的补丁里有一条 `- id: connection`，
    那是给宿主修服务作用域的覆盖行；把它当成"本包的行"禁掉，官方那批等 `connection`
    服务的行就永远不激活，整个应用起不来（日志只会含糊地写 `7 entries did not activate`）。

    还要跳过注释（注释里常带示例 `- insert: - id: ...`）和 `!!js` 表达式正文。
    """
    ids: list[str] = []
    insert_indent: int | None = None
    js_indent: int | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if "!!js" in raw:
            js_indent = indent
            continue
        if js_indent is not None:
            if indent > js_indent:
                continue
            js_indent = None
        stripped = raw.lstrip()
        if re.match(r"^-\s*insert:\s*$", stripped):
            insert_indent = indent
            continue
        if stripped.startswith("- insert:"):
            # 行内写法：- insert: [{id: browser, name: '...'}]
            for inline in re.findall(r"[{,]\s*id:\s*['\"]?([A-Za-z0-9_.@/:-]+)", stripped):
                if inline not in ids:
                    ids.append(inline)
            continue
        if insert_indent is None:
            continue
        if indent <= insert_indent:
            insert_indent = None
            continue
        match = PATCH_ID_RE.match(raw)
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def bundle_row_ids(home, pkg: str, profile: str = PROFILE_NAME) -> list[str]:
    """这个组合包会往 loader 里插哪些行。优先按 package.json 里的 `dsh.bundle.patch` 找补丁文件。"""
    base = profile_dir(home, profile) / "node_modules" / pkg
    patch: Path | None = None
    manifest = base / "package.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            rel = ((data.get("dsh") or {}).get("bundle") or {}).get("patch") or ""
            if rel:
                patch = base / str(rel).lstrip("./")
        except Exception:
            patch = None
    if patch is None or not patch.is_file():
        fallback = base / "cordis.patch.yml"
        patch = fallback if fallback.is_file() else None
    if patch is None:
        return []
    try:
        return patch_ids(patch.read_text(encoding="utf-8"))
    except OSError:
        return []


def parse_dump(text: str) -> dict[str, dict]:
    """把 `--dump-config` 的 YAML 解成 {行 id: {name, disabled, expr}}。

    只要有 id 行就起一条记录，随后缩进更深的 `disabled:` 归属它——不引 yaml 依赖，
    因为这个输出是我们自己要看的两三个字段，规则稳定。
    """
    rows: dict[str, dict] = {}
    cur: str | None = None
    cur_indent = 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = DUMP_ID_RE.match(raw)
        if match:
            cur = match.group(2)
            cur_indent = len(match.group(1))
            rows.setdefault(cur, {"name": "", "disabled": False, "expr": False})
            continue
        if cur is None:
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= cur_indent:
            cur = None
            continue
        name_match = re.match(r"^\s*name:\s*(.+?)\s*$", raw)
        if name_match:
            rows[cur]["name"] = name_match.group(1).strip("'\"")
            continue
        off_match = DUMP_DISABLED_RE.match(raw)
        if off_match:
            value = off_match.group(1).strip()
            if value.startswith("!!js"):
                rows[cur]["expr"] = True
            else:
                rows[cur]["disabled"] = value.lower() in ("true", "yes", "1")
    return rows


def run_dump_config(cfg: dict, lane: str, profile: str = PROFILE_NAME, timeout: int = 300) -> dict:
    """离线跑一次 `dsh --profile <p> --dump-config`，拿**组合之后的真实行清单**。

    这是唯一能证明"禁用真的生效"的手段：它打印每一行的 id / name / disabled，
    并把 `patch: entry X not found` 这种**静默跳过**写进 stderr。

    两个实测细节：
    · 它**不是只读**——会重写 `<home>/profiles/<p>/cordis.yml`（那是每次启动都会重新生成的）。
    · 子进程输出直接接**文件句柄**，不用管道：某些沙箱下管道会被拒绝（EPERM）。
    """
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    node = find_node(cfg)
    entry = lane_entry_script(cfg, lane)
    out_p = paths(cfg)["logs"] / f".dump-{lane}.txt"
    err_p = paths(cfg)["logs"] / f".dump-{lane}.err"
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    if node:
        node_dir = str(Path(node).parent)
        if node_dir not in env.get("PATH", ""):
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as fo, open(err_p, "w", encoding="utf-8") as fe:
            rc = subprocess.run(
                [str(node), str(entry), "--profile", profile, "--dump-config"],
                stdout=fo,
                stderr=fe,
                env=env,
                timeout=timeout,
                cwd=str(cfg.get("default_cwd") or home),
                creationflags=_no_window_flags(),
            ).returncode
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rc": -1, "error": f"{type(exc).__name__}: {exc}", "rows": {}, "unmatched": [], "skipped": []}
    text = out_p.read_text(encoding="utf-8", errors="replace") if out_p.is_file() else ""
    err = err_p.read_text(encoding="utf-8", errors="replace") if err_p.is_file() else ""
    for path in (out_p, err_p):
        try:
            path.unlink()
        except OSError:
            pass
    return {
        "ok": rc == 0,
        "rc": rc,
        "rows": parse_dump(text),
        "unmatched": sorted(set(UNMATCHED_RE.findall(err))),
        "skipped": sorted(set(SKIPPED_RE.findall(err))),
        "stderr": err,
    }


def plugin_patch_path(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> Path:
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    return profile_dir(home, profile) / "cordis.patch.yml"


def lane_home(cfg: dict, lane: str) -> Path:
    data = cfg["lanes"][lane]
    return Path(data.get("home") or (paths(cfg)["homes"] / lane))


def find_block(lines: list[str], begin: str = BLOCK_BEGIN, end: str = BLOCK_END) -> tuple[int, int] | None:
    """找我写的那一段（起止行号，含两端）。找不到返回 None。

    启动器会往用户层写**两种**带标记的块（一键禁用 / 插件市场开关），
    所以起止标记必须能指定——否则会认错别人的块。
    """
    b = e = None
    for index, line in enumerate(lines):
        if line.strip() == begin:
            b = index
        elif line.strip() == end and b is not None:
            e = index
            break
    if b is None or e is None:
        return None
    return b, e


def strip_launcher_blocks(lines: list[str]) -> list[str]:
    """去掉启动器写的全部块（两种），只留下用户自己的内容。"""
    out = list(lines)
    for begin, end in ((BLOCK_BEGIN, BLOCK_END), (MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)):
        while True:
            span = find_block(out, begin, end)
            if not span:
                break
            out = out[: span[0]] + out[span[1] + 1:]
    return out


def user_layer_text(text: str) -> str:
    """只剩"你自己写的内容"的正文（去掉启动器的两个块）。

    用来回答一个具体问题：撤销时发现文件与备份不一样，**到底是用户手改了，
    还是只是启动器的另一个开关块还挂在里面**——这两件事的提示语气完全不同。
    """
    return "\n".join(strip_launcher_blocks(text.splitlines())).strip("\n")


def user_disabled_ids(text: str) -> set[str]:
    """用户层里已经 `disabled: true` 的行 id（**不含**启动器写的两个块）。"""
    lines = strip_launcher_blocks(text.splitlines())
    ids: set[str] = set()
    current: str | None = None
    for raw in lines:
        if raw.strip().startswith("#"):
            continue
        match = PATCH_ID_RE.match(raw.split(" #", 1)[0].rstrip())
        if match:
            current = match.group(1)
            continue
        off = DUMP_DISABLED_RE.match(raw)
        if off and current and off.group(1).strip().lower() in ("true", "yes", "1"):
            ids.add(current)
            current = None
    return ids


# ── 写补丁层：照插件市场 dshmarket 的写法（lib/patch.js）────────────────────
#
# 为什么不用 YAML 库：本启动器只依赖标准库；而插件市场自己也是**逐行扫描**这份文件的
# （它同样不解析 YAML，因为用户手写的补丁可能带它不认的结构）。所以"市场能不能读懂我写的
# 开关"这件事，唯一的判据就是市场那套行规则：
#
#     - id: <行 id>            ← 必须**整行就这些**（行尾跟 `# 注释` 市场就看不见）
#       disabled: true|false
#
# 实测（拿市场自己的 readUserPatchState 读我们写的文件）：旧格式把包名写成行尾注释
# `- id: browser   # dsh-plugin-browser`，市场读到的是 **0 条禁用**——也就是说
# 启动器关掉的插件，在插件市场页面上显示的还是"启用中"。两个工具看同一份文件却各说各话。
#
# 还有三个"会把 profile 写坏"的形状，市场都挡掉了，这里同样挡：
#   1. 模板自带的 `[]` 占位符——在它后面追加条目 = 一份文档里两个顶层元素，YAML 直接报错；
#   2. 删掉最后一条后只剩注释——那不再是顶层数组，dsh 拒绝启动这个 profile；
#   3. 本来就不是条目数组的文件——拒绝写入，别把它弄得更坏。
ROW_ID_STRICT_RE = re.compile(r"^- id: ([A-Za-z0-9_.-]+)\s*$")
ROW_ID_OK_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DISABLED_LINE_RE = re.compile(r"^ {2}disabled: (true|false)\s*$")
PLACEHOLDER_RE = re.compile(r"^[ \t]*\[[ \t]*\][ \t]*(?:#.*)?$", re.M)
COMMENTED_PLACEHOLDER_RE = re.compile(r"^[ \t]*#[ \t]*\[[ \t]*\][ \t]*$", re.M)

# 宿主基础设施行：关掉它们等于掐掉补丁层自己赖以生效的那条链（定时器 → 热加载 →
# web 服务 → 存储/设置）。这张表照抄 dshmarket 的 PROTECTED_MODULE_PATTERNS——
# 它是从 dsh-plugin-hub 那套插件控制台搬过来的，针对的就是同一个宿主。
PROTECTED_MODULE_PATTERNS = [
    re.compile(r"^cordis:"),
    re.compile(r"^@deepseek-ai/cordis-plugin-"),
    re.compile(r"^@deepseek-ai/dsh-host-"),
    re.compile(r"^@deepseek-ai/dsh-client-modules$"),
    re.compile(r"^@deepseek-ai/dsh-client-connection$"),
    re.compile(r"^@deepseek-ai/dsh-client-hmr$"),
    re.compile(r"^@deepseek-ai/dsh-client-runtime$"),
    re.compile(r"^@deepseek-ai/dsh-client-locale$"),
    re.compile(r"^@deepseek-ai/dsh-client-web"),
    re.compile(r"^@deepseek-ai/dsh-web-frontend$"),
    re.compile(r"^@deepseek-ai/dsh-web-app$"),
    re.compile(r"^@deepseek-ai/dsh-hmr$"),
    re.compile(r"^@deepseek-ai/dsh-settings"),
    re.compile(r"^@deepseek-ai/dsh-credentials"),
    re.compile(r"^@deepseek-ai/dsh-session"),
    re.compile(r"^@deepseek-ai/dsh-storage"),
    re.compile(r"^@deepseek-ai/dsh-typert"),
    re.compile(r"^@deepseek-ai/dsh-api-remotes$"),
    re.compile(r"^@deepseek-ai/dsh-tools$"),
    re.compile(r"^@deepseek-ai/dsh-system-prompt$"),
    re.compile(r"^@deepseek-ai/dsh-agent"),
    re.compile(r"^@deepseek-ai/dsh-llm"),
    re.compile(r"^@deepseek-ai/dsh-persona$"),
    re.compile(r"^@deepseek-ai/dsh-scope$"),
    re.compile(r"^@deepseek-ai/dsh-launch-environment$"),
    re.compile(r"^@deepseek-ai/dsh-shell$"),
    re.compile(r"^@deepseek-ai/dsh-subprocess"),
    re.compile(r"^@deepseek-ai/dsh-fs"),
    re.compile(r"^@deepseek-ai/dsh-sandbox"),
    re.compile(r"^@deepseek-ai/dsh-jobs"),
    re.compile(r"^@deepseek-ai/dsh-skill"),
    re.compile(r"^@deepseek-ai/dsh-goal"),
    re.compile(r"^@deepseek-ai/dsh-workflow"),
    re.compile(r"^@deepseek-ai/dsh-subagent"),
    re.compile(r"^@deepseek-ai/dsh-web$"),
    re.compile(r"^@deepseek-ai/dsh-workspace"),
    re.compile(r"^@deepseek-ai/dsh-user-approval$"),
    re.compile(r"^@deepseek-ai/dsh-user-questions$"),
    re.compile(r"^@deepseek-ai/dsh-commands$"),
    re.compile(r"^@deepseek-ai/dsh-hook"),
    re.compile(r"^@deepseek-ai/dsh-spill"),
    re.compile(r"^@deepseek-ai/dsh-guard"),
    re.compile(r"^@deepseek-ai/dsh-tool-call-timeout-policy$"),
    re.compile(r"^@deepseek-ai/dsh-repeat-tool-reminder$"),
]


def is_protected_module(name: str | None) -> bool:
    return bool(name) and any(p.search(str(name)) for p in PROTECTED_MODULE_PATTERNS)


def patch_content_lines(text: str) -> list[str]:
    """去掉注释与空行后的内容行（判 YAML 形状用）。"""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def patch_row_state(text: str) -> dict:
    """按**插件市场的行规则**读这份补丁：禁用 / 强制打开 / 插入了哪些行 id。

    行规则比我们自己的更严（行尾不能有注释）——这是故意的：市场读不出来的写法，
    在市场页面上就等于没关。所以这份函数的结论才是"市场看到的真相"。
    """
    disables: list[str] = []
    forced: list[str] = []
    inserts: list[str] = []
    lines = text.splitlines()
    in_insert = False
    for index, line in enumerate(lines):
        if re.match(r"^- insert:\s*$", line):
            in_insert = True
            continue
        if re.match(r"^- ", line):
            in_insert = False
        if in_insert:
            match = re.match(r"^ {4}- id: ([A-Za-z0-9_.-]+)", line)
            if match:
                inserts.append(match.group(1))
            continue
        match = ROW_ID_STRICT_RE.match(line)
        if not match:
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        value = DISABLED_LINE_RE.match(nxt)
        if value is None:
            continue
        (disables if value.group(1) == "true" else forced).append(match.group(1))
    return {"disables": disables, "forced": forced, "inserts": inserts}


def patch_dialect_ok(text: str) -> tuple[bool, str]:
    """这份补丁能不能**安全地追加一个顶层条目**。挡掉三类会写坏 profile 的形状。"""
    content = patch_content_lines(text)
    if not content:
        return True, ""
    core = "\n".join(content).strip()
    if core in ("[]", "[ ]"):
        return True, ""
    last = content[-1].strip()
    if last.startswith("[") or last.startswith("{"):
        return False, "补丁层以顶层流式结构结尾，追加进去会变成两个顶层元素（YAML 直接报错）"
    bad = [line for line in content if not line.startswith(("-", " "))]
    if bad:
        return False, f"补丁层顶层出现了不是条目的内容：`{bad[0].strip()[:40]}`"
    if not content[0].lstrip().startswith("-"):
        return False, "补丁层不是顶层数组"
    return True, ""


# ======================= 补丁层体检（verify 用） =======================
#
# 2026-09-27 那次把用户日常那套写坏（`YAMLException: duplicated mapping key (13:3)`，
# 整个 profile 拒绝启动）之后加的防线。核心教训是**格式正确性不能自证**：
# 我们自己的形状检查（`patch_dialect_ok`）对那份坏文件判"通过"，只有别人的解析器
# 才判 REJECTED。所以这里的第一条判据就是"拿宿主自己的解析器真解析一遍"。
#
# 两条判据：
#   1. **宿主判据**：用 dsh 自己那份 js-yaml（带 `!!js` 方言，与 dsh-app-boot 的
#      entryListSchema、插件市场 lib/check.js 同一套）解析；报错原话（含 行:列）直接给用户。
#      node 或 js-yaml 找不到时降级为"跳过"，绝不假装通过。
#   2. **市场判据**：插件市场是**逐行扫**这份文件的（`readUserPatchState`），
#      行尾带 `# 注释` 或引号的 `- id: X` 它读不到 —— 那意味着"看着关了、市场上还开着"。
#
# 只读不写：这个函数永远不改补丁层（改由 apply_plugin_states 写，写前备份 + 写后校验/回滚）。
PATCH_LAYER_FILE = "cordis.patch.yml"
MARKET_ID_LINE_RE = re.compile(r"^- id: ([A-Za-z0-9_.-]+)\s*$")
MARKET_LOOSE_ID_RE = re.compile(r"^-\s*id:\s*\S")
MARKET_DISABLED_LINE_RE = re.compile(r"^ {2}disabled: (true|false)\s*$")

# 判决脚本：方言抄自 dsh-app-boot 的 entryListSchema（JSON_SCHEMA + `!!js` 标量类型）。
# 输出一行 JSON 到 stdout，Python 侧只认这一行。
# 走 `node -e`（脚本作为**一个 argv**传进去）而不是落一个临时 .cjs 文件：
# 少一次写盘、不碰临时目录（有些环境对 %TEMP% 是拒绝的），也不受路径转义影响。
# 两个路径走环境变量，免去对 `process.argv` 偏移的假设。
YAML_JUDGE_JS = r"""
const fs = require('fs');
const yaml = require(process.env.DSH_LANES_JUDGE_YAML);
const jsExpr = new yaml.Type('tag:yaml.org,2002:js', {
  kind: 'scalar',
  resolve: (data) => typeof data === 'string',
  construct: (data) => ({ __jsExpr: String(data) }),
});
const schema = yaml.JSON_SCHEMA.extend(jsExpr);
const answer = (payload) => { process.stdout.write(JSON.stringify(payload)); };
let text;
try {
  text = fs.readFileSync(process.env.DSH_LANES_JUDGE_FILE, 'utf8');
} catch (err) {
  answer({ ok: false, why: '读不了文件：' + err.message });
  process.exit(0);
}
try {
  const value = yaml.load(text, { schema });
  if (!Array.isArray(value)) {
    answer({
      ok: false,
      why: value === null || value === undefined
        ? '整份文件没有内容（只剩注释或空行）—— 它不再是条目数组，dsh 会拒绝启动这个 profile'
        : '顶层不是条目数组',
    });
  } else {
    answer({ ok: true, entries: value.length });
  }
} catch (err) {
  answer({ ok: false, why: String((err && err.message) || err).split('\n')[0] });
}
"""


# 通用 YAML 判决脚本：只要求"宿主那份 js-yaml 能把它读成一个映射/数组"。
# 凭据文件（顶层是映射）与补丁层之外的 YAML 都用它；补丁层另有 YAML_JUDGE_JS（要数组）。
YAML_PARSE_JS = r"""
const fs = require('fs');
const yaml = require(process.env.DSH_LANES_JUDGE_YAML);
const answer = (payload) => { process.stdout.write(JSON.stringify(payload)); };
let text;
try {
  text = fs.readFileSync(process.env.DSH_LANES_JUDGE_FILE, 'utf8');
} catch (err) {
  answer({ ok: false, why: '读不了文件：' + err.message });
  process.exit(0);
}
try {
  const value = yaml.load(text);
  if (value === null || value === undefined) {
    answer({ ok: false, why: '整份文件没有任何内容（只剩注释或空行）' });
  } else if (typeof value !== 'object') {
    answer({ ok: false, why: '顶层不是映射/数组（拿到一段标量）' });
  } else {
    const count = Array.isArray(value) ? value.length : Object.keys(value).length;
    answer({ ok: true, why: '宿主自己的 js-yaml 解析通过（顶层 '
      + (Array.isArray(value) ? '数组 ' : '映射 ') + count + ' 项）' });
  }
} catch (err) {
  answer({ ok: false, why: String((err && err.message) || err).split('\n')[0] });
}
"""


def find_js_yaml(home, pkg_dir, install_dir=None) -> str | None:
    """找**宿主自己那份** js-yaml —— dsh 读补丁层用的就是它。

    三种装法的位置不一样（实测）：
      · npm 全局装：`<...>/node_modules/@deepseek-ai/dsh/node_modules/js-yaml`
      · lane 的 prefix 装：提升到 `<安装树>/node_modules/js-yaml`（dsh 包内**没有**）
      · pnpm 装的 profile：在虚拟 store `<profile>/node_modules/.pnpm/js-yaml@*/...` 里
    """
    pkg = Path(pkg_dir)
    raw = [
        pkg / "node_modules" / "js-yaml",
        pkg.parent / "js-yaml",
        pkg.parent.parent / "js-yaml",
        Path(install_dir) / "node_modules" / "js-yaml" if install_dir else None,
        Path(home) / "profiles" / "node_modules" / "js-yaml",
        profile_dir(home) / "node_modules" / "js-yaml",
        profile_dir(home) / "node_modules" / "dshmarket" / "node_modules" / "js-yaml",
    ]
    for cand in raw:
        if cand is not None and (cand / "package.json").is_file():
            return str(cand)
    for pattern in (
        "profiles/node_modules/.pnpm/js-yaml@*/node_modules/js-yaml",
        "profiles/*/node_modules/.pnpm/js-yaml@*/node_modules/js-yaml",
    ):
        for hit in sorted(Path(home).glob(pattern)):
            if (hit / "package.json").is_file():
                return str(hit)
    return None


def judge_patch_yaml(cfg: dict, home, pkg_dir, path: Path, install_dir=None) -> tuple[str, str]:
    """用宿主自己的解析器真解析一遍补丁层 → ("ok"|"bad"|"skip", 说明)。"""
    node = find_node(cfg)
    if not node:
        return "skip", "本机找不到 node"
    yaml_dir = find_js_yaml(home, pkg_dir, install_dir)
    if not yaml_dir:
        return "skip", "找不到宿主自带的那份 js-yaml"
    env = dict(os.environ)
    env["DSH_LANES_JUDGE_YAML"] = yaml_dir
    env["DSH_LANES_JUDGE_FILE"] = str(path)
    try:
        proc = subprocess.run(
            [node, "-e", YAML_JUDGE_JS],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env=env,
            creationflags=_no_window_flags(),
        )
        payload = ""
        for line in reversed((proc.stdout or "").splitlines()):
            if line.strip().startswith("{"):
                payload = line.strip()
                break
        if not payload:
            tail = ((proc.stderr or "").strip().splitlines() or ["(没有任何输出)"])[-1]
            return "skip", f"判决脚本没给出结论（{tail[:120]}）"
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001 —— 判不了就说判不了，不假装通过
        return "skip", f"外部判据跑不起来（{type(exc).__name__}: {exc}）"
    if data.get("ok"):
        return "ok", f"宿主自己的 js-yaml 解析通过（{data.get('entries')} 个条目）"
    return "bad", str(data.get("why") or "解析失败")


def lane_judge_paths(cfg: dict, lane: str) -> tuple[Path, Path, Path]:
    """判决/写盘时要用的三个位置：lane 的 HOME、dsh 包目录、安装树。"""
    home = lane_home(cfg, lane)
    pkg_dir = package_dir_of_entry(lane_entry_script(cfg, lane))
    return home, pkg_dir, lane_install_dir(cfg, lane)


def judge_yaml_file(cfg: dict, lane: str, path: Path) -> tuple[str, str]:
    """用宿主自己那份 js-yaml 真解析一份 YAML → ("ok"|"bad"|"skip", 说明)。

    凭据文件、配置迁移产物都用它。找不到 node/js-yaml 就如实说 skip，绝不假装通过。
    """
    node = find_node(cfg)
    if not node:
        return "skip", "本机找不到 node"
    home, pkg_dir, install_dir = lane_judge_paths(cfg, lane)
    yaml_dir = find_js_yaml(home, pkg_dir, install_dir)
    if not yaml_dir:
        return "skip", "找不到宿主自带的那份 js-yaml"
    env = dict(os.environ)
    env["DSH_LANES_JUDGE_YAML"] = yaml_dir
    env["DSH_LANES_JUDGE_FILE"] = str(path)
    try:
        proc = subprocess.run(
            [node, "-e", YAML_PARSE_JS],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env=env,
            creationflags=_no_window_flags(),
        )
        payload = ""
        for line in reversed((proc.stdout or "").splitlines()):
            if line.strip().startswith("{"):
                payload = line.strip()
                break
        if not payload:
            tail = ((proc.stderr or "").strip().splitlines() or ["(没有任何输出)"])[-1]
            return "skip", f"判决脚本没给出结论（{tail[:120]}）"
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001 —— 判不了就说判不了
        return "skip", f"外部判据跑不起来（{type(exc).__name__}: {exc}）"
    if data.get("ok"):
        return "ok", str(data.get("why") or "解析通过")
    return "bad", str(data.get("why") or "解析失败")


def market_blind_rows(text: str) -> tuple[list[str], list[str]]:
    """市场（逐行扫）读不到的 `- id:` 行 → (带开关的坏行, 其余读不到的行)。

    市场规则：`^- id: ([A-Za-z0-9_.-]+)\\s*$` + 下一行 `^ {2}disabled: (true|false)$`。
    行尾写 `# 包名`、给 id 加引号、行内多余空格 —— 市场一律读不到。
    """
    toggle_blind: list[str] = []
    other_blind: list[str] = []
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        if not MARKET_LOOSE_ID_RE.match(raw):
            continue
        if MARKET_ID_LINE_RE.match(raw):
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        label = f"第 {index + 1} 行 `{raw.strip()[:60]}`"
        if MARKET_DISABLED_LINE_RE.match(nxt):
            toggle_blind.append(label)
        else:
            other_blind.append(label)
    return toggle_blind, other_blind


def report_patch_layer(cfg: dict, home, pkg_dir, path: Path, problems: list[str],
                       install_dir=None) -> None:
    """体检一份补丁层：宿主判据 + 市场判据。只读。"""
    relative = path
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"补丁层读不出来：{relative}（{type(exc).__name__}: {exc}）")
        return
    info(f"  补丁层    : {relative}")
    info(col(f"              {len(text.splitlines())} 行 / {len(text.encode('utf-8'))} 字节", C.GRAY))
    verdict, why = judge_patch_yaml(cfg, home, pkg_dir, path, install_dir)
    if verdict == "ok":
        ok(f"  {why}")
    elif verdict == "bad":
        problems.append(
            f"补丁层 {relative} 解析不过：{why} —— 宿主会**拒绝启动**这个 profile。"
            "（改法：条目是一个缩进树，不要按固定行数删；只就地改 `disabled:` 这一个键，"
            "或直接删整个条目 = id 行 + 它下面缩进更深的所有行）"
        )
    else:
        info(col(f"  {why} —— 这次只做了本启动器的形状检查（不够，见 README）", C.GRAY))

    toggle_blind, other_blind = market_blind_rows(text)
    if toggle_blind:
        problems.append(
            f"补丁层 {relative} 里有 {len(toggle_blind)} 行的开关**插件市场读不到**"
            f"（行尾带了注释或引号）：{'；'.join(toggle_blind[:3])} —— "
            "在「插件市场」页面上这些行会显示成没被改过"
        )
    if other_blind:
        warn(f"有 {len(other_blind)} 行 `- id:` 市场读不到（它们没带开关，只影响市场页面的显示）："
             f"{'；'.join(other_blind[:3])}")
    if not (toggle_blind or other_blind):
        ok("  市场能读懂这份补丁层的每一行（`- id: X` 整行只有 id）")


def append_patch_entries(text: str, block: list[str]) -> tuple[bool, str, str]:
    """把若干 `- id: X` + `disabled:` 条目追加到补丁层末尾（安全处理 `[]` 占位符）。"""
    ok_dialect, why = patch_dialect_ok(text)
    if not ok_dialect:
        return False, why, text
    core = "\n".join(patch_content_lines(text)).strip()
    head = text
    if core in ("[]", "[ ]"):
        # 模板自带的空列表占位符：注释掉它再追加，否则一份文档里会出现两个顶层元素
        head = PLACEHOLDER_RE.sub("# []", text, count=1)
        if head == text:
            head = text.rstrip("\n") + "\n# []\n"
    if head and not head.endswith("\n"):
        head += "\n"
    return True, "", head + "\n".join(block) + "\n"


def remove_patch_row_entries(text: str, ids: list[str]) -> str:
    """删掉这些行 id 的**整个条目**：`- id: X` 那行 + 它下面缩进更深的行。

    ⚠️ 2026-09-27 的教训（用户日常那套被写坏、profile 起不来）：以前只按"两行"删
    （`- id: X` + `  disabled: ...`），而一个条目可以带 `config:` 这类后续行。只删两行，
    剩下的 `config:` 就落进了**上一条目**里，同一个映射里于是出现两个 `config:` 键：

        Error: dsh: failed to parse overlay ...cordis.patch.yml:
        YAMLException: duplicated mapping key (13:3)

    整个 profile 因此拒绝加载。我们自己的方言检查看不出来（它只判"是不是条目列表"），
    而插件市场自己的 `parsePatchFile()` 一读就判 REJECTED —— 已本地复现。
    """
    targets = set(ids)
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        match = re.match(r"^-\s+id:\s*['\"]?([A-Za-z0-9_.@/:-]+)", raw)
        if match is None or match.group(1) not in targets:
            out.append(raw)
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        index += 1
        while index < len(lines):
            nxt = lines[index]
            if nxt.strip() == "":
                look = index + 1
                while look < len(lines) and lines[look].strip() == "":
                    look += 1
                if look < len(lines) and (len(lines[look]) - len(lines[look].lstrip())) > indent:
                    index += 1
                    continue
                break
            if not nxt.lstrip().startswith("#") and (len(nxt) - len(nxt.lstrip())) > indent:
                index += 1
                continue
            break
    joined = "\n".join(out)
    return joined + ("\n" if text.endswith("\n") and joined else "")


def ensure_patch_array(text: str) -> str:
    """删完条目后如果只剩注释 —— 那不再是顶层数组，dsh 会拒绝启动整个 profile。补回 `[]`。"""
    if not text.strip():
        return "[]\n"
    if patch_content_lines(text):
        return text
    restored = COMMENTED_PLACEHOLDER_RE.sub("[]", text, count=1)
    if restored != text:
        return restored
    return text.rstrip("\n") + ("\n" if text.endswith("\n") else "\n") + "[]\n"


def set_patch_row(text: str, row_id: str, disabled: bool) -> tuple[bool, str, str]:
    """把一行的开关写成一条 `disabled: true|false`（先删同 id 的旧条目，保证只有一条）。"""
    if not ROW_ID_OK_RE.match(row_id):
        return False, f"行 id「{row_id}」含特殊字符，不能写进补丁层", text
    base = remove_patch_row_entries(text, [row_id])
    block = [f"- id: {row_id}", f"  disabled: {'true' if disabled else 'false'}"]
    return append_patch_entries(base, block)


def enable_patch_row(text: str, row_id: str, force: bool = False) -> tuple[bool, str, str, str]:
    """打开一行。返回 (ok, 说明, 新文本, 动作)。

    动作分三种，这个区分很重要：
    · removed —— 补丁层里本来有 `disabled: true`，删掉它就回到"下层说了算"，这才是"打开"；
    · forced  —— 补丁层里没有禁用条目，说明是**组合包自己的补丁**压着它。这时只有写
                 `disabled: false` 能强行打开（插件市场就是这么做的）。它可能带来副作用
                 （比如某行被压着正是因为"已经有别的插件提供了同一个服务"），所以默认不写，
                 要用户明确加 --force。
    · nothing —— 本来就没被禁用，一个字都不用写。
    """
    if not ROW_ID_OK_RE.match(row_id):
        return False, f"行 id「{row_id}」含特殊字符，不能写进补丁层", text, "nothing"
    had = len(re.findall(
        rf"(?m)^- id: ['\"]?{re.escape(row_id)}['\"]?\r?\n  disabled: true\r?\n", text))
    if had:
        # 删掉最后一条之后可能只剩注释：那份文件不再是顶层数组，**dsh 会拒绝启动这个 profile**
        #（市场源码里的 withPlaceholderRestored 讲的就是这件事：关一个插件再打开，profile 就废了）。
        # 实测：plugin-on 之后补丁层变成"只有注释"，紧接着 --verify 的离线组合直接失败。
        return True, "", ensure_patch_array(remove_patch_row_entries(text, [row_id])), "removed"
    if not force:
        return True, "本来就没被禁用", text, "nothing"
    ok_row, why, new_text = set_patch_row(text, row_id, False)
    return ok_row, why, new_text, "forced"


def write_patch_text(path: Path, text: str) -> tuple[bool, str]:
    """原子写：先写临时文件再替换（运行中的 DSH 正盯着这个文件做热重载）。"""
    tmp = path.with_name(path.name + ".dsh-lanes.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False, str(exc)
    return True, ""


def backup_patch_text(path: Path, text: str) -> tuple[bool, str]:
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        backup.write_text(text, encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    return True, str(backup)


def profile_dependencies(home, profile: str = PROFILE_NAME) -> dict:
    manifest = profile_dir(home, profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    deps = data.get("dependencies") or {}
    return {k: str(v) for k, v in deps.items()}


def package_manifest(home, pkg: str, profile: str = PROFILE_NAME) -> dict:
    path = profile_dir(home, profile) / "node_modules" / pkg / "package.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def package_kind(home, pkg: str, rows: list[str], profile: str = PROFILE_NAME) -> str:
    """这个插件是哪种形态 —— 决定它的开关该写在哪一层。

    · bundle    ：有补丁行 → 开关写补丁层（热生效，不用重启）。
    · client-only：只有 `dsh.client` 没有 `dsh.bundle` → **补丁层没有它的行**，
                   它的开关在插件市场自己的禁用表里（市场关掉它，或直接卸载）。
    · norows    ：两者都没有 → 没有任何行可开关。
    """
    if rows:
        return "bundle"
    manifest = package_manifest(home, pkg, profile)
    dsh = manifest.get("dsh") or {}
    if dsh.get("client") and not dsh.get("bundle"):
        return "client-only"
    return "norows"


def lane_skipped_bundles(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> list[str]:
    """从这个 profile 的启动日志里捡出"被跳过的组合包"（例如版本不兼容）。"""
    log = lane_log_path(cfg, lane, profile)
    if not log.is_file():
        return []
    out: list[str] = []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for match in SKIPPED_RE.finditer(text):
        name = match.group(1)
        if name not in out:
            out.append(name)
    return out


def plugin_listing(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """**文件真值**的插件清单：不跑 `--dump-config`，所以是即时的（毫秒级）。

    开关要的是"补丁层现在怎么说"，那件事只取决于文件；`--dump-config` 是**验证**
    手段（证明组合结果真的变了），不是开关的前置条件。
    """
    home = lane_home(cfg, lane)
    bundles = read_profile_bundles(home, profile)
    deps = profile_dependencies(home, profile)
    skip = lane_skipped_bundles(cfg, lane, profile)
    patch_file = plugin_patch_path(cfg, lane, profile)
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    state = patch_row_state(text)
    launcher_lines = strip_launcher_blocks(text.splitlines())
    launcher_text = "\n".join(launcher_lines)
    launcher_state = patch_row_state(launcher_text)
    disabled = set(state["disables"])
    forced = set(state["forced"])
    own_disabled = set(launcher_state["disables"])

    names: list[str] = []
    for name in list(deps.keys()) + list(bundles):
        if name not in names:
            names.append(name)
    items = []
    for pkg in names:
        official = is_official_bundle(pkg)
        rows = bundle_row_ids(home, pkg, profile)
        kind = package_kind(home, pkg, rows, profile)
        row_state = {}
        for row_id in rows:
            row_state[row_id] = (
                "禁用" if row_id in disabled else ("强制打开" if row_id in forced else "启用")
            )
        off_rows = [r for r in rows if r in disabled]
        items.append({
            "pkg": pkg,
            "spec": deps.get(pkg, ""),
            "official": official,
            "in_bundles": pkg in bundles,
            "rows": rows,
            "kind": kind,
            "row_state": row_state,
            "disabled_rows": off_rows,
            "forced_rows": [r for r in rows if r in forced],
            "own_disabled": sorted(set(rows) & own_disabled),
            "protected": is_protected_module(pkg),
            "skipped": pkg in skip,
            "installed": (profile_dir(home, profile) / "node_modules" / pkg).is_dir(),
        })
    return {
        "lane": lane,
        "home": home,
        "profile": profile,
        "patch_file": patch_file,
        "patch_text": text,
        "patch_state": state,
        "has_block": bool(find_block(text.splitlines())),
        "has_market_block": bool(find_block(text.splitlines(), MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)),
        "bundles": items,
        "skipped": skip,
        "dialect_ok": patch_dialect_ok(text)[0],
        "dialect_why": patch_dialect_ok(text)[1],
        # 文件存在但内容行是空的（只剩注释）→ 不是顶层数组，dsh 会拒绝启动这条 lane
        "array_ok": (not patch_file.is_file()) or bool(patch_content_lines(text)),
    }


def resolve_plugin_target(listing: dict, target: str) -> tuple[dict | None, str]:
    """把用户写的名字认成一个插件：包名、行 id、包名的一部分都行。"""
    target = (target or "").strip()
    if not target:
        return None, "没写插件名"
    exact = [i for i in listing["bundles"] if i["pkg"] == target]
    if exact:
        return exact[0], ""
    by_row = [i for i in listing["bundles"] if target in i["rows"]]
    if by_row:
        return by_row[0], ""
    lower = target.lower()
    partial = [
        i for i in listing["bundles"]
        if lower in i["pkg"].lower() or any(lower in r.lower() for r in i["rows"])
    ]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, "这个名字对上多个插件：" + "、".join(i["pkg"] for i in partial[:6])
    return None, f"没找到叫「{target}」的插件（用 plugins {listing['lane']} 看都有哪些）"


def apply_plugin_states(cfg: dict, lane: str, changes: list[tuple[str, bool]],
                        profile: str = PROFILE_NAME, force: bool = False) -> dict:
    """按**行**写入插件开关（一次写完，只做一次读-改-写）。

    changes: [(包名, True=打开 / False=关掉)]。返回值里逐条说清楚写了什么、跳过了什么。
    关掉 = 写一条 `- id: <行 id>` + `disabled: true`（插件市场读得懂的同一套行格式）；
    打开 = 删掉那条禁用条目；只有加 force 才会写 `disabled: false` 去压组合包自己的禁用。
    """
    home = lane_home(cfg, lane)
    patch_file = plugin_patch_path(cfg, lane, profile)
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    original = text
    written: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for pkg, want_on in changes:
        rows = bundle_row_ids(home, pkg, profile)
        if is_protected_module(pkg):
            skipped.append(f"{pkg}：属于宿主基础设施，禁止开关（会破坏热加载/传输/存储链）")
            continue
        if not rows:
            kind = package_kind(home, pkg, rows, profile)
            if kind == "client-only":
                skipped.append(
                    f"{pkg}：纯客户端插件（只有 dsh.client、没有 dsh.bundle）——补丁层里没有它的行，"
                    "开关在插件市场自己的页面里"
                )
            else:
                skipped.append(f"{pkg}：找不到属于它的补丁行，无法用补丁层开关")
            continue
        for row_id in rows:
            if not want_on:
                ok_row, why, text = set_patch_row(text, row_id, True)
                action = "禁用"
            else:
                ok_row, why, text, act = enable_patch_row(text, row_id, force=force)
                action = {"removed": "打开（删掉禁用条目）", "forced": "强制打开（写 disabled: false）",
                          "nothing": "打开（本来就没被禁用，没动文件）"}.get(act, "打开")
            if not ok_row:
                skipped.append(f"{pkg}（行 {row_id}）：{why}")
                continue
            written.append((pkg, row_id, action))
    if text == original:
        # 没有条目变化，但文件本身可能已经坏了（只剩注释 = 不是顶层数组，dsh 拒绝启动）。
        # 这种"上一版留下的坏文件"必须顺手治好，否则用户再点一次开关也修不回来。
        healed = ensure_patch_array(text)
        if healed == text:
            return {"ok": True, "changed": False, "written": written, "skipped": skipped,
                    "patch_file": patch_file}
        text = healed
        skipped.append("补丁层原本只剩注释（不是顶层数组，dsh 会拒绝启动这条 lane）——已补回 `[]`")
    # 兜底：不论走了哪条分支，写出去的文件都必须是"顶层数组"（只剩注释就补回 `[]`）
    text = ensure_patch_array(text)
    backup_ok, backup_info = backup_patch_text(patch_file, original)
    if not backup_ok:
        return {"ok": False, "changed": False, "error": f"备份失败，没敢写：{backup_info}",
                "written": [], "skipped": skipped, "patch_file": patch_file}
    ok_write, why_write = write_patch_text(patch_file, text)
    if not ok_write:
        return {"ok": False, "changed": False, "error": f"写入失败：{why_write}",
                "written": [], "skipped": skipped, "patch_file": patch_file}
    # 自动校验 + 回滚：拿宿主自己的解析器真解析一遍，判不过就立刻退回写之前的原文。
    # 这是这一版重新打开写开关的前提——以前"只写不验"，写坏了要等下次启动才发现 profile 起不来。
    try:
        jhome, jpkg, jinstall = lane_judge_paths(cfg, lane)
        verdict, why_judge = judge_patch_yaml(cfg, jhome, jpkg, patch_file, jinstall)
    except Exception as exc:  # noqa: BLE001
        verdict, why_judge = "skip", f"外部判据跑不起来（{type(exc).__name__}: {exc}）"
    if verdict == "bad":
        back_ok, back_why = write_patch_text(patch_file, original)
        rolled = f"已用备份回滚（{backup_info}）" if back_ok else \
            f"回滚失败，请手动把备份复制回去：{backup_info} → {patch_file}（{back_why}）"
        return {"ok": False, "changed": False,
                "error": f"写出来的补丁层解析不过（{why_judge}）—— {rolled}",
                "written": [], "skipped": skipped, "patch_file": patch_file,
                "backup": backup_info, "rollback": rolled, "verify": verdict,
                "verify_why": why_judge}
    return {"ok": True, "changed": True, "written": written, "skipped": skipped,
            "patch_file": patch_file, "backup": backup_info, "text": text,
            "verify": verdict, "verify_why": why_judge}



def plugin_overview(cfg: dict, lane: str, profile: str = PROFILE_NAME,
                    dump: dict | None = None) -> dict:
    """一眼看清这条 lane 的插件格局：组合包 → 行 id → 现在到底关没关。"""
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    bundles = read_profile_bundles(home, profile)
    dump = dump if dump is not None else run_dump_config(cfg, lane, profile)
    rows = dump.get("rows") or {}
    patch_file = plugin_patch_path(cfg, lane, profile)
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    span = find_block(text.splitlines())
    items = []
    for pkg in bundles:
        ids = bundle_row_ids(home, pkg, profile)
        present = [i for i in ids if i in rows]
        off = [i for i in present if rows[i]["disabled"]]
        items.append(
            {
                "pkg": pkg,
                "official": is_official_bundle(pkg),
                "rows": ids,
                "present": present,
                "disabled": off,
                "managed": bool(span) and any(i in text for i in ids) if ids else False,
            }
        )
    return {
        "home": home,
        "profile": profile,
        "patch_file": patch_file,
        "bundles": items,
        "has_block": bool(span),
        "user_disabled": user_disabled_ids(text),
        "unmatched": dump.get("unmatched") or [],
        "skipped": dump.get("skipped") or [],
        "dump_ok": bool(dump.get("ok")),
        "dump_error": dump.get("error"),
    }


# ================= 插件清单 / 插件跨 profile·跨版本迁移（plugin-transfer） =================
#
# 真实需求：一条 lane 里可以开多个 profile（多环境），每个 profile 有自己的一套插件组合。
# "这套环境里已经装好的插件，原样搬到另一个环境 / 另一个版本" 是日常动作，所以单独做一条路。
#
# 两种搬法，代价与可信度完全不同，界面上让用户选：
#
#   install —— 读源里插件的**实际版本号**，在目标 profile 用 dsh 自己的 plugin 命令
#              点名到那个版本装一遍。链接、store、账本全部由 pnpm 自己建，装完就是一份
#              干净的目标环境；跨版本时依赖对不上会**明确报错**（这正是我们要的结果，
#              而不是"看起来搬过去了，启动才发现没生效"）。
#   copy    —— 离线直拷：把该插件在源 profile 里的**依赖闭包**整棵复制过去，链接按目标
#              路径重建。不需要网络，也不需要 node 在 PATH 里。代价是 pnpm 账本
#              （pnpm-lock.yaml）不会同步，之后在目标里装别的东西时 pnpm 可能要重新解析一次。
#              所以复制完必须逐条报清楚"哪些包复制上了、哪些没有"，不能假装成功。
#
# 两种方式都要补三样东西，缺一样插件都不会真的生效：
#   ① 目标 profile 的 package.json：dependencies 里要有这个包；
#   ② dsh.profile.bundles 里要有它——这是宿主真正加载的组合包清单，只装不登记不会加载；
#   ③ 补丁层：把源里针对这些插件**行 id** 的覆盖条目搬过去，否则"源里关掉的插件"
#      搬过去会变回开启（补丁是环境状态的一部分）。

PLUGIN_TRANSFER_MODES = ("install", "copy")


def installed_pkg_dir(home, pkg: str, profile: str = PROFILE_NAME) -> Path:
    return profile_dir(home, profile) / "node_modules" / pkg


def installed_pkg_version(home, pkg: str, profile: str = PROFILE_NAME) -> str | None:
    """这个包在 profile 里**实际装上**的版本：读它自己的 package.json。

    不是清单里写的范围——`^1.2.0` 说明不了装的是哪一个；翻版本时靠的是这个值。
    """
    return _manifest_version(installed_pkg_dir(home, pkg, profile))


def profile_plugins(cfg: dict, lane: str, profile: str = PROFILE_NAME,
                    include_official: bool = False) -> list[dict]:
    """这个 profile 装了哪些插件（**文件真值**，毫秒级，不跑 --dump-config）。"""
    listing = plugin_listing(cfg, lane, profile)
    home = listing["home"]
    out: list[dict] = []
    for item in listing["bundles"]:
        if item["official"] and not include_official:
            continue
        out.append({
            "pkg": item["pkg"],
            "spec": item["spec"],
            "version": installed_pkg_version(home, item["pkg"], profile) or "",
            "kind": item["kind"],
            "official": item["official"],
            "in_bundles": item["in_bundles"],
            "rows": list(item["rows"]),
            "disabled_rows": list(item["disabled_rows"]),
            "protected": item["protected"],
            "skipped": item["skipped"],
            "installed": item["installed"],
        })
    return out


def plugin_inventory(cfg: dict) -> list[dict]:
    """本启动器管的**全部** lane × profile 的插件清单（界面「插件总览」用）。只读。"""
    rows: list[dict] = []
    for lane in sorted(cfg.get("lanes", {})):
        data = cfg["lanes"].get(lane) or {}
        view = lane_profiles_view(cfg, lane)
        for prof in sorted(view):
            entry = {
                "lane": lane,
                "profile": prof,
                "version": data.get("version") or "?",
                "kind": data.get("kind") or "",
                "onDisk": bool(view[prof].get("onDisk")),
                "error": "",
                "plugins": [],
            }
            try:
                entry["plugins"] = profile_plugins(cfg, lane, prof)
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(entry)
    return rows


def _backup_file(path: Path) -> str:
    """改一份清单/补丁层之前先留一份 `.bak`（和凭据、pnpm 账本一个规矩）。"""
    try:
        if path.is_file():
            bak = path.with_name(path.name + ".bak")
            shutil.copyfile(path, bak)
            return str(bak)
    except OSError:
        pass
    return ""


def _declared_deps(pkg_dir: Path) -> list[str]:
    """这个包声明了哪些运行时依赖（dependencies + optionalDependencies）。"""
    try:
        data = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    names = dict(data.get("dependencies") or {})
    names.update(data.get("optionalDependencies") or {})
    return list(names)


def _resolve_nm(src_pdir: Path, from_dir: Path, pkg: str) -> Path | None:
    """在源 profile 里找 pkg 落在哪：先看包自己旁边的 node_modules，再看顶层（hoisted 的位置）。"""
    for base in (from_dir / "node_modules", src_pdir / "node_modules"):
        cand = base / pkg
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return None


def _plugin_closure(src_pdir: Path, pkgs: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """算出"要复制过去"的目录：返回 (要复制内容的相对目录, 走不到的包, 指向 profile 外的包, 涉及的全部包名)。

    只跟 **dependencies** 走，不跟 peerDependencies：本机 profile 用 hoisted 链接器
    （pnpm-workspace.yaml 里写死的），依赖被提升到 `node_modules` 顶层，所以闭包很浅；
    peer 是宿主提供的（dsh 自己），本来就不该复制。

    为什么要返回**包名**：`node_modules/<名>` 这一层在 hoisted 链接器下常常是 junction，
    而 junction 的内容（真实目录）在 `.pnpm/...` 里——复制了内容之后，这一层的入口链接
    还得**按目标路径重新建回来**，否则依赖解析会在目标里直接 `MODULE_NOT_FOUND`。
    """
    real: list[str] = []      # 需要**复制内容**的目录（相对 src_pdir，正斜杠）
    missing: list[str] = []   # 在源里根本找不到的包
    outside: list[str] = []   # 链接指到 profile 外面（宿主安装树等）：不该复制，也不该算失败
    names: list[str] = []     # 闭包里涉及的所有包名
    seen: set[str] = set()
    queue: list[tuple[Path, str]] = [(src_pdir, p) for p in pkgs]
    while queue:
        from_dir, pkg = queue.pop(0)
        if pkg in seen:
            continue
        seen.add(pkg)
        entry = _resolve_nm(src_pdir, from_dir, pkg)
        if entry is None:
            if pkg not in missing:
                missing.append(pkg)
            continue
        if pkg not in names:
            names.append(pkg)
        if _is_reparse(entry):
            target = _read_link_target(entry)
            root: Path | None = None
            if target:
                try:
                    root = src_pdir / Path(strip_win_prefix(target)).relative_to(src_pdir)
                except ValueError:
                    root = None
            if root is None:
                if pkg not in outside:
                    outside.append(pkg)
                continue
        else:
            root = entry
        try:
            rel = str(root.relative_to(src_pdir)).replace("\\", "/")
        except ValueError:
            if pkg not in outside:
                outside.append(pkg)
            continue
        if rel not in real:
            real.append(rel)
        for dep in _declared_deps(root):
            queue.append((root, dep))
    return real, missing, outside, names


def _copy_plugin_files(src_pdir: Path, dst_pdir: Path, pkgs: list[str]) -> dict:
    """离线直拷：复制依赖闭包 + 按目标路径重建链接 + 把顶层入口挂回 node_modules。

    两个必须做对的地方（都是这个项目已经踩过的坑）：
    · junction **不能照抄**：源里的链接目标是绝对路径，照抄会让目标 profile 继续指着源
      （"看着装上了"，其实两边共享同一份代码）。这里用 `relink_junctions` 按目标路径重建。
    · 顶层入口 `node_modules/<pkg>` 可能本身就是个链接（hoisted 链接器下很常见），
      它的真实内容在 `.pnpm/...` 里——内容复制完之后要**再把链接建回来**，连依赖的入口一起。
      只给"用户勾选的那几个包"建是不够的：实测里 alpha 依赖 beta，beta 的入口不建回来，
      alpha 在目标里就解析不到 beta。
    """
    real, missing, outside, names = _plugin_closure(src_pdir, pkgs)
    # 先记下"目标里本来就有的入口"：复制过程会把新入口建出来，事后再查会把刚建的当成"本来就有"
    pre_existing = set()
    for name in names:
        dst_entry = dst_pdir / "node_modules" / name
        try:
            if dst_entry.exists() or _is_reparse(dst_entry):
                pre_existing.add(name)
        except OSError:
            pass
    res = {
        "real": real, "missing": missing, "outside": outside,
        "files": 0, "bytes": 0, "links": 0, "errors": [], "fresh": [], "kept": [],
    }
    for rel in real:
        src, dst = src_pdir / rel, dst_pdir / rel
        if dst.exists() or _is_reparse(dst):
            # 目标里已经有这个东西：一律不覆盖。覆盖 .pnpm 里的条目会连累别的包，
            # 而版本对不对由调用方核对后写进结果，不靠"悄悄盖掉"。
            res["kept"].append(rel)
            continue
        stats = copy_tree(src, dst)
        res["files"] += stats["files"]
        res["bytes"] += stats["bytes"]
        res["links"] += stats["links"]
        res["errors"] += list(stats["errors"])
        res["fresh"].append(rel)
        # 目录内部的链接（.pnpm/<x>/node_modules/<dep>）按目标 profile 的路径重建
        relink = relink_junctions(src, dst, [(src_pdir, dst_pdir)])
        res["links"] += relink["made"]
        res["errors"] += list(relink["errors"])

    # 顶层入口：源里是链接的，按目标路径把链接建回来（内容已经在上面复制过了）
    made_at_top: list[str] = []
    for name in names:
        src_entry = src_pdir / "node_modules" / name
        dst_entry = dst_pdir / "node_modules" / name
        try:
            if not src_entry.exists():
                continue
        except OSError:
            continue
        if dst_entry.exists() or _is_reparse(dst_entry):
            if name in pre_existing:
                res["kept"].append(f"node_modules/{name}")
            continue
        if not _is_reparse(src_entry):
            continue          # 普通目录：闭包里已经整体复制过
        target = _read_link_target(src_entry)
        if not target:
            continue
        try:
            rel = str(Path(strip_win_prefix(target)).relative_to(src_pdir)).replace("\\", "/")
        except ValueError:
            continue          # 指向 profile 外面：不复制、也不重建
        new_target = dst_pdir / rel
        if not new_target.exists():
            res["errors"].append((str(dst_entry), f"链接目标还没复制上：{rel}"))
            continue
        try:
            make_junction(new_target, dst_entry)
            res["links"] += 1
            made_at_top.append(f"node_modules/{name}")
        except Exception as exc:  # noqa: BLE001
            res["errors"].append((str(dst_entry), f"{type(exc).__name__}: {exc}"))
    res["top_links"] = made_at_top
    return res


def _merge_profile_manifest(pdir: Path, deps: dict, bundles: list[str],
                            overwrite_deps: bool = False) -> dict:
    """把依赖与组合包登记合并进目标 profile 的 package.json（其它字段一字不动）。"""
    manifest = pdir / "package.json"
    data: dict = {}
    if manifest.is_file():
        try:
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    cur_deps = data.get("dependencies")
    if not isinstance(cur_deps, dict):
        cur_deps = {}
    added_deps, changed_deps = [], []
    for name, spec in deps.items():
        if name in cur_deps and not overwrite_deps:
            continue          # 清单里已经有这一条：不动它（pnpm 自己写的那份说了算）
        before = cur_deps.get(name)
        if before == spec:
            continue
        cur_deps[name] = spec
        (changed_deps if before is not None else added_deps).append(name)
    data["dependencies"] = cur_deps
    dsh = data.get("dsh")
    if not isinstance(dsh, dict):
        dsh = {}
    profile_block = dsh.get("profile")
    if not isinstance(profile_block, dict):
        profile_block = {}
    cur_bundles = profile_block.get("bundles")
    if not isinstance(cur_bundles, list):
        cur_bundles = []
    added_bundles = []
    for bundle in bundles:
        if bundle and bundle not in cur_bundles:
            cur_bundles.append(bundle)
            added_bundles.append(bundle)
    profile_block["bundles"] = cur_bundles
    dsh["profile"] = profile_block
    data["dsh"] = dsh
    pdir.mkdir(parents=True, exist_ok=True)
    backup = _backup_file(manifest)
    tmp = manifest.with_name(manifest.name + ".dsh-lanes.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, manifest)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "why": str(exc), "backup": backup,
                "added_deps": [], "changed_deps": [], "added_bundles": []}
    return {"ok": True, "why": "", "backup": backup,
            "added_deps": added_deps, "changed_deps": changed_deps,
            "added_bundles": added_bundles}


ENTRY_ID_RE = re.compile(r"^- id:\s*['\"]?([A-Za-z0-9_.@/:-]+)['\"]?\s*$")


def _patch_entry_blocks(text: str) -> list[tuple[str, list[str]]]:
    """把补丁层按**顶层条目**切开：[(行 id 或 "", 条目原文行)]。

    只认列 0 的 `- ...`：用户层就是一份顶层 YAML 数组，条目从列 0 开始，后续行缩进更深。
    切错会写出重复键（2026-09-27 那次 profile 起不来就是这个原因），所以宁可切不进去、
    也不要切错——切不进去的条目我们只是不搬，不会写坏任何东西。
    """
    blocks: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for raw in text.splitlines():
        if raw.startswith("- ") or raw.rstrip() == "-":
            if cur is not None:
                blocks.append(cur)  # type: ignore[arg-type]
            cur = [raw]
            continue
        if cur is not None:
            cur.append(raw)
    if cur is not None:
        blocks.append(cur)
    out: list[tuple[str, list[str]]] = []
    for block in blocks:
        match = ENTRY_ID_RE.match(block[0].rstrip())
        out.append((match.group(1) if match else "", block))
    return out


def patch_blocks_for_ids(text: str, ids: set[str]) -> list[list[str]]:
    """挑出源补丁层里针对这些行 id 的条目（原文照搬，不改一个字符）。"""
    return [block for row_id, block in _patch_entry_blocks(text) if row_id and row_id in ids]


def merge_patch_blocks(dst_text: str, blocks: list[list[str]]) -> tuple[bool, str, str, list[str]]:
    """把条目追加到目标补丁层（目标里已有的行 id 不重复写）。"""
    have = {row_id for row_id, _ in _patch_entry_blocks(dst_text) if row_id}
    fresh: list[list[str]] = []
    added: list[str] = []
    for block in blocks:
        match = ENTRY_ID_RE.match(block[0].rstrip())
        row_id = match.group(1) if match else ""
        if not row_id or row_id in have:
            continue
        have.add(row_id)
        fresh.append(block)
        added.append(row_id)
    if not fresh:
        return True, "", dst_text, []
    ok_dialect, why, new_text = append_patch_entries(
        dst_text, [line for block in fresh for line in block]
    )
    if not ok_dialect:
        return False, why, dst_text, []
    return True, "", new_text, added


NON_REGISTRY_SPEC_RE = re.compile(r"^(file|link|github|git\+|git@|https?|ssh|workspace):", re.I)


def _spec_for_install(src_pdir: Path, pkg: str, spec: str, version: str) -> str:
    """算出该在目标里点名装什么。

    规矩：**能点名到具体版本就一定点名**——`^1.2.0` 换个时间会解析成另一个版本，
    那就不叫"迁移"了，叫"装个新的"。只有三类例外：

    · `file:` / `link:` —— 本地路径，没有版本号这回事。相对路径是相对**源 profile** 的，
      而目标 profile 的深度不一样（这个项目复制副本时踩过同一个坑），所以换成绝对路径。
    · git / 远程地址 —— 实测里 `dsh-liquid-glass` 写的是 `github:xingyingyuzhui/...`，
      它装的 0.12.82 在 npm registry 上**未必存在**（这正是用 git 装的原因）。
      这种必须原样搬 spec，改写成版本号会 404。
    · git 地址带 commit 也是同理：锁定的就是那个 commit。
    """
    raw = (spec or "").strip()
    if raw.startswith(("file:", "link:")):
        tail = raw.split(":", 1)[1]
        path = Path(tail)
        if not path.is_absolute():
            path = src_pdir / path
        try:
            return f"file:{path.resolve()}"
        except OSError:
            return f"file:{path}"
    if NON_REGISTRY_SPEC_RE.match(raw):
        return raw
    if version:
        return version
    return raw or "latest"


def plugin_transfer_plan(cfg: dict, src_lane: str, src_profile: str,
                         dst_lane: str, dst_profile: str, pkgs: list[str],
                         mode: str = "install") -> dict:
    """先算清楚"会发生什么"，再让人点确认。任何一条不对就整件事不做。"""
    plan: dict = {"ok": False, "why": "", "mode": mode, "items": [], "warnings": []}
    if mode not in PLUGIN_TRANSFER_MODES:
        plan["why"] = f"不认识的迁移方式「{mode}」（只有 {'、'.join(PLUGIN_TRANSFER_MODES)}）"
        return plan
    lanes = cfg.get("lanes", {})
    for label, lane in (("源", src_lane), ("目标", dst_lane)):
        if lane not in lanes:
            plan["why"] = f"{label} lane「{lane}」不存在"
            return plan
    if src_lane == dst_lane and src_profile == dst_profile:
        plan["why"] = f"源和目标都是「{src_lane}/{src_profile}」，没有可搬的东西"
        return plan

    src_home, dst_home = lane_home(cfg, src_lane), lane_home(cfg, dst_lane)
    src_pdir = profile_dir(src_home, src_profile)
    dst_pdir = profile_dir(dst_home, dst_profile)
    src_view = lane_profiles_view(cfg, src_lane)
    if src_profile not in src_view or not src_view[src_profile]["onDisk"]:
        plan["why"] = (f"源 profile「{src_lane}/{src_profile}」在磁盘上不存在"
                       f"（现有：{'、'.join(sorted(src_view)) or '无'}）")
        return plan
    try:
        validate_profile_name(dst_profile)
    except ValueError as exc:
        plan["why"] = str(exc)
        return plan

    listing = plugin_listing(cfg, src_lane, src_profile)
    by_pkg = {item["pkg"]: item for item in listing["bundles"]}
    wanted: list[str] = []
    for pkg in pkgs:
        name = (pkg or "").strip()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        plan["why"] = "没有勾选任何插件"
        return plan

    items = []
    for name in wanted:
        item = by_pkg.get(name)
        if item is None:
            plan["why"] = (f"源里没有插件「{name}」（它既不在 profile 的依赖里，"
                           f"也不在 dsh.profile.bundles 里）")
            return plan
        version = installed_pkg_version(src_home, name, src_profile) or ""
        spec = item["spec"] or ""
        after = _spec_for_install(src_pdir, name, spec, version)
        dst_have = installed_pkg_version(dst_home,
                                         name, dst_profile) if dst_pdir.is_dir() else None
        action = "install" if mode == "install" else "copy"
        why = ""
        if not item["installed"] and mode == "install":
            action, why = "skip", "源里其实没装上（只有清单登记）"
        elif dst_have == version and version:
            # 需求①：两边同版本就别搬了——重装一次既费时又可能把目标里
            # 已经调好的东西（软链、本地改动）覆盖掉。直接过滤掉，并在计划里说清楚。
            action, why = "skip", f"两边都是 {version}（自动过滤）"
        items.append({
            "pkg": name, "spec": spec, "version": version, "want": after,
            "official": item["official"], "rows": list(item["rows"]),
            "dst_have": dst_have or "", "action": action, "why": why,
        })

    plan.update({
        "ok": True,
        "src": {"lane": src_lane, "profile": src_profile, "home": src_home, "dir": src_pdir},
        "dst": {"lane": dst_lane, "profile": dst_profile, "home": dst_home, "dir": dst_pdir,
                "onDisk": dst_pdir.is_dir()},
        "items": items,
        "patch_file": plugin_patch_path(cfg, dst_lane, dst_profile),
        "src_patch_text": listing["patch_text"],
    })
    if not plan["dst"]["onDisk"]:
        plan["warnings"].append(
            f"目标 profile「{dst_lane}/{dst_profile}」磁盘上还没有，会按官方 web 组合先建一个干净环境"
        )
    if lane_runtime(cfg, dst_lane, dst_profile):
        plan["warnings"].append(
            f"目标「{dst_lane}/{dst_profile}」正在运行：写的文件要重启那次启动才会生效"
        )
    if mode == "copy":
        plan["warnings"].append(
            "离线直拷不会同步 pnpm 账本（pnpm-lock.yaml）：包能用，但之后在目标里装别的东西时"
            "pnpm 可能要重新解析一次"
        )
    return plan


def transfer_plugins(cfg: dict, src_lane: str, src_profile: str,
                     dst_lane: str, dst_profile: str, pkgs: list[str],
                     mode: str = "install", timeout: int = 1800) -> dict:
    """真正搬插件。返回逐条结果，成败都写清楚。"""
    me = ME_NAME
    plan = plugin_transfer_plan(cfg, src_lane, src_profile, dst_lane, dst_profile, pkgs, mode)
    if not plan["ok"]:
        fail(plan["why"])
        return {"ok": False, "why": plan["why"], "items": [], "plan": plan}

    items = [i for i in plan["items"] if i["action"] != "skip"]
    if not items:
        # 全都"目标里已经是这个版本"：不算失败，但也确实什么都没做
        for item in plan["items"]:
            ok(f"  {item['pkg']}：{item['why'] or '无需处理'}")
        info(col("  没有需要搬的东西。", C.GRAY))
        return {"ok": True, "why": "", "items": plan["items"], "plan": plan, "done": 0}

    what = "重新安装（点名到版本）" if mode == "install" else "离线直拷（不走网络/node）"
    info(col(f"── 迁移插件 ──", C.BOLD))
    info(f"  源        {src_lane}/{src_profile}")
    info(f"  目标      {dst_lane}/{dst_profile}")
    info(f"  方式      {what}")
    info(f"  插件      {len(items)} 个：{'、'.join(i['pkg'] for i in items)}")
    for line in plan["warnings"]:
        warn(f"  注意      {line}")

    dst = plan["dst"]
    dst_pdir: Path = dst["dir"]
    if not dst["onDisk"]:
        info(col("  目标 profile 磁盘上不存在：先按官方 web 组合建干净环境", C.GRAY))
        init_profile_dir(dst_pdir, SHIPPED_TEMPLATES[DEFAULT_PROFILE])
        set_profile_hint(cfg, dst_lane, dst_profile,
                         createdAt=time.strftime("%Y-%m-%d %H:%M:%S"))

    results = []
    if mode == "install":
        for item in items:
            target_spec = f"{item['pkg']}@{item['want']}"
            info(col(f"  · 装 {target_spec} → {dst_lane}/{dst_profile}（走 dsh plugin → pnpm）…", C.GRAY))
            res = run_plugin_cmd(cfg, dst_lane, ["add", target_spec, "--save-exact"],
                                 timeout=timeout, label="plugin-transfer",
                                 profile=dst_profile)
            got = installed_pkg_version(dst["home"], item["pkg"], dst_profile) or ""
            entry = dict(item)
            entry.update({
                "ok": bool(res["ok"]) and bool(got),
                "got": got,
                "rc": res["rc"],
                "error": res["error"],
                "tail": list(res["tail"])[-6:],
            })
            if entry["ok"]:
                ok(f"  √ {item['pkg']} {got or item['want']}"
                   + ("（与源一致）" if got == item["version"] and item["version"] else ""))
                if item["version"] and got != item["version"]:
                    warn(f"    源里是 {item['version']}，目标装成 {got}（这个版本在目标的 dsh 版本上可能没同步）")
            else:
                fail(f"  × {item['pkg']} 没装上（退出码 {res['rc']}"
                     f"{'：' + res['error'] if res.get('error') else ''}）")
                for line in entry["tail"]:
                    info(col("      " + line, C.GRAY))
            results.append(entry)
    else:
        info(col("  · 正在复制依赖闭包…", C.GRAY))
        copied = _copy_plugin_files(plan["src"]["dir"], dst_pdir, [i["pkg"] for i in items])
        info(col(f"    复制 {copied['files']} 个文件 / {human_size(copied['bytes'])}，"
                 f"重建链接 {copied['links']} 个，目标里已有跳过 {len(copied['kept'])} 项", C.GRAY))
        for rel in copied["fresh"]:
            info(col(f"      + {rel}", C.GRAY))
        for rel in copied["kept"]:
            info(col(f"      = {rel}（目标里本来就有，没覆盖）", C.GRAY))
        for name in copied["outside"]:
            warn(f"      ! {name} 的链接指向 profile 外面（宿主安装树之类），没有复制")
        for name in copied["missing"]:
            warn(f"      ! {name} 在源里没找到")
        for path_text, reason in copied["errors"][:6]:
            warn(f"      ! {path_text}：{reason}")
        for item in items:
            got = installed_pkg_version(dst["home"], item["pkg"], dst_profile) or ""
            entry = dict(item)
            entry.update({"ok": bool(got), "got": got, "rc": 0, "error": "", "tail": []})
            if entry["ok"]:
                ok(f"  √ {item['pkg']} {got}")
            else:
                fail(f"  × {item['pkg']} 复制后仍然不在目标里")
            results.append(entry)

    # ── 清单：依赖 + 组合包登记（不登记 bundles 宿主根本不会加载它）
    deps = {i["pkg"]: (i["want"] if mode == "copy" else (i["spec"] or i["want"]))
            for i in results if i["ok"]}
    bundle_src = read_profile_bundles(plan["src"]["home"], src_profile)
    bundles = [i["pkg"] for i in results if i["ok"] and i["pkg"] in bundle_src]
    merge = _merge_profile_manifest(dst_pdir, deps, bundles,
                                    overwrite_deps=(mode == "copy"))
    if not merge["ok"]:
        fail(f"  更新目标 profile 的 package.json 失败：{merge['why']}")
    else:
        if merge["backup"]:
            info(col(f"  package.json 已留底：{merge['backup']}", C.GRAY))
        if merge["added_deps"] or merge["changed_deps"]:
            info(col(f"  依赖已写入：{'、'.join(merge['added_deps'] + merge['changed_deps'])}", C.GRAY))
        if merge["added_bundles"]:
            info(col(f"  组合包登记已写入 dsh.profile.bundles：{'、'.join(merge['added_bundles'])}", C.GRAY))
        missing_bundles = [i["pkg"] for i in results if i["ok"]
                           and i["pkg"] in bundle_src
                           and i["pkg"] not in read_profile_bundles(dst["home"], dst_profile)]
        if missing_bundles:
            warn(f"  这几个包在源里是组合包、但没写进目标：{'、'.join(missing_bundles)}")

    # ── 补丁层：把源里针对这些插件行 id 的覆盖条目搬过去（"关掉的插件"才不会被重新打开）
    wanted_rows = {row for item in results if item["ok"] for row in item["rows"]}
    blocks = patch_blocks_for_ids(plan["src_patch_text"], wanted_rows) if wanted_rows else []
    if blocks:
        patch_file: Path = plan["patch_file"]
        text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
        backup = _backup_file(patch_file)
        ok_merge, why, new_text, added = merge_patch_blocks(text, blocks)
        if not ok_merge:
            warn(f"  补丁层没搬（{why}）——源里对这些插件的开关状态需要你自己在目标里重设")
        elif added:
            wrote, why2 = write_patch_text(patch_file, new_text)
            if wrote:
                ok(f"  补丁层已搬过去 {len(added)} 条覆盖：{'、'.join(added)}")
                if backup:
                    info(col(f"      原补丁层已留底：{backup}", C.GRAY))
            else:
                warn(f"  补丁层写入失败：{why2}")
        else:
            info(col("  补丁层：目标里已经有同样的条目，没重复写", C.GRAY))

    good = [i for i in results if i["ok"]]
    bad = [i for i in results if not i["ok"]]
    info("")
    if bad:
        warn(f"  结果：成功 {len(good)} 个，失败 {len(bad)} 个：{'、'.join(i['pkg'] for i in bad)}")
    else:
        ok(f"  结果：{len(good)} 个插件都已就位")
    info(col(f"  核对： py {me} plugins {dst_lane} --profile {dst_profile}", C.CYAN))
    if lane_runtime(cfg, dst_lane, dst_profile):
        info(col(f"  目标正在运行 —— 重启它才会加载新插件： "
                 f"py {me} stop {dst_lane}/{dst_profile} ； py {me} open {dst_lane}/{dst_profile}", C.CYAN))
    return {"ok": not bad, "why": "", "items": results, "plan": plan, "done": len(good),
            "failed": [i["pkg"] for i in bad], "merge": merge}


def _unmerge_profile_manifest(pdir: Path, pkg: str) -> dict:
    """从 profile 的 package.json 里**摘掉**一个包：dependencies + dsh.profile.bundles。

    卸载插件时必须做这一步：`dsh plugin remove`（pnpm）只会动它自己写的那部分，
    而 `dsh.profile.bundles` 是宿主**真正加载谁**的清单——留着它，卸载过的插件下次
    启动还会被当作"该加载"（然后因为文件没了而报错/静默跳过）。所以两处一起清。
    """
    manifest = pdir / "package.json"
    res = {"ok": False, "why": "", "backup": "", "removed_dep": False, "removed_bundle": False}
    if not manifest.is_file():
        return {"ok": True, "why": "profile 还没有 package.json", "backup": "",
                "removed_dep": False, "removed_bundle": False}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        res["why"] = f"package.json 读不出来：{exc}"
        return res
    if not isinstance(data, dict):
        res["why"] = "package.json 顶层不是对象"
        return res
    deps = data.get("dependencies")
    if isinstance(deps, dict) and pkg in deps:
        deps.pop(pkg, None)
        res["removed_dep"] = True
    dsh = data.get("dsh")
    profile_block = (dsh or {}).get("profile") if isinstance(dsh, dict) else None
    if isinstance(profile_block, dict):
        bundles = profile_block.get("bundles")
        if isinstance(bundles, list) and pkg in bundles:
            profile_block["bundles"] = [b for b in bundles if b != pkg]
            res["removed_bundle"] = True
    if not (res["removed_dep"] or res["removed_bundle"]):
        return {"ok": True, "why": "清单里本来就没有这个包", "backup": "",
                "removed_dep": False, "removed_bundle": False}
    backup = _backup_file(manifest)
    tmp = manifest.with_name(manifest.name + ".dsh-lanes.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, manifest)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "why": str(exc), "backup": backup,
                "removed_dep": res["removed_dep"], "removed_bundle": res["removed_bundle"]}
    res.update({"ok": True, "why": "", "backup": backup})
    return res


def _pkg_name_from_spec(spec: str) -> str:
    """从 `@scope/name@1.2.3` / `name@ver` 里抠出包名；非 registry 的 spec 返回空。"""
    raw = (spec or "").strip()
    if not raw or NON_REGISTRY_SPEC_RE.match(raw):
        return ""
    if raw.startswith("@"):
        parts = raw.split("@", 2)
        return f"@{parts[1]}" if len(parts) > 1 and parts[1] else ""
    return raw.split("@", 1)[0]


def add_plugin(cfg: dict, lane: str, profile: str, spec: str, timeout: int = 1800) -> dict:
    """给某个 profile 装一个插件：dsh plugin add（离线优先）→ 登记依赖 + 组合包。

    为什么不能只跑 pnpm：宿主按 `dsh.profile.bundles` 决定加载谁，**只装不登记不会生效**。
    所以装完之后照 `transfer_plugins` 的规矩补两样：dependencies 与 bundles（bundle 形态才登）。
    """
    out = {"ok": False, "pkg": "", "version": "", "kind": "", "mode": "",
           "tail": [], "merge": None, "error": "", "how": spec}
    if lane not in cfg.get("lanes", {}):
        out["error"] = f"没有 lane「{lane}」"
        return out
    try:
        validate_profile_name(profile)
    except ValueError as exc:
        out["error"] = str(exc)
        return out
    spec = (spec or "").strip()
    if not spec:
        out["error"] = "没写要装什么（包名，可带版本号，如 @scope/name@1.2.3）"
        return out
    home = lane_home(cfg, lane)
    pdir = profile_dir(home, profile)
    does_not_exist = not pdir.is_dir()
    if does_not_exist:
        out["error"] = (f"profile「{lane}/{profile}」在磁盘上还不存在 —— "
                        "请先用 profile-add 或 plugin-transfer 建它")
        return out
    before_deps = set(profile_dependencies(home, profile))
    guess = _pkg_name_from_spec(spec)
    non_registry = bool(NON_REGISTRY_SPEC_RE.match(spec))
    res = plugin_add_smart(cfg, lane, profile, spec, label="plugin-add",
                           timeout=timeout, non_registry=non_registry)
    out["mode"] = res.get("mode", "")
    out["tail"] = list(res.get("tail") or [])[-10:]
    if not res.get("ok"):
        out["error"] = (f"装不上（退出码 {res.get('rc')}"
                        f"{'：' + res['error'] if res.get('error') else ''}）")
        return out
    # 认包名：registry spec 能直接抠出来；git/file 之类的靠装前装后的依赖差找新键
    pkg = ""
    if guess and installed_pkg_dir(home, guess, profile).is_dir():
        pkg = guess
    if not pkg:
        fresh = [k for k in profile_dependencies(home, profile) if k not in before_deps]
        if len(fresh) == 1:
            pkg = fresh[0]
        elif fresh:
            pkg = guess if guess in fresh else sorted(fresh)[0]
    if not pkg or not installed_pkg_dir(home, pkg, profile).is_dir():
        out["error"] = ("装完了，但认不出装了哪个包（依赖清单里没多出新条目）—— "
                        f"请用 plugins {lane} --profile {profile} 看一眼")
        return out
    version = installed_pkg_version(home, pkg, profile) or ""
    manifest = package_manifest(home, pkg, profile)
    dsh_block = manifest.get("dsh") or {}
    rows = bundle_row_ids(home, pkg, profile)
    kind = package_kind(home, pkg, rows, profile)
    # 登记：包是不是组合包由它自己的 dsh.bundle 说了算（有才登记进 bundles）
    bundles = [pkg] if dsh_block.get("bundle") else []
    merge = _merge_profile_manifest(pdir, {}, bundles)
    out.update({"ok": True, "pkg": pkg, "version": version, "kind": kind,
                "rows": rows, "merge": merge, "bundles": bundles})
    if not merge.get("ok"):
        out["error"] = f"装上了，但登记失败：{merge.get('why')}"
    return out


def remove_plugin(cfg: dict, lane: str, profile: str, pkg: str,
                  timeout: int = 1200, clean_rows: bool = False, verify: bool = True) -> dict:
    """卸载一个插件：dsh plugin remove（pnpm）→ 摘掉依赖 + 组合包登记 → 清补丁行。

    `clean_rows=True` 时顺带删掉补丁层里属于它的覆盖条目（默认**保留**：留着的坏覆盖不会报错，
    只是静默失效，但删错了会破坏别的行——所以默认不动，交给用户显式选择）。
    """
    out = {"ok": False, "pkg": pkg, "rc": -1, "rows_cleaned": False, "merge": None,
           "backup": "", "verify": "skip", "verify_why": "", "tail": [], "error": ""}
    if lane not in cfg.get("lanes", {}):
        out["error"] = f"没有 lane「{lane}」"
        return out
    try:
        validate_profile_name(profile)
    except ValueError as exc:
        out["error"] = str(exc)
        return out
    pkg = (pkg or "").strip()
    if not pkg:
        out["error"] = "没写要卸哪个插件"
        return out
    if is_official_bundle(pkg):
        out["error"] = f"{pkg} 是官方组合包，不能这样卸（会拆掉 DSH 自己的部件）"
        return out
    if is_protected_module(pkg):
        out["error"] = f"{pkg} 属于宿主基础设施，禁止卸载（会破坏热加载/传输/存储链）"
        return out
    if pkg == MARKET_PKG:
        out["error"] = f"{pkg}（插件市场）请用 market-off/市场页面处理，不要直接卸"
        return out
    home = lane_home(cfg, lane)
    pdir = profile_dir(home, profile)
    if not pdir.is_dir():
        out["error"] = f"profile「{lane}/{profile}」在磁盘上不存在"
        return out
    if not installed_pkg_dir(home, pkg, profile).is_dir() and pkg not in profile_dependencies(home, profile):
        out["ok"] = True
        out["error"] = ""  # 本来就没装：不算失败
        out["tail"] = ["（这个包本来就没装，什么都没做）"]
        return out
    rows = bundle_row_ids(home, pkg, profile)

    res = run_plugin_cmd(cfg, lane, ["remove", pkg], timeout=timeout,
                         label="plugin-rm", profile=profile)
    out["rc"] = res["rc"]
    out["tail"] = list(res.get("tail") or [])[-10:]
    if not res.get("ok"):
        out["error"] = (f"卸载失败（退出码 {res['rc']}"
                        f"{'：' + res['error'] if res.get('error') else ''}）")
        return out

    merge = _unmerge_profile_manifest(pdir, pkg)
    out["merge"] = merge
    if not merge.get("ok"):
        out["error"] = f"文件删了，但清单没改干净：{merge.get('why')}"

    if clean_rows and rows:
        patch_file = plugin_patch_path(cfg, lane, profile)
        if patch_file.is_file():
            text = patch_file.read_text(encoding="utf-8")
            new_text = ensure_patch_array(remove_patch_row_entries(text, rows))
            backup = _backup_file(patch_file)
            wrote, why_write = write_patch_text(patch_file, new_text)
            out["rows_cleaned"] = bool(wrote)
            out["backup"] = backup
            if not wrote:
                out["error"] = out["error"] or f"补丁层清理失败：{why_write}"
    out["ok"] = not out["error"]
    if verify and out["ok"] and not clean_rows:
        pass  # 没动补丁层就没什么可核对的
    return out


def cmd_plugin_add(cfg: dict, args) -> int:
    """给某个 profile 加装一个插件（装 + 登记 bundles）。"""
    lane = args.lane
    prof = getattr(args, "profile", None) or PROFILE_NAME
    spec = " ".join(getattr(args, "spec", []) or [])
    info(col(f"── 加装插件：{lane}/{prof} ──", C.BOLD))
    info(col(f"  {spec}（离线优先，缺包才联网）…", C.GRAY))
    res = add_plugin(cfg, lane, prof, spec, timeout=int(getattr(args, "timeout", 1800) or 1800))
    for line in res["tail"]:
        info(col("      " + line, C.GRAY))
    if not res["ok"]:
        fail(f"  {res['error']}")
        return 1
    ok(f"  √ {res['pkg']} {res['version'] or ''}（{res['mode'] or '?'}）已装好")
    if res["bundles"]:
        ok(f"  已登记进 dsh.profile.bundles（宿主下次启动会加载它）")
    if res.get("kind") == "client-only":
        info(col("  纯客户端插件：补丁层没有它的行，开关在插件市场页面上。", C.GRAY))
    if lane_runtime(cfg, lane, prof):
        warn("  这条 profile 正在运行：重启它才会加载新插件")
        info(col(f"      py {ME_NAME} stop {lane}/{prof} ； py {ME_NAME} open {lane}/{prof}", C.CYAN))
    return 0


def cmd_plugin_rm(cfg: dict, args) -> int:
    """卸载某个 profile 里的一个插件（host 文件 + 清单登记一起清）。"""
    lane = args.lane
    prof = getattr(args, "profile", None) or PROFILE_NAME
    targets = list(getattr(args, "target", []) or [])
    if not targets:
        fail("没写要卸哪个插件")
        return 1
    info(col(f"── 卸载插件：{lane}/{prof} ──", C.BOLD))
    bad = 0
    for target in targets:
        listing = plugin_listing(cfg, lane, prof)
        item, why = resolve_plugin_target(listing, target)
        name = item["pkg"] if item else target
        if item is None and not name:
            fail(f"  × {target}：{why}")
            bad += 1
            continue
        res = remove_plugin(cfg, lane, prof, name, timeout=int(getattr(args, "timeout", 1200) or 1200),
                            clean_rows=bool(getattr(args, "clean_rows", False)))
        for line in res["tail"]:
            info(col("      " + line, C.GRAY))
        if res["ok"]:
            ok(f"  √ {name} 已卸载" + ("（顺带清了补丁行）" if res["rows_cleaned"] else ""))
        else:
            fail(f"  × {name}：{res['error']}")
            bad += 1
    if lane_runtime(cfg, lane, prof):
        warn("  这条 profile 正在运行：重启它才会卸载生效")
    return 1 if bad else 0


def cmd_plugin_transfer(cfg: dict, args) -> int:
    """把插件从一个 profile 搬到另一个 profile / 另一个版本。"""
    lane = getattr(args, "lane", "") or ""
    parts = lane.split("/", 1)
    src_lane, src_profile = parts[0], (parts[1] if len(parts) > 1 else PROFILE_NAME)
    target = (getattr(args, "to", "") or "")
    if not src_lane or not target:
        fail("用法：plugin-transfer <源lane[/profile]> <目标lane[/profile]> 插件名 [插件名…]")
        return 1
    parts = target.split("/", 1)
    dst_lane, dst_profile = parts[0], (parts[1] if len(parts) > 1 else PROFILE_NAME)
    pkgs = list(getattr(args, "names", []) or [])
    if not pkgs:
        fail("没写要搬哪些插件（也可以写 all＝源里全部第三方插件）")
        return 1
    if pkgs == ["all"]:
        pkgs = [i["pkg"] for i in profile_plugins(cfg, src_lane, src_profile)]
        if not pkgs:
            fail(f"源「{src_lane}/{src_profile}」里没有第三方插件")
            return 1
    mode = getattr(args, "mode", None) or "install"
    if getattr(args, "dry_run", False):
        plan = plugin_transfer_plan(cfg, src_lane, src_profile, dst_lane, dst_profile, pkgs, mode)
        if not plan["ok"]:
            fail(plan["why"])
            return 1
        info(col(f"── 迁移计划（--dry-run，什么都没做）──", C.BOLD))
        info(f"  源        {src_lane}/{src_profile}")
        info(f"  目标      {dst_lane}/{dst_profile}"
             + ("" if plan["dst"]["onDisk"] else "（磁盘上还没有，会先建）"))
        info(f"  方式      {'重新安装（点名到版本）' if mode == 'install' else '离线直拷'}")
        for item in plan["items"]:
            info(f"  · {item['pkg']}"
                 + (f"  {item['version']}" if item["version"] else "")
                 + f"  → {item['want']}"
                 + (f"　（{item['why']}）" if item["why"] else ""))
        for line in plan["warnings"]:
            warn(f"  注意      {line}")
        return 0
    res = transfer_plugins(cfg, src_lane, src_profile, dst_lane, dst_profile, pkgs, mode)
    return 0 if res["ok"] else 1


def cmd_plugins(cfg: dict, args) -> int:
    """列出这条 lane 的插件格局：插件 → 真实行 id → 现在关没关，**谁能关它**。

    默认**不跑组合**（毫秒级返回）：开关要的"补丁层现在怎么说"只取决于文件。
    加 `--dump` 才额外离线组合一次，用来核对"文件说的"和"组合结果"是不是一致。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    prof = getattr(args, "profile", None) or PROFILE_NAME
    listing = plugin_listing(cfg, lane, prof)
    me = ME_NAME
    info(col(f"── lane「{lane}」的插件开关（profile {listing['profile']}）──　"
             "（这里只读；开关用 plugin-off / plugin-on / plugins-off / plugins-on）", C.BOLD))
    info(f"  DSH_HOME : {listing['home']}")
    info(f"  补丁层   : {listing['patch_file']}"
         + ("（有一键禁用块）" if listing["has_block"] else "")
         + ("（有插件市场开关块）" if listing["has_market_block"] else ""))
    if not listing["dialect_ok"]:
        warn(f"  这份补丁层现在追加不了条目：{listing['dialect_why']}")
    if not listing["array_ok"]:
        warn("  这份补丁层只剩注释 —— 不是顶层数组，dsh 会**拒绝启动**这条 lane；"
             "随便开关一次插件就会自动补回 `[]`")
    info("")
    third_party = [i for i in listing["bundles"] if not i["official"]]
    if not third_party:
        info(col("  这条 lane 没有第三方插件（只有官方组合包），没有可开关的东西", C.GRAY))
    for item in listing["bundles"]:
        if item["official"]:
            continue
        if item["disabled_rows"] and len(item["disabled_rows"]) == len(item["rows"]):
            state, mark = col("已禁用", C.GREEN), "×"
        elif item["disabled_rows"]:
            state, mark = col(f"部分禁用 {len(item['disabled_rows'])}/{len(item['rows'])}", C.YELLOW), "~"
        else:
            state, mark = "启用中", "√"
        if item["kind"] == "bundle":
            where = "有补丁行 → 到「插件市场」页面里开关"
        elif item["kind"] == "client-only":
            where = col("纯客户端：补丁层没有它的行，去「插件市场…」里关", C.YELLOW)
        else:
            where = col("没有可开关的行", C.GRAY)
        if item["skipped"]:
            where = col("启动时被跳过（版本不兼容）", C.RED)
        info(f"  [{mark}] {item['pkg']}   {state}")
        info(col(f"        行 id：{'、'.join(item['rows']) or '（无）'}　→　{where}", C.GRAY))
        if item["forced_rows"]:
            info(col(f"        其中 {len(item['forced_rows'])} 行是强制打开（disabled: false）："
                     f"{'、'.join(item['forced_rows'])}", C.GRAY))
    official = [i["pkg"] for i in listing["bundles"] if i["official"]]
    client_only = [i["pkg"] for i in listing["bundles"] if not i["official"] and i["kind"] == "client-only"]
    norows = [i["pkg"] for i in listing["bundles"] if not i["official"] and i["kind"] == "norows"]
    info("")
    info(col(f"  官方组合包（不会被一键禁用动）：{'、'.join(official) or '无'}", C.GRAY))
    if client_only:
        info(col(f"  纯客户端插件（补丁层关不掉，要去插件市场里关）：{'、'.join(client_only)}", C.GRAY))
    if norows:
        info(col(f"  没有补丁行、也没有客户端部分：{'、'.join(norows)}", C.GRAY))
    if listing["skipped"]:
        warn(f"启动时被跳过的组合包：{'、'.join(listing['skipped'])}")
    malformed = [row for row in listing["patch_state"]["disables"] if row not in
                 {r for i in listing["bundles"] for r in i["rows"]}]
    if malformed:
        warn(f"补丁层里有 {len(malformed)} 条禁用行没对上任何已装插件（写错了或目标已移除），"
             "这类覆盖**不会报错**，只是静默失效：")
        info(col("      " + "、".join(malformed), C.GRAY))
    info("")
    info(col(f"  开关单个插件： py {me} plugin-off {lane} <插件名> --profile {listing['profile']}"
             f" ； py {me} plugin-on {lane} <插件名> --profile {listing['profile']}", C.CYAN))
    info(col(f"  加装插件： py {me} plugin-add {lane} <包名> --profile {listing['profile']}"
             f" ； 卸载： py {me} plugin-rm {lane} <插件名> --profile {listing['profile']}", C.CYAN))
    info(col(f"  一键全关（可撤销）： py {me} plugins-off {lane} --profile {listing['profile']}", C.CYAN))
    if getattr(args, "dump", False):
        info("")
        info(col("  离线组合一次，核对文件与组合结果是否一致…", C.GRAY))
        dump = run_dump_config(cfg, lane, prof)
        if not dump.get("ok"):
            fail(f"组合检查失败（{dump.get('error') or '退出码非 0'}）")
            return 1
        rows = dump["rows"]
        problems = 0
        for item in listing["bundles"]:
            if item["official"] or item["kind"] != "bundle":
                continue
            live = [r for r in item["rows"] if r in rows]
            if not live:
                warn(f"{item['pkg']}：补丁层写了 {len(item['rows'])} 行，组合结果里一行都没有"
                     "（被跳过/不兼容/行 id 不对）")
                problems += 1
                continue
            for row_id in live:
                composed_off = bool(rows[row_id].get("disabled"))
                layer_off = row_id in set(listing["patch_state"]["disables"])
                if composed_off != layer_off:
                    warn(f"{item['pkg']} 行 {row_id}：补丁层说 {'禁用' if layer_off else '启用'}，"
                         f"组合结果说 {'禁用' if composed_off else '启用'}（下层还有别的补丁在压它）")
                    problems += 1
        if dump.get("unmatched"):
            warn(f"组合时 {len(dump['unmatched'])} 条覆盖没匹配上：{'、'.join(dump['unmatched'])}")
        if not problems:
            ok("核对通过：补丁层说的和组合结果一致")
    return 0


def cmd_plugin_switch(cfg: dict, args, want_on: bool) -> int:
    """单插件开关：`plugin-off <lane> <插件名>` / `plugin-on <lane> <插件名> [--force]`。

    写法照插件市场（dshmarket/lib/patch.js）：往用户层补丁写/删一条
    `- id: <真实行 id>` + `disabled: …`，而且**行尾不带任何注释**——市场是逐行扫这份
    文件的，行尾多一个 `# 包名` 它就看不见，页面上会显示成"启用中"。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    prof = getattr(args, "profile", None) or PROFILE_NAME
    listing = plugin_listing(cfg, lane, prof)
    item, why = resolve_plugin_target(listing, args.target)
    if item is None:
        fail(why)
        return 1
    name = item["pkg"]
    me = ME_NAME
    if item["official"]:
        fail(f"{name} 是官方组合包，不归这个开关管（关它等于把 DSH 自己的部件摘掉）")
        return 1
    if item["protected"]:
        fail(f"{name} 属于宿主基础设施，禁止开关（关它会破坏热加载/传输/存储链）")
        return 1
    if not item["rows"]:
        if item["kind"] == "client-only":
            warn(f"{name} 是**纯客户端插件**（只有 dsh.client、没有 dsh.bundle）：")
            info(col("      补丁层里没有它的行，所以写补丁层对它无效——它的开关在插件市场"
                     "自己的页面里（市场会记住关掉的清单并在每次启动时重放）。", C.GRAY))
            info(col(f"      要在这里彻底关掉它，只能卸载： py {me} market … 或 dsh plugin remove", C.GRAY))
        else:
            warn(f"{name} 没有任何补丁行，也没有客户端部分 —— 没有可开关的东西。")
        if item["skipped"]:
            info(col("      （它启动时被跳过了：版本不兼容）", C.GRAY))
        return 1
    running = lane_runtime(cfg, lane, prof)
    changes = [(name, want_on)]
    result = apply_plugin_states(cfg, lane, changes, profile=prof,
                                 force=bool(getattr(args, "force", False)))
    info("")
    if not result["ok"]:
        fail(result.get("error") or "写入失败")
        for note in result["skipped"]:
            info(col(f"      {note}", C.GRAY))
        return 1
    for pkg, row_id, action in result["written"]:
        info(f"  {action:<28}{pkg:<30}行 id = {row_id}")
    for note in result["skipped"]:
        info(col(f"      {note}", C.GRAY))
    if not result["changed"]:
        info("")
        if want_on:
            warn("补丁层里没有禁用它的条目，所以文件一个字都没动（它现在就是启用的状态）")
            info(col(f"      如果它在页面上仍是关的，说明是**组合包自己的补丁**压着它；"
                     f"要强行打开： py {me} plugin-on {lane} {name} --force", C.GRAY))
        else:
            info(col("文件没有变化（这些行本来就是禁用的）", C.GRAY))
        return 0
    info("")
    info(col(f"  备份      {result.get('backup')}", C.GRAY))
    verdict = result.get("verify")
    if verdict == "ok":
        ok(f"  自动校验：{result.get('verify_why')}")
    elif verdict == "skip":
        info(col(f"  自动校验跳过：{result.get('verify_why')}", C.GRAY))
    if running:
        ok(f"已写入。这条 lane 正在运行：补丁层是 DSH 热重载**正盯着**的文件（@deepseek-ai/dsh-hmr），"
           "不用重启——实测两个方向都是 ~2.3 秒生效。")
    else:
        info(col("这条 lane 没在运行：下次 open 时生效。想先核对组合结果： "
                 f"py {me} plugins {lane} --profile {prof} --dump", C.GRAY))
    if getattr(args, "verify", False):
        info("")
        info(col("  离线组合一次核对…", C.GRAY))
        dump = run_dump_config(cfg, lane, prof)
        if not dump.get("ok"):
            warn("组合检查没跑起来，改动是否生效未经验证")
            return 0
        rows_now = dump["rows"]
        bad = [row for _p, row, _a in result["written"]
               if row in rows_now and bool(rows_now[row].get("disabled")) != (not want_on)]
        gone = [row for _p, row, _a in result["written"] if row not in rows_now]
        if gone:
            warn(f"这些行在组合结果里找不到：{'、'.join(gone)}")
        if bad:
            fail(f"这些行的组合状态与刚写的不一致：{'、'.join(bad)}")
        if not gone and not bad:
            ok(f"核对通过：{len(result['written'])} 行的组合状态已随开关改变")
    return 0


def cmd_plugin_off(cfg: dict, args) -> int:
    return cmd_plugin_switch(cfg, args, False)


def cmd_plugin_on(cfg: dict, args) -> int:
    return cmd_plugin_switch(cfg, args, True)


def _collect_targets(cfg, lane, keep, profile, dump_rows) -> tuple[list[tuple[str, str]], list[str]]:
    """算出该禁哪些行：[(组合包, 行 id)]。只保留**确实出现在组合结果里**的行。"""
    view = plugin_overview(cfg, lane, profile, dump={"rows": dump_rows, "ok": True})
    home = lane_home(cfg, lane)
    targets: list[tuple[str, str]] = []
    notes: list[str] = []
    for item in view["bundles"]:
        if item["official"]:
            continue
        if item["pkg"] in keep:
            continue
        if is_protected_module(item["pkg"]):
            notes.append(f"{item['pkg']}：属于宿主基础设施（热加载/传输/存储链），禁止开关")
            continue
        if not item["present"]:
            kind = package_kind(home, item["pkg"], item["rows"], profile)
            if kind == "client-only":
                notes.append(f"{item['pkg']}：纯客户端插件（只有 dsh.client）——补丁层里没有它的行，"
                             "要去插件市场自己的页面里关，这里跳过")
            else:
                notes.append(f"{item['pkg']}：当前没有生效的行（被跳过或不兼容），跳过")
            continue
        for row_id in item["present"]:
            targets.append((item["pkg"], row_id))
    return targets, notes


def cmd_plugins_off(cfg: dict, args) -> int:
    """一键禁用**自己装的**（第三方）插件；官方组合包不动。

    只写用户层 `cordis.patch.yml` 里一段带标记的 `disabled: true` 覆盖：
    组合包仍然装着、仍然在 `dsh.profile.bundles` 里，所以撤销就是删掉这段（`plugins-on`）。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    keep = set()
    for item in getattr(args, "keep", None) or []:
        keep.update(x.strip() for x in str(item).split(",") if x.strip())
    prof = getattr(args, "profile", None) or PROFILE_NAME

    patch_file = plugin_patch_path(cfg, lane, prof)
    if not patch_file.is_file():
        fail(f"找不到用户层补丁文件：{patch_file}")
        return 1
    original = patch_file.read_text(encoding="utf-8")

    # 插件市场有自己的单独开关。它已经被那个开关关掉时，这里就别再写一遍
    #（同一行写两次覆盖不会报错，但配置会变得难读，而且两个块的撤销会互相踩）。
    market_note = ""
    if find_block(original.splitlines(), MARKET_BLOCK_BEGIN, MARKET_BLOCK_END) and MARKET_PKG not in keep:
        keep.add(MARKET_PKG)
        market_note = (
            f"{MARKET_PKG}：插件市场已经由它的单独开关关着，这里跳过"
            "（要连市场一起恢复，先 market-on）"
        )

    if lane_runtime(cfg, lane, prof):
        info(col(f"lane「{lane}」正在运行：补丁层是 DSH 热重载盯着的文件，写进去大约 2 秒生效"
                 "（实测 2.3 秒），不用重启；只有启动冒烟跑不了——那条要停一次。", C.GRAY))

    info(col(f"── 一键禁用第三方插件：lane「{lane}」/ profile「{prof}」──", C.BOLD))
    info(col("  正在离线组合一次 profile（dsh --dump-config）拿真实行清单…", C.GRAY))
    dump = run_dump_config(cfg, lane, prof)
    if not dump.get("ok"):
        fail(f"组合检查失败（{dump.get('error') or '退出码 ' + str(dump.get('rc'))}），为了不瞎写就不继续了")
        if dump.get("stderr"):
            for line in str(dump["stderr"]).splitlines()[:6]:
                info(col(f"      {line}", C.GRAY))
        return 1
    rows = dump["rows"]
    targets, notes = _collect_targets(cfg, lane, keep, prof, rows)
    if market_note:
        notes.append(market_note)
    if not targets:
        warn("没有可禁用的第三方插件行")
        for note in notes:
            info(col(f"      {note}", C.GRAY))
        return 0

    already = user_disabled_ids(original)
    to_write: list[tuple[str, str]] = []
    for pkg, row_id in targets:
        if row_id in already:
            notes.append(f"{row_id}：你自己的配置里已经禁用，不动它（撤销后仍会是关的）")
            continue
        to_write.append((pkg, row_id))
    if not to_write:
        warn("目标行都已经处于禁用状态，没有需要写的")
        for note in notes:
            info(col(f"      {note}", C.GRAY))
        return 0

    backup = patch_file.with_suffix(patch_file.suffix + ".bak")
    try:
        backup.write_text(original, encoding="utf-8")
    except OSError as exc:
        fail(f"备份失败，放弃写入：{exc}")
        return 1

    # 头部**原样保留**（一字不改），禁用块追加在后面；撤销时按标记整段删回去。
    # 只摘掉启动器自己那段（保留它后面可能存在的"插件市场开关"块），不能像以前那样
    # `lines[:span[0]]` 一刀切——那样会把排在这段后面的市场开关块整段丢掉。
    # 行格式照插件市场的规矩（包名独立注释行，`- id:` 整行只有 id），并且用
    # append_patch_entries 处理掉 `[]` 占位符——不然模板 profile 上会写成两个顶层元素。
    lines = original.splitlines()
    span = find_block(lines)
    kept = lines[: span[0]] + lines[span[1] + 1:] if span else lines
    base = "\n".join(kept)
    if base.strip() and not base.endswith("\n"):
        base += "\n"
    base = remove_patch_row_entries(base, sorted({row_id for _pkg, row_id in to_write}))
    block = [BLOCK_BEGIN]
    for pkg, row_id in sorted(set(to_write)):
        block.append(f"# {pkg}")
        block.append(f"- id: {row_id}")
        block.append("  disabled: true")
    block.append(BLOCK_END)
    ok_append, why_append, new_text = append_patch_entries(base, block)
    if not ok_append:
        fail(f"补丁层现在追加不了条目，没敢写：{why_append}")
        return 1
    write_ok, why_write = write_patch_text(patch_file, new_text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1

    info("")
    for pkg, row_id in sorted(set(to_write)):
        info(f"  已禁用    {pkg:<34}行 id = {row_id}")
    for note in notes:
        info(col(f"      {note}", C.GRAY))
    info("")
    info(col(f"  备份      {backup}", C.GRAY))

    if getattr(args, "no_verify", False):
        return 0
    info(col("  再组合一次，核对禁用到没到生效…", C.GRAY))
    after = run_dump_config(cfg, lane, prof)
    if not after.get("ok"):
        warn("复核没能跑起来，禁用的是否生效**未经验证**，建议手动打开这条 lane 看一眼")
        return 0
    rows_after = after["rows"]
    bad = [row_id for _pkg, row_id in to_write if not (rows_after.get(row_id) or {}).get("disabled")]
    dead = [row_id for _pkg, row_id in to_write if row_id not in rows_after]
    if dead:
        fail(f"有 {len(dead)} 个行 id 在组合结果里找不到（写错了？）：{'、'.join(sorted(set(dead)))}")
    if bad:
        fail(f"有 {len(bad)} 个行写进去了但仍然是启用状态：{'、'.join(sorted(set(bad)))}")
    still = [
        item["pkg"]
        for item in plugin_overview(cfg, lane, prof, dump=after)["bundles"]
        if not item["official"] and item["present"] and len(item["disabled"]) != len(item["present"])
    ]
    if not dead and not bad:
        ok(f"核对通过：{len(to_write)} 个行已全部禁用"
           + (f"，仍有启用的第三方组合包：{'、'.join(still)}" if still else "，第三方插件已全部关闭"))
    problems = bool(dead or bad)

    # 启动冒烟：**只有真起来过才算数**。实测踩到：把某个组合包插入的 `web` 行一起禁掉后，
    # 7 个官方行会一直等 `web` 服务、应用根本不出 URL——这种失败在离线组合结果里完全看不出来。
    # 所以这一步是默认开的；起不来就**自动撤销**，不留一个起不来的环境给你。
    if getattr(args, "no_boot_check", False):
        info(col("  （按你的要求跳过了启动冒烟）", C.GRAY))
    elif lane_runtime(cfg, lane, prof):
        info(col("  这条 lane 正在运行：补丁层已经热生效，跳过启动冒烟（那条要停一次才能跑）。"
                 "想连「下次启动能不能起来」一起验：先 stop 再跑一次。", C.GRAY))
    else:
        info(col("  启动冒烟：确认全禁用状态下还能起来…", C.GRAY))
        rc = cmd_open(
            cfg,
            argparse.Namespace(
                target=f"{lane}/{prof}", port=None, cwd=None, timeout=90,
                no_browser=True, detach=True, adopt_only=False,
            ),
        )
        if rc == 0:
            cmd_stop(cfg, argparse.Namespace(target=[f"{lane}/{prof}"]))
            ok("启动冒烟通过（已把它停回去）")
        else:
            problems = True
            fail("全禁用状态下**起不来**——已自动撤销回你原来的配置")
            try:
                patch_file.write_text(original, encoding="utf-8")
                ok(f"已还原 {patch_file.name}（备份留在 {backup.name}）")
            except OSError as exc:
                fail(f"自动还原失败，请手动把备份复制回去：{backup} → {patch_file}（{exc}）")
            warn("通常是因为某个组合包**提供的服务**被一起关掉了"
                 "（本机就是 @liustack/modsearch 插入的 `web` 行）。排除它再试：")
            info(col(f"      py {ME_NAME} plugins-off {lane} --keep @liustack/modsearch", C.CYAN))
    info("")
    info(col(f"  撤销： py {ME_NAME} plugins-on {lane} --profile {prof}", C.CYAN))
    if lane_runtime(cfg, lane, prof):
        info(col("  生效： 已经热生效了（这条 lane 正在运行，补丁层是热重载盯着的文件）", C.CYAN))
    else:
        info(col(f"  生效： py {ME_NAME} open {lane}/{prof}（下次启动时应用）", C.CYAN))
    return 1 if problems else 0


def cmd_plugins_on(cfg: dict, args) -> int:
    """撤销一键禁用：只删启动器写的那一段，你自己的配置一个字不动。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    prof = getattr(args, "profile", None) or PROFILE_NAME
    patch_file = plugin_patch_path(cfg, lane, prof)
    if not patch_file.is_file():
        fail(f"找不到用户层补丁文件：{patch_file}")
        return 1
    original = patch_file.read_text(encoding="utf-8")
    lines = original.splitlines()
    span = find_block(lines)
    if not span:
        warn("这段禁用块本来就不存在（没有可撤销的东西）")
        return 0
    kept = lines[: span[0]] + lines[span[1] + 1:]
    # 删完可能只剩注释 —— 那就不是顶层数组了，dsh 拒绝启动这个 profile，补回 `[]`
    text = ensure_patch_array("\n".join(kept))
    if text and not text.endswith("\n"):
        text += "\n"
    # 逐字节还原：如果"手术式删除"的结果与备份只差结尾换行，就直接用备份，保证一字不差
    backup = patch_file.with_suffix(patch_file.suffix + ".bak")
    verdict = ""
    if backup.is_file():
        try:
            bak_text = backup.read_text(encoding="utf-8")
        except OSError:
            bak_text = ""
        if bak_text:
            if text == bak_text:
                verdict = "与备份逐字节一致"
            elif text.rstrip("\n") == bak_text.rstrip("\n"):
                text = bak_text
                verdict = "与备份逐字节一致（按备份补齐了结尾换行）"
            elif user_layer_text(text) == user_layer_text(bak_text):
                verdict = "与备份一致（差异只是启动器写的另一个开关块；你自己的配置没动）"
            else:
                verdict = "与备份不同（你在这期间手改过；备份里是禁用前的原文）"
    write_ok, why_write = write_patch_text(patch_file, text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1
    ok(f"已撤销 lane「{lane}」的一键禁用（删掉 {span[1] - span[0] - 1} 行覆盖，你自己的配置没动）")
    if verdict:
        info(col(f"  校验：{verdict}", C.GRAY))
    if lane_runtime(cfg, lane, prof):
        info(col("这条 lane 正在运行：删掉的那几行会被热重载立刻重放（不用重启）", C.GRAY))
    if not getattr(args, "no_verify", False):
        listing = plugin_listing(cfg, lane, prof)
        enabled = [
            i["pkg"] for i in listing["bundles"]
            if not i["official"] and i["kind"] == "bundle" and not i["disabled_rows"]
        ]
        info(col(f"  当前仍在启用中的第三方插件：{'、'.join(enabled) or '（无）'}", C.GRAY))
        if listing["has_market_block"]:
            info(col(f"  注意：插件市场还挂着自己的开关块（要恢复用 market-on）", C.GRAY))
    return 0


# ======================= 插件市场单独开关 / 更新 =======================


def market_package_dir(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> Path:
    return profile_dir(lane_home(cfg, lane), profile) / "node_modules" / MARKET_PKG


def market_declared_spec(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> str | None:
    manifest = profile_dir(lane_home(cfg, lane), profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = (data.get("dependencies") or {}).get(MARKET_PKG)
    return str(value) if value else None


def market_block_rows(text: str) -> list[str]:
    """插件市场开关块里写了哪些行 id。"""
    lines = text.splitlines()
    span = find_block(lines, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)
    if not span:
        return []
    out: list[str] = []
    for raw in lines[span[0] + 1: span[1]]:
        match = PATCH_ID_RE.match(raw.split(" #", 1)[0].rstrip())
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def _fmt_age(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        stamp = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - stamp
    except Exception:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} 分钟前发布"
    hours = minutes / 60
    if hours < 48:
        return f"{int(hours)} 小时前发布"
    return f"{int(hours // 24)} 天前发布"


def market_snapshots(cfg: dict, lane: str) -> list[dict]:
    root = paths(cfg)["backups"] / "market" / lane
    items: list[dict] = []
    if not root.is_dir():
        return items
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        manifest = path / "manifest.json"
        item = {"name": path.name, "dir": path, "created": "", "version": "", "spec": "",
                "files": 0, "bytes": 0, "ok": manifest.is_file()}
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                for key in ("created", "version", "spec", "files", "bytes"):
                    if key in data:
                        item[key] = data[key]
            except Exception:
                pass
        items.append(item)
    return items


def prune_market_snapshots(cfg: dict, lane: str, keep: int = 5) -> list[str]:
    """只保留最近 N 份插件市场快照。

    每份只有 3.8 MB，但"更新一次存一份"会一直涨；留最近 5 份够回退，
    更老的清掉（只动 `<root>/backups/market/<lane>` 下、带 manifest.json 的目录）。
    """
    root = (paths(cfg)["backups"] / "market" / lane).resolve()
    if not root.is_dir():
        return []
    victims: list[str] = []
    for item in market_snapshots(cfg, lane)[keep:]:
        path = Path(item["dir"]).resolve()
        if path.parent != root or not item["ok"]:
            continue
        try:
            shutil.rmtree(path)
            victims.append(item["name"])
        except OSError:
            pass
    return victims


def market_status(cfg: dict, lane: str, profile: str = PROFILE_NAME,
                  with_registry: bool = False) -> dict:
    """插件市场的现状：装的是哪个版本、行 id 是什么、被谁关着。

    `market` 命令**故意不跑** `--dump-config`（那要十几秒），状态直接读
    profile 的 package.json / cordis.patch.yml 推出来——够快，GUI 打开对话框时不会卡。
    """
    home = lane_home(cfg, lane)
    pdir = profile_dir(home, profile)
    patch_file = pdir / "cordis.patch.yml"
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    lines = text.splitlines()
    pkgdir = pdir / "node_modules" / MARKET_PKG
    out = {
        "lane": lane,
        "home": home,
        "profile": profile,
        "profile_dir": pdir,
        "patch_file": patch_file,
        "package_dir": pkgdir,
        "installed": _manifest_version(pkgdir),
        "spec": market_declared_spec(cfg, lane, profile),
        "rows": bundle_row_ids(home, MARKET_PKG, profile),
        "in_bundles": MARKET_PKG in read_profile_bundles(home, profile),
        "has_block": bool(find_block(lines, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)),
        "block_rows": market_block_rows(text),
        "general_block": bool(find_block(lines, BLOCK_BEGIN, BLOCK_END)),
        "user_disabled": user_disabled_ids(text),
        "snapshots": market_snapshots(cfg, lane),
        "latest": None,
        "published": None,
        "registry": None,
        "registry_error": None,
        "versions": [],
    }
    if with_registry:
        try:
            data, reg = fetch_index(cfg, MARKET_PKG)
            tags = data.get("dist-tags") or {}
            out["registry"] = reg
            out["latest"] = tags.get("latest") or None
            out["published"] = (data.get("time") or {}).get(out["latest"] or "")
            out["versions"] = list((data.get("versions") or {}).keys())
        except Exception as exc:  # noqa: BLE001
            out["registry_error"] = f"{type(exc).__name__}: {exc}"
    return out


def market_disabled_rows(st: dict) -> list[str]:
    """现在被关着的行 id（启动器的开关块 + 用户自己写的禁用）。"""
    return [row for row in st["rows"] if row in st["block_rows"] or row in st["user_disabled"]]


def _compose_with_block(text: str, begin: str, end: str, rows: list[tuple[str, str]]) -> tuple[bool, str, str]:
    """把一块 `disabled: true` 覆盖追加到用户层末尾，返回 (ok, 原因, 新文本)。

    先摘掉同一种块（避免写两份），但**保留另一种块**——启动器有两种块
    （一键禁用 / 插件市场开关），一刀切会把另一个块整段丢掉。

    行格式照插件市场的规矩：包名写成**独立注释行**，`- id:` 那行整行只有 id。
    写成行尾注释 `- id: X  # 包名` 市场就看不见（实测读到 0 条禁用），
    于是"启动器关掉的插件"在市场页面上还显示"启用中"。
    """
    lines = text.splitlines()
    span = find_block(lines, begin, end)
    kept = lines[: span[0]] + lines[span[1] + 1:] if span else lines
    base = "\n".join(kept)
    if base.strip() and not base.endswith("\n"):
        base += "\n"
    # 这些行 id 的旧条目（不管谁写的、写的是 true 还是 false）先删掉：
    # 同一行同时存在 `disabled: true` 和 `disabled: false` 两条，谁说了算就成了谜。
    base = remove_patch_row_entries(base, sorted({row for _pkg, row in rows}))
    block = [begin]
    for pkg, row_id in sorted(set(rows)):
        block.append(f"# {pkg}")
        block.append(f"- id: {row_id}")
        block.append("  disabled: true")
    block.append(end)
    return append_patch_entries(base, block)



def boot_smoke(cfg: dict, lane: str, timeout: int = 90) -> bool:
    """真起一次再停回去：只有"起来过"才能证明这套配置能跑。

    实测教训（一键禁用那一步踩到的）：把某个组合包插入的 `web` 行一起禁掉后，
    离线组合结果**完全正常**，但应用不出 URL，日志只有一句含糊的 `entries did not activate`。
    所以"配置写对了"和"系统还能跑"是两个不同的断言，只有启动能证明后者。
    """
    rc = cmd_open(
        cfg,
        argparse.Namespace(target=lane, port=None, cwd=None, timeout=timeout,
                           no_browser=True, detach=True, adopt_only=False),
    )
    if rc == 0:
        cmd_stop(cfg, argparse.Namespace(target=[lane]))
        return True
    return False


def cmd_market(cfg: dict, args) -> int:
    """看一眼插件市场：装的是哪个版本、开还是关、registry 上有没有新版。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    prof = getattr(args, "profile", None) or PROFILE_NAME
    st = market_status(cfg, lane, prof, with_registry=not getattr(args, "offline", False))
    me = ME_NAME
    info(col(f"── 插件市场（{MARKET_PKG}）：lane「{lane}」──", C.BOLD))
    installed = st["installed"]
    info(f"  装在哪    {st['package_dir']}")
    info(f"  已装版本  {installed or '（没装）'}"
         + (f"      profile 里写的是 {st['spec']}" if st["spec"] else ""))
    info(f"  行 id     {'、'.join(st['rows']) or '(没有补丁行)'}")
    off = market_disabled_rows(st)
    if not st["spec"] and not st["package_dir"].is_dir():
        state = col("这条 lane 没装插件市场", C.YELLOW)
    elif not st["rows"]:
        state = col("装了，但它没有补丁行（可能被跳过/不兼容）", C.YELLOW)
    elif off and len(off) == len(st["rows"]):
        who = "启动器开关" if st["has_block"] else "你自己的配置"
        state = col(f"已关掉（{who}）", C.GREEN)
    elif off:
        state = col(f"部分关着（{ '、'.join(off) }）", C.YELLOW)
    else:
        state = "启用中"
    info(f"  当前状态  {state}")
    if st["latest"]:
        age = _fmt_age(st["published"])
        same = st["latest"] == installed
        info(f"  registry  {st['latest']}"
             + (f"（{age}）" if age else "")
             + ("      ← 就是你现在这个" if same else "      ← 比你现在的新"))
    elif st["registry_error"]:
        warn(f"没查到 registry：{st['registry_error']}")
    snaps = st["snapshots"]
    if snaps:
        newest = snaps[0]
        info(f"  更新快照  {len(snaps)} 个，最新 {newest['name']}"
             f"（快照里是 {newest['version'] or '?'}）")
        for item in snaps[1:4]:
            info(col(f"            {item['name']}（{item['version'] or '?'}）", C.GRAY))
    if st["general_block"]:
        info(col("  注意      这条 lane 还挂着一键禁用块（plugins-on 撤销）", C.GRAY))
    if not (st["spec"] or st["package_dir"].is_dir()):
        return 0
    info("")
    info(col(f"  关掉它 : py {me} market-off {lane}        （写完立刻热生效，不用重启）", C.CYAN))
    info(col(f"  打开它 : py {me} market-on {lane}", C.CYAN))
    info(col(f"  更新   : py {me} market-update {lane}        （点名版本 + 启动冒烟，失败自动回滚）", C.CYAN))
    if snaps:
        info(col(f"  退回   : py {me} market-rollback {lane}", C.CYAN))
    return 0


def cmd_market_off(cfg: dict, args) -> int:
    """单独关掉插件市场：官方组合包和其它插件都不动。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    me = ME_NAME
    st = market_status(cfg, lane)
    if not st["spec"] and not st["package_dir"].is_dir():
        fail(f"lane「{lane}」的 profile 里没有插件市场（{MARKET_PKG}）")
        return 1
    if st["has_block"]:
        warn("插件市场本来就已经关着了（启动器开关块还在）")
        info(col(f"  要打开它： py {me} market-on {lane}", C.CYAN))
        return 0
    if lane_runtime(cfg, lane):
        info(col(f"lane「{lane}」正在运行：补丁层是 DSH 热重载盯着的文件，写进去大约 2 秒生效（不用重启）",
                 C.GRAY))

    info(col(f"── 关掉插件市场：lane「{lane}」──", C.BOLD))
    info(col("  先离线组合一次，确认它的行现在真的在生效…", C.GRAY))
    dump = run_dump_config(cfg, lane)
    if not dump.get("ok"):
        fail(f"组合检查失败（{dump.get('error') or '退出码 ' + str(dump.get('rc'))}），不继续")
        return 1
    rows = [row for row in st["rows"] if row in dump["rows"]]
    if not rows:
        fail("插件市场在组合结果里没有生效的行（补丁里的行 id："
             f"{'、'.join(st['rows']) or '无'}）——它可能被跳过或不兼容")
        return 1

    patch_file = st["patch_file"]
    original = patch_file.read_text(encoding="utf-8")
    already = user_disabled_ids(original)
    to_write = [(MARKET_PKG, row) for row in rows if row not in already]
    notes = [f"{row}：你自己的配置里已经禁用它了，启动器不再写一遍" for row in rows if row in already]
    if not to_write:
        warn("它的行本来就已经是禁用状态（你自己配的），启动器不需要再写")
        for note in notes:
            info(col(f"      {note}", C.GRAY))
        return 0

    ok_compose, why_compose, new_text = _compose_with_block(
        original, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END, to_write)
    if not ok_compose:
        fail(f"补丁层现在追加不了条目，没敢写：{why_compose}")
        return 1
    backup = patch_file.with_suffix(patch_file.suffix + ".bak-market")
    backup_ok, backup_info = backup_patch_text(patch_file, original)
    if not backup_ok:
        fail(f"备份失败（{backup_info}），没敢写")
        return 1
    write_ok, why_write = write_patch_text(patch_file, new_text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1
    info("")
    ok(f"已关掉插件市场（行 id：{'、'.join(rows)}）")
    for note in notes:
        info(col(f"      {note}", C.GRAY))
    info(col(f"  备份      {backup}", C.GRAY))

    if getattr(args, "no_verify", False):
        return 0
    info(col("  再组合一次，核对禁用到没到生效…", C.GRAY))
    after = run_dump_config(cfg, lane)
    if not after.get("ok"):
        warn("复核没能跑起来，禁用的是否生效**未经验证**")
        return 0
    bad = [row for row in rows if not (after["rows"].get(row) or {}).get("disabled")]
    if bad:
        fail(f"写进去了但仍然是启用状态：{'、'.join(bad)}")
    else:
        ok(f"核对通过：{'、'.join(rows)} 已经是禁用状态")

    problems = bool(bad)
    if getattr(args, "no_boot_check", False):
        info(col("  （按你的要求跳过了启动冒烟）", C.GRAY))
    elif lane_runtime(cfg, lane):
        warn("这条 lane 正在运行，跳过启动冒烟——补丁层已经热生效，但"
             "「下次启动能不能起来」这一步没验（想验就 stop 后再跑一次）")
    else:
        info(col("  启动冒烟：确认关掉插件市场后还能起来…", C.GRAY))
        if boot_smoke(cfg, lane):
            ok("启动冒烟通过（已把它停回去）")
        else:
            problems = True
            fail("关掉插件市场后**起不来**——已自动还原回你原来的配置")
            try:
                patch_file.write_text(original, encoding="utf-8")
                ok(f"已还原 {patch_file.name}（备份留在 {backup.name}）")
            except OSError as exc:
                fail(f"自动还原失败，请手动把备份复制回去：{backup} → {patch_file}（{exc}）")
    info("")
    info(col(f"  打开它： py {me} market-on {lane}", C.CYAN))
    return 1 if problems else 0


def cmd_market_on(cfg: dict, args) -> int:
    """再打开插件市场：只删启动器写的那一小段，别的块（一键禁用）一个字不动。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    st = market_status(cfg, lane)
    if not st["has_block"]:
        warn("插件市场本来就没被启动器关着（没有可撤销的）")
        return 0
    patch_file = st["patch_file"]
    original = patch_file.read_text(encoding="utf-8")
    lines = original.splitlines()
    span = find_block(lines, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)
    removed = market_block_rows(original)
    kept = lines[: span[0]] + lines[span[1] + 1:]
    # 删完可能只剩注释 —— 那就不是顶层数组了，dsh 会拒绝启动这个 profile，补回 `[]`
    text = ensure_patch_array("\n".join(kept))
    if text and not text.endswith("\n"):
        text += "\n"
    backup = patch_file.with_suffix(patch_file.suffix + ".bak-market")
    verdict = ""
    if backup.is_file():
        try:
            bak_text = backup.read_text(encoding="utf-8")
        except OSError:
            bak_text = ""
        if bak_text:
            if text == bak_text:
                verdict = "与备份逐字节一致"
            elif text.rstrip("\n") == bak_text.rstrip("\n"):
                text = bak_text
                verdict = "与备份逐字节一致（按备份补齐了结尾换行）"
            elif user_layer_text(text) == user_layer_text(bak_text):
                verdict = "与备份一致（差异只是启动器写的另一个开关块；你自己的配置没动）"
            else:
                verdict = "与备份不同（你在这期间手改过；备份里是关闭前的原文）"
    write_ok, why_write = write_patch_text(patch_file, text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1
    ok(f"已打开插件市场（删掉 {span[1] - span[0] - 1} 行覆盖"
       + (f"：{'、'.join(removed)}" if removed else "") + "）")
    if verdict:
        info(col(f"  校验：{verdict}", C.GRAY))
    if lane_runtime(cfg, lane):
        info(col("这条 lane 正在运行：删掉的那几行会被热重载立刻重放（不用重启）", C.GRAY))
    if not getattr(args, "no_verify", False):
        after = run_dump_config(cfg, lane)
        if not after.get("ok"):
            warn("复核没能跑起来，是否恢复未经验证")
        else:
            still = [row for row in (removed or st["rows"]) if (after["rows"].get(row) or {}).get("disabled")]
            if still:
                info(col(f"  仍在禁用中的行：{'、'.join(still)}（你自己的配置写着禁用，不归启动器管）", C.GRAY))
            else:
                ok("核对通过：它的行已经重新启用")
    return 0


def pnpm_store_dir(home, profile: str = PROFILE_NAME) -> str | None:
    """读 profile 的 `node_modules/.modules.yaml`，取它当年是用哪个 pnpm store 装的。

    为什么必须显式告诉 pnpm：pnpm 的 store 默认按**盘符**选（`<盘>\\.pnpm-store\\v11`），
    于是"副本在 D 盘、原件在 C 盘"必然撞上：

        ERR_PNPM_UNEXPECTED_STORE: The dependencies at "...\\node_modules" are currently
        linked from the store at "C:\\...\\pnpm\\store\\v11". pnpm now wants to use the
        store at "D:\\.pnpm-store\\v11".

    而且它**拒绝干活**（退出码 1）——也就是说：**复制出来的副本，没法用 pnpm 装/更新插件**，
    这正好砸在"复制一份去测试插件冲突"这个用途上。修法不是换 store（那要重装 636 MB 依赖），
    而是把 node_modules 里记录的那个 store 原样告诉它。
    """
    manifest = Path(home) / "profiles" / profile / "node_modules" / ".modules.yaml"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"storeDir"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except Exception:  # noqa: BLE001
        return match.group(1).replace("\\\\", "\\")


def pnpm_ledger_state(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """读 `node_modules/.modules.yaml`（pnpm 的账本）：storeDir / virtualStoreDir 各指哪。

    只读。`virtualStoreDir` 记的是绝对路径，所以**复制出来的副本会带着原件的路径**——
    这是副本独有的、安静的一类"指回原件"。
    """
    path = Path(lane_home(cfg, lane)) / "profiles" / profile / "node_modules" / ".modules.yaml"
    want = str(profile_dir(lane_home(cfg, lane), profile) / "node_modules" / ".pnpm")
    out = {"path": path, "exists": path.is_file(), "store": None, "virtual": None,
           "want": want, "outside": False, "error": ""}
    if not out["exists"]:
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        out["error"] = str(exc)
        return out
    for key in ("storeDir", "virtualStoreDir"):
        match = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if not match:
            continue
        try:
            value = json.loads(f'"{match.group(1)}"')
        except Exception:  # noqa: BLE001
            value = match.group(1).replace("\\\\", "\\")
        out["store" if key == "storeDir" else "virtual"] = value
    if out["virtual"]:
        prefix = _norm_win(want)
        out["outside"] = not (
            _norm_win(out["virtual"]) == prefix
            or _norm_win(out["virtual"]).startswith(prefix.rstrip("\\") + "\\")
        )
    return out


def repair_pnpm_virtual_store(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """修掉复制出来的 profile 里那个**指回原件**的 `virtualStoreDir`。

    实测（复制副本 → 更新插件市场时踩到）：`clone` 出来的 profile，`node_modules/.modules.yaml`
    里记的还是原件的

        "virtualStoreDir": "C:\\\\Users\\\\Administrator\\\\.dsh\\\\profiles\\\\web\\\\node_modules\\\\.pnpm"

    pnpm 一比对（记录值 vs 它自己算出来的值）就直接拒绝干活，退出码 1：

        ERR_PNPM_UNEXPECTED_VIRTUAL_STORE ... pnpm now wants to use the virtual store at
        "D:\\\\dsh-lanes\\\\homes\\\\global-copy\\\\profiles\\\\web\\\\node_modules\\\\.pnpm"

    后果是"副本里装不了 / 更新不了插件"——正好砸在"复制一份去测插件冲突"这个用途上。
    更糟的是它记的是**原件**的路径：真按记录值来，pnpm 会往你日常那套的 node_modules 里写。

    `storeDir` **故意不动**：那是全机共用的内容寻址缓存，共用是对的（`run_plugin_cmd` 会显式传回去）。
    """
    state = pnpm_ledger_state(cfg, lane, profile)
    out = {"path": state["path"], "changed": False, "recorded": state["virtual"] or "",
           "want": state["want"], "error": state["error"]}
    if not state["exists"] or not state["virtual"] or not state["outside"]:
        return out
    try:
        text = state["path"].read_text(encoding="utf-8")
        match = re.search(r'("virtualStoreDir"\s*:\s*")((?:[^"\\]|\\.)*)(")', text)
        if not match:
            return out
        new_text = text[: match.start(2)] + state["want"].replace("\\", "\\\\") + text[match.end(2):]
        shutil.copyfile(state["path"], state["path"].with_suffix(".yaml.bak"))
        state["path"].write_text(new_text, encoding="utf-8")
    except OSError as exc:
        out["error"] = str(exc)
        return out
    out["changed"] = True
    return out


def _is_abs_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("/") or text.startswith("\\\\")


def pnpm_file_spec_state(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> list[dict]:
    """查 profile 里那些 `file:` 本地依赖，在**当前这个位置**还指不指得到东西。

    只读。为什么要有这个检查：pnpm 把 `file:` 依赖的解析结果记成**相对路径**，
    而相对路径只在原件那个深度成立（详见 `repair_pnpm_file_specs`）。
    """
    pdir = profile_dir(lane_home(cfg, lane), profile)
    manifest = pdir / "package.json"
    lock = pdir / "pnpm-lock.yaml"
    out: list[dict] = []
    try:
        deps = (json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {})
        text = lock.read_text(encoding="utf-8") if lock.is_file() else ""
    except Exception:
        return out
    for name, spec in deps.items():
        if not str(spec).startswith("file:"):
            continue
        item = {"name": str(name), "specifier": str(spec), "lock": "", "resolution": "",
                "resolved": "", "broken": False}
        match = re.search(
            r"^\s{6}" + re.escape(str(name)) + r":\s*\n\s+specifier:.*\n\s+version:\s*(.+?)\s*$",
            text, re.M,
        )
        if match:
            item["lock"] = match.group(1).strip().strip("'\"")
        res_match = re.search(
            r"^\s{2}" + re.escape(str(name)) + r"@[^\n]*:\s*\n\s+resolution:\s*\{directory:\s*(.+?),\s*type:",
            text, re.M,
        )
        if res_match:
            item["resolution"] = res_match.group(1).strip().strip("'\"")
        for raw_spec in (item["lock"], item["resolution"]):
            if not raw_spec:
                continue
            raw = raw_spec[5:] if raw_spec.startswith("file:") else raw_spec
            try:
                candidate = Path(raw) if _is_abs_path(raw) else (pdir / raw)
                if not candidate.exists():
                    item["broken"] = True
                    item["resolved"] = item["resolved"] or str(candidate)
            except OSError:
                item["broken"] = True
            if not item["resolved"]:
                item["resolved"] = str(candidate)
        out.append(item)
    return out


def repair_pnpm_file_specs(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """把复制后**深度变了**的 `file:` 依赖在 pnpm 账本里的相对路径换成绝对路径。

    实测（复制副本 → 更新插件市场时踩到，第三个坑）：

        [ENOENT] no such file or directory, scandir
        'D:\\dsh-lanes\\homes\\Desktop\\deepseek harness\\dsh-tool-result-trimmer'

    profile 里有个本地插件是按**绝对路径**装的
    （`"dsh-tool-result-trimmer": "file:C:/Users/Administrator/Desktop/deepseek harness/dsh-tool-result-trimmer"`），
    但 pnpm 把**解析结果**记成了相对路径：

        version: file:../../../Desktop/deepseek harness/dsh-tool-result-trimmer
        resolution: {directory: ../../../Desktop/deepseek harness/dsh-tool-result-trimmer, ...}

    相对路径只在原件那个深度成立：原件在 `C:\\Users\\Administrator\\.dsh\\profiles\\web`
    （往上三层正好是 `C:\\Users\\Administrator`），副本在 `D:\\dsh-lanes\\homes\\<lane>\\profiles\\web`
    （往上三层是 `D:\\dsh-lanes\\homes`）→ 于是 pnpm 跑去找
    `D:\\dsh-lanes\\homes\\Desktop\\...`，直接 ENOENT。

    这就是"复制"最典型的坑：**配置里混着相对路径，换个深度就悄悄指到别处**。

    两个必须做对的地方（都实测栽过）：
    · 相对路径在锁文件里出现**三次**，其中 `resolution: {directory: ...}` 那一次不带 `file:` 前缀，
      而 pnpm 解析时正是看它——只换 `file:` 那两处，报错会一模一样地再来一遍。
    · 还有**第三个文件**：`node_modules/.pnpm/lock.yaml`（虚拟仓库自己那份锁副本）。
    """
    out = {"changed": [], "skipped": [], "errors": []}
    pdir = profile_dir(lane_home(cfg, lane), profile)
    # 相对路径是"相对于安装时那个 profile 目录"算出来的，所以要把**源 lane 的 profile 目录**也算进来
    origins = [pdir]
    src = (cfg["lanes"].get(lane) or {}).get("clonedFrom")
    if src and src in cfg["lanes"]:
        origins.append(profile_dir(lane_home(cfg, src), profile))
    for item in pnpm_file_spec_state(cfg, lane, profile):
        raw = item["specifier"][5:]
        try:
            want = Path(raw) if _is_abs_path(raw) else (pdir / raw)
        except OSError:
            continue
        want_text = str(want).replace("\\", "/")
        candidates: list[str] = []
        for recorded in (item["lock"], item["resolution"]):
            if recorded.startswith("file:"):
                recorded = recorded[5:]
            if recorded and not _is_abs_path(recorded):
                candidates.append(recorded.replace("\\", "/"))
        for origin in origins:
            try:
                rel = os.path.relpath(str(want), str(origin)).replace("\\", "/")
            except ValueError:      # 跨盘符没有相对路径
                continue
            if not _is_abs_path(rel):
                candidates.append(rel)
        hit = False
        for candidate in dict.fromkeys(candidates):
            if not candidate or candidate == want_text:
                continue
            for fname in ("pnpm-lock.yaml", "node_modules/.pnpm/lock.yaml",
                          "node_modules/.modules.yaml"):
                path = pdir / fname
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if candidate not in text:
                    continue
                try:
                    shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
                    path.write_text(text.replace(candidate, want_text), encoding="utf-8")
                except OSError as exc:
                    out["errors"].append(f"{fname}: {exc}")
                    continue
                out["changed"].append(f"{item['name']} → {want_text}（{fname}）")
                hit = True
        if not hit:
            out["skipped"].append(item["name"])
    return out


def run_plugin_cmd(cfg: dict, lane: str, pnpm_args: list[str], timeout: int = 1200,
                   label: str = "plugin", profile: str = PROFILE_NAME) -> dict:
    """跑 `dsh plugin --profile <profile> <pnpm 参数>`，把 pnpm 的输出落到文件再回读。

    三个实测细节：
    · 子进程 stdio 必须接**文件句柄**：某些沙箱下管道会被拒绝（EPERM），而 pnpm 的输出还很长。
    · `plugin` 是 dsh 自己的子命令，剩余参数**原样转发**给 pnpm（bin.js 里 plugin 命令
      `allowUnknownOption` + `[args...]`），所以 `--save-exact`、`--config.xxx` 这类 pnpm 旗标能直接用。
    · 必须把 profile 记录的那个 pnpm store 显式传回去，否则跨盘副本一律
      `ERR_PNPM_UNEXPECTED_STORE`（见 `pnpm_store_dir`）。
    """
    home = lane_home(cfg, lane)
    # 顺手修副本里两类"跟着复制一起变味"的东西，否则 pnpm 直接拒绝干活：
    #   ① .modules.yaml 里指回原件的 virtualStoreDir
    #   ② pnpm-lock.yaml 里深度相关的 `file:` 相对路径（本地插件）
    repairs = [
        repair_pnpm_virtual_store(cfg, lane, profile),
        repair_pnpm_file_specs(cfg, lane, profile),
    ]
    node = find_node(cfg)
    if not node:
        return {"ok": False, "rc": -1, "tail": [], "error": "找不到 node", "log": None,
                "seconds": 0, "repairs": repairs}
    entry = lane_entry_script(cfg, lane)
    logs = ensure_layout(cfg)["logs"]
    # 日志名带上 profile：同一 lane 的两个 profile 可能同时装插件，共用一份日志会互相截断
    log_p = logs / f".{label}-{lane}__{profile}.log"
    err_p = logs / f".{label}-{lane}__{profile}.err"
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    node_dir = str(Path(node).parent)
    if node_dir not in env.get("PATH", ""):
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    args = list(pnpm_args)
    store = pnpm_store_dir(home, profile)
    if store:
        args.append(f"--config.store-dir={store}")
        # pnpm 11/12 读 PNPM_CONFIG_*（带盘符的路径写成命令行参数也能被某些版本忽略），两个都给最稳
        env["PNPM_CONFIG_STORE_DIR"] = store
    cmd = [str(node), str(entry), "plugin", "--profile", profile, *args]
    rc = -1
    error = ""
    started = time.time()
    try:
        with open(log_p, "w", encoding="utf-8") as fo, open(err_p, "w", encoding="utf-8") as fe:
            rc = subprocess.run(cmd, stdout=fo, stderr=fe, env=env, timeout=timeout,
                                cwd=str(profile_dir(home, profile)),
                                creationflags=_no_window_flags()).returncode
    except subprocess.TimeoutExpired:
        rc, error = -2, f"超过 {timeout} 秒还没结束（已终止）"
    except Exception as exc:  # noqa: BLE001
        rc, error = -1, f"{type(exc).__name__}: {exc}"
    tail = [line for line in (tail_lines(log_p, 14) + tail_lines(err_p, 14)) if line.strip()]
    return {"ok": rc == 0, "rc": rc, "log": log_p, "err_log": err_p, "tail": tail[-14:],
            "error": error, "seconds": round(time.time() - started, 1), "repairs": repairs}


def plugin_add_smart(cfg: dict, lane: str, profile: str, spec: str,
                     label: str = "cfg-restore", timeout: int = 1200,
                     non_registry: bool = False) -> dict:
    """装一个插件，能离线就不联网（配置迁移/本机重装时复用已下载内容）。

    · registry 包（pkg@version）：先 `--offline` 纯离线。pnpm 同盘共享一个内容寻址
      store（默认 D:\\.pnpm-store\\v11）——这台机器上别的 lane 装过**完全相同版本**，
      store 里就有，按内容哈希天然保证版本一致，直接硬链接、零网络。本地缺包
      （NO_OFFLINE_META / 无匹配版本）再退回 `--prefer-offline` 联网：本地有的不重下，
      只补缺的。
    · git/github/http 等非 registry 包（non_registry=True）：源码仍要联网拉，
      但依赖走 `--prefer-offline`，能复用 store 的就不重复下载。
    · file:/link: 本地包请直接用 run_plugin_cmd，不走这里。

    返回 run_plugin_cmd 的结果，并额外带 mode = "offline" | "online" | "failed"。
    """
    args = ["add", spec, "--save-exact"]
    if non_registry:
        res = run_plugin_cmd(cfg, lane, args + ["--prefer-offline"],
                             label=label, profile=profile, timeout=timeout)
        res["mode"] = "online" if res.get("ok") else "failed"
        return res
    # 1) 先纯离线
    res = run_plugin_cmd(cfg, lane, args + ["--offline"],
                         label=label, profile=profile, timeout=timeout)
    if res.get("ok"):
        res["mode"] = "offline"
        return res
    # 2) 本地缺包 → 联网补（仍优先本地缓存）
    res2 = run_plugin_cmd(cfg, lane, args + ["--prefer-offline"],
                          label=label, profile=profile, timeout=timeout)
    res2["mode"] = "online" if res2.get("ok") else "failed"
    res2["offline_tail"] = res.get("tail")
    return res2


def market_snapshot(cfg: dict, lane: str, label: str = "", note: str = "",
                    profile: str = PROFILE_NAME) -> dict:
    """更新前先存一份：插件市场自己的包目录 + 三个清单文件。

    只存这三样就够：版本号在 package.json，解析结果在 pnpm-lock.yaml，
    "新版本按住名单"在 pnpm-workspace.yaml，代码在 node_modules 里。
    实测这个包只有 3.8 MB / 174 个文件，存一份是零成本的。
    """
    home = lane_home(cfg, lane)
    pdir = profile_dir(home, profile)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    dest = paths(cfg)["backups"] / "market" / lane / name
    (dest / "node_modules").mkdir(parents=True, exist_ok=True)
    config_files: dict[str, int] = {}
    for fname in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
        src = pdir / fname
        if src.is_file():
            shutil.copyfile(src, dest / fname)
            config_files[fname] = src.stat().st_size
    pkgdir = pdir / "node_modules" / MARKET_PKG
    stats = copy_tree(pkgdir, dest / "node_modules" / MARKET_PKG) if pkgdir.is_dir() else {
        "files": 0, "bytes": 0, "errors": [], "links": 0
    }
    manifest = {
        "lane": lane,
        "profile": profile,
        "home": str(home),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "package": MARKET_PKG,
        "version": _manifest_version(pkgdir),
        "spec": market_declared_spec(cfg, lane, profile),
        "files": stats.get("files", 0),
        "bytes": stats.get("bytes", 0),
        "errors": [str(err) for err in (stats.get("errors") or [])],
        "config_files": config_files,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["dir"] = dest
    manifest["pruned"] = prune_market_snapshots(cfg, lane)
    return manifest


def market_restore_snapshot(cfg: dict, lane: str, snap_dir, profile: str = PROFILE_NAME) -> bool:
    """把快照里的包目录与三个清单文件放回去（更新失败 / 起不来时用）。"""
    pdir = profile_dir(lane_home(cfg, lane), profile)
    snap = Path(snap_dir)
    if not (snap / "manifest.json").is_file():
        fail(f"快照不完整（缺 manifest.json）：{snap}")
        return False
    ok_all = True
    try:
        pkgdir = pdir / "node_modules" / MARKET_PKG
        snap_pkg = snap / "node_modules" / MARKET_PKG
        if snap_pkg.is_dir():
            if pkgdir.exists():
                shutil.rmtree(pkgdir)
            stats = copy_tree(snap_pkg, pkgdir)
            if stats.get("errors"):
                fail(f"还原包目录时有 {len(stats['errors'])} 个错误：{stats['errors'][:2]}")
                ok_all = False
        for fname in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
            src = snap / fname
            if src.is_file():
                shutil.copyfile(src, pdir / fname)
    except OSError as exc:
        fail(f"还原失败：{exc}")
        return False
    return ok_all


def cmd_market_rollback(cfg: dict, args) -> int:
    """用启动器自动存的快照把插件市场退回上一版。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = market_snapshots(cfg, lane)
    if not snaps:
        fail(f"lane「{lane}」还没有插件市场快照（market-update 会每次自动存一份）")
        return 1
    target = None
    if getattr(args, "name", None):
        for item in snaps:
            if item["name"] == args.name:
                target = item
                break
        if target is None:
            fail(f"没有这个快照：{args.name}")
            return 1
    else:
        target = snaps[0]
    if not target["ok"]:
        fail(f"快照 {target['name']} 不完整（缺 manifest.json），不能用")
        return 1
    if lane_runtime(cfg, lane):
        warn(f"lane「{lane}」正在运行：还原的是磁盘上的文件，运行中的进程要重启才会用新的")
    info(col(f"── 退回插件市场：lane「{lane}」──", C.BOLD))
    info(f"  快照      {target['name']}（{target['created']}，里面是 {target['version'] or '?'}）")
    if not market_restore_snapshot(cfg, lane, target["dir"]):
        return 1
    now = _manifest_version(market_package_dir(cfg, lane))
    if now == target["version"]:
        ok(f"已退回 {now}")
    else:
        warn(f"退回后读到的是 {now or '（读不到）'}，和快照里的 {target['version'] or '?'} 不一致，请人工看一眼")
    info(col(f"  重启这条 lane 才会生效： py {ME_NAME} stop {lane} && py {ME_NAME} open {lane}", C.CYAN))
    return 0


def cmd_market_update(cfg: dict, args) -> int:
    """更新插件市场：**点名到版本**地装，装完做启动冒烟，失败自动回滚。

    实测（pnpm 11.22.0）的规矩是这样：`minimumReleaseAge` 只拦**自动升级**（解析 `@latest` 或版本范围），
    而**点名到具体版本的安装**它不但放行，还会自己往 `pnpm-workspace.yaml` 里记一条：

        Added 1 entry to minimumReleaseAgeExclude in pnpm-workspace.yaml ...
          dshmarket@1.66.1

    所以"更新"的关键不是绕过，而是**把版本号说出来**——写 `@latest` 才会被静默降级。
    只有 profile 显式打开了 `minimumReleaseAgeStrict`（连点名也拦）才需要 `--anyway`
    （带 pnpm 的一次性绕过 `--config.minimum-release-age=0`；这个拼法是 dshmarket 自己源码里的结论，
    `--config.minimumReleaseAge=0` 在 pnpm 12.3+ 上会被静默忽略）。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    me = ME_NAME
    info(col(f"── 更新插件市场：lane「{lane}」──", C.BOLD))
    if lane_runtime(cfg, lane) and not getattr(args, "force", False):
        fail(f"lane「{lane}」正在运行：更新会改它 profile 里的 node_modules，"
             "运行中的进程可能读到半截文件。先 stop 再更新（或加 --force 强来）")
        return 1
    st = market_status(cfg, lane, with_registry=not getattr(args, "offline", False))
    current = st["installed"]
    if not st["spec"] and not st["package_dir"].is_dir():
        fail(f"lane「{lane}」的 profile 里没有插件市场（{MARKET_PKG}），没有可更新的东西")
        return 1
    target = getattr(args, "version", None) or st["latest"]
    if not target:
        fail("不知道更新到哪个版本：registry 没查到"
             f"（{st['registry_error'] or '离线模式'}）。用 --version 指定")
        return 1
    versions = st.get("versions") or []
    if versions and getattr(args, "version", None) and target not in versions:
        near = sorted(versions, key=version_key)[-6:]
        fail(f"registry 上没有 {target} 这个版本（最近的几个：{'、'.join(near)}）")
        return 1
    if target == current and not getattr(args, "force", False):
        ok(f"已经是最新：{target}")
        return 0

    info(f"  当前版本  {current or '（没装）'}"
         + (f"      profile 里写的是 {st['spec']}" if st["spec"] else ""))
    info(f"  目标版本  {target}" + (f"（{_fmt_age(st['published'])}）" if st["published"] else ""))
    info(col("  规矩      只装点名的这个版本（pnpm 会自己把它记进 minimumReleaseAgeExclude）；"
             "它的「按住新版本」只拦**自动升级**，不拦点名安装", C.GRAY))
    if args.anyway:
        info(col("  --anyway   连「点名也要拦」的 profile（minimumReleaseAgeStrict）都硬闯", C.GRAY))
    snap = market_snapshot(cfg, lane, label=f"before-{target}", note=f"{current or '?'} → {target}")
    info(col(f"  已存快照  {snap['dir']}（{snap['files']} 个文件 / {human_size(snap['bytes'])}）", C.GRAY))
    if snap.get("pruned"):
        info(col(f"            顺手清掉更老的 {len(snap['pruned'])} 份快照（只留最近 5 份）", C.GRAY))
    info(col("  正在装（走 dsh plugin → pnpm，可能要一两分钟）…", C.GRAY))
    pnpm_args = ["add", f"{MARKET_PKG}@{target}"]
    if args.anyway:
        pnpm_args.append(MARKET_BYPASS_FLAG)
    res = run_plugin_cmd(cfg, lane, pnpm_args, label="market-add")
    for rep in res.get("repairs") or []:
        if isinstance(rep, dict) and rep.get("changed"):
            if isinstance(rep["changed"], list):
                for line in rep["changed"]:
                    info(col(f"  已修复    pnpm 账本里跟着复制变味的本地依赖：{line}", C.YELLOW))
            else:
                info(col(f"  已修复    pnpm 账本里的 virtualStoreDir 原本指回原件"
                         f"（{rep['recorded']}），已改成本 lane 自己的 {rep['want']}", C.YELLOW))
        elif isinstance(rep, dict) and rep.get("errors"):
            for line in rep["errors"]:
                info(col(f"  （pnpm 账本有一处没修上：{line}）", C.GRAY))
    if not res["ok"]:
        fail(f"pnpm 没装成（退出码 {res['rc']}{'：' + res['error'] if res['error'] else ''}）")
        for line in res["tail"]:
            info(col("      " + line, C.GRAY))
        if res.get("log"):
            info(col(f"      完整日志：{res['log']}", C.GRAY))
        info(col(f"      恢复现场： py {me} market-rollback {lane}", C.CYAN))
        return 1

    now = _manifest_version(market_package_dir(cfg, lane))
    if now != target:
        warn(f"pnpm 跑完了（退出码 0），但装上的还是 {now or '旧版本'}，不是 {target}")
        if not args.anyway:
            info(col("      点名了版本还是没装上，通常是这个 profile 打开了 minimumReleaseAgeStrict"
                     "（默认只拦自动升级，strict 连点名版本也拦）——pnpm 会安静地保留旧版本、返回 0。", C.GRAY))
            info(col(f"      要装就明确闯一次： py {me} market-update {lane} --version {target} --anyway", C.CYAN))
            info(col("      --anyway 会带上 pnpm 的一次性绕过 --config.minimum-release-age=0", C.GRAY))
        if market_restore_snapshot(cfg, lane, snap["dir"]):
            info(col("      已把刚才的改动还原回去", C.GRAY))
        else:
            info(col(f"      还原失败，请手动跑： py {me} market-rollback {lane}", C.CYAN))
        return 1
    ok(f"已更新：{current or '旧版本'} → {target}")
    info(col(f"      profile 里记的是 {market_declared_spec(cfg, lane) or '?'}"
             "（pnpm 更新已存在的依赖时保留它原来的 ^ 写法，装上的就是你点名那个版本）", C.GRAY))

    problems = False
    rows_new = bundle_row_ids(lane_home(cfg, lane), MARKET_PKG, PROFILE_NAME)
    if not getattr(args, "no_verify", False):
        info(col("  再组合一次，确认行还在、插件市场还能被加载…", C.GRAY))
        dump = run_dump_config(cfg, lane)
        if not dump.get("ok"):
            warn("组合复核没跑起来，更新的效果**未经验证**")
        else:
            present = [row for row in rows_new if row in dump["rows"]]
            if present:
                ok(f"组合核对通过：行 {'、'.join(present)} 在组合结果里"
                   + ("（当前是禁用状态）" if all((dump['rows'][r] or {}).get("disabled") for r in present) else ""))
            else:
                fail(f"更新后它的行（{'、'.join(rows_new) or '无'}）不在组合结果里了——新版本可能改了行 id 或不兼容")
                problems = True

    if getattr(args, "no_boot_check", False):
        info(col("  （按你的要求跳过了启动冒烟）", C.GRAY))
    elif lane_runtime(cfg, lane):
        warn("这条 lane 正在运行，跳过启动冒烟——更新要重启才生效")
    else:
        info(col("  启动冒烟：确认新版本还能起来…", C.GRAY))
        if boot_smoke(cfg, lane):
            ok("启动冒烟通过（已把它停回去）")
        else:
            problems = True
            fail("更新后**起不来**——正在回滚到更新前的版本")
            if market_restore_snapshot(cfg, lane, snap["dir"]):
                back = _manifest_version(market_package_dir(cfg, lane))
                ok(f"已回滚（现在是 {back or '?'}）")
            else:
                fail(f"自动回滚失败，请手动跑： py {me} market-rollback {lane}")
    info("")
    info(col(f"  重启生效： py {me} stop {lane} && py {me} open {lane}", C.CYAN))
    info(col(f"  反悔用  ： py {me} market-rollback {lane}", C.CYAN))
    return 1 if problems else 0


# ======================= 备份对话（sessions 快照） =======================


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def tree_hashes(root) -> dict[str, list]:
    """整棵树的逐文件指纹：{相对路径: [字节数, sha256]}（路径统一用 `/`）。

    对话文件都是 .zstd（已经压过），所以"逐文件哈希"是唯一能证明
    "这份备份真的能用"的办法——文件数对上不代表内容没坏。
    """
    out: dict[str, list] = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if _is_reparse(path):
                continue
            try:
                out[rel] = [path.stat().st_size, sha256_file(path)]
            except OSError:
                out[rel] = [-1, "read-error"]
    return out


def chat_lane_root(cfg: dict, lane: str) -> Path:
    return paths(cfg)["backups"] / "chat" / lane


def chat_snapshots(cfg: dict, lane: str) -> list[dict]:
    root = chat_lane_root(cfg, lane)
    items: list[dict] = []
    if not root.is_dir():
        return items
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        manifest = path / "manifest.json"
        item = {"name": path.name, "dir": path, "created": "", "sessions": 0, "files": 0,
                "bytes": 0, "version": "", "running": False, "note": "", "ok": manifest.is_file(),
                "size": 0}
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                for key in ("created", "sessions", "files", "bytes", "version", "running", "note"):
                    if key in data:
                        item[key] = data[key]
            except Exception:
                pass
        item["size"] = dir_size(path)
        items.append(item)
    return items


def sessions_snapshot(cfg: dict, lane: str, label: str = "", note: str = "") -> dict:
    """把 <HOME>/sessions 整棵复制成一份快照，并记录逐文件指纹。"""
    home = lane_home(cfg, lane)
    src = home / "sessions"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    dest = chat_lane_root(cfg, lane) / name
    if dest.exists():
        name = f"{name}-{int(time.time()) % 1000}"
        dest = chat_lane_root(cfg, lane) / name
    dest.mkdir(parents=True, exist_ok=True)
    running = bool(lane_runtime(cfg, lane))
    stats = copy_tree(src, dest / "sessions")
    hashes = tree_hashes(dest / "sessions")
    manifest = {
        "lane": lane,
        "home": str(home),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "source": str(src),
        "running": running,
        "version": cfg["lanes"].get(lane, {}).get("version") or "",
        "sessions": sum(1 for key in hashes if key.endswith(".zstd")),
        "files": stats.get("files", 0),
        "bytes": stats.get("bytes", 0),
        "errors": [str(err) for err in (stats.get("errors") or [])],
        "hashes": hashes,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["dir"] = dest
    manifest["name"] = name
    return manifest


def _ask_yes(question: str) -> bool:
    try:
        answer = input(f"{question}（输入 y 回车确认，其它一律取消）：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes", "是")


def cmd_chat_backup(cfg: dict, args) -> int:
    """给这条 lane 的对话记录打一份完整快照（升级 / 动插件之前先来一份）。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    home = lane_home(cfg, lane)
    src = home / "sessions"
    if not src.is_dir():
        fail(f"这条 lane 还没有对话记录目录：{src}")
        return 1
    info(col(f"── 备份对话：lane「{lane}」──", C.BOLD))
    info(f"  来源      {src}")
    if lane_runtime(cfg, lane):
        warn("这条 lane 正在运行：正在写入的那个会话文件可能是半截的（其它会话不受影响）。"
             "要完全一致就先 stop 再备份。")
    info(col("  正在复制并逐文件算指纹…", C.GRAY))
    started = time.time()
    snap = sessions_snapshot(cfg, lane, label=getattr(args, "name", None) or "",
                             note="手动备份")
    took = round(time.time() - started, 1)
    info("")
    ok(f"已备份 {snap['sessions']} 个会话文件 / {snap['files']} 个文件 / {human_size(snap['bytes'])}"
       f"（{took} 秒）")
    info(f"  快照      {snap['dir']}")
    if snap["errors"]:
        fail(f"复制时有 {len(snap['errors'])} 个错误，这份备份**不可信**：{snap['errors'][:3]}")
        return 1
    me = ME_NAME
    info("")
    info(col(f"  看备份： py {me} chat-list {lane}", C.CYAN))
    info(col(f"  恢复  ： py {me} chat-restore {lane} {snap['name']}", C.CYAN))
    return 0


def cmd_chat_list(cfg: dict, args) -> int:
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = chat_snapshots(cfg, lane)
    me = ME_NAME
    info(col(f"── lane「{lane}」的对话备份 ──", C.BOLD))
    info(f"  目录      {chat_lane_root(cfg, lane)}")
    info(f"  会话目录  {lane_home(cfg, lane) / 'sessions'}"
         f"（现在有 {count_sessions(lane_home(cfg, lane))} 个会话文件）")
    if not snaps:
        info("")
        warn("还没有备份。先来一份：")
        info(col(f"      py {me} chat-backup {lane}", C.CYAN))
        return 0
    total = sum(int(item["size"] or 0) for item in snaps)
    info(f"  共 {len(snaps)} 份 / {human_size(total)}")
    info("")
    info(f"  {'名称':<32}{'时间':<20}{'会话':>5}{'大小':>12}  说明")
    info("  " + "─" * 86)
    for item in snaps:
        flag = "" if item["ok"] else "  ← 缺 manifest.json，不可用"
        note = item["note"] or ("运行时备份" if item["running"] else "")
        info(f"  {item['name'][:31]:<32}{str(item['created'])[:19]:<20}"
             f"{item['sessions']:>5}{human_size(item['size']):>12}  {note}{flag}")
    info("")
    info(col(f"  恢复： py {me} chat-restore {lane} <名称>      删除： py {me} chat-rm {lane} <名称>", C.CYAN))
    return 0


def cmd_chat_restore(cfg: dict, args) -> int:
    """把对话记录恢复到某个快照。

    动手之前做两件安全事：先校验**快照自身**是不是好的（坏的备份恢复只会更糟），
    再把**现状**另存一份（恢复错了还能回来）。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = chat_snapshots(cfg, lane)
    if not snaps:
        fail(f"lane「{lane}」还没有对话备份，先 chat-backup {lane}")
        return 1
    target = None
    if getattr(args, "name", None):
        for item in snaps:
            if item["name"] == args.name:
                target = item
                break
        if target is None:
            fail(f"没有这个快照：{args.name}（用 chat-list 看有哪些）")
            return 1
    else:
        target = snaps[0]
    if not target["ok"]:
        fail(f"快照 {target['name']} 不完整（缺 manifest.json），不能用")
        return 1
    home = lane_home(cfg, lane)
    live = home / "sessions"
    if live.parent != home or live.name != "sessions":
        fail("内部安全检查没通过（会话目录路径异常），不动手")
        return 1
    data = json.loads((target["dir"] / "manifest.json").read_text(encoding="utf-8"))
    expected = data.get("hashes") or {}

    info(col(f"── 恢复对话记录：lane「{lane}」──", C.BOLD))
    info(f"  快照      {target['name']}（{target['created']}，"
         f"{target['sessions']} 个会话文件 / {human_size(target['bytes'])}）")
    info(f"  恢复到    {live}")
    if data.get("running"):
        warn("这份快照是**运行时**存的，其中一个会话文件可能是半截的")
    # 没让启动器替你停就直接拒掉：别等校验完一遍（几百 MB）才说"车道在跑"。
    # 带 --stop 的话，校验放在停之前——万一备份本身是坏的，就不用白白停一次。
    if lane_runtime(cfg, lane) and not getattr(args, "stop", False):
        fail(f"lane「{lane}」正在运行：恢复会话必须先停它（或加 --stop 让启动器替你停）")
        return 1
    info("")
    info(col("  先校验快照自身（万一备份本身坏了，恢复只会更糟）…", C.GRAY))
    if expected:
        actual = tree_hashes(target["dir"] / "sessions")
        bad = [key for key, value in expected.items() if actual.get(key) != value]
        extra = [key for key in actual if key not in expected]
        if bad or extra:
            message = f"快照自身有 {len(bad)} 个文件对不上、{len(extra)} 个多出来"
            if getattr(args, "force", False):
                warn(message + "（按 --force 继续）")
            else:
                fail(message + "——先别恢复。确认没问题再加 --force")
                return 1
        else:
            ok(f"快照校验通过（{len(expected)} 个文件逐字节一致）")
    else:
        warn("这份快照没记逐文件指纹（老备份），只能核对文件数")

    if lane_runtime(cfg, lane):
        if not getattr(args, "stop", False):
            fail(f"lane「{lane}」正在运行：恢复会话必须先停它（或加 --stop 让启动器替你停）")
            return 1
        info(col("  正在停止这条 lane…", C.GRAY))
        cmd_stop(cfg, argparse.Namespace(target=[lane]))

    if not getattr(args, "yes", False) and not _ask_yes("确认恢复？当前的对话记录会先被另存一份"):
        info("已取消")
        return 0

    pre = None
    if live.is_dir() and any(live.iterdir()):
        info(col("  先把现状另存一份（恢复错了还能回来）…", C.GRAY))
        pre = sessions_snapshot(cfg, lane, label="恢复前", note=f"恢复到 {target['name']} 之前的现状")
        info(col(f"      已存：{pre['name']}", C.GRAY))
    info(col("  正在替换会话目录…", C.GRAY))
    try:
        if live.exists():
            shutil.rmtree(live)
        stats = copy_tree(target["dir"] / "sessions", live)
    except OSError as exc:
        fail(f"替换失败：{exc}")
        if pre:
            info(col(f"      现状还在快照 {pre['name']} 里，可以用 chat-restore {lane} {pre['name']} --yes 找回来", C.GRAY))
        return 1
    ok(f"已恢复 {stats.get('files', 0)} 个文件（{human_size(stats.get('bytes', 0))}）")
    if expected:
        after = tree_hashes(live)
        diff = [key for key, value in expected.items() if after.get(key) != value]
        if diff:
            fail(f"恢复后有 {len(diff)} 个文件对不上：{diff[:3]}")
            return 1
        ok(f"逐文件校验通过：{len(expected)} 个文件与快照完全一致")
    me = ME_NAME
    if pre:
        info(col(f"  反悔用  ： py {me} chat-restore {lane} {pre['name']} --yes", C.CYAN))
    info(col(f"  重启    ： py {me} open {lane}", C.CYAN))
    return 0


def cmd_chat_rm(cfg: dict, args) -> int:
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = chat_snapshots(cfg, lane)
    target = None
    for item in snaps:
        if item["name"] == args.name:
            target = item
            break
    if target is None:
        fail(f"没有这个快照：{args.name}")
        return 1
    root = chat_lane_root(cfg, lane).resolve()
    victim = Path(target["dir"]).resolve()
    if victim.parent != root:
        fail("内部安全检查没通过（快照不在备份目录里），不动手")
        return 1
    size = target["size"]
    if not getattr(args, "yes", False) and not _ask_yes(f"确认删除快照 {target['name']}（{human_size(size)}）"):
        info("已取消")
        return 0
    try:
        shutil.rmtree(victim)
    except OSError as exc:
        fail(f"删除失败：{exc}")
        return 1
    ok(f"已删除快照 {target['name']}（{human_size(size)}）")
    return 0


# ================= 配置备份（轻量）：插件清单 + 会话 + 设置，恢复时自动拉取插件 =================
#
# clone 是"整套复制"（安装树 + HOME 动辄近 1 GB），慢且绑死同一个 dsh 版本；
# chat-backup 只备份会话。这里做第三种：
#
#   cfg-backup  —— 只备份"装了什么"（每个 profile 的 package.json / cordis.patch.yml /
#                  pnpm-workspace.yaml，含插件精确版本、bundles 登记、开关状态）+
#                  会话 + 设置文件（+ 可选凭据 / 插件数据目录）+ file: 依赖引用到的
#                  本地插件本体；**不备份** dsh 安装树与 profiles/node_modules。
#   cfg-restore —— 新建 lane 装好 dsh 后，按清单用 `dsh plugin add pkg@版本 --save-exact`
#                  把插件逐个联网拉回来，再把补丁层、会话、设置、凭据放回原位。
#
# 备份包是**单个 zip**（便于拷走 / 分享），放在 <root>/backups/config/：
#
#   manifest.json
#   profiles/<p>/package.json  profiles/<p>/cordis.patch.yml
#   profiles/<p>/pnpm-workspace.yaml  profiles/<p>/compatibility.json
#   sessions/**                  （可选，默认带；.zstd 已压缩，zip 里再存不压缩）
#   attachments/**               （可选，默认带，会话引用的附件）
#   home/<相对路径>               （HOME 顶层设置白名单小文件）
#   creds/.credentials.yaml      （可选，默认**不**带：敏感）
#   local/<key>/**               （package.json 里 file:/link: 依赖实际引用的本地插件）
#   data/<目录名>/**              （可选，用户勾选的插件数据目录）

CFG_FORMAT = "dsh-lanes-config-backup"
CFG_FORMAT_VERSION = 1
CFG_FILE_PREFIX = "dshcfg-"

# HOME 顶层"设置文件"的处理：显式点名一批，其余按"小体积 json/yaml"白名单兜底，
# 统计账本 / 用量 / 运行态 / .bak 一律排除。
CFG_SETTINGS_EXPLICIT = (
    "settings.yaml", "settings.yaml.imported",
    "dsh-service-config.json", "dsh-service-session-titles.json",
    "dsh-service-subagent-route.json", "dsh-web-settings-legacy-import.json",
    "pet.json", "studymate-config.yaml", "skin-center-active.json",
    "thinking-effort-loaded.json", "remote-web-ui-devices.json",
    ".anonymous-user-id", "cordis.patch.yml",
)
CFG_SETTING_EXCLUDE_RE = re.compile(
    r"(^\.credentials\.yaml$)"
    r"|(^\.dsh-usage)"
    r"|(^\.dshw-)"
    r"|(^tokenledger)"
    r"|(^dsh-service-usage-index)"
    r"|(\.bak$)"
    r"|(\.before-)"
    r"|(\.notify-)"
    r"|(\.reconcile)"
)
CFG_SETTING_RE = re.compile(r".*\.(json|ya?ml)$", re.I)
CFG_MAX_SETTING_BYTES = 2 * 1024 * 1024

# HOME 顶层目录里，"插件数据目录"可勾选名单的排除项：
# 安装树 / 会话 / 缓存 / 日志 / 备份 / 账本 / 市场工作目录不进（要么体积没意义，要么是工作态）。
CFG_DATA_EXCLUDE = {
    "profiles", "sessions", "attachments", "cache", "logs", "backups",
    "node_modules", "dsh-market", "dsh-config-manager", "storages",
    "plugins", "local-plugins", "dsh-session-archive",
}

# 每个 profile 原样带走的清单文件（缺一两个没关系，按实际存在的打）
CFG_PROFILE_FILES = ("package.json", "cordis.patch.yml", "pnpm-workspace.yaml",
                     "compatibility.json")


def cfg_backup_root(cfg: dict) -> Path:
    return paths(cfg)["backups"] / "config"


def cfg_setting_files(home: Path) -> list[Path]:
    """HOME 顶层该进备份的设置文件（白名单，小文件，排除统计/账本/运行态）。"""
    home = Path(home)
    out: list[Path] = []
    if not home.is_dir():
        return out
    seen: set[str] = set()
    for entry in sorted(home.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if CFG_SETTING_EXCLUDE_RE.search(name):
            continue
        keep = name in CFG_SETTINGS_EXPLICIT or bool(CFG_SETTING_RE.match(name))
        if not keep:
            continue
        try:
            if entry.stat().st_size > CFG_MAX_SETTING_BYTES:
                continue
        except OSError:
            continue
        if name not in seen:
            seen.add(name)
            out.append(entry)
    return out


def cfg_data_dirs(home: Path) -> list[dict]:
    """HOME 顶层可作为"插件数据"勾选的目录（排除工作态/缓存/账本后），带体积。"""
    home = Path(home)
    out: list[dict] = []
    if not home.is_dir():
        return out
    for entry in sorted(home.iterdir()):
        if not entry.is_dir() or entry.name in CFG_DATA_EXCLUDE:
            continue
        if _is_reparse(entry):
            continue
        size = dir_size(entry)
        out.append({"name": entry.name, "dir": entry, "bytes": size})
    return out


def cfg_collect_local_specs(home: Path, profile_names: list[str]) -> list[dict]:
    """找出各 profile package.json 里 file:/link: 依赖**实际引用**的本地文件/目录。

    只收集真实存在于磁盘上的引用，按绝对路径去重；记录它在源 HOME 内还是外
    （恢复时：HOME 内的还原到相对原位，HOME 外的收进目标 HOME/local-plugins/from-backup/）。
    """
    home = Path(home)
    found: list[dict] = []
    seen_paths: set[str] = set()
    for prof in profile_names:
        pdir = profile_dir(home, prof)
        deps = profile_dependencies(home, prof)
        for pkg, spec in deps.items():
            raw = (spec or "").strip()
            if not raw.lower().startswith(("file:", "link:")):
                continue
            tail = raw.split(":", 1)[1]
            target = Path(tail)
            if not target.is_absolute():
                target = pdir / tail
            try:
                target = target.resolve()
            except OSError:
                pass
            key = str(target).lower()
            if not target.exists() or key in seen_paths:
                continue
            seen_paths.add(key)
            try:
                rel = target.relative_to(home.resolve()).as_posix()
                inside = True
            except (ValueError, OSError):
                rel = ""
                inside = False
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", target.name).strip("-") or "item"
            found.append({
                "key": f"{len(found):02d}-{safe[:40]}",
                "pkg": pkg,
                "spec": raw,
                "sourcePath": str(target),
                "insideHome": inside,
                "relPath": rel,
                "isDir": target.is_dir(),
            })
    return found


def cfg_profiles_of(home: Path, only: list[str] | None) -> list[str]:
    """备份/恢复要处理的 profile 名单（磁盘真值；desktop 排除；默认全部）。"""
    names = [p for p in disk_profiles(Path(home)) if p != "desktop"]
    if only is not None:
        wanted = set(only)
        names = [p for p in names if p in wanted]
    return names


def cfg_backup_plan(cfg: dict, lane: str, sessions: bool = True, attachments: bool = True,
                    settings: bool = True, credentials: bool = False,
                    plugin_data: list[str] | None = None,
                    profile_names: list[str] | None = None) -> dict:
    """算清"这份备份会包含什么"（不写盘），供 CLI 与界面展示确认。"""
    if lane not in cfg.get("lanes", {}):
        return {"ok": False, "why": f"没有 lane「{lane}」"}
    home = lane_home(cfg, lane)
    if profile_names is not None:
        missing = [p for p in profile_names if p not in disk_profiles(home)]
        if missing:
            return {"ok": False, "why": f"这几个 profile 不存在：{'、'.join(missing)}"}
    profs = cfg_profiles_of(home, profile_names)
    profiles_out = []
    plugin_count = 0
    for prof in profs:
        plugins = profile_plugins(cfg, lane, prof, include_official=True)
        plugin_count += sum(1 for p in plugins if not p["official"])
        missing_files = [f for f in CFG_PROFILE_FILES
                         if not (profile_dir(home, prof) / f).is_file()]
        profiles_out.append({
            "name": prof,
            "plugins": plugins,
            "thirdParty": sum(1 for p in plugins if not p["official"]),
            "missingFiles": missing_files,
        })
    sess = {"files": 0, "bytes": 0}
    if sessions and (home / "sessions").is_dir():
        sess_bytes = dir_size(home / "sessions")
        sess_files = sum(1 for _ in (home / "sessions").rglob("*") if _.is_file())
        sess = {"files": sess_files, "bytes": sess_bytes}
    att = {"files": 0, "bytes": 0}
    if attachments and (home / "attachments").is_dir():
        att = {"files": sum(1 for _ in (home / "attachments").rglob("*") if _.is_file()),
               "bytes": dir_size(home / "attachments")}
    setting_files = cfg_setting_files(home) if settings else []
    cred_entries = sorted(read_credential_refs(home)) if credentials else []
    if credentials and not (home / CRED_FILENAME).is_file():
        cred_entries = []
    local_files = cfg_collect_local_specs(home, profs)
    for item in local_files:
        src = Path(item["sourcePath"])
        item["bytes"] = dir_size(src) if item["isDir"] else (src.stat().st_size if src.is_file() else 0)
    data_avail = {d["name"]: d for d in cfg_data_dirs(home)}
    data_picked = []
    for name in (plugin_data or []):
        d = data_avail.get(name)
        if d:
            data_picked.append({"name": name, "bytes": d["bytes"]})
    total = (sess["bytes"] + att["bytes"] + sum(d["bytes"] for d in data_picked)
             + sum(d["bytes"] for d in local_files)
             + sum(f.stat().st_size for f in setting_files))
    return {
        "ok": True, "lane": lane, "home": home,
        "dshVersion": cfg["lanes"][lane].get("version") or "",
        "profiles": profiles_out, "profilesNames": profs,
        "sessions": sess, "attachments": att,
        "settingsFiles": [{"name": f.name, "bytes": f.stat().st_size} for f in setting_files],
        "credentials": cred_entries,
        "localFiles": local_files,
        "dataDirs": [{"name": d["name"], "bytes": d["bytes"]} for d in data_avail.values()],
        "dataPicked": data_picked,
        "thirdParty": plugin_count,
        "totalBytes": total,
        "running": bool(lane_runtime(cfg, lane)),
    }


def _zip_add_tree(zf: zipfile.ZipFile, src: Path, prefix: str, stored: bool,
                  hashes: dict[str, str]) -> dict:
    """把一棵目录写进 zip（junction / reparse 点跳过）。stored=True 用不压缩（已压缩内容）。"""
    src = Path(src)
    stats = {"files": 0, "bytes": 0, "errors": []}
    if not src.is_dir():
        return stats
    compress = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    for dirpath, _dirs, filenames in os.walk(src):
        for name in filenames:
            path = Path(dirpath) / name
            if _is_reparse(path):
                continue
            rel = path.relative_to(src).as_posix()
            arc = f"{prefix.rstrip('/')}/{rel}"
            try:
                data = path.read_bytes()
            except OSError as exc:
                stats["errors"].append(f"{rel}: {exc}")
                continue
            zinfo = zipfile.ZipInfo.from_file(path, arcname=arc)
            zinfo.compress_type = compress
            zf.writestr(zinfo, data)
            hashes[arc] = hashlib.sha256(data).hexdigest()
            stats["files"] += 1
            stats["bytes"] += len(data)
    return stats


def _zip_add_bytes(zf: zipfile.ZipFile, arc: str, data: bytes,
                   hashes: dict[str, str], compress: int = zipfile.ZIP_DEFLATED) -> None:
    zinfo = zipfile.ZipInfo(arc)
    zinfo.compress_type = compress
    zinfo.external_attr = 0o600 << 16
    zf.writestr(zinfo, data)
    hashes[arc] = hashlib.sha256(data).hexdigest()


def cfg_create_backup(cfg: dict, lane: str, label: str = "",
                      sessions: bool = True, attachments: bool = True,
                      settings: bool = True, credentials: bool = False,
                      plugin_data: list[str] | None = None,
                      profile_names: list[str] | None = None) -> dict:
    """真正打一个配置备份 zip。返回 manifest（含 zip 路径）。"""
    plan = cfg_backup_plan(cfg, lane, sessions=sessions, attachments=attachments,
                           settings=settings, credentials=credentials,
                           plugin_data=plugin_data, profile_names=profile_names)
    if not plan["ok"]:
        raise RuntimeError(plan["why"])
    home = plan["home"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_lane = re.sub(r"[^A-Za-z0-9._-]+", "-", lane).strip("-") or "lane"
    name = f"{CFG_FILE_PREFIX}{safe_lane}-{stamp}"
    if label:
        name += "-" + re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")
    name += ".zip"
    root = cfg_backup_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / name
    if dest.exists():
        dest = root / f"{dest.stem}-{int(time.time()) % 1000}.zip"

    hashes: dict[str, str] = {}
    manifest_profiles = []
    try:
        _cfg_write_backup_zip(dest, home, plan, hashes, manifest_profiles, lane, cfg)
    except Exception:
        # 半成品（缺 manifest / 写到一半）必须删掉，否则 cfg-list 里会挂着"损坏"包
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        raise
    # 重新读一遍刚写完的 manifest（写 zip 的辅助函数会把它落到 dest）
    manifest = cfg_read_manifest(dest)
    manifest["zip"] = dest
    manifest["name"] = dest.name
    return manifest


def _cfg_write_backup_zip(dest: Path, home: Path, plan: dict, hashes: dict[str, str],
                          manifest_profiles: list[dict], lane: str, cfg: dict) -> None:
    """把 plan 里选好的内容写成 zip（manifest 最后写，写失败则由调用方删掉半成品）。"""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # ── profile 清单文件（不含 node_modules：插件本体恢复时联网拉回）
        for prof in plan["profiles"]:
            pname = prof["name"]
            pdir = profile_dir(home, pname)
            files_map = {}
            for fname in CFG_PROFILE_FILES:
                src = pdir / fname
                if not src.is_file():
                    continue
                arc = f"profiles/{pname}/{fname}"
                if fname == "pnpm-workspace.yaml":
                    # 备份前先把 allowBuilds 里的非法 git/URL 键规范化（坏键会让恢复时
                    # 所有 pnpm add 都失败）
                    data = _cfg_normalize_workspace_text(
                        src.read_text(encoding="utf-8")).encode("utf-8")
                else:
                    data = src.read_bytes()
                _zip_add_bytes(zf, arc, data, hashes)
                files_map[fname] = arc
            manifest_profiles.append({
                "name": pname,
                "bundles": [p["pkg"] for p in prof["plugins"] if p["in_bundles"]],
                "files": files_map,
                "plugins": [{
                    "pkg": p["pkg"],
                    "spec": p["spec"],
                    "version": p["version"],
                    "inBundles": p["in_bundles"],
                    "official": p["official"],
                    "kind": p["kind"],
                    "rows": p["rows"],
                    "disabledRows": p["disabled_rows"],
                    "localSpec": (p["spec"] if str(p["spec"]).lower().startswith(
                        ("file:", "link:")) else ""),
                } for p in prof["plugins"]],
            })

        contents: dict = {}

        # ── 会话（.zstd 已压缩，zip 里不重复压）
        if plan["sessions"].get("files"):
            st = _zip_add_tree(zf, home / "sessions", "sessions", True, hashes)
            if st["errors"]:
                raise RuntimeError(f"打包会话时有错误：{st['errors'][:3]}")
            contents["sessions"] = {"included": True, "files": st["files"],
                                    "bytes": st["bytes"], "zipPrefix": "sessions"}
        else:
            contents["sessions"] = {"included": False, "files": 0, "bytes": 0}

        # ── 会话附件
        if plan["attachments"].get("files"):
            st = _zip_add_tree(zf, home / "attachments", "attachments", True, hashes)
            contents["attachments"] = {"included": True, "files": st["files"],
                                       "bytes": st["bytes"], "zipPrefix": "attachments"}
        else:
            contents["attachments"] = {"included": False, "files": 0, "bytes": 0}

        # ── 设置文件（白名单小文件，可压缩）
        setting_items = []
        for f in plan["settingsFiles"]:
            fpath = home / f["name"]
            arc = f"home/{f['name']}"
            _zip_add_bytes(zf, arc, fpath.read_bytes(), hashes)
            setting_items.append({"name": f["name"], "zipPath": arc, "bytes": f["bytes"]})
        contents["settings"] = {"included": bool(setting_items), "files": setting_items}

        # ── 凭据（默认不带；敏感）
        if plan["credentials"]:
            arc = "creds/.credentials.yaml"
            _zip_add_bytes(zf, arc, (home / CRED_FILENAME).read_bytes(), hashes)
            contents["credentials"] = {"included": True,
                                        "entries": plan["credentials"],
                                        "zipPath": arc}
        else:
            contents["credentials"] = {"included": False, "entries": []}

        # ── file:/link: 本地依赖引用到的插件本体
        local_items = []
        for item in plan["localFiles"]:
            src = Path(item["sourcePath"])
            arc_base = f"local/{item['key']}"
            if item["isDir"]:
                st = _zip_add_tree(zf, src, arc_base, False, hashes)
                item.update({"files": st["files"], "errors": st["errors"]})
                zip_path = arc_base
            else:
                arc = f"{arc_base}/{src.name}"
                _zip_add_bytes(zf, arc, src.read_bytes(), hashes)
                item.update({"files": 1})
                zip_path = arc
            local_items.append({k: item[k] for k in
                                ("key", "pkg", "spec", "sourcePath", "insideHome", "relPath",
                                 "isDir", "bytes", "files")} | {"zipPath": zip_path})
        contents["localFiles"] = local_items

        # ── 勾选的插件数据目录
        data_items = []
        for d in plan["dataPicked"]:
            src = home / d["name"]
            st = _zip_add_tree(zf, src, f"data/{d['name']}", False, hashes)
            data_items.append({"name": d["name"], "files": st["files"], "bytes": d["bytes"],
                               "zipPrefix": f"data/{d['name']}", "errors": st["errors"]})
        contents["pluginData"] = data_items

        manifest = {
            "format": CFG_FORMAT,
            "formatVersion": CFG_FORMAT_VERSION,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "createdStamp": time.strftime("%Y%m%d-%H%M%S"),
            "source": {"lane": lane, "home": str(home),
                       "dshVersion": cfg["lanes"][lane].get("version") or ""},
            "profiles": manifest_profiles,
            "contents": contents,
            "hashes": hashes,
        }
        # manifest 本身不进 hashes（它是最后才定稿的）
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))


def cfg_resolve_backup(cfg: dict, name_or_path: str) -> Path:
    """备份名（可不带 .zip）或任意 zip 路径 → 实际 zip 路径。"""
    raw = str(name_or_path or "").strip().strip('"')
    if not raw:
        raise ValueError("没给备份名或路径")
    p = Path(raw)
    if p.is_file():
        return p
    root = cfg_backup_root(cfg)
    cand = root / raw
    if cand.is_file():
        return cand
    if not raw.lower().endswith(".zip"):
        cand = root / (raw + ".zip")
        if cand.is_file():
            return cand
    matches = sorted(root.glob(f"{raw}*.zip")) if root.is_dir() else []
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"找不到配置备份：{name_or_path}（目录 {root}）")


def cfg_read_manifest(zip_path: Path) -> dict:
    """读并校验备份包 manifest。"""
    with zipfile.ZipFile(zip_path) as zf:
        try:
            data = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            raise ValueError(f"{zip_path.name} 里没有 manifest.json，不是合法的配置备份包")
    if data.get("format") != CFG_FORMAT:
        raise ValueError(f"{zip_path.name} 不是 dsh-lanes 配置备份包（format={data.get('format')!r}）")
    return data


def cfg_list_backups(cfg: dict) -> list[dict]:
    """列出全部配置备份包（按时间倒序）。"""
    root = cfg_backup_root(cfg)
    items: list[dict] = []
    if not root.is_dir():
        return items
    for path in sorted(root.glob(f"{CFG_FILE_PREFIX}*.zip"), reverse=True):
        item = {"name": path.name, "path": path, "bytes": 0, "ok": False,
                "created": "", "sourceLane": "", "dshVersion": "", "profiles": [],
                "thirdParty": 0, "sessions": 0, "credentials": False, "error": ""}
        try:
            item["bytes"] = path.stat().st_size
            data = cfg_read_manifest(path)
            item["ok"] = True
            item["created"] = data.get("created") or ""
            item["sourceLane"] = (data.get("source") or {}).get("lane") or ""
            item["dshVersion"] = (data.get("source") or {}).get("dshVersion") or ""
            item["profiles"] = [p["name"] for p in data.get("profiles") or []]
            item["thirdParty"] = sum(
                1 for p in data.get("profiles") or []
                for plug in p.get("plugins") or [] if not plug.get("official"))
            item["sessions"] = ((data.get("contents") or {}).get("sessions") or {}).get("files") or 0
            item["credentials"] = bool(((data.get("contents") or {}).get("credentials") or {}).get(
                "included"))
        except Exception as exc:  # noqa: BLE001
            item["error"] = f"{type(exc).__name__}: {exc}"
        items.append(item)
    return items


def cfg_summary(manifest: dict) -> dict:
    """从 manifest 算一份给人看的摘要（CLI show / 界面查看共用）。"""
    contents = manifest.get("contents") or {}
    profiles = []
    for p in manifest.get("profiles") or []:
        plugins = p.get("plugins") or []
        profiles.append({
            "name": p.get("name") or "",
            "thirdParty": [x for x in plugins if not x.get("official")],
            "officialCount": sum(1 for x in plugins if x.get("official")),
            "bundles": p.get("bundles") or [],
        })
    return {
        "created": manifest.get("created") or "",
        "source": manifest.get("source") or {},
        "profiles": profiles,
        "sessions": contents.get("sessions") or {"included": False, "files": 0, "bytes": 0},
        "attachments": contents.get("attachments") or {"included": False, "files": 0, "bytes": 0},
        "settings": contents.get("settings") or {"included": False, "files": []},
        "credentials": contents.get("credentials") or {"included": False, "entries": []},
        "localFiles": contents.get("localFiles") or [],
        "pluginData": contents.get("pluginData") or [],
    }


def _zip_validate(zf: zipfile.ZipFile, arc: str, hashes: dict[str, str]) -> str:
    """按 manifest 里的指纹校验 zip 成员，返回错误串（空＝通过）。"""
    if arc not in hashes:
        return ""
    actual = hashlib.sha256(zf.read(arc)).hexdigest()
    return "" if actual == hashes[arc] else f"{arc} 指纹不符（备份包可能损坏）"


def _safe_dest(root: Path, *parts: str) -> Path:
    """拼 zip 解压目标路径，并挡住 zip-slip（越界路径直接报错）。"""
    root = Path(root).resolve()
    dest = root.joinpath(*parts).resolve()
    if root not in dest.parents and dest != root:
        raise ValueError(f"备份包里的路径越界：{parts}")
    return dest


def _zip_extract_tree(zf: zipfile.ZipFile, prefix: str, dest_root: Path,
                      hashes: dict[str, str] | None = None,
                      overwrite: bool = True, errors: list[str] | None = None) -> dict:
    """把 zip 里 prefix/ 整棵解压到 dest_root（dest_root/<prefix 之后的相对路径>）。"""
    stats = {"files": 0, "bytes": 0, "skipped": 0}
    prefix = prefix.strip("/") + "/"
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix):]
        if not rel or rel.startswith("/"):
            continue
        if hashes:
            err = _zip_validate(zf, info.filename, hashes)
            if err:
                if errors is not None:
                    errors.append(err)
                continue
        dest = _safe_dest(dest_root, *rel.split("/"))
        if dest.exists() and not overwrite:
            stats["skipped"] += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = zf.read(info.filename)
        dest.write_bytes(data)
        stats["files"] += 1
        stats["bytes"] += len(data)
    return stats


def _zip_extract_file(zf: zipfile.ZipFile, arc: str, dest: Path,
                      hashes: dict[str, str] | None = None) -> bytes:
    """解压单个成员到 dest，返回字节。"""
    if hashes:
        err = _zip_validate(zf, arc, hashes)
        if err:
            raise ValueError(err)
    data = zf.read(arc)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data


def cfg_restore_plan(cfg: dict, manifest: dict, target: str = "", new_lane: str = "",
                     version: str = "", profiles: list[str] | None = None,
                     sessions: bool = True, settings: bool = True,
                     credentials: bool = True, plugin_data: bool = True) -> dict:
    """把"恢复到谁、会装哪些插件、放回哪些东西"算清楚（不写盘）。"""
    problems: list[str] = []
    mode = "new" if new_lane else "existing"
    dst = new_lane or target
    if not dst:
        problems.append("给一个已存在的目标 lane，或用 --new-lane 新建")
    version = (version or "").strip()
    if mode == "new":
        if not new_lane:
            problems.append("新 lane 名不能为空")
        elif not LANE_NAME_RE.match(new_lane):
            problems.append("lane 名只能用字母/数字/._-，且以字母或数字开头（最长 32）")
        elif new_lane in cfg.get("lanes", {}):
            problems.append(f"lane「{new_lane}」已经存在，要往它里面恢复就选目标 lane")
        if not version:
            version = (manifest.get("source") or {}).get("dshVersion") or ""
            if not version:
                problems.append("备份里没有 dsh 版本信息，请用 --version 指定要装的版本")
        if cfg.get("external") and not cfg.get("cmd"):
            problems.append("lanes.json 是 external 模式且没给 cmd，无法安装新版本")
    else:
        if not target:
            problems.append("没给目标 lane")
        elif target not in cfg.get("lanes", {}):
            problems.append(f"没有 lane「{target}」（要新建请用 --new-lane）")

    profs = manifest.get("profiles") or []
    if profiles is not None:
        wanted = set(profiles)
        profs = [p for p in profs if p.get("name") in wanted]
        missing = wanted - {p.get("name") for p in profs}
        if missing:
            problems.append(f"备份里没有这几个 profile：{'、'.join(sorted(missing))}")
    third = [{"profile": p.get("name"), "pkg": x.get("pkg"), "want": x.get("version")
              or x.get("spec"), "kind": x.get("kind"),
              "local": bool(x.get("localSpec"))}
             for p in profs for x in (p.get("plugins") or []) if not x.get("official")]
    contents = manifest.get("contents") or {}
    return {
        "ok": not problems, "problems": problems, "mode": mode, "target": dst,
        "version": version, "source": manifest.get("source") or {},
        "profiles": [p.get("name") for p in profs],
        "profileData": profs,
        "thirdParty": third,
        "sessions": contents.get("sessions") or {},
        "settings": (contents.get("settings") or {}).get("files") or [],
        "credentials": contents.get("credentials") or {"included": False},
        "localFiles": contents.get("localFiles") or [],
        "pluginData": contents.get("pluginData") or [],
        "useSessions": sessions, "useSettings": settings,
        "useCredentials": credentials, "usePluginData": plugin_data,
        "running": (lane_any_running(cfg, target)
                    if mode == "existing" and target in cfg.get("lanes", {}) else None),
    }


def _cfg_normalize_allow_build_key(key: str) -> str:
    """allowBuilds 的键只接受「包名」或「包名@精确版本」。

    pnpm 10.26+ 的 allowBuilds 把 `pkg@<version>` 里的 version 当 semver union 解析，
    写 `pkg@git+https://...` / `pkg@https://....git` 会直接 ERR_PNPM_INVALID_VERSION_UNION，
    而且这一个坏键会让**之后所有** `pnpm add` 都失败。git-hosted 依赖只能用裸包名放行，
    所以这里把这类非 semver 后缀剥掉，降级成裸包名。
    """
    key = key.strip().strip('"').strip("'").strip()
    if key.startswith("@"):
        m = re.match(r"^(@[^/@]+/[^@\s]+)(?:@(.+))?$", key)
    else:
        m = re.match(r"^([^@\s]+)(?:@(.+))?$", key)
    if not m:
        return key
    name, ver = m.group(1), (m.group(2) or "").strip()
    if ver and ("://" in ver or ver.endswith(".git") or ver.endswith(".tgz")
                or ver.startswith(("git", "github:", "gitlab:", "bitbucket:",
                                   "file:", "link:"))):
        return name
    return key


_WS_QUOTED_RE = re.compile(r"^(\s+)[\"']([^\"']*)[\"'](\s*:.*)$")
_WS_BARE_RE = re.compile(r"^(\s+)([^\"':#\s]+)(\s*:.*)$")


def _cfg_workspace_entry(line: str) -> tuple[str, str, str] | None:
    """解析 allowBuilds 下一行条目 → (缩进, 原始键, 含冒号的剩余部分)；非条目返回 None。"""
    m = _WS_QUOTED_RE.match(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _WS_BARE_RE.match(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _cfg_normalize_workspace_text(text: str) -> str:
    """把整份 pnpm-workspace.yaml 里 allowBuilds 的非法 git/URL 键就地规范化（返回新文本）。"""
    in_block = False
    out: list[str] = []
    for line in text.splitlines():
        if re.match(r"^allowBuilds\s*:", line):
            in_block = True
            out.append(line)
            continue
        if in_block:
            if line.strip() and not line[0].isspace() and re.match(r"^[A-Za-z]", line):
                in_block = False
            else:
                parsed = _cfg_workspace_entry(line)
                if parsed:
                    indent, raw, rest = parsed
                    norm = _cfg_normalize_allow_build_key(raw)
                    if norm != raw.strip().strip("'\""):
                        out.append(f"{indent}{norm}{rest}")
                        continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _cfg_merge_workspace_allow_builds(dst_text: str, src_text: str) -> str:
    """把备份 pnpm-workspace.yaml 里的 allowBuilds 条目合并进目标文本（同键不重复）。

    git-hosted 插件（如 dsh-providers-hub）要在 allowBuilds 里点名才允许跑 prepare 构建，
    否则 pnpm 直接 ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED。这条必须在**装插件之前**就位，
    而模板新建的 workspace 文件里没有它。键会先经 _cfg_normalize_allow_build_key 规范化。
    """
    if "allowBuilds" not in src_text:
        return dst_text
    block: list[str] = []
    in_block = False
    for line in src_text.splitlines():
        if re.match(r"^allowBuilds\s*:", line):
            in_block = True
            continue
        if in_block:
            # 顶层新键（列 0 起字母）出现＝块结束
            if line.strip() and not line[0].isspace() and re.match(r"^[A-Za-z]", line):
                break
            block.append(line)
    # 规范化后的 (键, 值) —— 去重，保留第一个
    norm: dict[str, str] = {}
    for line in block:
        parsed = _cfg_workspace_entry(line)
        if not parsed:
            continue
        _indent, raw_key, rest = parsed
        key = _cfg_normalize_allow_build_key(raw_key)
        val = rest.split(":", 1)[1].strip() if ":" in rest else "true"
        if key not in norm:
            norm[key] = val or "true"
    if not norm:
        return dst_text

    # 目标里已有的键（同样规范化后比较）
    have: set[str] = set()
    for line in dst_text.splitlines():
        parsed = _cfg_workspace_entry(line)
        if parsed:
            have.add(_cfg_normalize_allow_build_key(parsed[1]))
    missing = [(k, v) for k, v in norm.items() if k not in have]
    if not missing:
        return dst_text
    added = [f"  {k}: {v}" for k, v in missing]

    if "allowBuilds" not in dst_text:
        return dst_text.rstrip("\n") + "\nallowBuilds:\n" + "\n".join(added) + "\n"
    out: list[str] = []
    inserted = False
    for line in dst_text.splitlines():
        out.append(line)
        if re.match(r"^allowBuilds\s*:", line) and not inserted:
            out.extend(added)
            inserted = True
    return "\n".join(out) + ("\n" if dst_text.endswith("\n") else "")


def cfg_restore(cfg: dict, zip_path: Path, target: str = "", new_lane: str = "",
                version: str = "", profiles: list[str] | None = None,
                use_sessions: bool = True, use_settings: bool = True,
                use_credentials: bool = True, use_plugin_data: bool = True,
                stop: bool = True, timeout: int = 1200) -> dict:
    """把配置备份恢复进目标 lane（不存在则按 --new-lane 先装一个 dsh）。"""
    zip_path = Path(zip_path)
    manifest = cfg_read_manifest(zip_path)
    plan = cfg_restore_plan(cfg, manifest, target=target, new_lane=new_lane,
                            version=version, profiles=profiles,
                            sessions=use_sessions, settings=use_settings,
                            credentials=use_credentials, plugin_data=use_plugin_data)
    if not plan["ok"]:
        raise RuntimeError("；".join(plan["problems"]))
    lane = plan["target"]

    # ── 1) 新建 lane：先装 dsh（复用 create 的完整安装流程）
    if plan["mode"] == "new":
        ok(f"① 新建 lane「{lane}」并安装 dsh {plan['version']} …")
        args = argparse.Namespace(lane=lane, version=plan["version"], registry=None,
                                  force=False, follow=False)
        rc = cmd_create(cfg, args)
        if rc != 0:
            raise RuntimeError(f"dsh 安装失败（退出码 {rc}），恢复中止")
        cfg = load_config()

    # ── 2) 运行中的 lane 先停（插件安装 / 会话覆盖都要求静态）
    rt = lane_any_running(cfg, lane)
    if rt:
        if not stop:
            raise RuntimeError(f"lane「{lane}」正在运行（PID {rt.get('pid')}）：先停掉它，或允许自动停")
        warn(f"  lane「{lane}」正在运行（PID {rt.get('pid')}），先停掉 …")
        for prof in lane_profiles_view(cfg, lane):
            _stop_pair(cfg, lane, prof)

    home = lane_home(cfg, lane)
    home.mkdir(parents=True, exist_ok=True)
    hashes = manifest.get("hashes") or {}
    contents = manifest.get("contents") or {}
    profs = plan["profileData"]
    report = {"installed": [], "failed": [], "skipped": [], "lane": lane,
              "new": plan["mode"] == "new", "restored": []}

    with zipfile.ZipFile(zip_path) as zf:
        # ── 3) 先还原 file:/link: 本地插件本体，得到 pkg → 新 spec 映射
        spec_map: dict[str, str] = {}
        restored_local: list[dict] = []
        for item in contents.get("localFiles") or []:
            pkg = item.get("pkg")
            prefix = item.get("zipPath")
            if item.get("insideHome") and item.get("relPath"):
                dst_path = home / item["relPath"]
            else:
                key = item.get("key") or "item"
                if item.get("isDir"):
                    dst_path = home / "local-plugins" / "from-backup" / key
                else:
                    dst_path = home / "local-plugins" / "from-backup" / key / Path(
                        item.get("sourcePath")).name
            if item.get("isDir"):
                st = _zip_extract_tree(zf, prefix, dst_path, hashes)
            else:
                _zip_extract_file(zf, prefix, dst_path, hashes)
                st = {"files": 1, "bytes": dst_path.stat().st_size}
            new_spec = "file:" + str(dst_path).replace("\\", "/")
            if pkg:
                spec_map[pkg] = new_spec
            restored_local.append({"pkg": pkg, "dest": str(dst_path), "files": st["files"]})
            info(f"  本地插件 {pkg}: {st['files']} 个文件 → {dst_path}")
        if restored_local:
            report["restored"].append(f"本地插件 {len(restored_local)} 个")

        # ── 4) 逐 profile 建环境、装插件、放回补丁层与清单
        for pdata in profs:
            pname = pdata.get("name")
            pdir = profile_dir(home, pname)
            is_new_profile = not pdir.is_dir()
            if is_new_profile:
                info(f"② profile「{pname}」不存在，先建干净环境")
                init_profile_dir(pdir, SHIPPED_TEMPLATES["web"])
            files = pdata.get("files") or {}

            # 4.0 先把 pnpm-workspace.yaml 的 allowBuilds 合并进去（git-hosted 插件
            # 要靠它允许 prepare 构建；必须在 plugin add 之前就位）
            if "pnpm-workspace.yaml" in files:
                src_ws = zf.read(files["pnpm-workspace.yaml"]).decode("utf-8")
                dst_ws = pdir / "pnpm-workspace.yaml"
                if dst_ws.is_file():
                    merged = _cfg_merge_workspace_allow_builds(
                        dst_ws.read_text(encoding="utf-8"), src_ws)
                    if merged != dst_ws.read_text(encoding="utf-8"):
                        dst_ws.write_text(merged, encoding="utf-8")
                else:
                    _zip_extract_file(zf, files["pnpm-workspace.yaml"], dst_ws, hashes)

            # 4.1 装第三方插件（官方组合包是宿主自带的，跳过）
            plugins = pdata.get("plugins") or []
            for plug in plugins:
                pkg = plug.get("pkg")
                if plug.get("official"):
                    report["skipped"].append((f"{pname}/{pkg}", "宿主自带"))
                    continue
                src_spec = plug.get("spec") or ""
                is_local = str(src_spec).lower().startswith(("file:", "link:"))
                is_nonreg = (not is_local) and bool(NON_REGISTRY_SPEC_RE.match(src_spec))
                if is_local:
                    # 本地 file:/link: 插件：已在第 3 步还原到目标 lane，直接按本地路径装
                    want = spec_map.get(pkg)
                    if not want:
                        report["failed"].append((f"{pname}/{pkg}",
                                                 f"本地插件未包含在备份中：{src_spec}"))
                        continue
                    ok(f"  {pname}: 还原本地插件 {pkg}（{want}）")
                    res = run_plugin_cmd(cfg, lane, ["add", want, "--save-exact"],
                                         label="cfg-restore", profile=pname, timeout=timeout)
                    mode = "local"
                else:
                    if is_nonreg:
                        spec = src_spec
                        what = src_spec
                    else:
                        want = plug.get("version") or src_spec
                        spec = f"{pkg}@{want}"
                        what = want
                    ok(f"  {pname}: 安装 {pkg} @ {what}")
                    # registry 包先离线（本地 store 有同版本就零网络），缺了再联网；
                    # git 等非 registry 包源码仍要联网，但依赖优先走本地缓存。
                    res = plugin_add_smart(cfg, lane, pname, spec,
                                           label="cfg-restore", timeout=timeout,
                                           non_registry=is_nonreg)
                    mode = res.get("mode")
                if res.get("ok"):
                    report["installed"].append(f"{pname}/{pkg}")
                    tag = {"offline": "离线复用本地缓存", "online": "联网下载",
                           "local": "本地插件还原"}.get(mode, "")
                    info(f"    ✓ {pkg}" + (f"（{tag}）" if tag else ""))
                else:
                    tail = "\n".join(res.get("tail") or [])[-400:]
                    why = res.get("error") or tail or f"rc={res.get('rc')}"
                    report["failed"].append((f"{pname}/{pkg}", why))
                    warn(f"    ✗ {pkg} 安装失败（见下，可稍后用「插件…」补装）")

            # 4.2 补丁层：剥掉 webserver 端口块；备份条目合并进目标（同名条目不覆盖目标）
            if "cordis.patch.yml" in files:
                arc = files["cordis.patch.yml"]
                text = zf.read(arc).decode("utf-8")
                text = remove_patch_row_entries(text, ["webserver"])
                dst_patch = pdir / "cordis.patch.yml"
                if dst_patch.is_file():
                    base = dst_patch.read_text(encoding="utf-8")
                    blocks = [b for _id, b in _patch_entry_blocks(text) if _id]
                    ok_m, why, merged, added = merge_patch_blocks(base, blocks)
                    if ok_m:
                        dst_patch.write_text(merged, encoding="utf-8")
                        if added:
                            info(f"  {pname}: 补丁层合并 {len(added)} 个条目")
                else:
                    dst_patch.write_text(text, encoding="utf-8")
                    report["restored"].append(f"{pname} 补丁层")

            # 4.3 bundles 登记补全（plugin add 偶尔不登记，照 transfer 的做法兜底）
            bundles = [b for b in (pdata.get("bundles") or []) if b]
            if bundles:
                _merge_profile_manifest(pdir, {}, bundles)

            # 4.4 compatibility.json：目标没有才放回（workspace 已在 4.0 处理）
            if "compatibility.json" in files and not (pdir / "compatibility.json").is_file():
                _zip_extract_file(zf, files["compatibility.json"],
                                  pdir / "compatibility.json", hashes)

        # ── 5) 会话（先另存现状，再覆盖，最后校验）
        sess = contents.get("sessions") or {}
        if use_sessions and sess.get("included") and sess.get("files"):
            sdir = home / "sessions"
            if sdir.exists():
                snap = sessions_snapshot(cfg, lane, "before-cfg-restore")
                info(f"③ 当前会话已另存为快照 {snap['name']}")
                shutil.rmtree(sdir, ignore_errors=True)
            st = _zip_extract_tree(zf, "sessions", sdir, hashes)
            bad = _cfg_verify_tree(zf, "sessions", sdir, hashes)
            if bad:
                raise RuntimeError(f"会话恢复后校验失败：{len(bad)} 个文件不符，例如 {bad[:2]}")
            info(f"  会话恢复 {st['files']} 个文件")
            report["restored"].append(f"会话 {st['files']} 个")

        att = contents.get("attachments") or {}
        if use_sessions and att.get("included") and att.get("files"):
            st = _zip_extract_tree(zf, "attachments", home / "attachments", hashes,
                                   overwrite=False)
            info(f"  附件恢复 {st['files']} 个（跳过已存在 {st['skipped']} 个）")
            report["restored"].append(f"附件 {st['files']} 个")

        # ── 6) 设置文件（已存在先留 .bak）
        if use_settings:
            n = 0
            for item in (contents.get("settings") or {}).get("files") or []:
                arc = item.get("zipPath")
                dst = home / item.get("name")
                if dst.is_file():
                    _backup_file(dst)
                _zip_extract_file(zf, arc, dst, hashes)
                n += 1
            if n:
                info(f"④ 设置文件恢复 {n} 个")
                report["restored"].append(f"设置 {n} 个")

        # ── 7) 凭据（只合并 refs 条目，不冲掉目标已有的浏览器会话等）
        cred = contents.get("credentials") or {}
        if use_credentials and cred.get("included"):
            arc = cred.get("zipPath")
            text = zf.read(arc).decode("utf-8")
            refs, _s, _e = _refs_block(text.splitlines())
            if isinstance(refs, dict) and refs:
                touched = merge_credential_refs(home / CRED_FILENAME, refs)
                info(f"⑤ 凭据合并 {len(touched)} 条（已存在且相同的不动）")
                report["restored"].append(f"凭据 {len(touched)} 条")
            else:
                warn("  备份里的凭据文件没有解析出 refs 条目，已跳过")

        # ── 8) 插件数据目录（合并进 HOME，同名文件不覆盖）
        if use_plugin_data:
            for item in contents.get("pluginData") or []:
                prefix = item.get("zipPrefix")
                dst = home / item.get("name")
                st = _zip_extract_tree(zf, prefix, dst, hashes, overwrite=False)
                info(f"  数据目录 {item.get('name')}: {st['files']} 个文件")
                report["restored"].append(f"{item.get('name')} {st['files']} 个")

    # ── 9) 汇总
    ok(f"配置恢复完成：lane「{lane}」")
    for line in report["installed"]:
        info(col(f"  ✓ 装回 {line}", C.GREEN))
    for who, why in report["failed"]:
        warn(f"  ✗ {who}: {why}")
    if report["failed"]:
        warn("  有插件没装回来：打开 lane 后到「插件…」里按包名补装，或重跑一次本恢复")
    info(col(f"  现在可以：py {ME_NAME} open {lane}", C.CYAN))
    return report


def _cfg_verify_tree(zf: zipfile.ZipFile, prefix: str, dest_root: Path,
                     hashes: dict[str, str]) -> list[str]:
    """解压后按 manifest 指纹逐文件复核（只校验被记录过指纹的成员）。"""
    bad: list[str] = []
    prefix = prefix.strip("/") + "/"
    for arc in zf.namelist():
        if not arc.startswith(prefix) or arc not in hashes:
            continue
        rel = arc[len(prefix):]
        on_disk = Path(dest_root) / rel
        if not on_disk.is_file():
            bad.append(f"{rel}（缺失）")
            continue
        if hashlib.sha256(on_disk.read_bytes()).hexdigest() != hashes[arc]:
            bad.append(rel)
    return bad


# ---------- 备份包内插件的增删 ----------

def _cfg_rewrite_zip(zip_path: Path, rewrite: dict[str, bytes]) -> None:
    """重写 zip：rewrite 里的成员用新内容替换，其余成员原样拷贝（保留压缩方式）。"""
    tmp = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(zip_path, "r") as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        for item in zin.infolist():
            if item.filename in rewrite:
                continue
            zout.writestr(item, zin.read(item.filename))
        for arc, data in rewrite.items():
            zinfo = zipfile.ZipInfo(arc)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zinfo, data)
    os.replace(tmp, zip_path)


def cfg_plugin_remove(zip_path: Path, pkgs: list[str], profile: str = "") -> dict:
    """从备份包里删除插件：manifest + 每个 profile 的 package.json / 补丁层条目。"""
    zip_path = Path(zip_path)
    manifest = cfg_read_manifest(zip_path)
    targets = set(pkgs)
    rewrite: dict[str, bytes] = {}
    removed: list[str] = []
    for pdata in manifest.get("profiles") or []:
        if profile and pdata.get("name") != profile:
            continue
        plugins = pdata.get("plugins") or []
        rows_to_drop: set[str] = set()
        drop = [x for x in plugins if x.get("pkg") in targets]
        if not drop:
            continue
        for x in drop:
            rows_to_drop.update(x.get("rows") or [])
            rows_to_drop.update(x.get("disabledRows") or [])
        kept = [x for x in plugins if x.get("pkg") not in targets]
        pdata["plugins"] = kept
        pdata["bundles"] = [b for b in (pdata.get("bundles") or []) if b not in targets]
        removed.extend(f"{pdata.get('name')}/{x.get('pkg')}" for x in drop)
        files = pdata.get("files") or {}
        pkg_arc = files.get("package.json")
        if pkg_arc:
            with zipfile.ZipFile(zip_path) as zf:
                pkg_data = json.loads(zf.read(pkg_arc).decode("utf-8"))
            deps = pkg_data.get("dependencies")
            if isinstance(deps, dict):
                for t in targets:
                    deps.pop(t, None)
            dsh = pkg_data.get("dsh")
            if isinstance(dsh, dict) and isinstance(dsh.get("profile"), dict):
                bundles = dsh["profile"].get("bundles")
                if isinstance(bundles, list):
                    dsh["profile"]["bundles"] = [b for b in bundles if b not in targets]
            rewrite[pkg_arc] = (json.dumps(pkg_data, ensure_ascii=False, indent=2)
                                + "\n").encode("utf-8")
        patch_arc = files.get("cordis.patch.yml")
        if patch_arc and rows_to_drop:
            with zipfile.ZipFile(zip_path) as zf:
                text = zf.read(patch_arc).decode("utf-8")
            rewrite[patch_arc] = remove_patch_row_entries(
                text, sorted(rows_to_drop)).encode("utf-8")
    if not removed:
        raise ValueError("备份包里没有匹配的插件（可先用 cfg-show 看包内清单）")
    rewrite["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    _cfg_rewrite_zip(zip_path, rewrite)
    return {"removed": removed}


def _cfg_parse_add_spec(text: str) -> tuple[str, str]:
    """把 pkg@spec 拆成 (pkg, spec)。scoped 包 @scope/name@spec 按最后一个 @ 拆。"""
    text = text.strip()
    if text.startswith("@"):
        pkg, sep, spec = text.rpartition("@")
        return (pkg, spec) if sep else (text, "")
    pkg, sep, spec = text.partition("@")
    return pkg, (spec if sep else "")


def cfg_plugin_add(zip_path: Path, profile_name: str, specs: list[str],
                   src_cfg: dict | None = None, src_lane: str = "",
                   src_profile: str = "") -> dict:
    """往备份包里加插件。

    · 只给 pkg@spec：按该 spec 登记，恢复时联网拉取（没有开关状态，装完默认启用）。
    · 给了 --from lane/profile：从本机那条 lane 采到实际版本、行 id 与补丁条目
      （file: 本地依赖还会一并收进备份）。
    profile 不存在时自动在备份里新建一份。
    """
    zip_path = Path(zip_path)
    manifest = cfg_read_manifest(zip_path)
    profs = manifest.get("profiles")
    if not isinstance(profs, list):
        profs = []
        manifest["profiles"] = profs
    pdata = next((p for p in profs if p.get("name") == profile_name), None)
    if pdata is None:
        pdata = {"name": profile_name, "bundles": [], "files": {}, "plugins": []}
        profs.append(pdata)
    pdata.setdefault("plugins", [])
    pdata.setdefault("bundles", [])
    pdata.setdefault("files", {})
    existing = {x.get("pkg") for x in pdata["plugins"]}

    added: list[str] = []
    extra_local = []
    for raw in specs:
        pkg, spec = _cfg_parse_add_spec(raw)
        if not pkg or pkg == "desktop":
            continue
        entry = {"pkg": pkg, "spec": spec or "latest", "version": "",
                 "inBundles": True, "official": pkg.startswith("@deepseek-ai/"),
                 "kind": "bundle", "rows": [], "disabledRows": [],
                 "localSpec": (spec if str(spec).lower().startswith(("file:", "link:")) else "")}
        # 从本机已有 lane 采集：实际版本 + 行 id + 补丁条目 + 本地依赖
        src_blocks_text: list[str] = []
        if src_cfg and src_lane and src_lane in src_cfg.get("lanes", {}):
            src_prof = src_profile or DEFAULT_PROFILE
            for found in profile_plugins(src_cfg, src_lane, src_prof, include_official=True):
                if found.get("pkg") != pkg:
                    continue
                entry.update({
                    "spec": found.get("spec") or spec or entry["spec"],
                    "version": found.get("version") or "",
                    "kind": found.get("kind") or "bundle",
                    "rows": found.get("rows") or [],
                    "disabledRows": found.get("disabled_rows") or [],
                    "localSpec": found.get("spec") if str(
                        found.get("spec") or "").lower().startswith(("file:", "link:")) else "",
                })
                sp = profile_dir(lane_home(src_cfg, src_lane), src_prof) / "cordis.patch.yml"
                if sp.is_file():
                    blocks = patch_blocks_for_ids(sp.read_text(encoding="utf-8"),
                                                  set(found.get("rows") or []))
                    src_blocks_text = [ln for block in blocks for ln in block]
                if entry["localSpec"]:
                    extra_local.extend(cfg_collect_local_specs(
                        lane_home(src_cfg, src_lane), [src_prof]))
                break
        if pkg in existing:
            # 已存在：更新版本/规格，不重复加
            for i, x in enumerate(pdata["plugins"]):
                if x.get("pkg") == pkg:
                    pdata["plugins"][i] = entry
        else:
            pdata["plugins"].append(entry)
            existing.add(pkg)
        if entry["inBundles"] and pkg not in pdata["bundles"]:
            pdata["bundles"].append(pkg)
        added.append(pkg)

    if not added:
        raise ValueError("没有可加入的插件")

    rewrite: dict[str, bytes] = {}
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        # package.json：登记 deps + bundles
        arc = f"profiles/{profile_name}/package.json"
        if arc in names:
            pkg_data = json.loads(zf.read(arc).decode("utf-8"))
        else:
            pkg_data = {"name": f"dsh-profile-{profile_name}", "private": True,
                        "dependencies": {}, "dsh": {"profile": {"bundles": []}}}
        deps = pkg_data.setdefault("dependencies", {})
        for entry in pdata["plugins"]:
            # 包内清单就是权威：本地依赖写新 file 路径，其次精确版本，最后原始 spec
            deps[entry["pkg"]] = entry["localSpec"] or entry["version"] or entry["spec"]
        dsh = pkg_data.setdefault("dsh", {}).setdefault("profile", {})
        dsh["bundles"] = pdata["bundles"]
        rewrite[arc] = (json.dumps(pkg_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        pdata["files"]["package.json"] = arc
        # 补丁层：追加采集到的条目
        patch_arc = f"profiles/{profile_name}/cordis.patch.yml"
        base = zf.read(patch_arc).decode("utf-8") if patch_arc in names else ""
        if src_blocks_text:
            ok_d, why, new_text = append_patch_entries(base, src_blocks_text)
            if ok_d:
                rewrite[patch_arc] = new_text.encode("utf-8")
                pdata["files"]["cordis.patch.yml"] = patch_arc

    # 本地依赖：去重后收进 zip
    contents = manifest.setdefault("contents", {})
    local_items = contents.setdefault("localFiles", [])
    for item in extra_local:
        if any(x.get("sourcePath") == item["sourcePath"] for x in local_items):
            continue
        src = Path(item["sourcePath"])
        if not src.exists():
            continue
        key = item["key"] = f"{len(local_items):02d}-{re.sub(r'[^A-Za-z0-9._-]+', '-', src.name).strip('-')[:40]}"
        prefix = f"local/{key}"
        item["zipPath"] = prefix if item["isDir"] else f"{prefix}/{src.name}"
        item["bytes"] = dir_size(src) if item["isDir"] else src.stat().st_size
        item["files"] = 0
        local_items.append(item)
    hashes = manifest.setdefault("hashes", {})
    with zipfile.ZipFile(zip_path, "a") as zf:
        for item in extra_local:
            if not any(h.startswith(f"local/{item['key']}") for h in hashes):
                src = Path(item["sourcePath"])
                if item["isDir"]:
                    st = _zip_add_tree(zf, src, f"local/{item['key']}", False, hashes)
                else:
                    _zip_add_bytes(zf, item["zipPath"], src.read_bytes(), hashes)
    rewrite["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    _cfg_rewrite_zip(zip_path, rewrite)
    return {"added": added}


# ---------- 配置备份 CLI ----------

def cmd_cfg_backup(cfg: dict, args) -> int:
    """把一条 lane 的插件清单 + 会话 + 设置打成一个轻量 zip（不复制插件本体/安装树）。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    plan = cfg_backup_plan(
        cfg, lane,
        sessions=not args.no_sessions,
        attachments=not args.no_attachments,
        settings=not args.no_settings,
        credentials=bool(args.credentials),
        plugin_data=args.data or [],
        profile_names=args.profiles,
    )
    if not plan["ok"]:
        fail(plan["why"])
        return 1
    if args.data:
        avail = {d["name"] for d in plan["dataDirs"]}
        bad = [d for d in args.data if d not in avail]
        if bad:
            fail(f"这几个数据目录不存在：{'、'.join(bad)}")
            return 1
    ok(f"配置备份：lane「{lane}」（dsh {plan['dshVersion']}）")
    for p in plan["profiles"]:
        info(f"  profile「{p['name']}」: {p['thirdParty']} 个第三方插件"
             + (f"（缺清单文件 {p['missingFiles']}）" if p["missingFiles"] else ""))
    if plan["sessions"]["files"]:
        info(f"  会话 {plan['sessions']['files']} 个文件 / {human_size(plan['sessions']['bytes'])}")
    if plan["attachments"]["files"]:
        info(f"  附件 {plan['attachments']['files']} 个 / "
             f"{human_size(plan['attachments']['bytes'])}")
    info(f"  设置文件 {len(plan['settingsFiles'])} 个")
    if plan["credentials"]:
        info(col(f"  凭据 {len(plan['credentials'])} 条（敏感，已按要求打入）", C.YELLOW))
    if plan["localFiles"]:
        info(f"  本地插件 {len(plan['localFiles'])} 个 / "
             f"{human_size(sum(x['bytes'] for x in plan['localFiles']))}")
    if plan["dataPicked"]:
        info(f"  插件数据目录 {len(plan['dataPicked'])} 个 / "
             f"{human_size(sum(x['bytes'] for x in plan['dataPicked']))}")
    info(col(f"  合计约 {human_size(plan['totalBytes'])}（不含 profiles 下的插件本体）", C.GRAY))
    if not args.yes and sys.stdin and getattr(sys.stdin, "isatty", lambda: False)():
        if input("开始打包？(回车继续 / n 取消)：").strip().lower() in ("n", "q"):
            warn("已取消")
            return 1
    manifest = cfg_create_backup(
        cfg, lane, label=getattr(args, "name", "") or "",
        sessions=not args.no_sessions,
        attachments=not args.no_attachments,
        settings=not args.no_settings,
        credentials=bool(args.credentials),
        plugin_data=args.data or [],
        profile_names=args.profiles,
    )
    ok(f"备份完成：{manifest['zip']}")
    info(col("  恢复到新版本：py " + ME_NAME + " cfg-restore \"" + manifest["name"]
             + "\" --new-lane 新lane名 --version 版本", C.CYAN))
    return 0


def cmd_cfg_list(cfg: dict, args) -> int:
    items = cfg_list_backups(cfg)
    if not items:
        warn(f"还没有配置备份（{cfg_backup_root(cfg)}）")
        return 0
    info(f"配置备份（{cfg_backup_root(cfg)}）：")
    for item in items:
        if not item["ok"]:
            info(col(f"  [损坏] {item['name']}  {human_size(item['bytes'])}  {item['error']}",
                     C.RED))
            continue
        line = (f"  {item['name']}  {human_size(item['bytes'])}"
                f"  来源 {item['sourceLane']}@{item['dshVersion']}"
                f"  profile {','.join(item['profiles'])}"
                f"  插件 {item['thirdParty']}  会话 {item['sessions']}")
        if item["credentials"]:
            line += "  含凭据"
        info(line)
    return 0


def _cfg_print_show(manifest: dict, zip_path: Path) -> None:
    s = cfg_summary(manifest)
    src = s["source"]
    ok(f"配置备份：{zip_path.name}")
    info(f"  打包时间  : {s['created']}")
    info(f"  来源 lane : {src.get('lane', '?')}（dsh {src.get('dshVersion', '?')}）")
    info(f"  来源 HOME : {src.get('home', '?')}")
    for p in s["profiles"]:
        third = p["thirdParty"]
        info(f"  profile「{p['name']}」: {len(third)} 个第三方插件"
             + (f"，官方组合包 {p['officialCount']} 个" if p["officialCount"] else ""))
        for x in third:
            state = "关闭" if x.get("disabledRows") else "启用"
            info(col(f"      {x.get('pkg')}  {x.get('version') or x.get('spec') or '?'}  "
                     f"[{x.get('kind')}]  {state}", C.GRAY))
    sess = s["sessions"]
    if sess.get("included"):
        info(f"  会话      : {sess.get('files', 0)} 个文件 / {human_size(sess.get('bytes', 0))}")
    att = s["attachments"]
    if att.get("included"):
        info(f"  附件      : {att.get('files', 0)} 个 / {human_size(att.get('bytes', 0))}")
    if s["settings"].get("included"):
        names = [f["name"] for f in s["settings"].get("files") or []]
        info(f"  设置文件  : {len(names)} 个 —— {', '.join(names)}")
    if s["credentials"].get("included"):
        info(col(f"  凭据      : {len(s['credentials'].get('entries') or [])} 条"
                 f"（{', '.join(s['credentials'].get('entries') or [])}）—— 敏感", C.YELLOW))
    else:
        info("  凭据      : 不含")
    if s["localFiles"]:
        info(f"  本地插件  : {len(s['localFiles'])} 个")
        for x in s["localFiles"]:
            info(col(f"      {x.get('pkg')}  ← {x.get('sourcePath')}", C.GRAY))
    if s["pluginData"]:
        info(f"  数据目录  : {', '.join(x['name'] for x in s['pluginData'])}")


def cmd_cfg_show(cfg: dict, args) -> int:
    try:
        zip_path = cfg_resolve_backup(cfg, args.backup)
        manifest = cfg_read_manifest(zip_path)
    except (OSError, ValueError, FileNotFoundError) as exc:
        fail(str(exc))
        return 1
    _cfg_print_show(manifest, zip_path)
    return 0


def cmd_cfg_rm(cfg: dict, args) -> int:
    try:
        zip_path = cfg_resolve_backup(cfg, args.backup)
    except (OSError, FileNotFoundError) as exc:
        fail(str(exc))
        return 1
    if not args.yes and sys.stdin and getattr(sys.stdin, "isatty", lambda: False)():
        if input(f"删掉 {zip_path}？(y/N)：").strip().lower() not in ("y", "yes"):
            warn("已取消")
            return 1
    zip_path.unlink()
    ok(f"已删除 {zip_path}")
    return 0


def cmd_cfg_restore(cfg: dict, args) -> int:
    try:
        zip_path = cfg_resolve_backup(cfg, args.backup)
        manifest = cfg_read_manifest(zip_path)
    except (OSError, ValueError, FileNotFoundError) as exc:
        fail(str(exc))
        return 1
    profs = args.profiles
    plan = cfg_restore_plan(
        cfg, manifest, target=args.lane or "", new_lane=args.new_lane or "",
        version=args.version or "", profiles=profs,
        sessions=not args.no_sessions, settings=not args.no_settings,
        credentials=not args.no_credentials, plugin_data=not args.no_data,
    )
    if not plan["ok"]:
        fail("无法恢复：")
        for p in plan["problems"]:
            fail("  - " + p)
        return 1
    if plan["mode"] == "new":
        info(f"新建 lane「{plan['target']}」（dsh {plan['version']}），再从备份恢复")
    else:
        info(f"往已有 lane「{plan['target']}」里恢复")
    info(f"  profile : {', '.join(plan['profiles'])}")
    info(f"  要装回的第三方插件 {len(plan['thirdParty'])} 个")
    for x in plan["thirdParty"]:
        info(col(f"      {x['profile']}/{x['pkg']}  {x['want']}"
                 + ("（本地）" if x["local"] else ""), C.GRAY))
    if plan["sessions"].get("included") and not args.no_sessions:
        info(f"  会话 {plan['sessions'].get('files', 0)} 个（现有会话会先另存）")
    if plan["credentials"].get("included") and args.no_credentials:
        info(col("  凭据：备份里含凭据，但按参数跳过", C.YELLOW))
    if plan["running"]:
        if not args.yes:
            fail("目标 lane 正在运行：恢复会先停掉它。确认后重跑（或加 --yes）")
            return 1
    if not args.yes and sys.stdin and getattr(sys.stdin, "isatty", lambda: False)():
        if input("开始恢复？(回车继续 / n 取消)：").strip().lower() in ("n", "q"):
            warn("已取消")
            return 1
    cfg_restore(cfg, zip_path, target=args.lane or "", new_lane=args.new_lane or "",
                version=args.version or "", profiles=profs,
                use_sessions=not args.no_sessions,
                use_settings=not args.no_settings,
                use_credentials=not args.no_credentials,
                use_plugin_data=not args.no_data,
                stop=True)
    return 0


def cmd_cfg_plugins(cfg: dict, args) -> int:
    try:
        zip_path = cfg_resolve_backup(cfg, args.backup)
        manifest = cfg_read_manifest(zip_path)
    except (OSError, ValueError, FileNotFoundError) as exc:
        fail(str(exc))
        return 1
    for pdata in manifest.get("profiles") or []:
        if args.profile and pdata.get("name") != args.profile:
            continue
        info(f"profile「{pdata.get('name')}」：")
        for x in pdata.get("plugins") or []:
            tag = "官方" if x.get("official") else ("关闭" if x.get("disabledRows") else "启用")
            info(f"  {x.get('pkg')}  {x.get('version') or x.get('spec') or '?'}  [{tag}]")
    return 0


def cmd_cfg_plugin_add(cfg: dict, args) -> int:
    try:
        zip_path = cfg_resolve_backup(cfg, args.backup)
        src_cfg = load_config() if args.from_lane else None
        result = cfg_plugin_add(zip_path, args.profile, args.plugins,
                                src_cfg=src_cfg, src_lane=args.from_lane or "",
                                src_profile=args.from_profile or "")
    except (OSError, ValueError, FileNotFoundError) as exc:
        fail(str(exc))
        return 1
    ok(f"已加入备份 {len(result['added'])} 个插件：{', '.join(result['added'])}")
    if args.from_lane:
        info("版本、开关状态与补丁配置已从源 lane 采集；本地依赖已一并收入")
    else:
        info("恢复时按规格联网拉取（装完默认启用；要带开关状态就用 --from 从已有 lane 采集）")
    return 0


def cmd_cfg_plugin_rm(cfg: dict, args) -> int:
    try:
        zip_path = cfg_resolve_backup(cfg, args.backup)
        result = cfg_plugin_remove(zip_path, args.plugins, profile=args.profile or "")
    except (OSError, ValueError, FileNotFoundError) as exc:
        fail(str(exc))
        return 1
    ok(f"已从备份移除：{', '.join(result['removed'])}")
    return 0


def lane_delete_plan(cfg: dict, lane: str, sizes: bool = True) -> dict:
    """算清"删掉这条 lane"具体会动哪些文件，供 GUI / CLI 展示与确认。

    两条安全线：
    · 只删 lane 根目录（<root>/versions、<root>/homes）里的东西；
      接管型 lane 的安装树在 npm 全局目录、HOME 可能是 %USERPROFILE%\\.dsh —— 默认都不碰。
    · 多个 lane 共用同一棵安装树（同版本）时，安装树不删。

    sizes=False 时跳过目录体积统计（那是全盘遍历，几百毫秒起步）——
    GUI 先拿这份即时结果把对话框画出来，体积再在后台线程里补。
    """
    data = cfg["lanes"][lane]
    p = paths(cfg)
    root = Path(cfg["root"])
    home = Path(data.get("home") or (p["homes"] / lane))
    install = lane_install_dir(cfg, lane)

    def inside(target: Path) -> bool:
        try:
            Path(target).resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    # 副本的安装树是我们自己复制出来的（<root>/clones/<lane>），可以随 lane 一起删；
    # 接管型的安装树在 npm 全局目录 / DSH Desktop 之类的地方，默认不碰。
    owns_install = False
    if data.get("installDir"):
        try:
            Path(install).resolve().relative_to(p["clones"].resolve())
            owns_install = True
        except Exception:
            owns_install = False

    shared = [
        n
        for n, d in cfg.get("lanes", {}).items()
        if n != lane
        and isinstance(d, dict)
        and not d.get("installDir")
        and str(d.get("version")) == str(data.get("version"))
    ]
    return {
        "lane": lane,
        "data": data,
        "version": data.get("version"),
        "home": home,
        "install": install,
        "profiles": sorted(lane_profiles_view(cfg, lane)),
        "home_inside": inside(home),
        "install_inside": inside(install) and (not data.get("installDir") or owns_install),
        "adopted": bool(data.get("installDir")) and not owns_install,
        "owns_install": owns_install,
        "kind": data.get("kind") or ("adopted" if data.get("installDir") else "created"),
        "shared_with": [] if owns_install else shared,
        "logs": sorted(p["logs"].glob(f"{lane}__*.log")) + [p["logs"] / f"{lane}.log"],
        "runs": sorted(p["run"].glob(f"{lane}__*.json")) + [p["run"] / f"{lane}.json"],
        "home_size": dir_size(home) if (sizes and home.is_dir()) else None,
        "install_size": dir_size(install) if (sizes and install.is_dir()) else None,
    }


def cmd_delete(cfg: dict, args) -> int:
    """删掉一套 DSH：登记 + 运行态 + 安装树 + DSH_HOME（后两项按安全线决定）。"""
    lane = args.lane
    data = cfg.get("lanes", {}).get(lane)
    if not isinstance(data, dict):
        fail(f"没有 lane「{lane}」")
        return 1

    plan = lane_delete_plan(cfg, lane)
    rt = lane_any_running(cfg, lane)
    del_home = (not args.keep_home) and (plan["home_inside"] or args.home_too)
    del_install = (not args.keep_install) and plan["install_inside"] and not plan["shared_with"]

    if not args.yes:
        info("")
        info(col(f"── 删除 lane「{lane}」──", C.BOLD))
        info(f"      版本      : {plan['version']}")
        if plan["profiles"]:
            info(col(f"      profiles  : {'、'.join(plan['profiles'])}（随 HOME 一起消失）", C.GRAY))
        info("  将被删除：")
        info(f"    · 登记信息    lane「{lane}」")
        for run_file_path in plan["runs"]:
            if Path(run_file_path).is_file():
                info(f"    · 运行态      {run_file_path}")
        for log_path in plan["logs"]:
            if Path(log_path).is_file():
                info(f"    · 启动日志    {log_path}")
        if del_install:
            info(f"    · 安装树      {plan['install']}　({human_size(plan['install_size'])})")
            if plan["profiles"]:
                info(col(f"                  （含全部 profile 目录，插件组合无法找回）", C.GRAY))
        if del_home:
            info(f"    · DSH_HOME    {plan['home']}　({human_size(plan['home_size'])})")
            info(col("                  （会话记录、设置、凭据一并消失，无法恢复）", C.GRAY))
            info("  将被保留：")
            if not del_install:
                why = (
                    "接管型：删的是 npm 全局那份，本启动器不动它（要卸载用 npm uninstall -g）"
                    if plan["adopted"]
                    else f"其它 lane 还在用同一棵安装树：{'、'.join(plan['shared_with'])}"
                )
                info(f"    · 安装树      {plan['install']}")
                info(col(f"                  （{why}）", C.GRAY))
            if rt:
                if not args.stop:
                    info("")
                    warn(f"它现在正在运行（pid {rt.get('pid')}，端口 {rt.get('port')}）。")
                    warn(f"加 --stop 让它先停下来再删：delete {lane} --stop")
                    return 1
                info(col(f"      （会先停止 pid {rt.get('pid')}）", C.GRAY))
            info("")
            try:
                typed = input(f"  请输入「{lane}」以确认删除（其它任意输入＝取消）：").strip()
            except (EOFError, KeyboardInterrupt):
                typed = ""
            if typed != lane:
                warn("已取消，什么都没有删除。")
                return 1
    elif rt and not args.stop:
        # --yes（GUI / Web 界面）：那边的二次确认（手打 lane 名）已经在窗口里做过了，
        # 所以这里只补一条它做不了的检查 —— 正在运行又没让先停。
        fail(f"lane「{lane}」正在运行（pid {rt.get('pid')}）。先 stop，或加 --stop。")
        return 1

    if rt and args.stop:
        info(f"先停止 lane「{lane}」…")
        cmd_stop(cfg, argparse.Namespace(target=[lane]))
        cfg = load_config()

    info("")
    removed: list[str] = []
    if del_install and plan["install"].is_dir():
        try:
            shutil.rmtree(plan["install"])
            removed.append(f"安装树    {plan['install']}　({human_size(plan['install_size'])})")
        except Exception as exc:  # noqa: BLE001
            fail(f"删安装树失败（{plan['install']}）：{exc}")
    if del_home and plan["home"].is_dir():
        try:
            shutil.rmtree(plan["home"])
            removed.append(f"DSH_HOME  {plan['home']}　({human_size(plan['home_size'])})")
        except Exception as exc:  # noqa: BLE001
            fail(f"删 DSH_HOME 失败（{plan['home']}）：{exc}")
    for f in plan["runs"] + plan["logs"]:
        try:
            if Path(f).is_file():
                Path(f).unlink()
        except OSError:
            pass

    cfg = load_config()
    cfg.get("lanes", {}).pop(lane, None)
    save_config(cfg)
    clear_run(cfg, lane)

    info("")
    ok(f"已删除 lane「{lane}」（版本 {plan['version']}）")
    for item in removed:
        info(f"      · {item}")
    if not removed:
        info(col("      （只清了登记与运行态，没删任何目录）", C.GRAY))
    return 0


def lane_log_path(cfg: dict, lane: str, profile: str = DEFAULT_PROFILE) -> Path:
    """这个 profile 的启动日志。写入一律用新命名 <lane>__<profile>.log；
    读取时若新文件还没有、而 v0.2 的 <lane>.log 在（web profile），回落到旧文件。"""
    new = paths(cfg)["logs"] / f"{lane}__{profile}.log"
    if new.is_file() or profile != DEFAULT_PROFILE:
        return new
    legacy = paths(cfg)["logs"] / f"{lane}.log"
    return legacy if legacy.is_file() else new


def cmd_logs(cfg: dict, args) -> int:
    lane_part, sep, prof_part = args.lane.partition("/")
    profile = (prof_part.strip() if sep else "") or DEFAULT_PROFILE
    try:
        validate_profile_name(profile)
    except ValueError as exc:
        fail(str(exc))
        return 1
    log = lane_log_path(cfg, lane_part, profile)
    if not log.is_file():
        fail(f"没有日志：{log}")
        return 1
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-args.lines:]
    info(col(f"── {log}（最后 {len(tail)} 行）──", C.BOLD))
    for line in tail:
        info(line)
    return 0


# ======================= 入口 =======================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh_lanes",
        description="DSH 多版本启动器：创建 / 打开指定版本的 DeepSeek Harness（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  py dsh_lanes.py doctor\n"
            "  py dsh_lanes.py versions\n"
            "  py dsh_lanes.py create next 0.1.7-rc.2\n"
            "  py dsh_lanes.py open next                     # 默认 profile（web），端口自动挑\n"
            "  py dsh_lanes.py open next/test --port 3082    # 指定 profile 与端口\n"
            "  py dsh_lanes.py profile-add next clean --from web\n"
            "  py dsh_lanes.py copy-key global next --only DEEPSEEK_API_KEY\n"
            "  py dsh_lanes.py stop next                     # 停这条 lane 的全部 profile\n"
        ),
    )
    parser.add_argument("--root", help="覆盖 lane 根目录（也可写进 lanes.json）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="环境自检：node / npm / root 可写 / registry 可达")
    p_ver = sub.add_parser("versions", help="查询 npm 上可用的 dsh 版本与 dist-tag")
    p_ver.add_argument("--all", action="store_true", help="列出全部版本（默认最近 15 个）")
    p_ver.add_argument("--registry", metavar="auto|official|mirror|URL",
                       help="查哪个 npm 源（默认沿用 lanes.json；空/auto＝先官方再镜像）")

    p_create = sub.add_parser("create", help="安装指定版本并登记为一条 lane")
    p_create.add_argument("lane", help="lane 名，例如 stable / next / last")
    p_create.add_argument("version", help="版本号或 dist-tag，例如 0.1.7-rc.2 / next / latest")
    p_create.add_argument("--force", action="store_true", help="lane 已存在时覆盖其版本")
    p_create.add_argument("--no-follow", dest="follow", action="store_false",
                          help="安装时不实时跟随日志")
    p_create.add_argument("--registry", metavar="auto|official|mirror|URL",
                          help="从哪个 npm 源查版本并下载：auto（默认，先官方再镜像）/ "
                               "official / mirror / 一个地址。缺依赖会自动换另一个源重试")

    p_open = sub.add_parser("open", help="打开一个 profile（lane 名或 lane/profile）")
    p_open.add_argument("target", help="lane 名、lane/profile 或版本号")
    p_open.add_argument("--profile", help="要启动的 profile（默认 web；也可写 lane/profile）")
    p_open.add_argument("--port", type=int,
                        help="这次启动用的端口（默认实时挑空闲；偏好端口被占用也会自动改口）")
    p_open.add_argument("--cwd", help="覆盖工作区（影响 session 归属）")
    p_open.add_argument("--timeout", type=int, help="启动等待超时秒数")
    p_open.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_open.add_argument("--detach", action="store_true", help="启动后立即返回（后台运行）")
    p_open.add_argument(
        "--adopt-only",
        dest="adopt_only",
        action="store_true",
        help="端口已有实例时只接管、不打开浏览器",
    )

    sub.add_parser("list", help="列出全部 lane 及其 profile 运行状态")

    p_stop = sub.add_parser("stop", help="停止：缺省全部；lane＝它的全部 profile；lane/profile＝单个")
    p_stop.add_argument("target", nargs="*", help="lane 名或 lane/profile，缺省停止全部")

    p_logs = sub.add_parser("logs", help="查看某个 profile 的启动日志")
    p_logs.add_argument("lane", help="lane 名或 lane/profile")
    p_logs.add_argument("--lines", type=int, default=60, help="显示最后 N 行（默认 60）")

    sub.add_parser("instances", help="列出本机所有 DSH 实例（含不是本启动器启动的，如全局安装的 3080）")

    p_adopt = sub.add_parser(
        "adopt", help="把本机已有的安装（默认 npm 全局那份）注册成 lane，沿用它的 DSH_HOME"
    )
    p_adopt.add_argument("lane", help="lane 名，例如 global")
    p_adopt.add_argument("--install", help="指定安装树路径（默认取 npm 全局那份）")
    p_adopt.add_argument("--port", type=int, help="端口（默认 3080；被非 DSH 占用时自动顺延）")
    p_adopt.add_argument("--home", help="DSH_HOME（默认 %%USERPROFILE%%\\.dsh）")
    p_adopt.add_argument("--force", action="store_true", help="lane 已存在时覆盖")

    p_remove = sub.add_parser("remove", help="从登记表移除一条 lane（不动安装树与 DSH_HOME）")
    p_remove.add_argument("lane")
    p_remove.add_argument("--force", action="store_true", help="即使正在运行也注销登记")

    p_profiles = sub.add_parser(
        "profiles", help="列出一条 lane 的全部 profile（环境）与各自运行状态"
    )
    p_profiles.add_argument("lane")

    p_profile_add = sub.add_parser(
        "profile-add",
        help="给一条 lane 新建 profile：干净环境（默认）或 --from 复制现有 profile 的插件组合",
    )
    p_profile_add.add_argument("lane")
    p_profile_add.add_argument("name", help="新 profile 名（字母/数字开头，含 . _ -）")
    p_profile_add.add_argument("--from", dest="from_profile", metavar="现有profile",
                               help="整目录复制这个现有 profile（含已装插件与补丁层）")

    p_profile_rm = sub.add_parser(
        "profile-rm", help="移除一个 profile（删它的目录；默认 profile web 不可删）"
    )
    p_profile_rm.add_argument("lane")
    p_profile_rm.add_argument("name")
    p_profile_rm.add_argument("--yes", action="store_true", help="跳过确认（GUI / 脚本用）")

    p_keys = sub.add_parser(
        "keys", help="列出一条 lane 的凭据条目（.credentials.yaml refs 段，即各提供商的 key）"
    )
    p_keys.add_argument("lane")
    p_keys.add_argument("--show", action="store_true",
                        help="把明文密钥也打出来（只在本机看：终端历史、日志里都别留）")

    p_set_key = sub.add_parser(
        "set-key", help="改 / 加一条凭据（写后用 dsh 自己的解析器校验，不过自动回滚）"
    )
    p_set_key.add_argument("lane")
    p_set_key.add_argument("name", help="条目名，如 MY_API_KEY（字母/数字/_.-）")
    p_set_key.add_argument("--value", metavar="V", help="明文值（会进命令行历史，慎用）")
    p_set_key.add_argument("--from-env", metavar="ENV",
                           help="从环境变量取值（推荐：不在历史里留明文）")

    p_rm_key = sub.add_parser("rm-key", help="删掉一条 / 几条凭据条目")
    p_rm_key.add_argument("lane")
    p_rm_key.add_argument("names", nargs="+", help="条目名（可写多个，或逗号分隔）")

    p_provider_list = sub.add_parser(
        "provider-list", help="列出一个 profile 补丁层里手写的自定义服务商"
    )
    p_provider_list.add_argument("lane")
    p_provider_list.add_argument("--profile", help=f"环境名（缺省 {PROFILE_NAME}）")

    p_provider_add = sub.add_parser(
        "provider-add", help="写一个自定义服务商进补丁层（写 dsh「设置 → 模型」的同一处）"
    )
    p_provider_add.add_argument("lane")
    p_provider_add.add_argument("route", help="服务商短名，如 my-gateway（小写字母/数字/中划线）")
    p_provider_add.add_argument("--api", required=True,
                                help="协议：" + " / ".join(PROVIDER_APIS))
    p_provider_add.add_argument("--base-url", help="接口地址，如 https://api.example.com/v1")
    p_provider_add.add_argument("--display", help="在 dsh 里显示的名字")
    p_provider_add.add_argument("--key", help="API key（会写进凭据 refs，条目名 <ROUTE>_API_KEY）")
    p_provider_add.add_argument("--model", help="模型 id，逗号分隔（如 m-1,m-2）")
    p_provider_add.add_argument("--profile", help=f"环境名（缺省 {PROFILE_NAME}）")
    p_provider_add.add_argument("--overwrite", action="store_true",
                                help="覆盖已存在的同名 route（默认拒绝，怕冲掉调好的东西）")

    p_copy_key = sub.add_parser(
        "copy-key", help="在 lane 之间按条目复制 API key：--only 挑条目，缺省全部"
    )
    p_copy_key.add_argument("source", help="源 lane 名")
    p_copy_key.add_argument("dest", help="目标 lane 名")
    p_copy_key.add_argument("--only", metavar="条目,条目",
                            help="只复制这些条目（逗号分隔，如 DEEPSEEK_API_KEY）")

    p_delete = sub.add_parser(
        "delete", help="彻底删除一套 DSH：登记 + 运行态 + 安装树 + DSH_HOME"
    )
    p_delete.add_argument("lane", help="要删除的 lane 名")
    p_delete.add_argument("--stop", action="store_true", help="正在运行就先停止它再删")
    p_delete.add_argument("--keep-home", action="store_true", help="保留 DSH_HOME（只删安装树与登记）")
    p_delete.add_argument("--keep-install", action="store_true", help="保留安装树")
    p_delete.add_argument(
        "--home-too",
        action="store_true",
        help="连 lane 根目录之外的 DSH_HOME（如 %%USERPROFILE%%\\.dsh）一起删（危险）",
    )
    p_delete.add_argument("--yes", action="store_true", help="跳过交互确认（GUI / 脚本用）")

    p_clone = sub.add_parser(
        "clone",
        help="把一条 lane 连安装树带 DSH_HOME 完整复制成新 lane（升级/插件冲突测试用，原件不动）",
    )
    p_clone.add_argument("source", help="源 lane 名或版本号（建议填主要版本）")
    p_clone.add_argument("lane", help="新 lane 名，例如 test-next")
    p_clone.add_argument("--port", type=int, help="指定端口（默认自动挑空闲端口）")
    p_clone.add_argument(
        "--profiles", metavar="名,名",
        help="只复制这些 profile（逗号分隔，如 web,test）；缺省＝源 HOME 里全部 "
             "（没选的 profile 不进副本，装机体积也跟着小）")

    p_upgrade = sub.add_parser(
        "upgrade",
        help="把某条 lane 精确升到指定版本（装完核对 + 启动冒烟，起不来自动退回）",
    )
    p_upgrade.add_argument("lane")
    p_upgrade.add_argument("version", nargs="?", help="目标版本或 dist-tag（如 0.1.7-rc.2 / latest）")
    p_upgrade.add_argument("--rollback", action="store_true", help="退回上一次升级前的版本")
    p_upgrade.add_argument("--stop", action="store_true",
                           help="正在运行就先停掉（默认拒绝在运行时升级；升完还给你起回来）")
    p_upgrade.add_argument("--no-boot-check", dest="no_boot_check", action="store_true",
                           help="跳过「升完启动一次」的冒烟验证（默认会做，起不来就自动退回）")
    p_upgrade.add_argument("--dry-run", dest="dry_run", action="store_true",
                           help="只显示会做什么，不动任何东西")
    p_upgrade.add_argument("--registry", metavar="auto|official|mirror|URL",
                           help="从哪个 npm 源查版本并下载（默认沿用 lanes.json；"
                                "查和装一定用同一个源，缺依赖自动换另一个源重试）")

    p_verify = sub.add_parser(
        "verify", help="核对一条 lane 的隔离与内容（clone 之后、升级之后再各跑一次）"
    )
    p_verify.add_argument("lane")
    p_verify.add_argument("--profile", help="针对哪个 profile 体检（补丁层 / pnpm 账本；默认 web）")
    p_verify.add_argument(
        "--fix", action="store_true", help="按源 lane 重连宿主依赖农场（修复中断/不完整的复制）"
    )

    p_plugins = sub.add_parser(
        "plugins", help="列出某个 profile 的插件开关（插件 → 真实行 id → 关没关、谁能关它）"
    )
    p_plugins.add_argument("lane")
    p_plugins.add_argument("--profile", help="看哪个 profile（默认 web）")
    p_plugins.add_argument("--dump", action="store_true",
                           help="额外离线组合一次，核对「文件说的」与「组合结果」是否一致")

    p_transfer = sub.add_parser(
        "plugin-transfer",
        help="把插件搬到别的 profile / 别的版本（默认点名到同一个版本重装，也可离线直拷）",
    )
    p_transfer.add_argument("lane", help="源：lane 或 lane/profile（缺 profile 用 web）")
    p_transfer.add_argument("to", help="目标：lane 或 lane/profile（缺 profile 用 web）")
    p_transfer.add_argument("names", nargs="+",
                            help="要搬的插件包名（可多个；写 all ＝源里全部第三方插件）")
    p_transfer.add_argument("--mode", choices=list(PLUGIN_TRANSFER_MODES), default="install",
                            help="install＝点名到同一个版本重装（默认，pnpm 账本干净）；"
                                 "copy＝离线直拷（不走网络、不需要 node）")
    p_transfer.add_argument("--dry-run", dest="dry_run", action="store_true",
                            help="只打印计划与逐条动作，不做任何改动")
    p_transfer.add_argument("--timeout", type=int, default=1800, help="单个插件安装的超时秒数")

    p_plugin_off = sub.add_parser(
        "plugin-off", help="关掉一个插件（写它的真实行 id；运行中的话约 2 秒热生效，不用重启）"
    )
    p_plugin_off.add_argument("lane")
    p_plugin_off.add_argument("target", help="插件名 / 行 id / 名字的一部分")
    p_plugin_off.add_argument("--profile", help="哪个 profile（默认 web）")
    p_plugin_off.add_argument("--verify", action="store_true", help="写完额外离线组合一次核对")

    p_plugin_on = sub.add_parser(
        "plugin-on", help="打开一个插件（删掉禁用条目；--force 可强行压过组合包自己的禁用）"
    )
    p_plugin_on.add_argument("lane")
    p_plugin_on.add_argument("target", help="插件名 / 行 id / 名字的一部分")
    p_plugin_on.add_argument("--profile", help="哪个 profile（默认 web）")
    p_plugin_on.add_argument("--force", action="store_true",
                             help="没有禁用条目时也写一条 disabled: false 强行打开（可能有副作用）")
    p_plugin_on.add_argument("--verify", action="store_true", help="写完额外离线组合一次核对")

    p_plugin_add = sub.add_parser(
        "plugin-add", help="给某个 profile 加装一个插件（装 + 登记进 dsh.profile.bundles）"
    )
    p_plugin_add.add_argument("lane")
    p_plugin_add.add_argument("spec", nargs="+",
                              help="包名（可带版本，如 @scope/name@1.2.3；也支持 github:/file: 等 spec）")
    p_plugin_add.add_argument("--profile", help="哪个 profile（默认 web）")
    p_plugin_add.add_argument("--timeout", type=int, default=1800, help="安装超时秒数")

    p_plugin_rm = sub.add_parser(
        "plugin-rm", help="卸载某个 profile 里的一个插件（host 文件 + 清单登记一起清）"
    )
    p_plugin_rm.add_argument("lane")
    p_plugin_rm.add_argument("target", nargs="+", help="插件名 / 行 id / 名字的一部分")
    p_plugin_rm.add_argument("--profile", help="哪个 profile（默认 web）")
    p_plugin_rm.add_argument("--clean-rows", dest="clean_rows", action="store_true",
                             help="连补丁层里属于它的覆盖条目也删掉（默认保留）")
    p_plugin_rm.add_argument("--timeout", type=int, default=1200, help="卸载超时秒数")

    p_off = sub.add_parser(
        "plugins-off", help="一次关掉所有自己装的（第三方）插件；官方组合包不动，可 plugins-on 撤销"
    )
    p_off.add_argument("lane")
    p_off.add_argument("--profile", help="哪个 profile（默认 web）")
    p_off.add_argument("--keep", action="append", help="保留某个组合包（可多次，也可逗号分隔）")
    p_off.add_argument("--no-verify", dest="no_verify", action="store_true", help="跳过写入后的组合复核")
    p_off.add_argument(
        "--no-boot-check",
        dest="no_boot_check",
        action="store_true",
        help="跳过「禁用后启动一次」的冒烟验证（默认会做，起不来就自动撤销）",
    )

    p_on = sub.add_parser("plugins-on", help="撤销一键禁用（只删启动器写的那一段覆盖）")
    p_on.add_argument("lane")
    p_on.add_argument("--profile", help="哪个 profile（默认 web）")
    p_on.add_argument("--no-verify", dest="no_verify", action="store_true", help="跳过撤销后的组合复核")

    p_market = sub.add_parser(
        "market", help="插件市场（dshmarket）现状：装的是哪个版本、开还是关、registry 上有没有新版"
    )
    p_market.add_argument("lane")
    p_market.add_argument("--profile", help="看哪个 profile（默认 web）")
    p_market.add_argument("--offline", action="store_true", help="不查 registry（离线看本地状态）")

    p_market_off = sub.add_parser(
        "market-off", help="单独关掉插件市场（其它插件一个都不动；market-on 撤销）"
    )
    p_market_off.add_argument("lane")
    p_market_off.add_argument("--no-verify", dest="no_verify", action="store_true",
                              help="跳过写入后的组合复核")
    p_market_off.add_argument("--no-boot-check", dest="no_boot_check", action="store_true",
                              help="跳过「关掉后启动一次」的冒烟验证（默认会做，起不来就自动还原）")

    p_market_on = sub.add_parser("market-on", help="再打开插件市场（只删启动器写的那一小段开关）")
    p_market_on.add_argument("lane")
    p_market_on.add_argument("--no-verify", dest="no_verify", action="store_true",
                             help="跳过撤销后的组合复核")

    p_market_up = sub.add_parser(
        "market-update",
        help="更新插件市场（点名到版本装 + 启动冒烟，起不来自动回滚到更新前的快照）",
    )
    p_market_up.add_argument("lane")
    p_market_up.add_argument("--version", help="目标版本（默认取 registry 上的 latest）")
    p_market_up.add_argument("--anyway", action="store_true",
                             help="连 minimumReleaseAgeStrict（点名版本也要拦）都硬闯："
                                  "带 pnpm 的一次性绕过 --config.minimum-release-age=0")
    p_market_up.add_argument("--force", action="store_true", help="这条 lane 正在运行也强行更新")
    p_market_up.add_argument("--offline", action="store_true", help="不查 registry（必须配 --version）")
    p_market_up.add_argument("--no-verify", dest="no_verify", action="store_true",
                             help="跳过更新后的组合复核")
    p_market_up.add_argument("--no-boot-check", dest="no_boot_check", action="store_true",
                             help="跳过更新后的启动冒烟（默认会做，起不来就自动回滚）")

    p_market_rb = sub.add_parser("market-rollback", help="用启动器自动存的快照把插件市场退回上一版")
    p_market_rb.add_argument("lane")
    p_market_rb.add_argument("--name", help="指定快照名（默认用最新的那份）")

    p_chat_bk = sub.add_parser(
        "chat-backup", help="把这条 lane 的对话记录（sessions）整份备份，并逐文件算指纹"
    )
    p_chat_bk.add_argument("lane")
    p_chat_bk.add_argument("--name", help="给这份快照起个后缀名，例如 before-upgrade")

    p_chat_ls = sub.add_parser("chat-list", help="列出某条 lane 的对话备份")
    p_chat_ls.add_argument("lane")

    p_chat_rs = sub.add_parser(
        "chat-restore", help="把对话记录恢复到某个快照（恢复前会先校验快照、再另存现状）"
    )
    p_chat_rs.add_argument("lane")
    p_chat_rs.add_argument("name", nargs="?", help="快照名（缺省＝最新那份）")
    p_chat_rs.add_argument("--stop", action="store_true", help="正在运行就先替你停掉它")
    p_chat_rs.add_argument("--yes", action="store_true", help="跳过确认（GUI / 脚本用）")
    p_chat_rs.add_argument("--force", action="store_true", help="快照自身校验不过也照恢复")

    p_chat_rm = sub.add_parser("chat-rm", help="删掉一份对话备份")
    p_chat_rm.add_argument("lane")
    p_chat_rm.add_argument("name")
    p_chat_rm.add_argument("--yes", action="store_true", help="跳过确认（GUI / 脚本用）")

    # ── 配置备份（轻量：插件清单 + 会话 + 设置；恢复时自动联网拉回插件）──
    p_cfg_bk = sub.add_parser(
        "cfg-backup",
        help="把这条 lane 的插件清单+会话+设置打成一个轻量 zip（不含插件本体/安装树）"
    )
    p_cfg_bk.add_argument("lane")
    p_cfg_bk.add_argument("--name", help="给这份备份起个后缀名，例如 before-reinstall")
    p_cfg_bk.add_argument("--no-sessions", action="store_true", help="不带会话记录")
    p_cfg_bk.add_argument("--no-attachments", action="store_true", help="不带会话附件")
    p_cfg_bk.add_argument("--no-settings", action="store_true", help="不带设置文件")
    p_cfg_bk.add_argument("--credentials", action="store_true",
                          help="连凭据（API key 等）一起打包（敏感，默认不带）")
    p_cfg_bk.add_argument("--data", action="append", metavar="目录名",
                          help="额外打包 HOME 下的插件数据目录（可多次给）")
    p_cfg_bk.add_argument("--profiles", nargs="+", help="只备份这几个 profile（默认全部）")
    p_cfg_bk.add_argument("--yes", action="store_true", help="跳过确认")

    p_cfg_ls = sub.add_parser("cfg-list", help="列出全部配置备份包")

    p_cfg_show = sub.add_parser("cfg-show", help="查看一个配置备份包里有什么（插件/会话/设置）")
    p_cfg_show.add_argument("backup", help="备份名（可不带 .zip）或 zip 路径")

    p_cfg_rm = sub.add_parser("cfg-rm", help="删掉一个配置备份包")
    p_cfg_rm.add_argument("backup")
    p_cfg_rm.add_argument("--yes", action="store_true")

    p_cfg_rs = sub.add_parser(
        "cfg-restore",
        help="把配置备份恢复进 lane：新建 lane 自动装 dsh 后按清单联网拉回插件，或导入已有 lane"
    )
    p_cfg_rs.add_argument("backup", help="备份名（可不带 .zip）或 zip 路径")
    p_cfg_rs.add_argument("lane", nargs="?", help="目标 lane（已存在）；不填就要用 --new-lane")
    p_cfg_rs.add_argument("--new-lane", help="新建一条 lane 并恢复到它里面")
    p_cfg_rs.add_argument("--version", help="新建 lane 时装的 dsh 版本（默认用备份里的版本）")
    p_cfg_rs.add_argument("--profiles", nargs="+", help="只恢复这几个 profile（默认全部）")
    p_cfg_rs.add_argument("--no-sessions", action="store_true", help="不恢复会话")
    p_cfg_rs.add_argument("--no-settings", action="store_true", help="不恢复设置文件")
    p_cfg_rs.add_argument("--no-credentials", action="store_true", help="即使备份含凭据也不合并")
    p_cfg_rs.add_argument("--no-data", action="store_true", help="不恢复插件数据目录")
    p_cfg_rs.add_argument("--yes", action="store_true", help="跳过确认（运行中的 lane 会先停）")

    p_cfg_pl = sub.add_parser("cfg-plugins", help="列出备份包内每个 profile 的插件")
    p_cfg_pl.add_argument("backup")
    p_cfg_pl.add_argument("--profile", help="只看这一个 profile")

    p_cfg_pa = sub.add_parser("cfg-plugin-add",
                              help="往备份包里加插件（pkg@spec；可从本机已有 lane 采集版本与开关）")
    p_cfg_pa.add_argument("backup")
    p_cfg_pa.add_argument("profile", help="加进备份里的哪个 profile（不存在会新建）")
    p_cfg_pa.add_argument("plugins", nargs="+", help="pkg@spec，例如 @scope/foo@1.2.3")
    p_cfg_pa.add_argument("--from-lane", help="从本机这条 lane 采集实际版本/开关/补丁")
    p_cfg_pa.add_argument("--from-profile", help="从源 lane 的哪个 profile 采集（默认 web）")

    p_cfg_pr = sub.add_parser("cfg-plugin-rm", help="从备份包里删除插件")
    p_cfg_pr.add_argument("backup")
    p_cfg_pr.add_argument("plugins", nargs="+", help="要删的包名")
    p_cfg_pr.add_argument("--profile", help="只在这个 profile 里删（默认所有 profile）")

    return parser


def main() -> int:
    setup_terminal()
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        # 双击 exe（不带参数）时控制台会立刻关掉，看起来像"打不开"；这里等一下回车。
        # 只有真人在终端里跑（stdin 是终端）才停；管道/脚本里不拦。
        if getattr(sys.stdin, "isatty", lambda: False)():
            try:
                input("\n按回车键关闭这个窗口…")
            except (EOFError, KeyboardInterrupt):
                pass
        return 0

    cfg = load_config()
    if getattr(args, "root", None):
        cfg["root"] = args.root
    # 磁盘上有、登记表里没有的 lane（登记表丢过 / 换目录跑过）自动认回来——
    # 只补登记不动目录；失败不挡任何命令
    try:
        for item in adopt_lost_lanes(cfg):
            info(col(f"自动认领：lane「{item['name']}」（{item['why']}）", C.CYAN))
    except Exception as exc:  # noqa: BLE001
        warn(f"自动认领未登记 lane 失败：{type(exc).__name__}: {exc}")
    # 接管 / 副本 lane 的登记版本对齐到安装树真实版本（本体被 npm 升级后登记会过时）
    try:
        for item in sync_lane_versions(cfg):
            info(col(f"版本对齐：lane「{item['lane']}」{item['from']} → {item['to']}（以安装树为准）",
                     C.CYAN))
    except Exception as exc:  # noqa: BLE001
        warn(f"版本对齐失败：{type(exc).__name__}: {exc}")

    handlers = {
        "doctor": cmd_doctor,
        "versions": cmd_versions,
        "create": cmd_create,
        "open": cmd_open,
        "list": cmd_list,
        "stop": cmd_stop,
        "logs": cmd_logs,
        "instances": cmd_instances,
        "adopt": cmd_adopt,
        "remove": cmd_remove,
        "profiles": cmd_profiles,
        "profile-add": cmd_profile_add,
        "profile-rm": cmd_profile_rm,
        "keys": cmd_keys,
        "set-key": cmd_set_key,
        "rm-key": cmd_rm_key,
        "provider-list": cmd_provider_list,
        "provider-add": cmd_provider_add,
        "copy-key": cmd_copy_key,
        "delete": cmd_delete,
        "clone": cmd_clone,
        "upgrade": cmd_upgrade,
        "verify": cmd_verify,
        "plugins": cmd_plugins,
        "plugin-transfer": cmd_plugin_transfer,
        "plugin-add": cmd_plugin_add,
        "plugin-rm": cmd_plugin_rm,
        "plugin-off": cmd_plugin_off,
        "plugin-on": cmd_plugin_on,
        "plugins-off": cmd_plugins_off,
        "plugins-on": cmd_plugins_on,
        "market": cmd_market,
        "market-off": cmd_market_off,
        "market-on": cmd_market_on,
        "market-update": cmd_market_update,
        "market-rollback": cmd_market_rollback,
        "chat-backup": cmd_chat_backup,
        "chat-list": cmd_chat_list,
        "chat-restore": cmd_chat_restore,
        "chat-rm": cmd_chat_rm,
        "cfg-backup": cmd_cfg_backup,
        "cfg-list": cmd_cfg_list,
        "cfg-show": cmd_cfg_show,
        "cfg-rm": cmd_cfg_rm,
        "cfg-restore": cmd_cfg_restore,
        "cfg-plugins": cmd_cfg_plugins,
        "cfg-plugin-add": cmd_cfg_plugin_add,
        "cfg-plugin-rm": cmd_cfg_plugin_rm,
    }
    try:
        return handlers[args.command](cfg, args)
    except KeyboardInterrupt:
        info("")
        return 130
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        # 任何命令跑完都把 TCP 表缓存作废，别让下一次读拿到过期状态
        invalidate_tcp_cache()


if __name__ == "__main__":
    sys.exit(main())
