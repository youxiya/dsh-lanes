#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 多版本启动器 —— 应用窗口（Web 前端版）
==========================================
后端只做三件事：**起一个本机 HTTP 服务**、**把已核实过的核心函数接到 API 上**、
**用 Edge/Chrome 的 --app 模式开一个无边框窗口指向它**。界面本身是 `web/` 下的
HTML/CSS/JS——浏览器拿 GPU 合成动画，所以换主题/展开收起/日志滚动都能有真过渡，
而不是"销毁重建控件树 + 重排版"。

和 `dsh_lanes_gui.py`（tkinter 版）的关系
----------------------------------------
两者是**同一个壳的两种画法**：都调用 `dsh_lanes.py` 里同一批函数
（`cmd_open` / `cmd_stop` / `lane_states` / `discover_instances` / `cmd_adopt` / …），
行为一致、可以混用（同时开也行，状态以运行态文件和 TCP 表为准）。
`--classic` 可以把活儿交给旧的 tkinter 界面。

用法：
    pyw dsh_lanes_web.py               # 无控制台窗口（默认）
    py  dsh_lanes_web.py --dev         # 带控制台 + 开 DevTools（排障用）
    py  dsh_lanes_web.py --stay        # 只起服务，不开窗口，用系统浏览器访问
    py  dsh_lanes_web.py --classic     # 转交旧的 tkinter 界面
    py  dsh_lanes_web.py --selfcheck   # 服务器自检后退出（自动化；不碰真实 lanes.json）

进程与线程
----------
    主线程            ：起服务 → 起各后台线程 → 拉起窗口 → 等退出条件 → 收尾
    HTTP 服务线程     ：serve_forever（每请求一线程，daemon）
    SSE 线程（每客户端）：长连接，阻塞在队列上；15s 发一次心跳注释以探测掉线
    ticker 线程       ：每 2.5s 重算一次快照，签名变了才推 state（＝旧界面的 _tick）
    日志 flush 线程   ：把日志攒 50ms 批量推（npm 一秒上百行，逐行推太吵）
    job 线程（临时）  ：跑核心命令，stdout 重定向进日志（＝旧界面的 run_job）
    remote 线程（临时）：查 npm dist-tag
"""

from __future__ import annotations

import argparse
import http.cookies
import http.server
import io
import json
import os
import queue
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dsh_lanes as core  # noqa: E402

BUILD = "web-0.6.3"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

MAX_LOG_LINES = 4000          # 服务器侧日志环形缓冲（与旧界面 _MAX_LOG_LINES 对齐）
HELLO_LOG_LINES = 400         # 新连上来的客户端回补多少行历史
TICK_SECONDS = 2.5            # 状态轮询间隔（与旧界面 _tick 一致）
SNAP_TTL = 1.0                # /api/state 的快照复用窗口
LOG_FLUSH_SECONDS = 0.05
SSE_HEARTBEAT_SECONDS = 15.0
QUIET_EXIT_SECONDS = 6.0      # 页面关掉后多久收摊（刷新会重新连上从而取消）
NO_CLIENT_TIMEOUT = 120.0     # 窗口一直没连上来（浏览器起不来/被拦）就别僵着
GEO_THROTTLE_MS = 400

LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}


# ======================= 基础工具 =======================


def asset_dir() -> Path:
    """`web/` 资源目录。

    冻结成 exe 之后 `__file__` 在临时解包目录里，PyInstaller 的 `--add-data "web;web"`
    会把资源放到 `sys._MEIPASS` 下，所以两种情形都要认。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        cand = base / "web"
        if cand.is_dir():
            return cand
    return HERE / "web"


def tag_for(text: str, tag: str | None = None) -> str:
    """日志配色标签。没有显式标签时按旧界面的规矩从文本里认。"""
    if tag:
        return tag
    if "[OK]" in text:
        return "ok"
    if "[XX]" in text:
        return "err"
    if "[!!]" in text:
        return "warn"
    return ""


def _safe_std_streams() -> None:
    """pythonw（无控制台）下 sys.stdout/stderr 是 None，任何 print 都会抛异常。
    核心模块是靠 print 输出日志的（`load_config` 解析失败时就会），所以先补兜底。

    顺手改成**行缓冲**：stdout 被重定向到文件/管道时默认是块缓冲，界面地址那几行
    会一直卡在缓冲区里 —— `--stay` 配合脚本或自动化时，等半天读不到地址就是这么来的。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


class HubWriter(io.TextIOBase):
    """把核心模块 print 出来的内容按行送进日志面板（＝旧界面的 QueueWriter）。"""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._buf = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.hub.log(line)
        return len(text)

    def flush(self) -> None:
        # 故意不把半行推出去：否则 `dsh web: <url>` 会被拆成两行显示
        return


# ======================= 事件中转 =======================


class Hub:
    """所有推送事件的中转站：日志 / 任务 / 状态 / 远端查询。

    每个 SSE 客户端一个队列（互相不阻塞）；日志先攒后发（`LOG_FLUSH_SECONDS`），
    因为 npm 安装一秒能吐上百行，逐行推会把浏览器的渲染线程压住。
    """

    def __init__(self, dev: bool = False) -> None:
        self.dev = dev
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()
        self._pending: list[dict] = []
        self.lines: list[dict] = []          # 历史日志：新连上来的客户端要能回补

    # ── 订阅 ────────────────────────────────────────────────────────────────
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: str, data) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put((event, data))

    # ── 日志 ────────────────────────────────────────────────────────────────
    def log(self, text: str, tag: str | None = None) -> None:
        line = {"text": ANSI_RE.sub("", str(text)), "tag": tag_for(str(text), tag)}
        if self.dev:
            # --dev 时日志同时打到 stderr：界面里的红字（比如前端异常上报）在控制台
            # 也能直接看到，排障不用盯着浏览器
            sys.stderr.write(f"[log:{line['tag'] or '-'}] {line['text']}\n")
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]
            self._pending.append(line)

    def clear_log(self) -> None:
        with self._lock:
            self.lines = []
            self._pending = []

    def tail(self, count: int) -> list[dict]:
        with self._lock:
            return list(self.lines[-count:])

    def _flush_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            stop.wait(LOG_FLUSH_SECONDS)
            with self._lock:
                if not self._pending:
                    continue
                batch, self._pending = self._pending, []
            self.publish("log", {"lines": batch})


# ======================= 应用本体 =======================


class Launcher:
    """窗口后端：状态快照、任务执行、日志、主题、几何。"""

    def __init__(self, token: str, stay: bool = False, dev: bool = False) -> None:
        self.token = token
        self.stay = stay
        self.dev = dev
        self.hub = Hub(dev=dev)
        self.cfg = core.load_config()

        self._lock = threading.Lock()
        self._snap: dict | None = None
        self._sig = None
        self._adopt_attempted = False
        self._force_once = False
        self._wake = threading.Event()
        self._stop = threading.Event()

        self.busy = False
        self.busy_title = ""

        self._remote_busy = False
        self._remote_at = 0.0
        self._remote_text = "正在查询 npm 上的最新版本…"
        self._remote_ok = True
        self._remote_pending = True

        self._geom: dict | None = None
        self._root_check: tuple[bool, str] | None = None
        self._started_at = time.monotonic()
        self._ever_connected = False
        self._quiet_since: float | None = None
        self._job_seq = 0          # 任务完成事件的序号（对话框靠它判断"我这个任务结束了没"）

    # ── 快照 ────────────────────────────────────────────────────────────────
    def refresh_snapshot(self, force: bool = False) -> dict:
        """一轮刷新要用到的全部"外部事实"，只算一次（＝旧界面的 _snapshot）。

        以前算签名、建卡片、更新计数各算一遍，每次都是真去读 TCP 表 / 探端口；
        这里一次算完，所有请求与推送复用同一份。
        """
        cfg = core.load_config()
        # 磁盘上有、登记表里没有的 lane（登记表丢过 / 换目录跑过）自动认回来。
        # 只看启动和点「刷新」时做一次：扫盘 + 写配置不值得每 2.5 秒来一遍。
        if not self._adopt_attempted:
            self._adopt_attempted = True
            try:
                for item in core.adopt_lost_lanes(cfg):
                    self.hub.log(f"自动认领磁盘上的 lane「{item['name']}」（{item['why']}）", "ok")
            except Exception as exc:  # noqa: BLE001
                self.hub.log(f"[!!] 自动认领未登记 lane 失败：{type(exc).__name__}: {exc}", "warn")
            # 接管 / 副本 lane 的登记版本对齐到安装树真实版本（本体被 npm 升级后登记会过时）
            try:
                for item in core.sync_lane_versions(cfg):
                    self.hub.log(
                        f"版本对齐：lane「{item['lane']}」{item['from']} → {item['to']}"
                        "（以安装树为准）", "ok",
                    )
            except Exception as exc:  # noqa: BLE001
                self.hub.log(f"[!!] 版本对齐失败：{type(exc).__name__}: {exc}", "warn")

        states = core.lane_states(cfg)          # {lane: {profile: 运行态|None}}，复用同一份 TCP 表
        try:
            foreign = core.discover_instances(cfg)
        except Exception:  # noqa: BLE001
            foreign = []
        try:
            installs = core.find_installs(cfg)
        except Exception:  # noqa: BLE001
            installs = []

        signature = (
            tuple(
                (
                    name,
                    data.get("version"),
                    tuple(sorted(
                        (prof,
                         (rt or {}).get("pid"),
                         (rt or {}).get("port"),
                         (rt or {}).get("url"))
                        for prof, rt in (states.get(name) or {}).items()
                    )),
                )
                for name, data in cfg.get("lanes", {}).items()
                if isinstance(data, dict)
            ),
            tuple((i["port"], i["pid"]) for i in foreign),
        )
        with self._lock:
            self._snap = {
                "cfg": cfg,
                "states": states,
                "foreign": foreign,
                "installs": installs,
                "signature": signature,
            }
        return self._snap

    def snapshot(self) -> dict | None:
        with self._lock:
            return self._snap

    def state_payload(self) -> dict:
        """给前端的完整状态。快照还没算出来时返回 ready=false，前端显示骨架屏。"""
        snap = self.snapshot()
        if snap is None:
            return {"ready": False, "busy": self.busy, "busyTitle": self.busy_title}
        cfg = snap["cfg"]
        ui = cfg.get("ui") or {}

        lanes = []
        for name, data in cfg.get("lanes", {}).items():
            if not isinstance(data, dict):
                continue
            view = core.lane_profiles_view(cfg, name)
            states = snap["states"].get(name) or {}
            profs = []
            for prof, info in view.items():
                rt = states.get(prof)
                profs.append({
                    "name": prof,
                    "prefPort": info.get("port"),
                    "onDisk": bool(info.get("onDisk")),
                    "registered": bool(info.get("registered")),
                    "isDefault": prof == core.DEFAULT_PROFILE,
                    "running": bool(rt),
                    "pid": (rt or {}).get("pid"),
                    "livePort": (rt or {}).get("port"),
                    "url": (rt or {}).get("url") or "",
                })
            profs.sort(key=lambda p: (not p["isDefault"], p["name"]))
            lanes.append({
                "name": name,
                "version": str(data.get("version") or "?"),
                "kind": str(data.get("kind") or ""),
                "clonedFrom": str(data.get("clonedFrom") or ""),
                "profiles": profs,
                "running": sum(1 for p in profs if p["running"]),
                "total": len(profs),
                "defaultProfile": core.default_profile_of(view),
            })

        installs = [i for i in snap["installs"] if not str(i.get("source", "")).startswith("lane ")]
        running = sum(1 for v in snap["states"].values() for rt in (v or {}).values() if rt)
        total_profiles = sum(len(v or {}) for v in snap["states"].values())

        try:
            root_ok, root_detail = self.root_writable(cfg)
        except Exception as exc:  # noqa: BLE001
            root_ok, root_detail = True, f"（检测失败：{exc}）"

        return {
            "ready": True,
            "root": str(cfg.get("root") or ""),
            "rootWritable": {"ok": bool(root_ok), "detail": str(root_detail)},
            "theme": "dark" if str(ui.get("theme") or "").lower() == "dark" else "light",
            "registry": {
                # 新建 / 升级对话框里的「下载源」三选一：默认选中用户上一次的选择
                "choice": core.registry_choice_of(cfg),
                "value": str(cfg.get("registry") or ""),
            },
            "ui": {
                "w": int(ui.get("w") or 0), "h": int(ui.get("h") or 0),
                "x": int(ui.get("x") or 0), "y": int(ui.get("y") or 0),
            },
            "lanes": lanes,
            "foreign": [
                {
                    "port": i["port"], "pid": i["pid"], "exe": i.get("exe") or "?",
                    "url": i.get("url") or f"http://127.0.0.1:{i['port']}",
                    "dshLike": bool(i.get("dsh_like")), "started": i.get("started") or "",
                }
                for i in snap["foreign"]
            ],
            "installs": [
                {
                    "source": str(i.get("source") or ""), "version": str(i.get("version") or "?"),
                    "path": str(i.get("path") or ""), "installedAt": str(i.get("installedAt") or ""),
                }
                for i in installs
            ],
            "counts": {
                "lanes": len(snap["states"]), "profiles": total_profiles,
                "running": running, "foreign": len(snap["foreign"]),
            },
            "busy": self.busy,
            "busyTitle": self.busy_title,
            "remote": {
                "text": self._remote_text, "ok": self._remote_ok, "pending": self._remote_pending,
            },
        }

    def hello_payload(self) -> dict:
        return {
            "build": BUILD,
            "stay": self.stay,
            "lines": self.hub.tail(HELLO_LOG_LINES),
            "geoThrottleMs": GEO_THROTTLE_MS,
        }

    def publish_state(self, force: bool = True) -> None:
        if force or self.snapshot() is None:
            self.refresh_snapshot(force=True)
        self._sig = (self.snapshot() or {}).get("signature")
        self.hub.publish("state", self.state_payload())

    def _ticker(self) -> None:
        """每 2.5s 算一次快照，签名变了才推（＝旧界面的 _tick）。"""
        while not self._stop.is_set():
            try:
                self.refresh_snapshot(force=True)
                snap = self.snapshot() or {}
                if self._force_once or snap.get("signature") != self._sig:
                    self._force_once = False
                    self._sig = snap.get("signature")
                    self.hub.publish("state", self.state_payload())
            except Exception as exc:  # noqa: BLE001
                self.hub.log(f"[!!] 状态刷新失败：{type(exc).__name__}: {exc}", "warn")
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    def request_refresh(self) -> None:
        """有东西被改动过（起停 / 认领 / 杀进程）→ 立刻重算并强制推一次。"""
        self._force_once = True
        self._adopt_attempted = False
        #[被改动过，别复用缓存]
        core.invalidate_tcp_cache()
        self._wake.set()

    # ── 任务 ────────────────────────────────────────────────────────────────
    def run_job(self, title: str, fn, on_done=None) -> bool:
        """后台跑一个核心命令，stdout 实时进日志（＝旧界面的 run_job）。忙则拒绝。"""
        with self._lock:
            if self.busy:
                return False
            self.busy = True
        self.busy_title = title
        self.hub.publish("busy", {"busy": True, "title": title})
        self.hub.log("", None)
        self.hub.log(f"──── {title} ────", "head")

        def worker() -> None:
            old = sys.stdout
            sys.stdout = HubWriter(self.hub)  # type: ignore[assignment]
            good = False
            try:
                fn()
                good = True
            except Exception as exc:  # noqa: BLE001
                self.hub.log(f"[XX] {type(exc).__name__}: {exc}", "err")
                self.hub.log(traceback.format_exc().rstrip(), "muted")
            finally:
                sys.stdout = old
                with self._lock:
                    self.busy = False
                    self.busy_title = ""
                    self._job_seq += 1
                    seq = self._job_seq
                self.hub.publish("busy", {"busy": False, "title": title})
                # 任务结束单独推一条：对话框（凭据 / 备份对话 / 插件库…）靠它决定"该重新读一遍了"，
                # 光看 busy 由真变假在"连着起两个任务"时会漏掉中间那一次。
                self.hub.publish("job_done", {"title": title, "ok": good, "seq": seq})
                self.hub.log(f"──── {title} {'完成' if good else '失败'} ────",
                             "head" if good else "err")
                self.request_refresh()
                if good and on_done:
                    try:
                        on_done()
                    except Exception as exc:  # noqa: BLE001
                        self.hub.log(f"[XX] 回调异常：{exc}", "err")

        threading.Thread(target=worker, daemon=True).start()
        return True

    # ── npm 版本查询 ────────────────────────────────────────────────────────
    def refresh_remote(self, force: bool = False) -> None:
        """查一次 npm 的 dist-tag（纯只读、后台线程）。默认 10 分钟内不重复查。"""
        if self._remote_busy:
            return
        if not force and time.time() - self._remote_at < 600:
            return
        self._remote_busy = True
        self._remote_pending = True
        self._remote_text = "正在查询 npm 上的最新版本…"
        self._remote_ok = True
        self.hub.publish("remote", {"text": self._remote_text, "ok": True, "pending": True})

        installed = {str((d or {}).get("version") or "") for d in (self.cfg.get("lanes") or {}).values()}

        def worker() -> None:
            try:
                packument, reg = core.fetch_index(core.load_config())
            except Exception as exc:  # noqa: BLE001
                self._remote_busy = False
                self._remote_at = time.time()
                self._remote_pending = False
                self._remote_ok = False
                self._remote_text = "查不到 npm 上的版本（离线或 registry 不通），点「刷新」重试"
                self.hub.publish("remote", {"text": self._remote_text, "ok": False})
                self.hub.log(f"[!!] 查询 npm 版本失败：{exc}", "warn")
                return
            tags = packument.get("dist-tags") or {}
            order = ["latest", "next", "alpha"]
            keys = [k for k in order if k in tags] + sorted(k for k in tags if k not in order)
            bits = [f"{k} {tags[k]}" + ("√" if str(tags[k]) in installed else "") for k in keys]
            # 把实际答话的源写在这儿：镜像常常落后官方一两天，"这行数字从哪来"必须看得见
            self._remote_busy = False
            self._remote_at = time.time()
            self._remote_pending = False
            self._remote_ok = True
            self._remote_text = f"npm 上（{core.registry_label(reg)}）：" + "　·　".join(bits)
            self.hub.publish("remote", {"text": self._remote_text, "ok": True})

        threading.Thread(target=worker, daemon=True).start()

    def root_writable(self, cfg: dict) -> tuple[bool, str]:
        """根目录能不能写（只测一次）。

        这个检查是**真去写一个探针文件再删掉**的，别每轮都做：ticker（每 2.5s）和
        HTTP 请求线程会同时动同一个探针文件，撞出 WinError 32「另一个程序正在使用此
        文件」，界面就会莫名其妙地报"根目录不可写"（2026-10-16 实测踩到）。
        旧界面也是启动时只查一次。
        """
        if self._root_check is None:
            self._root_check = core.lane_root_writable(cfg)
        return self._root_check

    # ── 主题 / 几何 ─────────────────────────────────────────────────────────
    def set_theme(self, theme: str) -> str:
        target = "dark" if str(theme).lower() == "dark" else "light"
        try:
            cfg = core.load_config()
            ui = dict(cfg.get("ui") or {})
            ui["theme"] = target
            cfg["ui"] = ui
            core.save_config(cfg)
        except Exception as exc:  # noqa: BLE001
            self.hub.log(f"[!!] 主题没记进 lanes.json（本次切换照样生效）：{exc}", "warn")
        return target

    def set_geometry(self, w: int, h: int, x: int, y: int) -> None:
        """记窗口几何（＝旧界面的 _save_geometry）。最小化/异常尺寸不记。"""
        if w < 400 or h < 300:
            return
        self._geom = {"w": int(w), "h": int(h), "x": int(x), "y": int(y)}
        try:
            cfg = core.load_config()
            ui = dict(cfg.get("ui") or {})     # 保留 theme 等其它字段，别把整块覆盖掉
            ui.update(self._geom)
            cfg["ui"] = ui
            core.save_config(cfg)
        except Exception:  # noqa: BLE001
            pass

    def save_geometry(self) -> None:
        if self._geom:
            g = self._geom
            self.set_geometry(g["w"], g["h"], g["x"], g["y"])

    # ── 退出判定 ────────────────────────────────────────────────────────────
    def note_client_connected(self) -> None:
        self._ever_connected = True
        self._quiet_since = None

    def should_exit(self) -> bool:
        """没有页面连着（且已经连过一次）就收摊。

        `--stay` 时永不自退。关键点：**有一个 SSE 客户端连着就绝不退**——页面加载完
        之后只在 SSE 上跑，不再发 HTTP 请求，拿"最后一次请求时间"判断会把好端端的
        界面判死。窗口关掉时 SSE 断开，心跳注释写不出去 → 连接被注销 → 进倒计时。
        """
        if self.stay or self._stop.is_set():
            return False
        now = time.monotonic()
        if self.hub.subscriber_count() > 0:
            self._quiet_since = None
            return False
        if not self._ever_connected:
            return now - self._started_at >= NO_CLIENT_TIMEOUT
        if self._quiet_since is None:
            self._quiet_since = now
            return False
        return now - self._quiet_since >= QUIET_EXIT_SECONDS

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self.save_geometry()


# ======================= HTTP 层 =======================


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dsh-lanes-web"
    sys_version = ""
    # 连接空闲多久断开（只在本机，且 SSE 每 15s 有心跳，300s 纯属兜底，防止
    # 半开连接一直占着线程）
    timeout = 300

    app: Launcher = None  # type: ignore[assignment]   # 由 make_server 注入

    # ── 基础 ────────────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args) -> None:
        if self.app.dev:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def _bytes(self, body: bytes, ctype: str, status: int = 200,
               extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ── 安全 ────────────────────────────────────────────────────────────────
    def _host_ok(self) -> bool:
        """Host 必须是本机地址（防 DNS rebinding：别人域名解析到 127.0.0.1 也进不来）。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):                 # [::1]:8080
            name = host.split("]")[0] + "]"
        elif ":" in host:
            name = host.rsplit(":", 1)[0]
        else:
            name = host
        return name in LOCAL_HOSTS

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return ""
        item = jar.get("dsh_token")
        return item.value if item else ""

    def _token_ok(self, parsed: urllib.parse.SplitResult) -> bool:
        want = self.app.token
        got = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        if got and secrets.compare_digest(got, want):
            return True
        if secrets.compare_digest(self.headers.get("X-DSH-Token") or "", want):
            return True
        return secrets.compare_digest(self._cookie_token(), want)

    def _origin_ok(self) -> bool:
        """带 Origin 时必须是自己的源（浏览器同源请求才有 Origin；跨站的一律挡掉）。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parts = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return parts.hostname in LOCAL_HOSTS

    def _guard(self, parsed: urllib.parse.SplitResult, *, mutation: bool = False) -> bool:
        if not self._host_ok():
            self._error(403, "Host 不是本机地址")
            return False
        if not self._token_ok(parsed):
            self._error(403, "缺少或错误的访问令牌（请从启动器给的地址打开）")
            return False
        if mutation:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            # 写操作要 JSON（sendBeacon 送 Blob 时可能被浏览器写成 text/plain，所以两个都收）。
            # 挡 CSRF 靠的是三条：这个类型检查、cookie 的 SameSite=Strict（跨站根本不发）、
            # 以及我们从不回 CORS 头（跨站的预检必失败）。
            if ctype not in ("application/json", "text/plain"):
                self._error(415, f"写操作必须是 application/json（收到 {ctype or '空'}）")
                return False
            if not self._origin_ok():
                self._error(403, "跨站来源被拒绝")
                return False
        return True

    # ── 路由 ────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            return self._page(parsed)
        if path == "/favicon.ico":
            return self._error(404, "no favicon")
        if path.startswith("/static/"):
            if not self._guard(parsed):
                return
            return self._static(urllib.parse.unquote(path[len("/static/"):]))
        if path == "/api/state":
            if not self._guard(parsed):
                return
            return self._json(self.app.state_payload())
        if path == "/api/events":
            if not self._guard(parsed):
                return
            return self._events()
        if path == "/api/ping":
            if not self._guard(parsed):
                return
            return self._json({"ok": True, "build": BUILD})
        return self._error(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        # 先把请求体收干净**再**做鉴权/应答。反过来的话：拒绝时没读走 body，剩下的字节会被
        # 当成"下一个请求"，在同一条 keep-alive 连接上报
        # `Bad request syntax ('{}')` —— 页面关窗口时的 sendBeacon 就会踩这个。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(400, "Content-Length 不合法")
        if length > 1 << 20:
            return self._error(413, "请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not self._guard(parsed, mutation=True):
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return self._error(400, "请求体不是合法 JSON")
        if not isinstance(body, dict):
            return self._error(400, "请求体必须是 JSON 对象")
        action = parsed.path[len("/api/"):] if parsed.path.startswith("/api/") else ""
        fn = self.ACTIONS.get(action)
        if fn is None:
            return self._error(404, f"未知操作 {action!r}")
        try:
            return fn(self, body)
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.app.hub.log(f"[XX] {action} 出错：{type(exc).__name__}: {exc}", "err")
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 一律不回 CORS 头：跨站预检直接失败，写操作就发不出来
        self._error(403, "不支持跨站请求")

    # ── 页面与静态资源 ──────────────────────────────────────────────────────
    def _page(self, parsed: urllib.parse.SplitResult) -> None:
        if not self._host_ok():
            return self._error(403, "Host 不是本机地址")
        if not self._token_ok(parsed):
            return self._error(403, "缺少访问令牌：请用启动器给出的地址打开"
                                    "（形如 http://127.0.0.1:端口/?token=…）")
        page = asset_dir() / "index.html"
        if not page.is_file():
            return self._error(
                500, f"找不到界面文件 {page}（打包时需要 --add-data \"web;web\"）")
        cookie = (f"dsh_token={self.app.token}; Path=/; HttpOnly; SameSite=Strict")
        return self._bytes(page.read_bytes(), STATIC_TYPES["index.html"], extra={
            "Set-Cookie": cookie,
            "Content-Security-Policy":
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })

    def _static(self, name: str) -> None:
        # 白名单查表，不做路径拼接 —— 目录穿越在这儿天然不可能
        ctype = STATIC_TYPES.get(name)
        if ctype is None or name == "index.html":
            return self._error(404, f"没有这个静态文件 {name!r}")
        path = asset_dir() / name
        if not path.is_file():
            return self._error(404, f"缺少 {name}")
        return self._bytes(path.read_bytes(), ctype)

    # ── SSE ─────────────────────────────────────────────────────────────────
    def _sse_raw(self, text: str) -> None:
        data = text.encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _sse_send(self, event: str, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        lines = "".join(f"data: {ln}\n" for ln in body.split("\n"))
        self._sse_raw(f"event: {event}\n{lines}\n")

    def _events(self) -> None:
        """长连接事件流。

        掉线是怎么发现的（这里有个真实的坑）：**不能只靠"写失败"**。窗口关掉后对端已经走了，
        但第一次写通常还是"成功"的（TCP 允许收到 FIN 后继续写），要等下一轮心跳才真报错 ——
        15s 一次心跳、常常要两轮，实测退出要拖到 36 秒。所以这里改成：等事件的同时每秒
        探一次套接字（peek 到空＝对端已关），关窗口后启动器几秒内就自己收摊。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        q = self.app.hub.subscribe()
        self.app.note_client_connected()
        last_beat = time.monotonic()
        try:
            self._sse_send("hello", self.app.hello_payload())
            # 连上就先给一份当前状态（快照可能还没算完，那就先给 ready=false）
            self._sse_send("state", self.app.state_payload())
            self._sse_send("remote", {
                "text": self.app._remote_text, "ok": self.app._remote_ok,
                "pending": self.app._remote_pending,
            })
            while True:
                try:
                    event, data = q.get(timeout=1.0)
                except queue.Empty:
                    if self._peer_gone():
                        break
                    if time.monotonic() - last_beat >= SSE_HEARTBEAT_SECONDS:
                        self._sse_raw(": hb\n\n")          # 保活
                        last_beat = time.monotonic()
                    continue
                self._sse_send(event, data)
                last_beat = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(q)
            try:
                self._sse_raw("")                          # chunked 的结束帧（尽力而为）
            except OSError:
                pass

    def _peer_gone(self) -> bool:
        """对端是不是已经关了。

        只 peek 不消费；真收到意外数据就读掉，否则 select 会一直说"可读"，这里就变成忙等。
        """
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not ready:
            return False
        try:
            if not self.connection.recv(4096, socket.MSG_PEEK):
                return True                                # EOF：对端走了
        except OSError:
            return True
        try:
            self.connection.recv(4096)                     # SSE 这条连接上不该有客户端数据
        except OSError:
            return True
        return False

    # ── 各操作 ──────────────────────────────────────────────────────────────
    def _act_open(self, body: dict):
        lane = _need_str(body, "lane")
        profile = _need_str(body, "profile")
        port = _parse_port(body.get("port"))
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        args = argparse.Namespace(
            target=f"{lane}/{profile}", profile=profile, port=port, cwd=None, timeout=None,
            no_browser=False, detach=True, adopt_only=False,
        )
        started = self.app.run_job(
            f"打开 {lane}/{profile}", lambda: core.cmd_open(core.load_config(), args))
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": f"打开 {lane}/{profile}"})

    def _act_stop(self, body: dict):
        lane = _need_str(body, "lane")
        profile = str(body.get("profile") or "").strip()
        if profile:
            targets = [f"{lane}/{profile}"]
            title = f"停止 {lane}/{profile}"
        else:
            targets = [lane]
            title = f"停止 {lane} 的全部 profile"
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        args = argparse.Namespace(target=targets)
        started = self.app.run_job(title, lambda: core.cmd_stop(core.load_config(), args))
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": title})

    def _act_stop_all(self, body: dict):
        cfg = core.load_config()
        running = [
            f"{lane}/{prof}"
            for lane, profs in core.lane_states(cfg).items()
            for prof, rt in (profs or {}).items()
            if rt
        ]
        if not running:
            return self._json({"error": "没有正在运行的 profile"}, 409)
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        self.app.hub.log(f"准备停止：{'、'.join(running)}", "muted")
        args = argparse.Namespace(target=running)
        started = self.app.run_job("全部停止", lambda: core.cmd_stop(core.load_config(), args))
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": "全部停止", "targets": running})

    def _act_tail_log(self, body: dict):
        lane = _need_str(body, "lane")
        profile = str(body.get("profile") or core.DEFAULT_PROFILE)
        cfg = core.load_config()
        path = core.lane_log_path(cfg, lane, profile)
        if not path.is_file():
            self.app.hub.log(f"[!!] 这个 profile 还没有日志：{path}", "warn")
            return self._json({"ok": False, "reason": "no-log", "path": str(path)})
        self.app.hub.log(f"──── {lane}/{profile} 日志尾部（{path}）────", "head")
        for line in core.tail_lines(path, 40):
            self.app.hub.log(line)
        return self._json({"ok": True, "path": str(path)})

    def _act_profile_rm(self, body: dict):
        """移除一个 profile（默认 profile 会被核心拒绝；运行中的也会被拒绝）。"""
        lane = _need_str(body, "lane")
        name = _need_str(body, "name")
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        # yes=True：确认已经在界面上做过了（弹窗里写了会删目录、插件找不回）
        args = argparse.Namespace(lane=lane, name=name, yes=True)
        started = self.app.run_job(
            f"移除 profile {lane}/{name}",
            lambda: core.cmd_profile_rm(core.load_config(), args))
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": f"移除 profile {lane}/{name}"})

    def _act_kill(self, body: dict):
        pid = int(body.get("pid") or 0)
        port = int(body.get("port") or 0)
        if pid <= 0:
            raise ValueError("pid 不合法")
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)

        def job() -> None:
            core.kill_tree(pid)
            print(f"已结束 PID {pid}（端口 {port}）")

        started = self.app.run_job(f"结束进程 (PID {pid})", job)
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": f"结束 PID {pid}"})

    def _act_adopt_plan(self, body: dict):
        """「加入 lane 列表」的预演：先把 lane 名 / HOME / 提示算出来给用户看，再让他确认。"""
        path = _need_str(body, "path")
        item = _find_install(self.app, path)
        if item is None:
            return self._json({"error": "这份安装已经不在列表里了，点「刷新」重看"}, 409)
        plan = _adopt_plan(core.load_config(), item)
        return self._json(plan)

    def _act_adopt(self, body: dict):
        path = _need_str(body, "path")
        item = _find_install(self.app, path)
        if item is None:
            return self._json({"error": "这份安装已经不在列表里了，点「刷新」重看"}, 409)
        cfg = core.load_config()
        plan = _adopt_plan(cfg, item)
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        args = argparse.Namespace(
            lane=plan["lane"], install=item["path"], port=None, home=plan["home"], force=True)
        started = self.app.run_job(
            f"加入 lane：{plan['lane']}", lambda: core.cmd_adopt(core.load_config(), args))
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": f"加入 lane：{plan['lane']}"})

    def _act_open_path(self, body: dict):
        which = str(body.get("which") or "root")
        if which != "root":
            raise ValueError(f"不认识的目录 {which!r}")
        root = core.ensure_layout(core.load_config())["root"]
        try:
            os.startfile(str(root))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.app.hub.log(f"[!!] 打不开目录：{exc}", "warn")
            return self._json({"error": f"打不开目录：{exc}"}, 500)
        self.app.hub.log(f"已打开根目录 {root}", "muted")
        return self._json({"ok": True, "root": str(root)})

    def _act_open_url(self, body: dict):
        url = _need_str(body, "url")
        # 只允许本机地址：这一条挡住"前端被塞个外部链接，后端替它打开"的滥用
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname not in LOCAL_HOSTS:
            raise ValueError("只允许打开本机 http(s) 地址")
        self.app.hub.log(f"用浏览器打开 {url}", "muted")
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            self.app.hub.log(f"[!!] 打开失败：{exc}", "warn")
            return self._json({"error": str(exc)}, 500)
        return self._json({"ok": True})

    def _act_open_repo(self, body: dict):
        self.app.hub.log(f"用浏览器打开官方代码库 {core.REPO_URL}", "muted")
        try:
            webbrowser.open(core.REPO_URL)
        except Exception as exc:  # noqa: BLE001
            self.app.hub.log(f"[!!] 打开失败：{exc}", "warn")
            return self._json({"error": str(exc)}, 500)
        return self._json({"ok": True})

    def _act_refresh(self, body: dict):
        self.app.request_refresh()
        self.app.refresh_remote(force=True)
        return self._json({"ok": True})

    def _act_doctor(self, body: dict):
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        started = self.app.run_job(
            "环境自检", lambda: core.cmd_doctor(core.load_config(), None))
        if not started:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": "环境自检"})

    def _act_theme(self, body: dict):
        theme = self.app.set_theme(str(body.get("theme") or ""))
        self.app.publish_state()
        return self._json({"ok": True, "theme": theme})

    def _act_geometry(self, body: dict):
        def num(key: str) -> int:
            try:
                return int(round(float(body.get(key) or 0)))
            except (TypeError, ValueError):
                return 0
        self.app.set_geometry(num("w"), num("h"), num("x"), num("y"))
        return self._json({"ok": True})

    def _act_log_clear(self, body: dict):
        self.app.hub.clear_log()
        self.app.hub.publish("log_clear", {})
        return self._json({"ok": True})

    def _act_bye(self, body: dict):
        self.app._quiet_since = time.monotonic() if not self.app.hub.subscriber_count() else None
        return self._json({"ok": True})

    def _act_client_error(self, body: dict):
        message = str(body.get("message") or "")[:2000]
        where = str(body.get("where") or "")[:400]
        self.app.hub.log(f"[XX] 界面脚本异常：{message}" + (f"（{where}）" if where else ""), "err")
        return self._json({"ok": True})

    def _act_client_metric(self, body: dict):
        """前端性能上报（?debug=1 时的 FPS 采样）。走日志，方便自动化核对。"""
        fps = body.get("fps")
        dropped = body.get("dropped")
        self.app.hub.log(f"[OK] 界面帧率采样：{fps} fps（掉帧 {dropped}）", "ok")
        return self._json({"ok": True})

    # ── 第二批：新建 / 添加 Profile / 凭据 / 插件 / 市场 / 对话 / 复制 / 升级 / 删除 ──
    #
    # 两段式：`*_plan` / `*_get` / `*_list` 是**只读**的（把"会发生什么"算出来摆给用户看），
    # 真正动手的一律走 `_start` → run_job（stdout 进日志面板，和经典界面同一条路）。
    # 这也是经典界面的规矩：先算清范围再确认，确认了才写盘。

    def _start(self, title: str, fn):
        """统一的"起一个后台任务"出口。忙就回 409（前端会解释成"上一个任务还在跑"）。"""
        if self.app.busy:
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        if not self.app.run_job(title, fn):
            return self._json({"error": "busy", "title": self.app.busy_title}, 409)
        return self._json({"job": title})

    def _save_registry(self, body: dict) -> bool:
        """勾了「记住这个选择」才写 lanes.json（＝经典界面的 _save_registry_choice）。"""
        if not body.get("remember"):
            return False
        cfg = core.load_config()
        cfg["registry"] = core.registry_value_of(str(body.get("registry") or "auto"))
        core.save_config(cfg)
        self.app.hub.log(f"下载源已记住：{core.registry_choice_of(cfg)}"
                         + (f"（{cfg['registry']}）" if cfg.get("registry") else "（auto＝先官方再镜像）"),
                         "muted")
        return True

    # ① 新建版本 ─────────────────────────────────────────────────────────────
    def _act_registry_versions(self, body: dict):
        """查 npm 上现成的版本（查哪一家由「下载源」决定，和下载用同一个源）。"""
        cfg = core.load_config()
        current = str(body.get("current") or "")
        try:
            plan = core.registry_plan(cfg, str(body.get("registry") or "").strip() or None)
            packument, reg = core.fetch_index(cfg, registries=plan)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        tags = {str(k): str(v) for k, v in (packument.get("dist-tags") or {}).items()}
        tag_of: dict[str, list[str]] = {}
        for tag, ver in tags.items():
            tag_of.setdefault(ver, []).append(tag)
        versions = core.sorted_versions(packument)
        vdir = core.paths(cfg)["versions"]
        items = []
        for ver in reversed(versions[-40:]):        # 新版在最前
            marks = []
            if ver == current:
                marks.append("现在用的")
            try:
                if core.installed_version(vdir / ver) == ver:
                    marks.append("本地已装")
            except Exception:  # noqa: BLE001
                pass
            items.append({"v": ver, "tags": sorted(tag_of.get(ver, [])), "marks": marks})
        return self._json({
            "ok": True, "reg": str(reg), "label": core.registry_label(reg),
            "registry": core.registry_choice_of(cfg),
            "total": len(versions), "tags": tags, "items": items,
            "newest": versions[-1] if versions else "",
        })

    def _act_create(self, body: dict):
        lane = _need_str(body, "lane")
        version = _need_str(body, "version")
        if not core.LANE_NAME_RE.match(lane):
            raise ValueError("名称只能用字母/数字/._-，且以字母或数字开头（最长 32 字符）")
        self._save_registry(body)
        args = argparse.Namespace(lane=lane, version=version, force=True, follow=True,
                                  registry=str(body.get("registry") or "auto"))
        return self._start(f"新建 {lane}（{version}）",
                           lambda: core.cmd_create(core.load_config(), args))

    # ② 添加 Profile ────────────────────────────────────────────────────────
    def _act_lane_profiles(self, body: dict):
        """这条 lane 现有的 profile（给"复制自"下拉框用）。"""
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        view = core.lane_profiles_view(cfg, lane)
        return self._json({
            "lane": lane,
            "default": core.default_profile_of(view),
            "profiles": [{"name": name, "onDisk": bool(v.get("onDisk"))}
                         for name, v in sorted(view.items())],
        })

    def _act_profile_add(self, body: dict):
        lane = _need_str(body, "lane")
        name = _need_str(body, "name")
        from_profile = str(body.get("fromProfile") or "").strip() or None
        try:
            core.validate_profile_name(name)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        if from_profile == name:
            raise ValueError("源 profile 和新名字不能相同")
        args = argparse.Namespace(lane=lane, name=name, from_profile=from_profile)
        return self._start(f"添加 profile {lane}/{name}",
                           lambda: core.cmd_profile_add(core.load_config(), args))

    # ③ 凭据：按条目复制 API key ─────────────────────────────────────────────
    def _act_credentials(self, body: dict):
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        lanes = [n for n, d in (cfg.get("lanes") or {}).items() if isinstance(d, dict)]
        home = str(((cfg.get("lanes") or {}).get(lane) or {}).get("home") or "")
        refs = core.read_credential_refs(home)
        # **只回键名**：refs 的值就是真正的密钥，界面从来只显示条目名（＝经典界面的做法）
        return self._json({
            "lane": lane, "lanes": lanes,
            "path": str(core.credentials_path(home)),
            "keys": sorted(refs),
        })

    def _act_copy_key(self, body: dict):
        source = _need_str(body, "source")
        dest = _need_str(body, "dest")
        if source == dest:
            raise ValueError("源和目标是同一条 lane")
        only = body.get("only")
        if isinstance(only, list):
            only = ",".join(str(x).strip() for x in only if str(x).strip())
        only = str(only or "").strip()
        if not only:
            raise ValueError("至少勾选一个条目（或换一个源）")
        args = argparse.Namespace(source=source, dest=dest, only=only)
        return self._start(f"复制凭据 {source} → {dest}",
                           lambda: core.cmd_copy_key(core.load_config(), args))

    # ④ 插件库：读取已装插件 + 跨 profile / 跨版本搬运 ────────────────────────
    def _act_plugin_list(self, body: dict):
        lane = _need_str(body, "lane")
        profile = str(body.get("profile") or core.DEFAULT_PROFILE).strip() or core.DEFAULT_PROFILE
        cfg = core.load_config()
        view = core.lane_profiles_view(cfg, lane)
        try:
            plugins = _jsonable(core.profile_plugins(cfg, lane, profile))
            error = ""
        except Exception as exc:  # noqa: BLE001
            plugins, error = [], f"{type(exc).__name__}: {exc}"
        return self._json({
            "lane": lane, "profile": profile, "plugins": plugins, "error": error,
            "lanes": [n for n, d in (cfg.get("lanes") or {}).items() if isinstance(d, dict)],
            "profiles": [{"name": name, "onDisk": bool(v.get("onDisk"))}
                         for name, v in sorted(view.items())],
        })

    def _act_plugin_plan(self, body: dict):
        """搬运计划（＝经典界面的「预览计划」）：先把"会发生什么"摆出来。"""
        names = ("srcLane", "srcProfile", "dstLane", "dstProfile")
        src_lane, src_prof, dst_lane, dst_prof = (_need_str(body, key) for key in names)
        pkgs = [str(p) for p in (body.get("pkgs") or []) if str(p).strip()]
        mode = str(body.get("mode") or "install")
        plan = core.plugin_transfer_plan(core.load_config(), src_lane, src_prof,
                                         dst_lane, dst_prof, pkgs, mode)
        plan.pop("src_patch_text", None)      # 补丁层全文没必要送到浏览器
        plan.pop("patch_file", None)
        out = _jsonable(plan)
        out.update({"srcLane": src_lane, "srcProfile": src_prof,
                    "dstLane": dst_lane, "dstProfile": dst_prof, "mode": mode})
        return self._json(out)

    def _act_plugin_transfer(self, body: dict):
        names = ("srcLane", "srcProfile", "dstLane", "dstProfile")
        src_lane, src_prof, dst_lane, dst_prof = (_need_str(body, key) for key in names)
        pkgs = [str(p) for p in (body.get("pkgs") or []) if str(p).strip()]
        mode = str(body.get("mode") or "install")
        if not pkgs:
            raise ValueError("没有勾选任何插件")
        def job() -> None:
            core.transfer_plugins(core.load_config(), src_lane, src_prof,
                                  dst_lane, dst_prof, pkgs, mode)
        return self._start(f"搬运插件 → {dst_lane}/{dst_prof}", job)

    def _act_plugin_overview(self, body: dict):
        """插件总览：全部 lane × profile 各装了哪些第三方插件（只读，走日志面板）。"""
        def job() -> None:
            rows = core.plugin_inventory(core.load_config())
            core.info(core.col("── 插件总览（只读）──", core.C.BOLD))
            for row in rows:
                core.info(f"  {row['lane']}/{row['profile']}   v{row['version']}"
                          f"   第三方插件 {len(row['plugins'])} 个"
                          + ("（磁盘上没有）" if not row["onDisk"] else ""))
                if row["error"]:
                    core.warn(f"      读取失败：{row['error']}")
                    continue
                for item in row["plugins"]:
                    core.info(core.col(f"      {item['pkg']}  {item['version'] or '?'}"
                                       f"  [{item['kind']}]", core.C.GRAY))
        return self._start("插件总览", job)

    # ⑤ 插件市场（dshmarket）单独开关 / 更新 ─────────────────────────────────
    def _act_market_get(self, body: dict):
        """读插件市场现状（顺带查一次 registry —— 会联网，所以前端要显示加载态）。"""
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        try:
            st = core.market_status(cfg, lane, with_registry=True)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        package_on_disk = Path(st.get("package_dir")).is_dir()
        snaps = st.get("snapshots") or []
        return self._json({
            "ok": True, "lane": lane,
            "installed": str(st.get("installed") or ""),
            "spec": str(st.get("spec") or ""),
            "rows": [str(r) for r in (st.get("rows") or [])],
            "disabled": [str(r) for r in core.market_disabled_rows(st)],
            "latest": st.get("latest") or "",
            "registryError": str(st.get("registry_error") or ""),
            "registry": str(st.get("registry") or ""),
            "packageOnDisk": bool(package_on_disk),
            "generalBlock": bool(st.get("general_block")),
            "snapshots": [{"name": str(s.get("name") or ""),
                           "version": str(s.get("version") or "")} for s in snaps],
        })

    def _act_market_off(self, body: dict):
        lane = _need_str(body, "lane")
        boot = bool(body.get("bootCheck", True))
        args = argparse.Namespace(lane=lane, no_verify=False, no_boot_check=not boot)
        return self._start(f"关掉插件市场：{lane}",
                           lambda: core.cmd_market_off(core.load_config(), args))

    def _act_market_on(self, body: dict):
        lane = _need_str(body, "lane")
        args = argparse.Namespace(lane=lane, no_verify=False)
        return self._start(f"打开插件市场：{lane}",
                           lambda: core.cmd_market_on(core.load_config(), args))

    def _act_market_update(self, body: dict):
        lane = _need_str(body, "lane")
        boot = bool(body.get("bootCheck", True))
        version = str(body.get("version") or "").strip() or None
        args = argparse.Namespace(lane=lane, version=version, anyway=bool(body.get("anyway")),
                                  force=False, offline=False, no_verify=False,
                                  no_boot_check=not boot)
        return self._start(f"更新插件市场：{lane}",
                           lambda: core.cmd_market_update(core.load_config(), args))

    # ⑥ 备份对话 ────────────────────────────────────────────────────────────
    def _act_chat_list(self, body: dict):
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        try:
            now = core.count_sessions(core.lane_home(cfg, lane))
        except Exception:  # noqa: BLE001
            now = 0
        items = []
        for it in core.chat_snapshots(cfg, lane):
            size = int(it.get("size") or 0)
            items.append({
                "name": str(it.get("name") or ""),
                "created": str(it.get("created") or ""),
                "sessions": int(it.get("sessions") or 0),
                "version": str(it.get("version") or ""),
                "running": bool(it.get("running")),
                "note": str(it.get("note") or ""),
                "ok": bool(it.get("ok")),
                "size": size, "human": core.human_size(size),
            })
        return self._json({
            "lane": lane, "snapshots": items, "current": int(now),
            "totalHuman": core.human_size(sum(i["size"] for i in items)),
            "running": bool(core.lane_any_running(cfg, lane)),
        })

    def _act_chat_backup(self, body: dict):
        lane = _need_str(body, "lane")
        name = str(body.get("name") or "").strip() or None
        args = argparse.Namespace(lane=lane, name=name)
        return self._start(f"备份对话：{lane}",
                           lambda: core.cmd_chat_backup(core.load_config(), args))

    def _act_chat_restore(self, body: dict):
        lane = _need_str(body, "lane")
        name = _need_str(body, "name")
        # stop=True / yes=True：确认已经在界面上做过了（弹窗里写了"会先停掉它、会另存现状"）
        args = argparse.Namespace(lane=lane, name=name, stop=True, yes=True, force=False)
        return self._start(f"恢复对话：{lane}",
                           lambda: core.cmd_chat_restore(core.load_config(), args))

    def _act_chat_rm(self, body: dict):
        lane = _need_str(body, "lane")
        name = _need_str(body, "name")
        args = argparse.Namespace(lane=lane, name=name, yes=True)
        return self._start(f"删除备份：{lane}",
                           lambda: core.cmd_chat_rm(core.load_config(), args))

    # ⑥b 配置备份（轻量：插件清单 + 会话 + 设置；恢复时自动联网拉回插件）────────
    def _act_cfg_plan(self, body: dict):
        """备份前预览：会包含哪些 profile/插件/会话/设置/可选数据目录，以及体积。"""
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        plan = core.cfg_backup_plan(
            cfg, lane,
            sessions=bool(body.get("sessions", True)),
            attachments=bool(body.get("attachments", True)),
            settings=bool(body.get("settings", True)),
            credentials=bool(body.get("credentials", False)),
            plugin_data=body.get("pluginData") or [],
        )
        if not plan.get("ok"):
            raise ValueError(plan.get("why") or "无法生成备份计划")
        return self._json({
            "ok": True,
            "lane": lane,
            "dshVersion": plan["dshVersion"],
            "profiles": [{"name": p["name"], "thirdParty": p["thirdParty"],
                          "missingFiles": p["missingFiles"]} for p in plan["profiles"]],
            "sessions": {"files": plan["sessions"]["files"],
                         "human": core.human_size(plan["sessions"]["bytes"])},
            "attachments": {"files": plan["attachments"]["files"],
                            "human": core.human_size(plan["attachments"]["bytes"])},
            "settingsFiles": [{"name": f["name"], "human": core.human_size(f["bytes"])}
                              for f in plan["settingsFiles"]],
            "credentials": plan["credentials"],
            "localFiles": [{"pkg": x["pkg"], "bytes": x["bytes"],
                            "human": core.human_size(x["bytes"])} for x in plan["localFiles"]],
            "dataDirs": [{"name": d["name"], "human": core.human_size(d["bytes"]),
                          "bytes": d["bytes"]} for d in plan["dataDirs"]],
            "totalHuman": core.human_size(plan["totalBytes"]),
        })

    def _act_cfg_backup(self, body: dict):
        lane = _need_str(body, "lane")
        name = str(body.get("name") or "").strip() or None
        args = argparse.Namespace(
            lane=lane, name=name, yes=True,
            no_sessions=not bool(body.get("sessions", True)),
            no_attachments=not bool(body.get("attachments", True)),
            no_settings=not bool(body.get("settings", True)),
            credentials=bool(body.get("credentials", False)),
            data=body.get("pluginData") or [],
            profiles=None,
        )
        return self._start(f"配置备份：{lane}",
                           lambda: core.cmd_cfg_backup(core.load_config(), args))

    def _act_cfg_list(self, body: dict):
        cfg = core.load_config()
        items = core.cfg_list_backups(cfg)
        clean = []
        for item in items:
            item = dict(item)
            item.pop("path", None)       # Path 不能 JSON 序列化
            item["human"] = core.human_size(item["bytes"])
            clean.append(item)
        return self._json({"ok": True, "items": clean,
                           "dir": str(core.cfg_backup_root(cfg))})

    def _act_cfg_show(self, body: dict):
        name = _need_str(body, "name")
        cfg = core.load_config()
        path = core.cfg_resolve_backup(cfg, name)
        manifest = core.cfg_read_manifest(path)
        summary = core.cfg_summary(manifest)
        return self._json({
            "ok": True, "name": path.name, "path": str(path),
            "human": core.human_size(path.stat().st_size),
            "created": summary["created"], "source": summary["source"],
            "profiles": [{"name": p["name"], "officialCount": p["officialCount"],
                          "plugins": [{"pkg": x.get("pkg"),
                                       "version": x.get("version") or x.get("spec") or "?",
                                       "kind": x.get("kind"),
                                       "disabled": bool(x.get("disabledRows")),
                                       "official": bool(x.get("official")),
                                       "local": bool(x.get("localSpec"))}
                                      for x in p["thirdParty"]]}
                         for p in summary["profiles"]],
            "sessions": {"included": summary["sessions"].get("included"),
                         "files": summary["sessions"].get("files", 0),
                         "human": core.human_size(summary["sessions"].get("bytes", 0))},
            "attachments": {"included": summary["attachments"].get("included"),
                            "files": summary["attachments"].get("files", 0),
                            "human": core.human_size(summary["attachments"].get("bytes", 0))},
            "settings": {"included": summary["settings"].get("included"),
                         "files": [f.get("name") for f in summary["settings"].get("files") or []]},
            "credentials": {"included": summary["credentials"].get("included"),
                            "entries": summary["credentials"].get("entries") or []},
            "localFiles": [{"pkg": x.get("pkg"), "sourcePath": x.get("sourcePath"),
                            "human": core.human_size(x.get("bytes", 0))}
                           for x in summary["localFiles"]],
            "pluginData": [{"name": x.get("name"), "files": x.get("files")}
                           for x in summary["pluginData"]],
        })

    def _act_cfg_rm(self, body: dict):
        name = _need_str(body, "name")
        cfg = core.load_config()
        path = core.cfg_resolve_backup(cfg, name)
        path.unlink()
        return self._json({"ok": True, "name": path.name})

    def _act_cfg_plugin_rm(self, body: dict):
        name = _need_str(body, "name")
        plugins = body.get("plugins") or []
        if not plugins:
            raise ValueError("没给要删的插件")
        cfg = core.load_config()
        path = core.cfg_resolve_backup(cfg, name)
        result = core.cfg_plugin_remove(path, [str(p) for p in plugins],
                                        profile=str(body.get("profile") or ""))
        return self._json({"ok": True, "removed": result["removed"]})

    def _act_cfg_plugin_add(self, body: dict):
        name = _need_str(body, "name")
        profile = _need_str(body, "profile")
        plugins = body.get("plugins") or []
        if not plugins:
            raise ValueError("没给要加的插件（pkg@spec）")
        cfg = core.load_config()
        path = core.cfg_resolve_backup(cfg, name)
        from_lane = str(body.get("fromLane") or "")
        if from_lane and from_lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{from_lane}」")
        result = core.cfg_plugin_add(
            path, profile, [str(p) for p in plugins],
            src_cfg=cfg, src_lane=from_lane,
            src_profile=str(body.get("fromProfile") or ""))
        return self._json({"ok": True, "added": result["added"]})

    def _act_cfg_restore_plan(self, body: dict):
        name = _need_str(body, "name")
        cfg = core.load_config()
        path = core.cfg_resolve_backup(cfg, name)
        manifest = core.cfg_read_manifest(path)
        target = str(body.get("target") or "")
        new_lane = str(body.get("newLane") or "")
        if new_lane and new_lane not in (cfg.get("lanes") or {}) and \
                not core.LANE_NAME_RE.match(new_lane):
            raise ValueError("lane 名只能用字母/数字/._-，且以字母或数字开头（最长 32）")
        plan = core.cfg_restore_plan(
            cfg, manifest, target=target, new_lane=new_lane,
            version=str(body.get("version") or ""),
            profiles=body.get("profiles") or None,
            sessions=bool(body.get("sessions", True)),
            settings=bool(body.get("settings", True)),
            credentials=bool(body.get("credentials", True)),
            plugin_data=bool(body.get("pluginData", True)),
        )
        return self._json({"ok": plan["ok"], "problems": plan["problems"],
                           "mode": plan["mode"], "target": plan["target"],
                           "version": plan["version"],
                           "sourceVersion": plan["source"].get("dshVersion") or "",
                           "profiles": plan["profiles"],
                           "plugins": [{"profile": x["profile"], "pkg": x["pkg"],
                                        "want": x["want"], "local": x["local"]}
                                       for x in plan["thirdParty"]],
                           "sessions": {"included": bool(plan["sessions"].get("included")),
                                        "files": plan["sessions"].get("files", 0)},
                           "settingsCount": len(plan["settings"]),
                           "credentialsIncluded": bool(plan["credentials"].get("included")),
                           "pluginDataCount": len(plan["pluginData"]),
                           "running": ({"pid": plan["running"].get("pid"),
                                        "port": plan["running"].get("port")}
                                       if plan["running"] else None),
                           "lanes": sorted((cfg.get("lanes") or {}).keys())})

    def _act_cfg_restore(self, body: dict):
        name = _need_str(body, "name")
        cfg = core.load_config()
        path = core.cfg_resolve_backup(cfg, name)
        target = str(body.get("target") or "")
        new_lane = str(body.get("newLane") or "")
        if not target and not new_lane:
            raise ValueError("给一个已存在的目标 lane，或填要新建的 lane 名")
        title = f"配置恢复：{path.name} → {new_lane or target}"

        def job() -> None:
            fresh = core.load_config()
            path2 = core.cfg_resolve_backup(fresh, name)
            core.cfg_restore(
                fresh, path2, target=target, new_lane=new_lane,
                version=str(body.get("version") or ""),
                profiles=body.get("profiles") or None,
                use_sessions=bool(body.get("sessions", True)),
                use_settings=bool(body.get("settings", True)),
                use_credentials=bool(body.get("credentials", True)),
                use_plugin_data=bool(body.get("pluginData", True)),
                stop=True,
            )
        return self._start(title, job)

    # ⑦ 复制这套 DSH ────────────────────────────────────────────────────────
    def _act_clone_plan(self, body: dict):
        """复制方案 + 体积估算。体积要全盘遍历（几百毫秒），所以是独立的一次请求，
        前端开着加载态等它（和经典界面把 fill_size 放后台线程是同一个理由）。

        `profiles` 只列要一起复制的 profile；不传 / null ＝源 HOME 里全部。
        前端每改一次勾选就重算一次，所以体积只算选中的那部分。
        """
        lane = _need_str(body, "lane")
        new = str(body.get("name") or "").strip() or f"{lane}-copy"
        profiles = _need_profiles(body)
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        try:
            plan = core.lane_clone_plan(cfg, lane, new, sizes=True, profiles=profiles)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        home_size = int(plan.get("home_size") or 0)
        install_size = int(plan.get("install_size") or 0)
        return self._json({
            "ok": True, "name": new, "exists": new in (cfg.get("lanes") or {}),
            "homeSize": home_size, "installSize": install_size,
            "homeSizeAll": int(plan.get("home_size_all") or 0),
            "humanTotal": core.human_size(home_size + install_size),
            "humanHome": core.human_size(home_size),
            "humanHomeAll": core.human_size(plan.get("home_size_all") or 0),
            "humanInstall": core.human_size(install_size),
            "free": core.human_size(plan["free"]) if plan.get("free") else "",
            "home": str(plan.get("home")), "install": str(plan.get("install")),
            "profiles": [str(x) for x in (plan.get("profiles") or [])],
            "profilesSelected": [str(x) for x in (plan.get("profiles_selected") or [])],
            "profilesSkipped": [str(x) for x in (plan.get("profiles_skipped") or [])],
            "problems": [str(p) for p in (plan.get("problems") or [])],
        })

    def _act_clone(self, body: dict):
        lane = _need_str(body, "lane")
        new = _need_str(body, "name")
        if new == lane:
            raise ValueError("新 lane 名不能和源一样")
        if not core.LANE_NAME_RE.match(new):
            raise ValueError("名称只能用字母/数字/._-，且以字母或数字开头（最长 32 字符）")
        cfg = core.load_config()
        if new in (cfg.get("lanes") or {}):
            raise ValueError(f"lane「{new}」已经存在了")
        profiles = _need_profiles(body)
        # 起任务之前先把"一个都没选"挡掉：源里明明有 profile 却一个都不带，几乎一定是点错了
        plan = core.lane_clone_plan(cfg, lane, new, sizes=False, profiles=profiles)
        if plan["profiles"] and not plan["profiles_selected"]:
            raise ValueError("一个 profile 都没选 —— 至少留一个（不想挑就全选）")
        args = argparse.Namespace(source=lane, lane=new, port=None, profiles=profiles)
        return self._start(f"复制 {lane} → {new}",
                           lambda: core.cmd_clone(core.load_config(), args))

    # ⑧ 升级版本 / 核对隔离 ─────────────────────────────────────────────────
    def _act_upgrade_plan(self, body: dict):
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        data = (cfg.get("lanes") or {}).get(lane)
        if not isinstance(data, dict):
            raise ValueError(f"没有 lane「{lane}」")
        plan = core.lane_upgrade_plan(cfg, lane)
        running = core.lane_any_running(cfg, lane)
        return self._json({
            "ok": True, "lane": lane,
            "version": str(data.get("version") or "?"),
            "previousVersion": str(data.get("previousVersion") or ""),
            "mode": plan["mode"], "dir": str(plan["dir"]), "why": plan["why"],
            "external": plan["mode"] == "external",
            "running": ({"pid": running.get("pid"), "port": running.get("port")}
                        if running else None),
            "registry": core.registry_choice_of(cfg),
        })

    def _act_upgrade(self, body: dict):
        lane = _need_str(body, "lane")
        rollback = bool(body.get("rollback"))
        dry_run = bool(body.get("dryRun"))
        version = str(body.get("version") or "").strip()
        if not rollback and not version:
            raise ValueError("先填版本号（例如 0.1.7-rc.2 / latest）")
        if not dry_run:
            self._save_registry(body)
        args = argparse.Namespace(lane=lane, version=(None if rollback else version),
                                  rollback=rollback, stop=True, no_boot_check=False,
                                  dry_run=dry_run, registry=str(body.get("registry") or "auto"))
        title = f"升级 {lane}" + ("（只看会做什么）" if dry_run else "")
        return self._start(title, lambda: core.cmd_upgrade(core.load_config(), args))

    def _act_verify(self, body: dict):
        lane = _need_str(body, "lane")
        args = argparse.Namespace(lane=lane)
        return self._start(f"核对隔离：{lane}",
                           lambda: core.cmd_verify(core.load_config(), args))

    # ⑨ 删除这套 DSH ────────────────────────────────────────────────────────
    def _act_delete_plan(self, body: dict):
        """删除范围（不含体积：那是全盘遍历，走 delete_sizes 另收）。"""
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        plan = core.lane_delete_plan(cfg, lane, sizes=False)
        rt = core.lane_any_running(cfg, lane)
        return self._json({
            "ok": True, "lane": lane,
            "version": str(plan.get("version") or "?"),
            "profiles": [str(p) for p in (plan.get("profiles") or [])],
            "adopted": bool(plan.get("adopted")),
            "kind": str(plan.get("kind") or ""),
            "home": str(plan.get("home")), "install": str(plan.get("install")),
            "homeInside": bool(plan.get("home_inside")),
            "installInside": bool(plan.get("install_inside")),
            "sharedWith": [str(n) for n in (plan.get("shared_with") or [])],
            "runs": [str(p) for p in (plan.get("runs") or []) if Path(p).is_file()],
            "logs": [str(p) for p in (plan.get("logs") or []) if Path(p).is_file()],
            "running": ({"pid": rt.get("pid"), "port": rt.get("port")} if rt else None),
        })

    def _act_delete_sizes(self, body: dict):
        """体积是**单独一步**：目录可能上 GB，别把对话框的首屏卡在这里。"""
        lane = _need_str(body, "lane")
        cfg = core.load_config()
        if lane not in (cfg.get("lanes") or {}):
            raise ValueError(f"没有 lane「{lane}」")
        plan = core.lane_delete_plan(cfg, lane, sizes=False)
        home, install = Path(plan["home"]), Path(plan["install"])
        home_size = core.dir_size(home) if home.is_dir() else 0
        install_size = core.dir_size(install) if install.is_dir() else 0
        return self._json({"ok": True,
                           "home": core.human_size(home_size),
                           "install": core.human_size(install_size)})

    def _act_delete(self, body: dict):
        lane = _need_str(body, "lane")
        # yes=True：二次确认（手打 lane 名）已经在界面上做过了，和经典界面一致
        args = argparse.Namespace(lane=lane, yes=True, stop=bool(body.get("stop")),
                                  keep_home=bool(body.get("keepHome")),
                                  keep_install=bool(body.get("keepInstall")),
                                  home_too=bool(body.get("homeToo")))

        def job() -> None:
            core.cmd_delete(core.load_config(), args)
            core.invalidate_tcp_cache()      # 让下一次刷新重新发现实例

        return self._start(f"删除 {lane}", job)

    ACTIONS = {
        "open": _act_open,
        "stop": _act_stop,
        "stop_all": _act_stop_all,
        "tail_log": _act_tail_log,
        "profile_rm": _act_profile_rm,
        "kill": _act_kill,
        "adopt_plan": _act_adopt_plan,
        "adopt": _act_adopt,
        "open_path": _act_open_path,
        "open_url": _act_open_url,
        "open_repo": _act_open_repo,
        "refresh": _act_refresh,
        "doctor": _act_doctor,
        "theme": _act_theme,
        "geometry": _act_geometry,
        "log_clear": _act_log_clear,
        "bye": _act_bye,
        "client_error": _act_client_error,
        "client_metric": _act_client_metric,
        # 第二批
        "registry_versions": _act_registry_versions,
        "create": _act_create,
        "lane_profiles": _act_lane_profiles,
        "profile_add": _act_profile_add,
        "credentials": _act_credentials,
        "copy_key": _act_copy_key,
        "plugin_list": _act_plugin_list,
        "plugin_plan": _act_plugin_plan,
        "plugin_transfer": _act_plugin_transfer,
        "plugin_overview": _act_plugin_overview,
        "market_get": _act_market_get,
        "market_off": _act_market_off,
        "market_on": _act_market_on,
        "market_update": _act_market_update,
        "chat_list": _act_chat_list,
        "chat_backup": _act_chat_backup,
        "chat_restore": _act_chat_restore,
        "chat_rm": _act_chat_rm,
        "cfg_plan": _act_cfg_plan,
        "cfg_backup": _act_cfg_backup,
        "cfg_list": _act_cfg_list,
        "cfg_show": _act_cfg_show,
        "cfg_rm": _act_cfg_rm,
        "cfg_plugin_add": _act_cfg_plugin_add,
        "cfg_plugin_rm": _act_cfg_plugin_rm,
        "cfg_restore_plan": _act_cfg_restore_plan,
        "cfg_restore": _act_cfg_restore,
        "clone_plan": _act_clone_plan,
        "clone": _act_clone,
        "upgrade_plan": _act_upgrade_plan,
        "upgrade": _act_upgrade,
        "verify": _act_verify,
        "delete_plan": _act_delete_plan,
        "delete_sizes": _act_delete_sizes,
        "delete": _act_delete,
    }


def _need_str(body: dict, key: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise ValueError(f"缺少参数 {key}")
    return value


def _need_profiles(body: dict) -> list[str] | None:
    """`profiles` 参数 → 名字列表；缺省 / null → None（＝全部）。

    None 与 [] 意思完全不同：None 是"没挑，全都复制"，[] 是"一个都不要"。
    后者几乎一定是界面上把勾全取消了，所以调用方要把它当错误报出来。
    """
    raw = body.get("profiles")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        raise ValueError("profiles 必须是数组（或逗号分隔的字符串）")
    return [str(part).strip() for part in raw if str(part).strip()]


def _jsonable(obj):
    """把核心函数返回的对象转成能过 `json.dumps` 的形状。

    为什么需要它：`lane_delete_plan` / `market_status` / `chat_snapshots` 这些直接返回
    `Path` 和 `set`（它们本来是给 tkinter 界面用的，那边什么都能塞）。不转的话
    json.dumps 抛 TypeError，浏览器收到的是 500 —— 看起来就像"这个按钮坏了"。
    """
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_jsonable(v) for v in sorted(obj, key=str)]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _parse_port(value) -> int | None:
    """端口框内容 → 端口号。空 / 「自动」＝ None（启动时实时挑空闲）。"""
    text = "" if value is None else str(value).strip()
    if not text or text == "自动":
        return None
    if not text.isdigit():
        raise ValueError(f"端口必须是数字，得到 {text!r}")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"端口超出范围：{port}")
    return port


def _find_install(app: Launcher, path: str) -> dict | None:
    want = str(path).lower()
    for item in (app.snapshot() or {}).get("installs") or []:
        if str(item.get("path", "")).lower() == want:
            return item
    return None


def _adopt_plan(cfg: dict, item: dict) -> dict:
    """把一份本机安装登记成 lane 的"预演"。与旧界面 adopt_install 的算法逐条对齐。

    为什么要有这一步：登记会写进 lanes.json，lane 名和 DSH_HOME 定下来就不好改了 ——
    所以先把"要写什么"摆给用户看清楚，确认了再动手。
    """
    source = str(item.get("source") or "")
    default_name = {"npm 全局": "global", "npx 缓存": "npx"}.get(source, "adopted")
    home = str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh")
    note = "就是你现有的那份，含已有会话 / 设置 / 插件"
    extra = ""
    if source.startswith("versions 目录"):
        # 根目录 versions\<版本> 里的孤儿安装树：lane 名沿用建名习惯，HOME 用根目录下的新目录
        version = re.sub(r"[^A-Za-z0-9]+", "-", str(item.get("version") or "")).strip("-")[:12]
        default_name = "lane-" + version
        home = str(core.paths(cfg)["homes"] / default_name)
        note = "根目录下的新 HOME；这个版本树原来自带的 HOME 若还在，刷新时会自动认领成 lane"
    elif source.startswith("clones 目录"):
        default_name = Path(str(item.get("tree") or item.get("path"))).name
        home = str(core.paths(cfg)["homes"] / default_name)
        note = "clone 布局：homes\\<同名>；HOME 不在的话会新建一个空的"
        extra = "如果它的 HOME（homes\\<同名>）也还在，直接点「刷新」就会自动认领，不用手动加。"

    already = ""
    want = str(item.get("path") or "").lower()
    for name, data in (cfg.get("lanes") or {}).items():
        if isinstance(data, dict) and str(data.get("installDir", "")).lower() == want:
            already = name
            break

    return {
        "lane": default_name,
        "home": home,
        "note": note,
        "extra": extra,
        "alreadyRegistered": already,
        "source": source,
        "version": str(item.get("version") or "?"),
        "path": str(item.get("path") or ""),
    }


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # 端口占用要**明确失败**，不能悄悄绑上第二个。
    # Windows 的 SO_REUSEADDR 语义和 Unix 不一样：它允许两个进程绑同一个地址:端口，
    # 于是"上一个启动器还在跑"时新实例照样绑得上，两个服务并存、令牌各说各话
    # （2026-10-16 踩到：新实例的地址拿到 403，因为应答的是旧实例）。
    # 关掉它，占用时直接抛 OSError，由 main() 报清楚。
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address) -> None:
        """客户端半路断开（关窗口、刷新页面）是常态，别把 traceback 刷满 stderr。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        bound = getattr(self, "RequestHandlerClass", None)
        if bound is not None and getattr(bound, "dev", False):
            traceback.print_exc()


def make_server(host: str, port: int, app: Launcher) -> Server:
    # 每个服务器一份"绑定了 app"的处理器类：BaseHTTPRequestHandler 拿不到构造参数，
    # 只能这样把它自己的实例和上层应用对上。
    handler = type("BoundHandler", (Handler,), {"app": app, "dev": app.dev})
    return Server((host, port), handler)


# ======================= 拉起窗口 =======================


def browser_candidates() -> list[str]:
    out: list[str] = []
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    for cand in (
        Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pf86) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe" if local else None,
    ):
        if cand and cand.is_file():
            out.append(str(cand))
    return out


def profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "dsh-lanes" / "webview-profile"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def launch_window(url: str, cfg: dict, exe: str | None, dev: bool) -> str:
    """用 --app 模式开一个无边框窗口（没有地址栏、没有标签页）。

    为什么要独立 user-data-dir：不这么干的话，Edge 已经在跑时会直接把地址丢给你**日常
    那个窗口**，变成一个新标签页——那就完全没有"应用窗口"的意思了。代价是这个目录下
    会有一份浏览器数据（放在 %LOCALAPPDATA%，不进项目目录）。
    """
    ui = cfg.get("ui") or {}
    w, h = int(ui.get("w") or 0), int(ui.get("h") or 0)
    x, y = int(ui.get("x") or 0), int(ui.get("y") or 0)
    if w < 400 or h < 300:
        w, h = 1200, 780
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    if exe:
        args = [
            exe, f"--app={url}", f"--user-data-dir={profile_dir()}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            f"--window-size={w},{h}",
        ]
        if x or y:
            args.append(f"--window-position={x},{y}")
        if dev:
            args.append("--auto-open-devtools-for-tabs")
        try:
            subprocess.Popen(args, close_fds=True, creationflags=flags)
            return f"窗口已拉起：{Path(exe).name}（--app 模式）"
        except Exception as exc:  # noqa: BLE001
            return f"[!!] 拉起 {Path(exe).name} 失败：{exc}　→ 改用系统浏览器"
    try:
        webbrowser.open(url)          # 退路：有地址栏，但至少能用
        return "[!!] 没找到 Edge/Chrome，已用系统默认浏览器打开（会带地址栏）"
    except Exception as exc:  # noqa: BLE001
        return f"[!!] 打开浏览器失败：{exc}　请手动访问 {url}"


# ======================= 入口 =======================


def run_classic(rest: list[str]) -> int:
    """--classic：把活儿交给旧的 tkinter 界面（同一个核心，行为一致）。"""
    frozen = getattr(sys, "frozen", False)
    if not frozen:
        target = HERE / "dsh_lanes_gui.py"
        if not target.is_file():
            print(f"[XX] 找不到旧界面 {target}", file=sys.stderr)
            return 1
        try:
            return subprocess.call([sys.executable, str(target), *rest])
        except Exception as exc:  # noqa: BLE001
            print(f"[XX] 启动旧界面失败：{exc}", file=sys.stderr)
            return 1
    exe = Path(sys.executable).resolve().parent / "dsh-lanes-gui.exe"
    if exe.is_file():
        return subprocess.call([str(exe), *rest])
    print("[XX] --classic 需要旧界面：源码运行时用 py dsh_lanes_gui.py；"
          "打包后需要同目录下的 dsh-lanes-gui.exe", file=sys.stderr)
    return 1


def selfcheck() -> int:
    """服务器自检：**只用临时配置**，绝不碰你真实的 lanes.json。

    为什么强调这一点：lanes.json 在源码运行时就在脚本目录，是**本机配置**。测试里一旦
    走到 save_config，用户的登记表就被写成测试数据了（2026-10-16 踩过一次）。这里把
    core.CONFIG_PATH 指到临时目录，所有读写都落在临时的那份上。
    """
    import tempfile
    import urllib.error

    print("=== dsh-lanes Web 后端自检（使用临时配置，不动真实 lanes.json）===")
    tmp = Path(tempfile.mkdtemp(prefix="dsh-web4selfcheck-"))
    real_config = core.CONFIG_PATH
    core.CONFIG_PATH = tmp / "lanes.json"
    cfg = core.load_config()
    cfg["root"] = str(tmp / "root")
    cfg["lanes"] = {}
    cfg["ui"] = {"w": 1200, "h": 780, "x": 10, "y": 20, "theme": "light"}
    core.save_config(cfg)
    core.ensure_layout(cfg)      # 临时根目录下把 versions/homes/clones/logs 建出来，贴近真实环境

    app = Launcher(secrets.token_hex(16), stay=True)
    httpd = make_server("127.0.0.1", 0, app)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=app.hub._flush_loop, args=(app._stop,), daemon=True).start()
    threading.Thread(target=app._ticker, daemon=True).start()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK' if ok else 'XX'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = []

    def call(method: str, path: str, body=None, headers=None, use_opener=True):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if data is not None and not (headers or {}).get("Content-Type"):
            req.add_header("Content-Type", "application/json")
        sender = opener.open if use_opener else urllib.request.urlopen
        try:
            with sender(req, timeout=15) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw.decode("utf-8"))
                except ValueError:
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except ValueError:
                return exc.code, raw

    token = app.token
    try:
        # ① 鉴权
        status, _ = call("GET", "/", use_opener=False)
        check("无 token 打开页面 → 403", status == 403, f"HTTP {status}")
        status, _ = call("GET", "/?token=" + "0" * 32, use_opener=False)
        check("错误 token → 403", status == 403, f"HTTP {status}")
        status, body = call("GET", "/?token=" + token)
        ok_page = status == 200 and isinstance(body, bytes) and b"<html" in body.lower()
        check("正确 token → 200 且返回页面", ok_page, f"HTTP {status}, cookie={len(jar)} 个")
        check("页面响应下发了 cookie", len(jar) > 0)

        # ② Host 校验（伪造 Host 挡住 DNS rebinding）
        status, _ = call("GET", "/api/state", headers={"Host": "evil.example.com"})
        check("伪造 Host → 403", status == 403, f"HTTP {status}")

        # ③ 写操作鉴权
        status, _ = call("POST", "/api/theme", {"theme": "dark"}, use_opener=False)
        check("无 token 的写操作 → 403", status == 403, f"HTTP {status}")
        status, _ = call("POST", "/api/theme", None,
                         headers={"Content-Type": "text/plain"}, use_opener=False)
        check("写操作非 JSON 类型 → 403/415", status in (403, 415), f"HTTP {status}")
        status, _ = call("POST", "/api/theme", {"theme": "dark"},
                         headers={"Origin": "http://evil.example.com"})
        check("跨站来源的写操作 → 403", status == 403, f"HTTP {status}")

        # ④ 静态资源与目录穿越
        status, body = call("GET", "/static/style.css")
        check("静态 CSS 可取", status == 200, f"HTTP {status}")
        status, _ = call("GET", "/static/../dsh_lanes.py")
        check("目录穿越被挡", status in (403, 404), f"HTTP {status}")
        status, _ = call("GET", "/static/dsh_lanes.py")
        check("非白名单文件被挡", status == 404, f"HTTP {status}")

        # ⑤ 状态字段
        deadline = time.time() + 40
        state = {}
        while time.time() < deadline:
            status, state = call("GET", "/api/state")
            if isinstance(state, dict) and state.get("ready"):
                break
            time.sleep(0.3)
        need = {"ready", "root", "theme", "ui", "lanes", "foreign", "installs",
                "counts", "busy", "remote", "rootWritable"}
        check("/api/state 字段齐全", isinstance(state, dict) and need <= set(state),
              f"缺 {sorted(need - set(state or {}))}" if isinstance(state, dict) else "非 JSON")
        check("/api/state 的 root 是临时目录", str(state.get("root", "")) == cfg["root"],
              str(state.get("root")))

        # ⑥ SSE：hello + 主题切换后收到 state
        events: list[str] = []
        req = urllib.request.Request(base + "/api/events")
        req.add_header("Cookie", f"dsh_token={token}")
        conn = urllib.request.urlopen(req, timeout=30)
        def reader():
            try:
                for raw in conn:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
                    if len(events) > 40:
                        break
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + 20
        # 两条分开等：读到 hello 不代表 state 那一帧已经被这个读线程追加进列表了
        # （以前共用一个 deadline 再立刻断言，偶发会看到"只有 hello"）
        while time.time() < deadline and "hello" not in events:
            time.sleep(0.1)
        check("SSE 收到 hello", "hello" in events, f"事件：{events[:6]}")
        while time.time() < deadline and "state" not in events:
            time.sleep(0.1)
        check("SSE 首屏带 state", "state" in events, f"事件：{events[:6]}")

        events.clear()
        status, body = call("POST", "/api/theme", {"theme": "dark"})
        deadline = time.time() + 10
        while time.time() < deadline and "state" not in events:
            time.sleep(0.1)
        check("切主题后 SSE 推到 state", "state" in events, f"HTTP {status}，事件：{events[:6]}")
        saved = json.loads((tmp / "lanes.json").read_text(encoding="utf-8"))
        check("主题写进了（临时的）配置", (saved.get("ui") or {}).get("theme") == "dark",
              str((saved.get("ui") or {}).get("theme")))

        # ⑦ 几何
        status, _ = call("POST", "/api/geometry", {"w": 1024, "h": 700, "x": 5, "y": 6})
        saved = json.loads((tmp / "lanes.json").read_text(encoding="utf-8"))
        ui = saved.get("ui") or {}
        check("几何写进配置且没弄丢 theme",
              (ui.get("w"), ui.get("h"), ui.get("theme")) == (1024, 700, "dark"),
              str(ui))

        # ⑧ 端口解析规则
        check("端口空＝自动", _parse_port("") is None and _parse_port("自动") is None)
        check("端口数字可用", _parse_port("3080") == 3080)
        try:
            _parse_port("80a")
            check("端口非数字被拒", False, "没抛异常")
        except ValueError:
            check("端口非数字被拒", True)

        # ⑨ 客户端异常上报能进日志
        call("POST", "/api/client_error", {"message": "自检用的假异常", "where": "selfcheck"})
        time.sleep(0.3)
        check("前端异常上报进了日志",
              any("自检用的假异常" in ln["text"] for ln in app.hub.tail(50)))

        # ⑩ 忙碌互斥（不开真任务，直接占位）
        app.busy = True
        status, body = call("POST", "/api/open", {"lane": "x", "profile": "web"})
        check("忙碌时拒绝新任务 → 409", status == 409, f"HTTP {status}")
        app.busy = False

        # ⑪ 未知操作与不存在的方法
        status, _ = call("POST", "/api/nope", {})
        check("未知操作 → 404", status == 404, f"HTTP {status}")
        status, _ = call("GET", "/api/nope")
        check("未知路径 → 404", status == 404, f"HTTP {status}")

        # ⑫ 第二批：注册一条临时 lane，把只读的"计划"接口逐个走一遍（一个字节都不写真实配置）
        tmp_lane = "t1"
        home = tmp / "root" / "homes" / tmp_lane
        (home / "profiles" / "web").mkdir(parents=True, exist_ok=True)
        # 让 web profile 在磁盘上真的存在（disk_profiles 认的是有 package.json 的目录）：
        # 这样 plugin_* 这些接口才会走到"读清单"那一步，而不是提前被"profile 不在磁盘上"挡掉
        (home / "profiles" / "web" / "package.json").write_text(
            '{"name": "profile-web", "dependencies": {}}', encoding="utf-8")
        (home / ".credentials.yaml").write_text(
            "version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-selfcheck-not-a-real-key\n",
            encoding="utf-8")
        cfg2 = json.loads((tmp / "lanes.json").read_text(encoding="utf-8"))
        cfg2.setdefault("lanes", {})[tmp_lane] = {
            "version": "0.1.7-rc.2", "home": str(home), "createdAt": "2026-10-17",
        }
        (tmp / "lanes.json").write_text(json.dumps(cfg2, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

        status, body = call("POST", "/api/lane_profiles", {"lane": tmp_lane})
        check("lane_profiles 给出 profile 列表",
              status == 200 and [p["name"] for p in body.get("profiles", [])] == ["web"],
              str(body)[:140])
        status, _ = call("POST", "/api/lane_profiles", {"lane": "nope"})
        check("lane_profiles 未知 lane → 400", status == 400, f"HTTP {status}")

        status, body = call("POST", "/api/credentials", {"lane": tmp_lane})
        blob = json.dumps(body, ensure_ascii=False)
        check("credentials 列出条目名",
              status == 200 and body.get("keys") == ["DEEPSEEK_API_KEY"], blob[:160])
        # 这条是安全断言：refs 的**值**就是真密钥，界面从来只显示条目名
        check("credentials 不回密钥值", "sk-selfcheck-not-a-real-key" not in blob, blob[:200])

        status, body = call("POST", "/api/chat_list", {"lane": tmp_lane})
        check("chat_list 空备份 + 会话数",
              status == 200 and body.get("snapshots") == [] and body.get("current") == 0,
              str(body)[:140])

        status, body = call("POST", "/api/delete_plan", {"lane": tmp_lane})
        need_plan = {"lane", "version", "profiles", "home", "install", "homeInside",
                     "installInside", "adopted", "sharedWith", "runs", "logs", "running"}
        check("delete_plan 字段齐全",
              status == 200 and need_plan <= set(body or {}),
              f"缺 {sorted(need_plan - set(body or {}))}" if isinstance(body, dict) else str(body)[:120])
        check("delete_plan 认出 HOME 在根目录内", body.get("homeInside") is True,
              str(body.get("home")))
        status, body = call("POST", "/api/delete_sizes", {"lane": tmp_lane})
        check("delete_sizes 能算出体积", status == 200 and bool(body.get("home")), str(body)[:140])

        status, body = call("POST", "/api/clone_plan", {"lane": tmp_lane, "name": "t1-copy"})
        check("clone_plan 给出体积与问题清单",
              status == 200 and body.get("ok") is True and "humanTotal" in body
              and isinstance(body.get("problems"), list), str(body)[:180])

        status, body = call("POST", "/api/upgrade_plan", {"lane": tmp_lane})
        check("upgrade_plan 给出方式与当前版本",
              status == 200 and body.get("mode") == "tree" and body.get("version") == "0.1.7-rc.2"
              and body.get("external") is False, str(body)[:180])
        status, _ = call("POST", "/api/upgrade_plan", {"lane": "nope"})
        check("upgrade_plan 未知 lane → 400", status == 400, f"HTTP {status}")

        status, body = call("POST", "/api/plugin_list", {"lane": tmp_lane, "profile": "web"})
        check("plugin_list 空环境 → 空清单",
              status == 200 and body.get("plugins") == [], str(body)[:160])

        status, body = call("POST", "/api/plugin_plan",
                            {"srcLane": tmp_lane, "srcProfile": "web", "dstLane": tmp_lane,
                             "dstProfile": "web-1", "pkgs": ["nope"], "mode": "install"})
        check("plugin_plan 源里没有的插件 → 明确理由（不 500）",
              status == 200 and body.get("ok") is False and "nope" in str(body.get("why")),
              str(body)[:180])

        status, body = call("POST", "/api/plugin_plan",
                            {"srcLane": tmp_lane, "srcProfile": "web", "dstLane": tmp_lane,
                             "dstProfile": "web", "pkgs": ["nope"], "mode": "install"})
        check("plugin_plan 源＝目标 → 明确理由",
              status == 200 and body.get("ok") is False and "没有可搬" in str(body.get("why")),
              str(body)[:180])

        status, _ = call("POST", "/api/plugin_transfer",
                         {"srcLane": tmp_lane, "srcProfile": "web", "dstLane": tmp_lane,
                          "dstProfile": "web-1", "pkgs": [], "mode": "install"})
        check("plugin_transfer 没勾插件 → 400", status == 400, f"HTTP {status}")
        status, _ = call("POST", "/api/market_get", {"lane": "nope"})
        check("market_get 未知 lane → 400", status == 400, f"HTTP {status}")

        # 参数校验（都该是 400，而不是 500 —— 500 会让界面看起来"坏了"）
        for name, path, payload in (
            ("create 名称不合法", "/api/create", {"lane": "-bad", "version": "1.0.0"}),
            ("create 缺版本", "/api/create", {"lane": "ok-name", "version": ""}),
            ("profile_add 名称不合法", "/api/profile_add", {"lane": tmp_lane, "name": ".."}),
            ("profile_add 源＝新名", "/api/profile_add",
             {"lane": tmp_lane, "name": "web", "fromProfile": "web"}),
            ("copy_key 源＝目标", "/api/copy_key",
             {"source": tmp_lane, "dest": tmp_lane, "only": ["A"]}),
            ("copy_key 没勾条目", "/api/copy_key",
             {"source": tmp_lane, "dest": "other", "only": []}),
            ("chat_restore 缺 name", "/api/chat_restore", {"lane": tmp_lane}),
            ("chat_rm 缺 name", "/api/chat_rm", {"lane": tmp_lane}),
            ("clone 新名＝源", "/api/clone", {"lane": tmp_lane, "name": tmp_lane}),
            ("clone 已存在的名字", "/api/clone", {"lane": tmp_lane, "name": tmp_lane}),
            ("upgrade 缺版本", "/api/upgrade", {"lane": tmp_lane}),
            ("verify 缺 lane", "/api/verify", {}),
            ("delete_plan 未知 lane", "/api/delete_plan", {"lane": "nope"}),
        ):
            status, _ = call("POST", path, payload)
            check(f"{name} → 400", status == 400, f"HTTP {status}")

        # 忙碌互斥对第二批的动作同样成立（不开真任务，直接占位）
        app.busy = True
        status, _ = call("POST", "/api/create", {"lane": "busy-test", "version": "1.0.0"})
        check("忙碌时 create → 409", status == 409, f"HTTP {status}")
        status, _ = call("POST", "/api/verify", {"lane": tmp_lane})
        check("忙碌时 verify → 409", status == 409, f"HTTP {status}")
        app.busy = False

        # ⑬ 任务结束事件：对话框靠它决定"该重新读一遍了"。
        # 用 doctor 起一个**真任务**（它是只读体检，不会动任何东西）。
        job_events: list[str] = []
        req2 = urllib.request.Request(base + "/api/events")
        req2.add_header("Cookie", f"dsh_token={token}")
        conn2 = urllib.request.urlopen(req2, timeout=120)
        def reader2():
            try:
                for raw in conn2:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        job_events.append(line.split(":", 1)[1].strip())
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=reader2, daemon=True).start()
        deadline = time.time() + 10
        while time.time() < deadline and "hello" not in job_events:
            time.sleep(0.1)
        check("第二条 SSE 也能连上", "hello" in job_events, f"事件：{job_events[:6]}")
        status, body = call("POST", "/api/doctor", {})
        deadline = time.time() + 120
        while time.time() < deadline and "job_done" not in job_events:
            time.sleep(0.2)
        check("任务结束推到 job_done（HTTP %d）" % status, "job_done" in job_events,
              f"事件：{job_events[:10]}")
        try:
            conn2.close()
        except Exception:  # noqa: BLE001
            pass

        # ⑭ 复制：只把选中的 profile 带进副本（2026-10-17 用户要的能力）。
        # 先给它一条真有安装树的源 lane，否则 lane_clone_plan 会直接报"入口缺失"。
        src_home = tmp / "root" / "homes" / "t1"
        fake_pkg = tmp / "root" / "versions" / "0.1.7-rc.2" / "node_modules" / "@deepseek-ai" / "dsh"
        fake_pkg.mkdir(parents=True, exist_ok=True)
        (fake_pkg / "package.json").write_text(
            '{"name": "@deepseek-ai/dsh", "version": "0.1.7-rc.2"}', encoding="utf-8")
        (src_home / "sessions").mkdir(parents=True, exist_ok=True)
        (src_home / "sessions" / "s1.zstd").write_bytes(b"x" * 64)
        (src_home / "profiles" / "test").mkdir(parents=True, exist_ok=True)
        (src_home / "profiles" / "test" / "package.json").write_text(
            '{"name": "profile-test", "dependencies": {}}', encoding="utf-8")

        status, body = call("POST", "/api/clone_plan",
                            {"lane": tmp_lane, "name": "t1-part", "profiles": ["web"]})
        check("clone_plan 能只算选中的 profile",
              status == 200 and body.get("profilesSelected") == ["web"]
              and body.get("profilesSkipped") == ["test"], str(body)[:200])
        check("clone_plan 的体积只算选中的那部分（跳过的被扣掉了）",
              status == 200 and 0 < body.get("homeSize", 0) < body.get("homeSizeAll", 0),
              f"选中 {body.get('humanHome')} / 整份 {body.get('humanHomeAll')}")

        status, _ = call("POST", "/api/clone",
                         {"lane": tmp_lane, "name": "t1-none", "profiles": []})
        check("clone 一个 profile 都没选 → 400", status == 400, f"HTTP {status}")
        status, _ = call("POST", "/api/clone",
                         {"lane": tmp_lane, "name": "t1-bad", "profiles": ["nope"]})
        check("clone 选了不存在的 profile → 400", status == 400, f"HTTP {status}")

        status, body = call("POST", "/api/clone",
                            {"lane": tmp_lane, "name": "t1-part", "profiles": ["web"]})
        check("clone 起得来（HTTP %d）" % status, status == 200, str(body)[:160])
        part_home = tmp / "root" / "homes" / "t1-part"
        deadline = time.time() + 90
        while time.time() < deadline and not (part_home / "profiles" / "web").is_dir():
            time.sleep(0.3)
        check("clone 把选中的 profile 复制过来了", (part_home / "profiles" / "web").is_dir(),
              str(part_home / "profiles"))
        check("clone **没有**把没选的 profile 带过来",
              not (part_home / "profiles" / "test").exists())
        check("clone 仍然带上了 HOME 级的东西（会话 / 凭据）",
              (part_home / "sessions" / "s1.zstd").is_file()
              and (part_home / ".credentials.yaml").is_file())
        saved = json.loads((tmp / "lanes.json").read_text(encoding="utf-8"))
        check("clone 只登记了选中的 profile",
              sorted((saved.get("lanes", {}).get("t1-part") or {}).get("profiles") or []) == ["web"],
              str((saved.get("lanes", {}).get("t1-part") or {}).get("profiles")))

        # ⑮ 删除：Web / GUI 传 yes=True（二次确认已经在窗口里做过）时**绝不能再碰 stdin**。
        # 这是 2026-10-17 报上来的 bug：--yes 只挡住了报告的前半段，后面那段 input() 照样执行
        # —— 窗口版直接在日志里变成 `RuntimeError: lost sys.stdin`，控制台版会永远卡在等输入。
        import builtins
        real_input = builtins.input

        def _no_input(*_a, **_k):
            raise AssertionError("不该出现交互输入：--yes 表示确认已经在界面里做过了")

        builtins.input = _no_input
        try:
            del_home = tmp / "root" / "homes" / "t1-part"
            cfg4 = json.loads((tmp / "lanes.json").read_text(encoding="utf-8"))
            cfg4["lanes"]["t1-part"]["home"] = str(del_home)
            (tmp / "lanes.json").write_text(json.dumps(cfg4, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
            status, body = call("POST", "/api/delete",
                                {"lane": "t1-part", "stop": True,
                                 "keepHome": False, "keepInstall": False, "homeToo": False})
            check("delete 起得来（yes=True 时不再要 stdin 确认）",
                  status == 200, f"HTTP {status} {str(body)[:120]}")
            deadline = time.time() + 90
            while time.time() < deadline and del_home.exists():
                time.sleep(0.3)
            check("delete 真的把 DSH_HOME 删掉了", not del_home.exists(), str(del_home))
            saved = json.loads((tmp / "lanes.json").read_text(encoding="utf-8"))
            check("delete 把登记也清掉了", "t1-part" not in (saved.get("lanes") or {}),
                  "、".join(saved.get("lanes") or {}))
            check("delete 没有碰到交互输入", True)
        finally:
            builtins.input = real_input
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        app._stop.set()
        httpd.shutdown()
        httpd.server_close()
        core.CONFIG_PATH = real_config
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"[XX] 自检失败 {len(failures)} 项：" + "、".join(failures))
        return 1
    print("[OK] 自检全部通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DSH 多版本启动器（Web 前端版）",
        epilog="例：py dsh_lanes_web.py --dev　·　py dsh_lanes_web.py --classic",
    )
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认 0＝随机空闲）")
    parser.add_argument("--stay", action="store_true", help="只起服务，不开窗口（用系统浏览器访问）")
    parser.add_argument("--classic", action="store_true", help="用旧的 tkinter 界面")
    parser.add_argument("--dev", action="store_true", help="开 DevTools、打印请求日志")
    parser.add_argument("--browser", default="", help="指定浏览器可执行文件")
    parser.add_argument("--selfcheck", action="store_true", help="服务器自检后退出（自动化用）")
    parser.add_argument("--config", default="",
                        help="改用指定的 lanes.json（默认用脚本/exe 旁边那份）。"
                             "自动化测试用它可以完全不碰真实配置")
    args, rest = parser.parse_known_args(argv)

    if args.classic:
        return run_classic((["--selfcheck"] if args.selfcheck else []) + rest)

    if args.config:
        # 必须在建 Launcher 之前改：load_config / save_config 都是即时读这个模块级常量的
        target = Path(args.config).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        core.CONFIG_PATH = target
        print(f"（改用配置：{core.CONFIG_PATH}）")

    _safe_std_streams()
    if args.selfcheck:
        return selfcheck()

    page = asset_dir() / "index.html"
    if not page.is_file():
        print(f"[XX] 找不到界面文件 {page}", file=sys.stderr)
        print("     源码运行时请确认 web/ 目录还在；打包时需要 --add-data \"web;web\"",
              file=sys.stderr)
        return 1

    token = secrets.token_hex(16)
    app = Launcher(token, stay=args.stay, dev=args.dev)
    # 开场白写进日志缓冲：窗口+界面都是后起的，但客户一连上就能看到
    app.hub.log("欢迎使用 DSH 多版本启动器（Web 前端版）。"
                "左边是各版本 lane（内含 profiles 环境列表），右边是实时日志。", "head")
    app.hub.log("快捷键：F5 重新载入界面　Ctrl+L 清空日志　双击卡片＝打开默认 profile", "muted")
    app.hub.log("每个 profile 行上的端口框：留空＝自动挑空闲端口，也可以现场填一个；"
                "被占用会自动改口。", "muted")
    app.hub.log("卡片上的「凭据…/插件…/插件市场…/备份对话…/复制…/升级版本…/删除这套 DSH」"
                "和右上角的「＋ 新建版本 / 插件库」都在这个界面里；"
                "命令行版（py dsh_lanes.py）与经典界面读写同一份配置。", "muted")
    try:
        httpd = make_server("127.0.0.1", max(0, int(args.port)), app)
    except OSError as exc:
        print(f"[XX] 起不了服务（端口 {args.port}）：{exc}", file=sys.stderr)
        return 1
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"

    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    threading.Thread(target=app.hub._flush_loop, args=(app._stop,), daemon=True).start()
    threading.Thread(target=app._ticker, daemon=True).start()

    cfg = core.load_config()
    print(f"DSH 多版本启动器（Web 前端）{BUILD}")
    print(f"  界面地址：{url}")
    print(f"  根目录　：{cfg.get('root')}")
    print("  （这个地址带一次性访问令牌，别的程序/网页猜不到；只在 127.0.0.1 上监听）")

    if args.stay:
        print("  --stay：只起服务，不开窗口。浏览器里打开上面的地址即可。Ctrl+C 退出。")
    else:
        exe = args.browser or next(iter(browser_candidates()), "")
        print("  " + launch_window(url, cfg, exe or None, args.dev))
    app.refresh_remote(force=False)   # 不挡开窗：查完由 SSE 贴到标题下面那一行

    # 根目录不可写时说清楚（不是不能用，而是"记不住 + 部分命令会失败"）
    try:
        root_ok, root_detail = app.root_writable(cfg)
        if not root_ok:
            app.hub.log(f"[!!] lane 根目录不可写：{root_detail}", "warn")
            app.hub.log("     卡片和打开/停止仍可用（状态现场探测）；但运行态记不住，"
                        "克隆 / 删除 / 升级这些要写目录的命令会失败。", "warn")
    except Exception:  # noqa: BLE001
        pass

    try:
        while not app.should_exit():
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出。")
    finally:
        app.shutdown()
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    if not args.stay:
        print("窗口已关闭，启动器退出。正在运行的 DSH 不受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
