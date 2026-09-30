#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 多版本启动器 —— 应用窗口（图形界面版）
==========================================
tkinter 实现（Python 自带，零第三方依赖）。它是 `dsh_lanes.py` 的**薄壳**：
所有真正的活儿（查版本 / 安装 / 启动 / 停止 / 握手自检）都调用已经验证过的核心函数，
界面只负责交互与实时日志。

用法：
    pyw dsh_lanes_gui.py          # 不弹控制台窗口
    py  dsh_lanes_gui.py          # 带控制台（排障用）
    py  dsh_lanes_gui.py --selfcheck   # 只做构建/刷新自检后退出（自动化用）
"""

from __future__ import annotations

import argparse
import ctypes
import io
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dsh_lanes as core  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
LANE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# ── 配色（对齐 DSH 的浅色观感）──────────────────────────────────────────────
BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E3E5E9"
TEXT = "#1F2328"
MUTED = "#6B7280"
ACCENT = "#4D6BFE"
ACCENT_DK = "#3A56E0"
GREEN = "#16A34A"
GREEN_BG = "#E8F6EE"
GRAY = "#9CA3AF"
GRAY_BG = "#F0F1F3"
RED = "#DC2626"
RED_BG = "#FDECEC"
AMBER = "#B45309"
AMBER_BG = "#FEF3E2"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"


def enable_dpi_awareness() -> None:
    """让窗口在高 DPI 屏上不发虚（本机 2560×1440 / 133%）。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class QueueWriter(io.TextIOBase):
    """把核心模块 print 出来的内容按行送进队列，供主线程写进日志面板。"""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self._buf = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(("line", line))
        return len(text)

    def flush(self) -> None:
        # 故意不把半行推出去：否则 `dsh web: <url>` 会被拆成两行显示
        return


def short_url(url: str, keep: int = 14) -> str:
    """卡片上只显示缩略 URL（完整地址点一下就复制），避免长 token 把布局挤变形。"""
    m = re.match(r"^(.*\?token=)(.+)$", url or "")
    if not m:
        return url or ""
    head, tok = m.group(1), m.group(2)
    if len(tok) <= keep + 8:
        return url
    return f"{head}{tok[:keep]}…{tok[-4:]}"


class LaneCard(tk.Frame):
    """一条 lane 的卡片：名称 / 版本 + **profile 列表**（每个环境一行）+ 操作。

    lane ＝ 一个版本（一棵安装树 + 一个 DSH_HOME）；profile ＝ 这个 HOME 里的一个环境，
    各自有独立的插件组合，会话与凭据共享。每个 profile 行上有**实时端口框**：
    留空＝启动时自动挑空闲端口；填了但被占会自动改口——端口不再固定在配置里。
    """

    def __init__(
        self,
        master,
        app: "LauncherApp",
        name: str,
        data: dict,
        profile_states: dict[str, dict | None],
        profiles_view: dict[str, dict],
    ) -> None:
        super().__init__(master, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self.app = app
        self.name = name
        self.data = data
        self.profile_states = profile_states or {}
        self.profiles_view = profiles_view or {}

        pad = tk.Frame(self, bg=CARD)
        pad.pack(fill="x", padx=14, pady=11)

        # ── 第一行：主体信息 + 主操作
        # 先放右侧定宽部件（按钮、徽章），最后放会撑开的左侧块，
        # 否则长 URL 会把徽章挤没（这是 pack 的经典坑）。
        head = tk.Frame(pad, bg=CARD)
        head.pack(fill="x")

        running_n = sum(1 for rt in self.profile_states.values() if rt)
        btns = tk.Frame(head, bg=CARD)
        btns.pack(side="right")
        self.app.button(btns, "打开", self.open_default, primary=True).pack(side="left", padx=3)
        self.app.button(btns, "全部停止", lambda: self.app.stop_lane(self.name), danger=True).pack(
            side="left", padx=3
        )

        badge = tk.Label(
            head,
            text=(f"● {running_n}/{len(self.profiles_view)} 运行中" if running_n else "○ 已停止"),
            bg=(GREEN_BG if running_n else GRAY_BG),
            fg=(GREEN if running_n else GRAY),
            font=(FONT_UI, 9, "bold"),
            padx=10,
            pady=3,
        )
        badge.pack(side="right", padx=(0, 12))

        left = tk.Frame(head, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        title = tk.Frame(left, bg=CARD)
        title.pack(anchor="w")
        tk.Label(title, text=name, bg=CARD, fg=TEXT, font=(FONT_UI, 12, "bold")).pack(side="left")
        tk.Label(
            title, text=f"  {data.get('version', '?')}", bg=CARD, fg=MUTED, font=(FONT_UI, 10)
        ).pack(side="left")
        if data.get("kind") == "adopted":
            tk.Label(
                title, text="  接管", bg=GRAY_BG, fg=MUTED, font=(FONT_UI, 8), padx=4, pady=1
            ).pack(side="left", padx=(6, 0))
        elif data.get("kind") == "clone":
            tk.Label(
                title,
                text=f"  副本（源 {data.get('clonedFrom') or '?'}）",
                bg=GRAY_BG,
                fg=MUTED,
                font=(FONT_UI, 8),
                padx=4,
                pady=1,
            ).pack(side="left", padx=(6, 0))

        # ── profile 列表（每个环境一行；可收起/展开，避免卡片太长）
        # 展开状态记在 app 上（按 lane 名），刷新重建卡片后保持不变；
        # 没动过开关时：有 profile 在运行就展开，全部停止就收起。
        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=(9, 5))

        self._running_n = sum(1 for rt in self.profile_states.values() if rt)
        self._expanded = self.app.profile_expand_state.get(
            self.name, self._running_n > 0
        )

        prof_head = tk.Frame(pad, bg=CARD)
        prof_head.pack(fill="x")
        summary = f"PROFILES　{len(self.profiles_view)} 个环境"
        if self._running_n:
            summary += f" · {self._running_n} 个运行中"
        tk.Label(
            prof_head,
            text=summary,
            bg=CARD,
            fg=(GREEN if self._running_n else GRAY),
            font=(FONT_UI, 8, "bold"),
        ).pack(side="left")
        self.app.link_button(
            prof_head, "＋ 添加 Profile…",
            lambda: self.app.add_profile_dialog(self.name), fg=ACCENT_DK,
        ).pack(side="right")
        self._toggle_btn = self.app.link_button(
            prof_head, "收起 ▴" if self._expanded else "展开 ▾", self._toggle_profiles, fg=MUTED
        )
        self._toggle_btn.pack(side="right", padx=(0, 12))

        # body 的高度完全由启动器控制（propagate 恒为 False，宽度仍由 fill="x" 跟随卡片），
        # 内容装在 inner 里就地增删——切换时其它卡片和页脚都不动，只有高度在动
        self._prof_body = tk.Frame(pad, bg=CARD)
        self._prof_body.pack(fill="x")
        self._prof_body.pack_propagate(False)
        self._prof_inner = tk.Frame(self._prof_body, bg=CARD)
        self._prof_inner.pack(fill="x")
        self._body_height = 0
        if self._expanded:
            self._fill_profiles()
            self._prof_inner.update_idletasks()
            self._apply_body_height(self._prof_inner.winfo_reqheight())

        # ── 次要操作 + 说明
        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=(9, 7))
        foot = tk.Frame(pad, bg=CARD)
        foot.pack(fill="x")

        acts = tk.Frame(foot, bg=CARD)
        acts.pack(side="right")
        self.app.link_button(
            acts, "删除这套 DSH", lambda: self.app.delete_lane(self.name), fg=RED
        ).pack(side="right")
        if data.get("kind") == "clone":
            self.app.link_button(
                acts, "核对隔离", lambda: self.app.verify_lane(self.name), fg=ACCENT_DK
            ).pack(side="right", padx=(0, 14))
        self.app.link_button(
            acts, "备份对话…", lambda: self.app.chat_dialog(self.name), fg=ACCENT_DK
        ).pack(side="right", padx=(0, 14))
        self.app.link_button(
            acts, "插件市场…", lambda: self.app.market_dialog(self.name), fg=ACCENT_DK
        ).pack(side="right", padx=(0, 14))
        self.app.link_button(
            acts, "复制…", lambda: self.app.clone_lane(self.name), fg=ACCENT_DK
        ).pack(side="right", padx=(0, 14))
        self.app.link_button(
            acts, "升级版本…", lambda: self.app.upgrade_dialog(self.name), fg=ACCENT_DK
        ).pack(side="right", padx=(0, 14))
        self.app.link_button(
            acts, "凭据…", lambda: self.app.credential_dialog(self.name), fg=ACCENT_DK
        ).pack(side="right", padx=(0, 14))

        note_text = (
            "凭据…＝把其它版本的 API key 按条目勾选复制过来；"
            "复制…＝整套复制成独立副本（升级测试用）；端口留空＝每次启动自动挑空闲。"
        )
        note = tk.Label(
            foot,
            text=note_text,
            bg=CARD,
            fg=GRAY,
            font=(FONT_UI, 8),
            justify="left",
            anchor="w",
            wraplength=300,
        )
        note.pack(side="left", fill="x", expand=True)

        def _rewrap(event) -> None:
            try:
                reserved = acts.winfo_reqwidth() + 16
            except tk.TclError:
                reserved = 0
            w = max(120, event.width - reserved)
            if abs(int(note.cget("wraplength")) - w) > 12:
                note.configure(wraplength=w)

        foot.bind("<Configure>", _rewrap)

        # 双击卡片任意位置＝打开默认 profile
        def bind_open(widget) -> None:
            widget.bind("<Double-Button-1>", lambda _e: self.open_default())
            for child in widget.winfo_children():
                if not isinstance(child, tk.Button) and not isinstance(child, tk.Entry):
                    bind_open(child)

        bind_open(self)

    # ── 卡片内的一个 profile 行 ────────────────────────────────────────────
    def _profile_row(self, pad: tk.Frame, prof: str) -> None:
        info = self.profiles_view.get(prof) or {}
        rt = self.profile_states.get(prof)
        row = tk.Frame(pad, bg=CARD)
        row.pack(fill="x", pady=2)

        # 右侧操作先 pack：端口框 + 按钮
        ops = tk.Frame(row, bg=CARD)
        ops.pack(side="right")

        port_now = (rt or {}).get("port")
        pref = info.get("port")
        port_var = tk.StringVar(
            value=str(port_now) if port_now else (str(pref) if pref else "")
        )
        port_entry = tk.Entry(
            ops,
            textvariable=port_var,
            width=7,
            font=(FONT_MONO, 9),
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        port_entry.pack(side="left", padx=(0, 4))
        self.app._attach_port_hint(port_entry)
        self.app.button(
            ops, "打开", lambda: self.app.open_profile(self.name, prof, port_var.get()),
            primary=not rt,
        ).pack(side="left", padx=3)
        self.app.button(
            ops, "停止", lambda: self.app.stop_profile(self.name, prof), danger=bool(rt)
        ).pack(side="left", padx=3)
        self.app.link_button(
            ops, "日志", lambda: self.app.show_lane_log(self.name, prof), fg=MUTED
        ).pack(side="left", padx=(2, 6))
        if prof != core.DEFAULT_PROFILE:
            self.app.link_button(
                ops, "移除", lambda: self.app.remove_profile(self.name, prof), fg=RED
            ).pack(side="left", padx=(6, 0))

        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        line = tk.Frame(left, bg=CARD)
        line.pack(anchor="w")
        tk.Label(
            line,
            text=("●" if rt else "○"),
            bg=CARD,
            fg=(GREEN if rt else GRAY),
            font=(FONT_UI, 10, "bold"),
        ).pack(side="left")
        tk.Label(
            line, text=f" {prof}", bg=CARD, fg=TEXT, font=(FONT_UI, 10, "bold")
        ).pack(side="left")
        meta_bits = []
        if rt:
            meta_bits.append(f"PID {rt.get('pid')}")
            meta_bits.append(f"端口 {rt.get('port')}")
        else:
            meta_bits.append("端口 " + (f"偏好 {info.get('port')}（被占自动改口）" if info.get("port") else "自动挑空闲"))
        tags = []
        if prof == core.DEFAULT_PROFILE:
            tags.append("默认")
        if not info.get("onDisk"):
            tags.append("首次启动时自动初始化")
        elif not info.get("registered"):
            tags.append("磁盘上有")
        tk.Label(
            line, text="　" + " · ".join(meta_bits), bg=CARD, fg=MUTED, font=(FONT_UI, 9)
        ).pack(side="left")
        if tags:
            tk.Label(
                line, text="　（" + "；".join(tags) + "）", bg=CARD, fg=GRAY, font=(FONT_UI, 8)
            ).pack(side="left")

        url = (rt or {}).get("url") or ""
        if url:
            link = tk.Label(
                left,
                text=short_url(url),
                bg=CARD,
                fg=ACCENT,
                font=(FONT_MONO, 8),
                cursor="hand2",
                anchor="w",
                wraplength=330,
                justify="left",
            )
            link.pack(anchor="w", padx=(14, 0))
            link.bind("<Button-1>", lambda _e: self.app.copy_text(url))
            link.bind("<Enter>", lambda _e, l=link, u=url: l.configure(
                fg=ACCENT_DK, text=short_url(u) + "   ⧉ 点击复制"))
            link.bind("<Leave>", lambda _e, l=link, u=url: l.configure(fg=ACCENT, text=short_url(u)))

    def open_default(self) -> None:
        prof = core.default_profile_of(self.profiles_view)
        self.app.open_profile(self.name, prof, None)

    def _toggle_profiles(self) -> None:
        """收起 / 展开本卡片的 profile 列表：就地改高度 + 渐变动画，不重建整张卡片。

        展开状态记在 app 上（按 lane 名），卡片重建后保持不变。动画期间再次点击
        会取消上一次未完成的动画，从当前高度继续——连点不会跳变。
        """
        target = not self._expanded
        self.app.profile_expand_state[self.name] = target
        self._expanded = target
        try:
            self._toggle_btn.configure(text="收起 ▴" if target else "展开 ▾")
        except tk.TclError:
            pass
        if target:
            self._fill_profiles()
            self._animate_profiles(expanding=True)
        else:
            self._animate_profiles(expanding=False)

    # ── profiles 区的展开/收起动画 ────────────────────────────────────────────
    _ANIM_FRAMES = 12      # 展开/收起的帧数（可调：帧越多越顺滑，每帧重排版也越多）
    _ANIM_FRAME_MS = 10    # 每帧 10ms 调度（单帧重排版 ≈9ms，实际帧距以其为准）→ 约 0.3-0.5s

    def _apply_body_height(self, h: float) -> None:
        """把 body 高度设为 h 并**立即重排版**。

        configure 只是"请求"，重排版默认攒到 idle 才做——中间帧会全部挤成一跳，
        这就是之前"闪"的原因。每帧强制 update_idletasks，高度变化当场生效。
        """
        self._body_height = max(0, int(h))
        try:
            self._prof_body.configure(height=self._body_height)
            self.app.root.update_idletasks()
        except tk.TclError:
            pass

    def _fill_profiles(self) -> None:
        """（重新）渲染 profile 行。顺序：默认 profile（web）最前，其余按名字排。"""
        for child in self._prof_inner.winfo_children():
            child.destroy()
        order = [core.DEFAULT_PROFILE] + sorted(
            p for p in self.profiles_view if p != core.DEFAULT_PROFILE
        )
        order = [p for p in order if p in self.profiles_view]
        for prof in order:
            self._profile_row(self._prof_inner, prof)
        tk.Label(
            self._prof_inner,
            text="同一套会话与凭据；各自独立的插件组合＝多环境。端口留空＝自动挑空闲。",
            bg=CARD,
            fg=GRAY,
            font=(FONT_UI, 8),
        ).pack(anchor="w", pady=(2, 0))

    def _animate_profiles(self, expanding: bool) -> None:
        body, inner = self._prof_body, self._prof_inner
        # 上一次动画没放完就又点了：取消它，从当前高度接着走（连点不跳变）
        if getattr(self, "_anim_id", None):
            try:
                self.app.root.after_cancel(self._anim_id)
            except Exception:  # noqa: BLE001
                pass
            self._anim_id = None
        try:
            inner.update_idletasks()
        except tk.TclError:
            return
        if expanding:
            target = inner.winfo_reqheight()
            start = getattr(self, "_body_height", 0)
        else:
            target = 0
            start = getattr(self, "_body_height", 0) or inner.winfo_reqheight()
        # 帧数固定：每帧都是一次真实的"高度变化 + 重排版"，每帧 = 一个**互不相同**的
        # 高度。预生成严格单调的整数序列：int 取整会让相邻帧撞车，所以每帧强制比上一帧
        # 至少多/少 1px，并给剩余的帧预留像素（否则尾部的缓动会提前吃光像素）。
        # 内容不足 30px 时按像素数降级帧数——渲染不出比像素更多的帧。
        dist = abs(target - start)
        if dist == 0:
            self._apply_body_height(target)
            return
        frames_n = self._ANIM_FRAMES if dist >= self._ANIM_FRAMES else max(2, dist)
        heights = []
        last = start
        for i in range(1, frames_n + 1):
            ratio = i / frames_n
            eased = 1 - (1 - ratio) ** 2 if expanding else ratio ** 2
            h = int(round(start + (target - start) * eased))
            if expanding:
                h = max(h, last + 1)
                h = min(h, target - (frames_n - i))
            else:
                h = min(h, last - 1)
                h = max(h, target + (frames_n - i))
            heights.append(h)
            last = h
        state = {"i": 0, "heights": heights}

        def step() -> None:
            self._apply_body_height(state["heights"][state["i"]])
            state["i"] += 1
            if state["i"] < frames_n:
                self._anim_id = self.app.root.after(self._ANIM_FRAME_MS, step)
                return
            self._anim_id = None
            if not expanding:
                # 收起完成后再清内容：动画过程中内容还在（被逐渐压掉），看起来是"滑上去"
                for child in inner.winfo_children():
                    child.destroy()

        step()


class LauncherApp:
    _MAX_LOG_LINES = 4000

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.cfg = core.load_config()

        root.title("DSH 多版本启动器")
        root.configure(bg=BG)
        ui = self.cfg.get("ui") or {}
        if ui.get("w") and ui.get("h"):
            root.geometry(f"{int(ui['w'])}x{int(ui['h'])}+{int(ui.get('x', 80))}+{int(ui.get('y', 60))}")
        else:
            self._center(1060, 720)
        root.minsize(880, 560)
        self._style()

        self._build_header()
        self._build_body()
        self._build_status()

        self._sig: tuple | None = None
        self._adopt_attempted = False       # 磁盘上未登记 lane 的自动认领：启动 / 手动刷新时各试一次
        self.profile_expand_state: dict[str, bool] = {}   # 卡片 profiles 区的收起/展开（按 lane 记）
        self._snap: dict | None = None      # 一轮刷新共用的快照（见 _snapshot）
        self._snap_at = 0.0
        self._remote_busy = False           # 顶部「npm 上：…」那行：是否查询中 / 上次查询时间
        self._remote_at = 0.0
        try:
            self.refresh_lanes(force=True)
        except Exception as exc:  # noqa: BLE001
            # 首次刷新失败也要把窗口开出来（能看日志、能手动重试），
            # 而不是留一个半成品窗口或者干脆消失。
            self.log(f"[XX] 首次刷新失败：{type(exc).__name__}: {exc}", "warn")
            self.log("     窗口仍然可用：修掉原因后按 F5 / 点「刷新」重试。", "muted")
            self.log(traceback.format_exc().rstrip(), "muted")
        self.root.after(120, self._poll_queue)
        self.root.after(200, lambda: self.refresh_remote(force=True))  # 不挡开窗，查完再贴上去
        self.root.after(2000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _e: self.manual_refresh())
        self.root.bind("<Control-l>", lambda _e: self.clear_log())
        self.root.bind("<Control-n>", lambda _e: self.new_version_dialog())
        self.log("欢迎使用 DSH 多版本启动器。左边是各版本 lane（内含 profiles 环境列表），右边是实时日志。", "head")
        self.log("快捷键：F5 刷新　Ctrl+L 清空日志　Ctrl+N 新建版本　双击卡片＝打开默认 profile", "muted")
        self.log("每个 profile 行上的端口框：留空＝自动挑空闲端口，也可以现场填一个；被占用会自动改口。", "muted")

        # 根目录不可写时说清楚（不是不能用，而是"记不住 + 部分命令会失败"），
        # 别让它以"某个操作突然报错"的形式冒出来。
        root_ok, root_detail = core.lane_root_writable(self.cfg)
        if not root_ok:
            self.log(f"[!!] lane 根目录不可写：{root_detail}", "warn")
            self.log(
                "     卡片和打开/停止仍可用（状态现场探测）；但运行态记不住，"
                "克隆 / 删除 / 升级这些要写目录的命令会失败。",
                "warn",
            )

    # ── 基础 ────────────────────────────────────────────────────────────────
    def _center(self, w: int, h: int) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", padding=4)
        style.configure("TEntry", padding=4)
        style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG, borderwidth=0)

    def button(self, master, text, command, primary=False, danger=False, width=None):
        bg = ACCENT if primary else (RED_BG if danger else "#FFFFFF")
        fg = "#FFFFFF" if primary else (RED if danger else TEXT)
        active = ACCENT_DK if primary else ("#FBE2E2" if danger else "#F0F1F3")
        btn = tk.Button(
            master,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief="flat",
            bd=0,
            padx=12,
            pady=5,
            cursor="hand2",
            font=(FONT_UI, 9),
            highlightthickness=0,
        )
        if width:
            btn.configure(width=width)
        return btn

    def link_button(self, master, text, command, fg=None, bg=None):
        """文字链式小按钮（次要操作用，避免一行塞四五个方块按钮）。"""
        color = fg or TEXT
        back = bg or CARD
        btn = tk.Button(
            master,
            text=text,
            command=command,
            bg=back,
            fg=color,
            activebackground=back,
            activeforeground=color,
            relief="flat",
            bd=0,
            padx=2,
            pady=0,
            cursor="hand2",
            font=(FONT_UI, 9),
            highlightthickness=0,
        )
        btn.bind("<Enter>", lambda _e: btn.configure(font=(FONT_UI, 9, "underline")))
        btn.bind("<Leave>", lambda _e: btn.configure(font=(FONT_UI, 9)))
        return btn

    # ── 下载源选择（新建 / 升级共用） ─────────────────────────────────────────
    def registry_row(self, parent, cfg: dict, pady=(12, 0)):
        """「下载源」三选一 ＋「记住这个选择」。返回 (取值 StringVar, 记住 BooleanVar)。

        为什么把它摆到明面上（2026-09-28 实测）：官方源当天发新版时，镜像常常只同步了一半
        —— dsh@0.2.0-rc.1 镜像有了，可它的 12 个 @deepseek-ai/dsh-* 子包还没到（查 259 个包）。
        以前"查版本走官方、下载走镜像"两边不一致，就会 npm ETARGET 装不上。现在的规矩：
        选谁就**用它查版本、也用它下载**；万一它还没同步全，会自动换另一个源重试一次（写进日志）。
        """
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x", padx=18, pady=pady)
        var = tk.StringVar(value=core.registry_choice_of(cfg))
        remember = tk.BooleanVar(value=False)
        tk.Label(box, text="下载源", bg=BG, fg=MUTED, font=(FONT_UI, 9)).pack(anchor="w")
        row = tk.Frame(box, bg=BG)
        row.pack(anchor="w", pady=(2, 0))
        for value, text in (
            ("auto", "自动（先官方，缺东西再换镜像）"),
            ("official", "官方源 npmjs.org"),
            ("mirror", "镜像源 npmmirror.com（国内快）"),
        ):
            tk.Radiobutton(
                row, text=text, value=value, variable=var, bg=BG, fg=TEXT, selectcolor=CARD,
                activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
                highlightthickness=0, cursor="hand2",
            ).pack(side="left", padx=(0, 14))
        tk.Checkbutton(
            box, text="记住这个选择（写进 lanes.json，以后新建/升级默认用它）",
            variable=remember, bg=BG, fg=MUTED, selectcolor=CARD,
            activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
            highlightthickness=0, cursor="hand2",
        ).pack(anchor="w", pady=(2, 0))
        raw = str(cfg.get("registry") or "").strip()
        if raw.lower() not in ("", "auto", "official", "mirror", "npmjs", "npm", "npmmirror", "taobao"):
            tk.Label(box, text=f"（lanes.json 里现在写的是自定义地址 {raw}；不想换掉就别勾「记住」）",
                     bg=BG, fg=AMBER, font=(FONT_UI, 8)).pack(anchor="w")
        return var, remember

    def _save_registry_choice(self, var, remember) -> None:
        """勾了「记住」才写 lanes.json；不勾就只是这一次有效，不动你的全局设置。"""
        if not remember.get():
            return
        try:
            cfg = core.load_config()
            cfg["registry"] = core.registry_value_of(var.get())
            core.save_config(cfg)
            self.log(f"下载源已记住：{core.registry_choice_of(cfg)}"
                     + (f"（{cfg['registry']}）" if cfg["registry"] else "（auto＝先官方再镜像）"),
                     "muted")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 记住下载源失败：{exc}", "warn")

    # ── 顶部 ────────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 8))

        # 右侧按钮**先** pack：窗口拉到最窄时宁可挤左边的文字，也不能把「＋ 新建版本」挤没
        right = tk.Frame(head, bg=BG)
        right.pack(side="right")
        self.button(right, "刷新", self.manual_refresh).pack(side="left", padx=4)
        self.button(right, "环境自检", lambda: self.run_job("环境自检", lambda: core.cmd_doctor(self.cfg, None))).pack(
            side="left", padx=4
        )
        self.button(right, "全部停止", self.stop_all, danger=True).pack(side="left", padx=4)
        self.button(right, "打开根目录", self.open_root_dir).pack(side="left", padx=4)
        self.button(right, "＋ 新建版本", self.new_version_dialog, primary=True).pack(side="left", padx=(10, 0))

        left = tk.Frame(head, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="DSH 多版本启动器", bg=BG, fg=TEXT, font=(FONT_UI, 15, "bold")).pack(anchor="w")
        self.sub = tk.Label(
            left,
            text="一条 lane＝一个版本（独立安装树 + HOME）；版本内可开多个 profile 环境；"
                 "端口每次启动实时挑空闲",
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
        )
        self.sub.pack(anchor="w", pady=(2, 0))

        # 第三行：npm 上现在有哪些版本（只读查询）+ 官方代码库入口
        remote = tk.Frame(left, bg=BG)
        remote.pack(anchor="w", pady=(4, 0))
        self.remote_label = tk.Label(
            remote,
            text="正在查询 npm 上的最新版本…",
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
            anchor="w",
        )
        self.remote_label.pack(side="left")
        self.link_button(remote, "官方代码库 ↗", self.open_repo, fg=ACCENT_DK).pack(side="left", padx=(10, 0))

    # ── 主体 ────────────────────────────────────────────────────────────────
    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        body.columnconfigure(0, weight=3, minsize=430)
        body.columnconfigure(1, weight=2, minsize=330)
        body.rowconfigure(0, weight=1)

        # 左：lane 列表（可滚动）
        left_wrap = tk.Frame(body, bg=BG)
        left_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_wrap.rowconfigure(0, weight=1)
        left_wrap.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(left_wrap, bg=BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(left_wrap, orient="vertical", command=self.canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=sb.set)

        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width)
        )
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # 右：日志
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        bar = tk.Frame(right, bg=BG)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tk.Label(bar, text="实时日志", bg=BG, fg=TEXT, font=(FONT_UI, 11, "bold")).pack(side="left")
        self.button(bar, "清空", self.clear_log).pack(side="right")
        self.button(bar, "复制全部", self.copy_log).pack(side="right", padx=4)

        wrap = tk.Frame(right, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            wrap,
            width=1,  # 关键：默认 80 字符会撑爆右列、把左列压扁（grid 先满足请求宽度再分权重）
            height=1,
            bg="#FBFBFC",
            fg=TEXT,
            relief="flat",
            wrap="word",
            font=(FONT_MONO, 9),
            padx=10,
            pady=8,
            insertbackground=TEXT,
            state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.tag_configure("head", foreground=ACCENT, font=(FONT_MONO, 9, "bold"))
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("err", foreground=RED)
        self.log_text.tag_configure("warn", foreground=AMBER)
        self.log_text.tag_configure("muted", foreground=MUTED)

    def _build_status(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=18, pady=(0, 12))
        self.status = tk.Label(bar, text="就绪", bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        self.status.pack(side="left")
        self.busy_label = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=(FONT_UI, 9, "bold"))
        self.busy_label.pack(side="right")
        self.counts = tk.Label(bar, text="", bg=BG, fg=MUTED, font=(FONT_UI, 9))
        self.counts.pack(side="right", padx=14)

    def _on_wheel(self, event) -> None:
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    # ── 日志 ────────────────────────────────────────────────────────────────
    def _tag_for(self, text: str, tag: str | None = None) -> str:
        if tag:
            return tag
        if "[OK]" in text:
            return "ok"
        if "[XX]" in text:
            return "err"
        if "[!!]" in text:
            return "warn"
        return ""

    def _log_lines(self, items: list[tuple[str, str | None]]) -> None:
        """批量写日志：一次插入、只滚动一次、并裁掉过老的尾巴。

        安装 npm 时一秒能来上百行，逐行 insert + 逐行 see("end") 会把界面拖住；
        而且文本控件只增不减，跑几次安装后连滚动都会变卡。
        """
        if not items:
            return
        text = self.log_text
        text.configure(state="normal")
        for raw, tag in items:
            line = ANSI_RE.sub("", str(raw))
            text.insert("end", line + "\n", self._tag_for(line, tag))
        try:  # 只留尾部若干行
            total = int(str(text.index("end-1c")).split(".")[0])
            if total > self._MAX_LOG_LINES:
                text.delete("1.0", f"{total - self._MAX_LOG_LINES}.0")
        except (tk.TclError, ValueError):
            pass
        text.see("end")
        text.configure(state="disabled")

    def log(self, text: str, tag: str | None = None) -> None:
        self._log_lines([(text, tag)])

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self) -> None:
        self.copy_text(self.log_text.get("1.0", "end").strip())

    def copy_text(self, text: str) -> None:
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("已复制到剪贴板")

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # ── 数据刷新 ────────────────────────────────────────────────────────────
    def _snapshot(self, force: bool = False, ttl: float = 1.0) -> dict:
        """一轮刷新要用到的全部"外部事实"，只算一次。

        以前算签名时算一遍、建卡片时再算一遍、更新计数时又算一遍——同一件事算三遍，
        每次都是真的去读 TCP 表 / 探端口。现在一次算完，后面全部复用。
        """
        now = time.monotonic()
        if not force and self._snap is not None and now - self._snap_at < ttl:
            return self._snap

        cfg = core.load_config()
        # 磁盘上有、登记表里没有的 lane（登记表丢过 / 换目录跑过）自动认回来。
        # 只在启动和手动刷新时尝试：扫盘 + 写配置不值得每 2.5 秒做一遍。
        if not getattr(self, "_adopt_attempted", False):
            self._adopt_attempted = True
            try:
                for item in core.adopt_lost_lanes(cfg):
                    self.log(f"自动认领磁盘上的 lane「{item['name']}」（{item['why']}）", "ok")
            except Exception as exc:  # noqa: BLE001
                self.log(f"[!!] 自动认领未登记 lane 失败：{type(exc).__name__}: {exc}", "warn")
            # 接管 / 副本 lane 的登记版本对齐到安装树真实版本（本体被 npm 升级后登记会过时）
            try:
                for item in core.sync_lane_versions(cfg):
                    self.log(f"版本对齐：lane「{item['lane']}」{item['from']} → {item['to']}"
                             "（以安装树为准）", "ok")
            except Exception as exc:  # noqa: BLE001
                self.log(f"[!!] 版本对齐失败：{type(exc).__name__}: {exc}", "warn")
        states = core.lane_states(cfg)          # {lane: {profile: 运行态|None}}，复用同一份 TCP 表
        try:
            foreign = core.discover_instances(cfg)
        except Exception:
            foreign = []
        try:
            installs = core.find_installs(cfg)
        except Exception:
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
        self._snap = {
            "cfg": cfg,
            "states": states,
            "foreign": foreign,
            "installs": installs,
            "signature": signature,
        }
        self._snap_at = now
        return self._snap

    def _invalidate(self) -> None:
        """有东西被改动过（起停 / 删除 / 接管）→ 下一轮强制重算。"""
        self._snap = None
        core.invalidate_tcp_cache()

    def _update_counts(self, snap: dict) -> None:
        cfg = snap["cfg"]
        states = snap["states"]
        total_profiles = sum(len(v) for v in states.values())
        running = sum(1 for v in states.values() for rt in (v or {}).values() if rt)
        self.counts.configure(
            text=f"lane {len(states)} 条 / profile {total_profiles} 个（运行 {running}）"
                 f"｜未登记实例 {len(snap['foreign'])} 个｜根目录 {cfg['root']}"
        )

    def refresh_lanes(self, force: bool = True) -> None:
        snap = self._snapshot(force=force)
        if not force and snap["signature"] == self._sig:
            return
        self._sig = snap["signature"]
        self.cfg = snap["cfg"]
        for child in self.list_frame.winfo_children():
            child.destroy()

        lanes = self.cfg.get("lanes", {})
        if not lanes:
            box = tk.Frame(self.list_frame, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            box.pack(fill="x", pady=4)
            tk.Label(
                box,
                text="还没有任何 lane",
                bg=CARD,
                fg=TEXT,
                font=(FONT_UI, 11, "bold"),
            ).pack(anchor="w", padx=16, pady=(14, 2))
            tk.Label(
                box,
                text="点右上角「＋ 新建版本」，选一个版本装进来（例如 next / 0.1.7-rc.2）。\n"
                     "装好之后这里会出现卡片：卡片上是这条 lane 的 profiles（环境）列表，\n"
                     "每个 profile 可以单独打开/停止，端口留空＝每次启动自动挑空闲端口。\n\n"
                     "同一条 lane 里可以「＋ 添加 Profile」开出多个环境（各自装各自的插件），\n"
                     "不必为每个环境复制一整套 dsh；API key 用「凭据…」按条目从其它版本复制。",
                bg=CARD,
                fg=MUTED,
                font=(FONT_UI, 9),
                justify="left",
            ).pack(anchor="w", padx=16, pady=(0, 14))
        else:
            # core.lane_profiles_view：磁盘真值 ∪ lanes.json 登记
            for name, data in lanes.items():
                if not isinstance(data, dict):
                    continue
                card = LaneCard(
                    self.list_frame,
                    self,
                    name,
                    data,
                    snap["states"].get(name) or {},
                    core.lane_profiles_view(self.cfg, name),
                )
                card.pack(fill="x", pady=4)

        self._build_foreign_section(snap)
        self._update_counts(snap)

    # ── 未登记实例 / 系统全局安装 ───────────────────────────────────────────
    def _section(self, text: str) -> None:
        tk.Label(
            self.list_frame, text=text, bg=BG, fg=MUTED, font=(FONT_UI, 9, "bold")
        ).pack(anchor="w", pady=(14, 2))

    def _build_foreign_section(self, snap: dict) -> None:
        instances = snap["foreign"]
        # lane 自己的安装树不在这里重复列（上面已经有卡片了）
        installs = [i for i in snap["installs"] if not i["source"].startswith("lane ")]
        if not instances and not installs:
            return

        self._section("未登记的实例（不是本启动器启动的）")

        if not instances:
            tk.Label(
                self.list_frame,
                text="（当前没有别的 DSH 实例在跑）",
                bg=BG,
                fg=GRAY,
                font=(FONT_UI, 9),
            ).pack(anchor="w", pady=(0, 4))

        for item in instances:
            card = tk.Frame(self.list_frame, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=4)
            pad = tk.Frame(card, bg=CARD)
            pad.pack(fill="x", padx=14, pady=11)

            btns = tk.Frame(pad, bg=CARD)
            btns.pack(side="right")
            self.button(btns, "打开页面", lambda u=item["url"]: self.open_url(u), primary=True).pack(
                side="left", padx=3
            )
            self.button(btns, "结束进程", lambda i=item: self.kill_instance(i), danger=True).pack(
                side="left", padx=3
            )

            flag = "● 像是 DSH" if item["dsh_like"] else "○ 占用中"
            tk.Label(
                pad,
                text=flag,
                bg=(AMBER_BG if item["dsh_like"] else GRAY_BG),
                fg=(AMBER if item["dsh_like"] else GRAY),
                font=(FONT_UI, 9, "bold"),
                padx=10,
                pady=3,
            ).pack(side="right", padx=(0, 12))

            left = tk.Frame(pad, bg=CARD)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(
                left,
                text=f"端口 {item['port']}",
                bg=CARD,
                fg=TEXT,
                font=(FONT_UI, 12, "bold"),
            ).pack(anchor="w")
            tk.Label(
                left,
                text=f"PID {item['pid']}　进程 {item['exe']}",
                bg=CARD,
                fg=MUTED,
                font=(FONT_UI, 9),
            ).pack(anchor="w", pady=(3, 0))
            tk.Label(
                left,
                text=f"启动于 {item.get('started') or '?'}　·　非本启动器管理",
                bg=CARD,
                fg=GRAY,
                font=(FONT_UI, 8),
            ).pack(anchor="w", pady=(1, 0))
            link = tk.Label(
                left,
                text=item["url"] + "   ⧉ 点击复制",
                bg=CARD,
                fg=ACCENT,
                font=(FONT_MONO, 8),
                cursor="hand2",
                anchor="w",
            )
            link.pack(anchor="w", pady=(2, 0))
            link.bind("<Button-1>", lambda _e, u=item["url"]: self.copy_text(u))

        if installs:
            card = tk.Frame(self.list_frame, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=4)
            pad = tk.Frame(card, bg=CARD)
            pad.pack(fill="x", padx=14, pady=11)
            self.button(pad, "打开根目录", self.open_root_dir).pack(side="right")
            left = tk.Frame(pad, bg=CARD)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(
                left,
                text="本机各套安装（你平时在用的那份在这里）",
                bg=CARD,
                fg=TEXT,
                font=(FONT_UI, 12, "bold"),
            ).pack(anchor="w")
            for item in installs:
                row = tk.Frame(left, bg=CARD)
                row.pack(fill="x", pady=(3, 0))
                self.button(
                    row,
                    "加入 lane 列表",
                    lambda i=item: self.adopt_install(i),
                ).pack(side="right", padx=(8, 0))
                text = tk.Frame(row, bg=CARD)
                text.pack(side="left", fill="x", expand=True)
                tk.Label(
                    text,
                    text=f"{item['source']}　·　{item['version']}　·　写入 {item.get('installedAt') or '?'}",
                    bg=CARD,
                    fg=MUTED,
                    font=(FONT_UI, 9),
                    anchor="w",
                ).pack(anchor="w")
                tk.Label(
                    text,
                    text=item["path"],
                    bg=CARD,
                    fg=GRAY,
                    font=(FONT_MONO, 8),
                    anchor="w",
                    wraplength=250,
                    justify="left",
                ).pack(anchor="w")
            tk.Label(
                left,
                text="「加入 lane 列表」＝用它现有的 DSH_HOME（~/.dsh）注册成一条 lane，之后就能在这里打开/停止它。",
                bg=CARD,
                fg=GRAY,
                font=(FONT_UI, 8),
                wraplength=330,
                justify="left",
            ).pack(anchor="w", pady=(4, 0))

    def _tick(self) -> None:
        if not self.busy:
            try:
                self.refresh_lanes(force=False)
            except Exception:
                pass
        self.root.after(2500, self._tick)

    # ── 任务执行（后台线程 + 日志流）────────────────────────────────────────
    def run_job(self, title: str, fn, on_done=None) -> None:
        if self.busy:
            self.set_status("上一个任务还在进行中，请稍候…")
            return
        self.busy = True
        self.busy_label.configure(text=f"● {title} 进行中…")
        self.set_status(title)
        self.log("", None)
        self.log(f"──── {title} ────", "head")

        def worker() -> None:
            old = sys.stdout
            sys.stdout = QueueWriter(self.q)  # type: ignore[assignment]
            try:
                fn()
                self.q.put(("job_done", title))
                if on_done:
                    self.q.put(("callback", on_done))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("line", f"[XX] {type(exc).__name__}: {exc}"))
                self.q.put(("job_fail", title))
            finally:
                sys.stdout = old

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        pending: list[tuple[str, str | None]] = []   # 攒一批一起写，避免逐行重绘
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    pending.append((payload, None))
                    continue
                if pending:                            # 先落盘日志，保持顺序可读
                    self._log_lines(pending)
                    pending = []
                if kind == "job_done":
                    self.busy = False
                    self.busy_label.configure(text="")
                    self.set_status(f"{payload} 完成")
                    self._invalidate()                 # 刚起停过，状态必变
                    self.refresh_lanes(force=True)
                elif kind == "job_fail":
                    self.busy = False
                    self.busy_label.configure(text="")
                    self.set_status(f"{payload} 失败")
                    self._invalidate()
                    self.refresh_lanes(force=True)
                elif kind == "callback":
                    try:
                        payload()
                    except Exception as exc:  # noqa: BLE001
                        self._log_lines([(f"[XX] 回调异常：{exc}", None)])
        except queue.Empty:
            pass
        if pending:
            self._log_lines(pending)
        self.root.after(120, self._poll_queue)

    # ── 操作 ────────────────────────────────────────────────────────────────
    # ── 端口框的占位提示 ────────────────────────────────────────────────────
    @staticmethod
    def _parse_port_text(text: str) -> int | None:
        """端口框内容 → 端口号。空 / 占位符 ＝ None（自动挑空闲）。"""
        value = (text or "").strip()
        if not value or value == "自动":
            return None
        if not value.isdigit():
            raise ValueError(f"端口必须是数字，得到 {value!r}")
        return int(value)

    def _attach_port_hint(self, entry: tk.Entry) -> None:
        """端口框的占位提示：空框里显示灰色的「自动」，焦点进入清掉。"""
        placeholder = "自动"

        def show_placeholder() -> None:
            if not entry.get():
                entry.insert(0, placeholder)
                entry.configure(fg=GRAY)

        def on_focus_in(_e) -> None:
            if entry.get() == placeholder:
                entry.delete(0, "end")
                entry.configure(fg=TEXT)

        def on_focus_out(_e) -> None:
            show_placeholder()

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        show_placeholder()

    # ── 打开 / 停止 ────────────────────────────────────────────────────────
    def open_profile(self, lane: str, profile: str, port_text: str | int | None = None) -> None:
        """打开一个 profile。port_text 为空 ＝ 这次启动实时挑空闲端口。"""
        try:
            port = self._parse_port_text(str(port_text)) if port_text is not None else None
        except ValueError as exc:
            self.log(f"[!!] {exc}", "warn")
            self.set_status("端口填写有误")
            return
        args = argparse.Namespace(
            target=f"{lane}/{profile}", profile=profile, port=port, cwd=None, timeout=None,
            no_browser=False, detach=True, adopt_only=False,
        )

        def job() -> None:
            core.cmd_open(core.load_config(), args)

        self.run_job(f"打开 {lane}/{profile}", job)

    def stop_profile(self, lane: str, profile: str) -> None:
        args = argparse.Namespace(target=[f"{lane}/{profile}"])

        def job() -> None:
            core.cmd_stop(core.load_config(), args)

        self.run_job(f"停止 {lane}/{profile}", job)

    def stop_lane(self, lane: str) -> None:
        args = argparse.Namespace(target=[lane])

        def job() -> None:
            core.cmd_stop(core.load_config(), args)

        self.run_job(f"停止 {lane} 的全部 profile", job)

    def stop_all(self) -> None:
        cfg = core.load_config()
        running = [
            f"{lane}/{prof}"
            for lane, profs in core.lane_states(cfg).items()
            for prof, rt in (profs or {}).items()
            if rt
        ]
        if not running:
            self.set_status("没有正在运行的 profile")
            return
        self.log(f"准备停止：{'、'.join(running)}", "muted")

        def job() -> None:
            core.cmd_stop(core.load_config(), argparse.Namespace(target=running))

        self.run_job("全部停止", job)

    def show_lane_log(self, lane: str, profile: str = core.DEFAULT_PROFILE) -> None:
        log_path = core.lane_log_path(self.cfg, lane, profile)
        if not log_path.is_file():
            self.log(f"[!!] 这个 profile 还没有日志：{log_path}", "warn")
            return
        self.log(f"──── {lane}/{profile} 日志尾部（{log_path}）────", "head")
        for line in core.tail_lines(log_path, 40):
            self.log(line)

    # ── profile 管理 ────────────────────────────────────────────────────────
    def add_profile_dialog(self, lane: str) -> None:
        """新建 profile：干净环境（官方 web 组合）或整目录复制现有 profile（含插件）。"""
        cfg = core.load_config()
        view = core.lane_profiles_view(cfg, lane)
        on_disk = [n for n, v in sorted(view.items()) if v.get("onDisk")] or [core.DEFAULT_PROFILE]

        dlg = tk.Toplevel(self.root)
        dlg.title("添加 Profile")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 520, 400
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 110
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"给 lane「{lane}」添加一个环境", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(
            dlg,
            text=(
                "profile＝同一套 dsh（同版本、同 HOME）里的一个环境：\n"
                "会话 / 凭据 / 设置共享，插件组合与补丁层各自独立——\n"
                "一个版本就能开出多环境，不必复制多套 dsh。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=470,
        ).pack(anchor="w", padx=20)

        form = tk.Frame(dlg, bg=BG)
        form.pack(fill="x", padx=20, pady=12)
        form.columnconfigure(1, weight=1)
        tk.Label(form, text="名称", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(
            row=0, column=0, sticky="w", pady=6
        )
        name_var = tk.StringVar()
        ttk.Entry(form, textvariable=name_var).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)

        mode_var = tk.StringVar(value="copy" if on_disk else "clean")
        for value, text in (
            ("copy", "复制现有 profile（连同已装插件、补丁层一起带走）"),
            ("clean", "干净环境（只有官方 web 组合，插件之后自己装）"),
        ):
            tk.Radiobutton(
                form, text=text, value=value, variable=mode_var, bg=BG, fg=TEXT,
                selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                font=(FONT_UI, 9), highlightthickness=0, cursor="hand2",
            ).grid(row=1 if value == "copy" else 2, column=0, columnspan=2, sticky="w", pady=2)

        src_row = tk.Frame(form, bg=BG)
        src_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(src_row, text="复制自", bg=BG, fg=MUTED, font=(FONT_UI, 9)).pack(side="left")
        src_var = tk.StringVar(value=(core.DEFAULT_PROFILE if core.DEFAULT_PROFILE in on_disk
                                      else on_disk[0]))
        ttk.Combobox(src_row, textvariable=src_var, values=on_disk, width=16,
                     state="readonly").pack(side="left", padx=(8, 0))

        hint = tk.Label(dlg, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9), anchor="w",
                        justify="left", wraplength=470)
        hint.pack(fill="x", padx=20)

        def start() -> None:
            name = name_var.get().strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name):
                messagebox.showwarning(
                    "名称不合法", "profile 名只能用字母/数字开头，含字母 数字 . _ -。", parent=dlg
                )
                return
            from_profile = src_var.get().strip() if mode_var.get() == "copy" else None
            if from_profile == name:
                messagebox.showwarning("复制", "源 profile 和新名字不能相同", parent=dlg)
                return
            dlg.destroy()
            args = argparse.Namespace(lane=lane, name=name, from_profile=from_profile)
            self.run_job(f"添加 profile {lane}/{name}",
                         lambda: core.cmd_profile_add(core.load_config(), args))

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(6, 16), side="bottom")
        self.button(btns, "添加", start, primary=True).pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)
        name_var.trace_add("write", lambda *_a: hint.configure(
            text=f"将以 {lane}/{name_var.get().strip()} 启动： py dsh_lanes.py open {lane}/{name_var.get().strip()}",
            fg=GRAY))

    def remove_profile(self, lane: str, profile: str) -> None:
        """移除一个 profile（默认 profile web 不可删；运行中的会先被拒绝）。"""
        if profile == core.DEFAULT_PROFILE:
            messagebox.showinfo("移除 Profile", "默认 profile「web」是启动器功能的默认对象，不允许移除。")
            return
        if not messagebox.askyesno(
            "移除 Profile",
            f"移除 profile「{lane}/{profile}」？\n\n"
            "· 会删掉它在磁盘上的目录（HOME\\profiles\\<名>），里面装的插件无法找回\n"
            "· 会话与凭据在 HOME 级，不受影响\n"
            "· 正在运行的话这一步会被拒绝（先停止它）",
            default="no",
        ):
            return
        args = argparse.Namespace(lane=lane, name=profile, yes=True)

        def job() -> None:
            core.cmd_profile_rm(core.load_config(), args)

        self.run_job(f"移除 profile {lane}/{profile}", job)

    def manual_refresh(self) -> None:
        """「刷新」按钮 / F5：重画卡片，顺带重查一次 npm 上的版本（10 分钟内查过就用缓存）。"""
        self._adopt_attempted = False   # 允许再扫一遍磁盘上未登记的 lane
        self.refresh_lanes(force=True)
        self.refresh_remote(force=True)

    def open_repo(self) -> None:
        """打开 dsh 官方代码库（地址来自已装包 package.json 的 repository 字段）。"""
        self.log(f"用浏览器打开官方代码库 {core.REPO_URL}", "muted")
        try:
            webbrowser.open(core.REPO_URL)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 打开失败：{exc}", "warn")

    def refresh_remote(self, force: bool = False) -> None:
        """查一次 npm 的 dist-tag，显示在标题下面那一行。

        纯只读、走后台线程：查不到只把这行文字变黄并往日志里记一句，不弹窗、不写文件。
        默认 10 分钟内不重复查（点「刷新」= force 重查）。
        """
        if self._remote_busy:
            return
        if not force and time.time() - self._remote_at < 600:
            return
        self._remote_busy = True
        self.remote_label.configure(text="正在查询 npm 上的最新版本…", fg=MUTED)

        # 本机已装了哪些版本（拿登记表比，不发任何请求）
        installed = {str((d or {}).get("version") or "") for d in (self.cfg.get("lanes") or {}).values()}

        def fail(reason: str) -> None:
            self._remote_done("查不到 npm 上的版本（离线或 registry 不通），点「刷新」重试", AMBER)
            self.log(f"[!!] 查询 npm 版本失败：{reason}", "warn")

        def worker() -> None:
            try:
                packument, reg = core.fetch_index(core.load_config())
            except Exception as exc:  # noqa: BLE001
                # 用主线程轮询的队列回话（而不是 worker 里调 root.after）：
                # 窗口不在 mainloop 里时（自检、正在关闭）after 会直接抛 RuntimeError。
                self.q.put(("callback", lambda: fail(str(exc))))
                return
            tags = packument.get("dist-tags") or {}
            order = ["latest", "next", "alpha"]
            keys = [k for k in order if k in tags] + sorted(k for k in tags if k not in order)
            bits = [f"{tag} {tags[tag]}" + ("√" if str(tags[tag]) in installed else "") for tag in keys]
            # 把实际答话的源写在这儿：镜像常常落后官方一两天，"这行数字从哪来"必须看得见
            text = f"npm 上（{core.registry_label(reg)}）：" + "　·　".join(bits)
            self.q.put(("callback", lambda: self._remote_done(text, MUTED)))

        threading.Thread(target=worker, daemon=True).start()

    def _remote_done(self, text: str, fg: str) -> None:
        self._remote_busy = False
        self._remote_at = time.time()
        try:
            self.remote_label.configure(text=text, fg=fg)
        except tk.TclError:
            pass

    def open_root_dir(self) -> None:
        root = core.ensure_layout(core.load_config())["root"]
        try:
            os.startfile(str(root))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 打不开目录：{exc}", "warn")

    def open_url(self, url: str) -> None:
        """未登记实例只能用裸地址打开：浏览器里若已有该实例的登录 cookie 就能直接进，
        否则会看到 401（DSH 的信任围栏，token 只在它自己打印的 URL 里）。"""
        self.log(f"用浏览器打开 {url}", "muted")
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 打开失败：{exc}", "warn")

    def kill_instance(self, item: dict) -> None:
        pid = int(item["pid"])
        if not messagebox.askyesno(
            "结束进程",
            f"确定结束端口 {item['port']} 上的进程吗？\n\n"
            f"PID {pid}　进程 {item['exe']}\n"
            "这不是本启动器启动的实例——如果它正是你此刻在用的 DSH，"
            "当前页面会立刻断开。",
            icon="warning",
            default="no",
        ):
            return

        def job() -> None:
            core.kill_tree(pid)
            print(f"已结束 PID {pid}（端口 {item['port']}）")
            core.invalidate_tcp_cache()  # 让下一次刷新重新发现

        self.run_job(f"结束 {item['exe']} (PID {pid})", job)

    def adopt_install(self, item: dict) -> None:
        """把本机已有的一份安装注册成 lane，沿用它的 DSH_HOME——
        这样就能用同一个窗口"打开 / 停止"你平时在用的那套。"""
        cfg = core.load_config()
        want = str(item["path"]).lower()
        for name, data in cfg.get("lanes", {}).items():
            if isinstance(data, dict) and str(data.get("installDir", "")).lower() == want:
                self.log(f"[!!] 这份安装已经注册为 lane「{name}」了", "warn")
                self.set_status(f"已注册为 lane「{name}」")
                return
        default_name = {"npm 全局": "global", "npx 缓存": "npx"}.get(item["source"], "adopted")
        home = str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh")
        home_note = "（就是你现有的那份，含已有会话 / 设置 / 插件）"
        extra = ""
        if str(item.get("source", "")).startswith("versions 目录"):
            # 根目录 versions\<版本> 里的孤儿安装树：lane 名沿用建名习惯，HOME 用根目录下的新目录
            default_name = "lane-" + re.sub(r"[^A-Za-z0-9]+", "-", str(item.get("version") or "")).strip("-")[:12]
            home = str(core.paths(cfg)["homes"] / default_name)
            home_note = "（根目录下的新 HOME；这个版本树原来自带的 HOME 若还在，刷新时会自动认领成 lane）"
        elif str(item.get("source", "")).startswith("clones 目录"):
            default_name = Path(item["tree"]).name
            home = str(core.paths(cfg)["homes"] / default_name)
            home_note = "（clone 布局：homes\\<同名>；HOME 不在的话会新建一个空的）"
            extra = "\n\n提示：如果它的 HOME（homes\\<同名>）也还在，直接点「刷新」就会自动认领，不用手动加。"
        if not messagebox.askyesno(
            "加入 lane 列表",
            "把这份安装注册成一条 lane？\n\n"
            f"来源　　　{item['source']}\n"
            f"版本　　　{item['version']}\n"
            f"安装目录　{item['path']}\n"
            f"lane 名　　{default_name}\n"
            "端口　　　web profile 偏好 3080（被占用会自动改口）\n"
            f"DSH_HOME　{home}\n"
            f"　　　　　{home_note}\n\n"
            "注册后就能在本窗口「打开 / 停止」它，和你原来的启动方式是同一套环境；\n"
            "HOME 里现成的 profiles（web / desktop / …）会显示在卡片上，可分别启动。"
            + extra,
        ):
            return
        args = argparse.Namespace(
            lane=default_name, install=item["path"], port=None, home=home, force=True
        )
        self.run_job(f"加入 lane：{default_name}", lambda: core.cmd_adopt(core.load_config(), args))

    # ── 凭据（API key）按条目复制 ───────────────────────────────────────────
    def credential_dialog(self, lane: str) -> None:
        """在 lane 之间按条目复制 API key：选源 → 勾选提供商条目 → 选目标 → 复制。

        v0.3 起取消「主要版本自动继承」：哪个环境要用哪把 key 由用户自己勾选决定，
        启动器只负责把选中的 refs 条目安全地合并进目标 HOME 的 .credentials.yaml。
        """
        cfg = core.load_config()
        lanes = [n for n, d in cfg.get("lanes", {}).items() if isinstance(d, dict)]
        if len(lanes) < 1:
            messagebox.showinfo("凭据", "还没有任何 lane，没有可复制的凭据。")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("凭据 · API key 按条目复制")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 620, 540
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 70
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text="按条目复制 API key", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=20, pady=(16, 2))
        tk.Label(
            dlg,
            text=(
                "每个 HOME 的 .credentials.yaml 里有一个 refs 段：一条 = 一个提供商的 key 引用。\n"
                "选一个源、勾选要的条目、选目标 lane，复制只合并这几条，其它内容一字不动\n"
                "（原文件会先备份成 .credentials.yaml.bak；目标正在运行时要重启才生效）。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=570,
        ).pack(anchor="w", padx=20)

        top = tk.Frame(dlg, bg=BG)
        top.pack(fill="x", padx=20, pady=(10, 0))
        tk.Label(top, text="源 lane", bg=BG, fg=TEXT, font=(FONT_UI, 10)).pack(side="left")
        src_var = tk.StringVar(value=lane)
        ttk.Combobox(top, textvariable=src_var, values=lanes, width=16,
                     state="readonly").pack(side="left", padx=(8, 18))
        tk.Label(top, text="目标 lane", bg=BG, fg=TEXT, font=(FONT_UI, 10)).pack(side="left")
        dst_var = tk.StringVar(value=lane)
        ttk.Combobox(top, textvariable=dst_var, values=lanes, width=16,
                     state="readonly").pack(side="left", padx=(8, 0))

        src_info = tk.Label(dlg, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9),
                            anchor="w", justify="left", wraplength=570)
        src_info.pack(fill="x", padx=20, pady=(8, 0))

        # 条目勾选区
        box = tk.Frame(dlg, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="both", expand=True, padx=20, pady=(8, 0))
        vars_by_key: dict[str, tk.BooleanVar] = {}
        empty_hint = tk.Label(box, text="（读取中…）", bg=CARD, fg=GRAY, font=(FONT_UI, 9))
        empty_hint.pack(anchor="w", padx=14, pady=10)

        def refresh_entries(*_a) -> None:
            src = src_var.get()
            for child in box.winfo_children():
                child.destroy()
            vars_by_key.clear()
            refs = core.read_credential_refs((cfg["lanes"].get(src) or {}).get("home") or "")
            if not refs:
                empty_hint = tk.Label(
                    box,
                    text=f"（源「{src}」的 HOME 里还没有凭据条目——先把它启动一次并填好 key）",
                    bg=CARD, fg=AMBER, font=(FONT_UI, 9), justify="left", wraplength=540,
                )
                empty_hint.pack(anchor="w", padx=14, pady=10)
                src_info.configure(text="")
                return
            tk.Label(
                box, text="勾选要复制的条目（提供商）：", bg=CARD, fg=MUTED, font=(FONT_UI, 8)
            ).pack(anchor="w", padx=14, pady=(8, 2))
            for key in sorted(refs):
                var = tk.BooleanVar(value=True)
                vars_by_key[key] = var
                tk.Checkbutton(
                    box, text=key, variable=var, bg=CARD, fg=TEXT, selectcolor="#FFFFFF",
                    activebackground=CARD, activeforeground=TEXT, font=(FONT_MONO, 9),
                    anchor="w", highlightthickness=0, cursor="hand2",
                ).pack(fill="x", padx=(24, 14), pady=1)
            src_info.configure(text=f"来源：{core.credentials_path((cfg['lanes'].get(src) or {}).get('home') or '')}")

        src_var.trace_add("write", refresh_entries)
        refresh_entries()

        def do_copy() -> None:
            src, dst = src_var.get(), dst_var.get()
            if src == dst:
                messagebox.showwarning("凭据", "源和目标是同一条 lane。", parent=dlg)
                return
            selected = sorted(k for k, v in vars_by_key.items() if v.get())
            if not selected:
                messagebox.showwarning("凭据", "至少勾选一个条目（或换一个源）。", parent=dlg)
                return
            if not messagebox.askyesno(
                "复制凭据",
                f"把「{src}」的 {len(selected)} 个条目复制给「{dst}」？\n\n"
                f"　{'、'.join(selected)}\n\n"
                "只合并这几条 refs；目标的会话、设置、插件不受影响。",
                parent=dlg,
            ):
                return
            args = argparse.Namespace(source=src, dest=dst, only=",".join(selected))
            dlg.destroy()

            def job() -> None:
                core.cmd_copy_key(core.load_config(), args)

            self.run_job(f"复制凭据 {src} → {dst}", job)

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(10, 16), side="bottom")
        self.button(btns, "复制选中的条目", do_copy, primary=True).pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)

    # ── 删除一套 DSH ────────────────────────────────────────────────────────
    def delete_lane(self, lane: str) -> None:
        """删除按钮：先算清会动哪些文件，再弹确认框（手打名称二次确认）。"""
        cfg = core.load_config()
        if lane not in cfg.get("lanes", {}):
            self.log(f"[!!] 没有 lane「{lane}」", "warn")
            return
        try:
            plan = core.lane_delete_plan(cfg, lane, sizes=False)   # 体积放后台算，别卡界面
        except Exception as exc:  # noqa: BLE001
            self.log(f"[XX] 无法计算删除范围：{exc}", "err")
            return
        rt = core.lane_any_running(cfg, lane)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"删除 DSH · {lane}")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)

        # 标题区
        title = tk.Frame(dlg, bg=BG)
        title.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(
            title,
            text="删除这套 DSH",
            bg=BG,
            fg=RED,
            font=(FONT_UI, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title,
            text=f"lane「{lane}」　·　版本 {plan['version']}"
                 + (f"　·　profiles：{'、'.join(plan['profiles'])}" if plan["profiles"] else "")
                 + ("　·　接管型（安装树在 npm 全局，本启动器不会删它）" if plan["adopted"] else ""),
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
        ).pack(anchor="w", pady=(2, 0))

        # 内容清单：每一条右侧带一个勾选框（勾＝删，不勾＝留）
        box = tk.Frame(dlg, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="x", padx=18, pady=(10, 0))

        def row(text: str, color=MUTED) -> tk.Label:
            lab = tk.Label(
                box,
                text=text,
                bg=CARD,
                fg=color,
                font=(FONT_UI, 9),
                anchor="w",
                justify="left",
                wraplength=520,
            )
            lab.pack(anchor="w", padx=14, pady=(4, 2))
            return lab

        def item(label_text: str, size: str = "", check: tk.BooleanVar | None = None,
                 color=TEXT, note: str = "") -> tk.Label:
            line = tk.Frame(box, bg=CARD)
            line.pack(fill="x", padx=(14, 14), pady=1)
            if check is not None:
                tk.Checkbutton(
                    line,
                    variable=check,
                    bg=CARD,
                    activebackground=CARD,
                    selectcolor="#FFFFFF",
                    highlightthickness=0,
                    bd=0,
                    width=2,
                    command=None,
                ).pack(side="right", padx=(8, 0))
            txt = label_text + (f"　（{size}）" if size else "")
            lab = tk.Label(
                line,
                text=txt,
                bg=CARD,
                fg=color,
                font=(FONT_UI, 9),
                anchor="w",
                justify="left",
                wraplength=560,
            )
            lab.pack(side="left", anchor="w")
            if note:  # 单独一行，否则长理由会被窗口边缘截断
                tk.Label(
                    box,
                    text=note,
                    bg=CARD,
                    fg=GRAY,
                    font=(FONT_UI, 8),
                    anchor="w",
                    justify="left",
                    wraplength=560,
                ).pack(anchor="w", padx=(32, 14), pady=(0, 2))
            return lab

        # 目录体积要全盘遍历（几百毫秒），不能在主线程算 —— 先摆"计算中…"，后台补上。
        size_slots: list[tuple[tk.Label, str, Path]] = []

        def item_sized(prefix: str, path: Path, check: tk.BooleanVar | None = None,
                       color=TEXT, note: str = "") -> None:
            lab = item(f"{prefix}　（计算中…）", check=check, color=color, note=note)
            size_slots.append((lab, prefix, path))

        row("将删除 / 保留", TEXT)
        item(f"登记信息　lane「{lane}」", size="必删")
        for run_path in plan["runs"]:
            if Path(run_path).is_file():
                item(f"运行态　　{Path(run_path).name}", size="必删")
        for log_path in plan["logs"]:
            if Path(log_path).is_file():
                item(f"启动日志　{Path(log_path).name}", size="必删")

        # 安装树
        can_install = plan["install_inside"] and not plan["shared_with"]
        del_install_var = tk.BooleanVar(value=True)
        if can_install:
            item_sized(f"安装树　　{plan['install']}", Path(plan["install"]), del_install_var)
        else:
            why = (
                "保留 —— 接管型：删的是 npm 全局那份，要卸载请用 npm uninstall -g"
                if plan["adopted"]
                else f"保留 —— 其它 lane 还在共用这棵树：{'、'.join(plan['shared_with'])}"
            )
            item_sized(f"安装树　　{plan['install']}", Path(plan["install"]), color=GRAY, note=why)

        # DSH_HOME
        home_inside = plan["home_inside"]
        keep_home_var = tk.BooleanVar(value=False)   # 根目录内：不勾＝删
        danger_var = tk.BooleanVar(value=False)      # 根目录外：勾上才会删
        if home_inside:
            item_sized(f"DSH_HOME　{plan['home']}", Path(plan["home"]), keep_home_var)
            note = tk.Label(
                box,
                text="勾上＝保留 DSH_HOME（只删安装树和登记，会话记录留着）",
                bg=CARD,
                fg=GRAY,
                font=(FONT_UI, 8),
                anchor="w",
            )
            note.pack(anchor="w", padx=(30, 14), pady=(0, 2))
        else:
            item_sized(f"DSH_HOME　{plan['home']}", Path(plan["home"]),
                       danger_var, color=RED)
            tk.Label(
                box,
                text="★ 它不在 lane 根目录里（像是你现有的 HOME），里面有真实会话记录、设置和插件，\n"
                     "　 默认保留。确实要一起删才勾左边的框 —— 删了无法恢复。",
                bg=CARD,
                fg=RED,
                font=(FONT_UI, 8),
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=(30, 14), pady=(0, 2))

        box.pack_configure(pady=(10, 0))

        def fill_sizes() -> None:
            """后台算体积，算完通过 after 回主线程贴到标签上（对话框可能已经被关掉）。"""
            done = [(lab, prefix, core.dir_size(p)) for lab, prefix, p in size_slots]

            def apply() -> None:
                for lab, prefix, nbytes in done:
                    try:
                        if lab.winfo_exists():
                            lab.configure(text=f"{prefix}　（{core.human_size(nbytes)}）")
                    except tk.TclError:
                        pass  # 对话框已关闭

            try:
                self.root.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=fill_sizes, daemon=True).start()

        # 运行中：必须先停下来
        stop_var = tk.BooleanVar(value=True)
        if rt:
            stopbar = tk.Frame(dlg, bg=AMBER_BG, highlightbackground=AMBER, highlightthickness=1)
            stopbar.pack(fill="x", padx=18, pady=(10, 0))
            tk.Checkbutton(
                stopbar,
                text=f"它正在运行（PID {rt.get('pid')}，端口 {rt.get('port')}）—— 先停止它再删除",
                variable=stop_var,
                bg=AMBER_BG,
                fg=AMBER,
                font=(FONT_UI, 9, "bold"),
                activebackground=AMBER_BG,
                selectcolor="#FFFFFF",
                anchor="w",
                padx=10,
                pady=6,
            ).pack(anchor="w")

        # 确认输入
        conf = tk.Frame(dlg, bg=BG)
        conf.pack(fill="x", padx=18, pady=(12, 0))
        tk.Label(
            conf, text="请输入 lane 名称 ", bg=BG, fg=TEXT, font=(FONT_UI, 10)
        ).pack(side="left")
        tk.Label(conf, text=lane, bg=BG, fg=RED, font=(FONT_MONO, 10, "bold")).pack(side="left")
        tk.Label(conf, text=" 以确认：", bg=BG, fg=TEXT, font=(FONT_UI, 10)).pack(side="left")
        entry_var = tk.StringVar()
        entry = ttk.Entry(conf, textvariable=entry_var, width=18)
        entry.pack(side="left", padx=(8, 0))
        entry.focus_set()

        hint = tk.Label(dlg, text="", bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        hint.pack(fill="x", padx=18, pady=(6, 0))

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=18, pady=(8, 16))

        def collect():
            """把三个勾选框翻译成 cmd_delete 的参数。"""
            if home_inside:
                keep_home, home_too = bool(keep_home_var.get()), False
            else:
                keep_home, home_too = True, bool(danger_var.get())
            keep_install = not bool(del_install_var.get()) if can_install else True
            return keep_home, home_too, keep_install

        del_btn = self.button(btns, "删除", lambda: None, danger=True)
        del_btn.pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)

        def refresh_btn(*_a) -> None:
            name_ok = entry_var.get().strip() == lane
            run_ok = (not rt) or stop_var.get()
            _keep_home, _home_too, keep_install = collect()
            if not name_ok:
                hint.configure(text=f"为避免误删，请完整输入「{lane}」", fg=MUTED)
            elif not run_ok:
                hint.configure(text="它正在运行，必须先勾上「先停止它」", fg=RED)
            else:
                bits = []
                bits.append("保留安装树" if keep_install else "删除安装树")
                if home_inside:
                    bits.append("保留 DSH_HOME" if keep_home_var.get() else "删除 DSH_HOME")
                elif danger_var.get():
                    bits.append("连现有 DSH_HOME 一起删除")
                else:
                    bits.append("保留现有 DSH_HOME")
                hint.configure(text="确认无误：" + "、".join(bits), fg=GREEN)
            del_btn.configure(state=("normal" if (name_ok and run_ok) else "disabled"))
            del_btn.configure(text="删除")

        def do_delete() -> None:
            keep_home, home_too, keep_install = collect()
            args = argparse.Namespace(
                lane=lane,
                yes=True,
                stop=bool(rt) and stop_var.get(),
                keep_home=keep_home,
                keep_install=keep_install,
                home_too=home_too,
            )
            dlg.destroy()

            def job() -> None:
                core.cmd_delete(core.load_config(), args)
                core.invalidate_tcp_cache()  # 让下一次刷新重新发现实例

            self.run_job(f"删除 {lane}", job)

        del_btn.configure(command=do_delete)
        entry_var.trace_add("write", refresh_btn)
        stop_var.trace_add("write", refresh_btn)
        keep_home_var.trace_add("write", refresh_btn)
        danger_var.trace_add("write", refresh_btn)
        del_install_var.trace_add("write", refresh_btn)
        refresh_btn()
        entry.bind("<Return>", lambda _e: do_delete() if str(del_btn["state"]) == "normal" else None)

        dlg.update_idletasks()
        w = 660
        h = min(760, max(360, dlg.winfo_reqheight() + 8))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 40
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

    # ── 新建版本对话框 ──────────────────────────────────────────────────────
    def clone_lane(self, lane: str) -> None:
        """复制这条 lane（安装树 + DSH_HOME 一起）——「先升级副本、看插件还活不活」的入口。"""
        cfg = core.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("复制这套 DSH")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 580, 380
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 80
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(
            dlg, text=f"复制「{lane}」生成副本", bg=BG, fg=TEXT, font=(FONT_UI, 13, "bold")
        ).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(
            dlg,
            text=(
                "副本带走这套的全部状态：会话记录、设置、已装插件、API key。\n"
                "副本有自己的安装树和 DSH_HOME —— 升级副本不会碰到原件，出问题删掉副本就行。\n"
                "典型用法：复制主要版本 → 升级副本 → 点「核对隔离」→ 看插件还都在不在。"
            ),
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=20)

        form = tk.Frame(dlg, bg=BG)
        form.pack(fill="x", padx=20, pady=14)
        form.columnconfigure(1, weight=1)
        tk.Label(form, text="新 lane 名", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(
            row=0, column=0, sticky="w", pady=6
        )
        name_var = tk.StringVar(value=f"{lane}-copy")
        ttk.Entry(form, textvariable=name_var).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)
        tk.Label(
            form,
            text="端口：启动时实时挑空闲端口（这条 lane 的全部 profile 都会跟着 HOME 复制过来）",
            bg=BG, fg=GRAY, font=(FONT_UI, 8),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        size_hint = tk.Label(dlg, text="正在估算体积…", bg=BG, fg=GRAY, font=(FONT_UI, 9), anchor="w")
        size_hint.pack(fill="x", padx=20)

        def fill_size() -> None:
            # 体积统计要扫 1GB 多的目录，放后台线程，别冻住对话框
            try:
                plan = core.lane_clone_plan(
                    core.load_config(), lane, name_var.get().strip() or f"{lane}-copy", sizes=True
                )
                need = (plan["home_size"] or 0) + (plan["install_size"] or 0)
                parts = [
                    f"预计复制 {core.human_size(need)}"
                    f"（安装树 {core.human_size(plan['install_size'] or 0)}"
                    f" + HOME {core.human_size(plan['home_size'] or 0)}）"
                ]
                if plan["free"]:
                    parts.append(f"目标盘剩余 {core.human_size(plan['free'])}")
                if plan["problems"]:
                    parts.append("问题：" + "；".join(plan["problems"]))
                text = "，".join(parts)
                bg, fg = (AMBER_BG, AMBER) if plan["problems"] else (BG, GRAY)
            except Exception as exc:  # noqa: BLE001
                text, bg, fg = f"体积估算失败：{exc}", AMBER_BG, AMBER

            def apply() -> None:
                try:
                    if dlg.winfo_exists():
                        size_hint.configure(text=text, bg=bg, fg=fg)
                except tk.TclError:
                    pass

            self.root.after(0, apply)

        threading.Thread(target=fill_size, daemon=True).start()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(6, 16), side="bottom")

        def start() -> None:
            new = name_var.get().strip()
            if not new:
                messagebox.showwarning("复制", "先填新 lane 名", parent=dlg)
                return
            if new == lane:
                messagebox.showwarning("复制", "新 lane 名不能和源一样", parent=dlg)
                return
            if new in (core.load_config().get("lanes") or {}):
                messagebox.showwarning("复制", f"lane「{new}」已经存在了", parent=dlg)
                return
            dlg.destroy()
            self.run_job(
                f"复制 {lane} → {new}",
                lambda: core.cmd_clone(
                    core.load_config(), argparse.Namespace(source=lane, lane=new, port=None)
                ),
            )

        ttk.Button(btns, text="开始复制", command=start).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))

    def verify_lane(self, lane: str) -> None:
        """核对这条 lane 是否真的和别的版本隔离（副本最该看这个）。"""
        self.run_job(
            f"核对隔离：{lane}",
            lambda: core.cmd_verify(core.load_config(), argparse.Namespace(lane=lane)),
        )

    def upgrade_dialog(self, lane: str) -> None:
        """把这条 lane 精确换到另一个版本（升完核对 + 启动冒烟，起不来自动退回）。

        为什么值得单独一个对话框：换 DSH 版本是这套启动器里**最容易出大事**的动作，
        所以三件事必须摆在明面上 —— 当前版本、会装到哪棵树、失败怎么退。
        真正干活的是 `core.cmd_upgrade`（和 CLI 同一条路），输出进下面的日志面板。
        """
        cfg = core.load_config()
        data = (cfg.get("lanes") or {}).get(lane) or {}
        plan = core.lane_upgrade_plan(cfg, lane)
        running = core.lane_any_running(cfg, lane)

        dlg = tk.Toplevel(self.root)
        dlg.title("升级版本")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        w, h = 700, 640
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 50
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"换 DSH 版本：lane「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        head = tk.Label(dlg, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9), justify="left",
                        wraplength=650, anchor="w")
        head.pack(anchor="w", padx=18)
        head.configure(text=(
            f"当前版本  {data.get('version') or '?'}\n"
            f"安装树    {plan['dir']}\n"
            f"升级方式  {plan['why']}"
            + (f"\n它现在正在运行（pid {running.get('pid')}，端口 {running.get('port')}）"
               " —— 升级会先停掉它，升完再起回来。" if running else "")
        ))

        note = tk.Label(dlg, text="", bg=BG, fg=AMBER, font=(FONT_UI, 9), justify="left",
                        anchor="w", wraplength=650)
        note.pack(anchor="w", padx=18, pady=(6, 0))
        external = plan["mode"] == "external"
        if external:
            note.configure(
                text="这条 lane 的安装树在本启动器目录之外（你日常真正在用的那一套）——"
                     "这个窗口有意不动它。要动它得单独一步、单独确认。",
                fg=RED,
            )

        reg_var, reg_remember = self.registry_row(dlg, cfg, pady=(12, 0))

        tk.Label(dlg, text="从 registry 挑一版（点一下填进下面，也可以自己手打版本号）",
                 bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w").pack(anchor="w", padx=18, pady=(12, 2))
        listbox = tk.Listbox(dlg, height=9, bg=CARD, fg=TEXT, font=(FONT_MONO, 9),
                             selectbackground=ACCENT, highlightthickness=0, activestyle="none")
        listbox.pack(fill="both", expand=True, padx=18)

        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=18, pady=(10, 2))
        tk.Label(row, text="版本", bg=BG, fg=MUTED, font=(FONT_UI, 9)).pack(side="left")
        var = tk.StringVar(value="")
        entry = tk.Entry(row, textvariable=var, font=(FONT_MONO, 10), width=24)
        entry.pack(side="left", padx=(8, 0))
        hint = tk.Label(row, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9))
        hint.pack(side="left", padx=(10, 0))

        foot = tk.Frame(dlg, bg=BG)
        foot.pack(fill="x", padx=18, pady=(8, 14))
        order: list[str] = []

        def pick(_event=None) -> None:
            sel = listbox.curselection()
            if sel and sel[0] < len(order):
                var.set(order[sel[0]])

        listbox.bind("<<ListboxSelect>>", pick)

        def start(rollback: bool, dry_run: bool = False) -> None:
            version = None if rollback else var.get().strip()
            if rollback:
                target = (data.get("previousVersion") or "").strip()
                if not target:
                    messagebox.showinfo("没有可退回的版本",
                                        "这条 lane 还没有升级记录，没有可退回的版本。", parent=dlg)
                    return
                if not dry_run and not messagebox.askyesno(
                    "退回上一版",
                    f"把「{lane}」退回 {target}？\n\n"
                    "会做：换回那棵树 → 核对 → 启动冒烟；起不来自动退回。",
                    parent=dlg,
                ):
                    return
            else:
                if not version:
                    messagebox.showinfo("先填版本号",
                                        "在上面挑一版或手打一个版本号（例如 0.1.7-rc.2 / latest）。",
                                        parent=dlg)
                    return
                if not dry_run and not messagebox.askyesno(
                    "升级",
                    f"把「{lane}」升到 {version}？\n\n"
                    f"当前 {data.get('version') or '?'} → 装到 {plan['dir']}\n"
                    f"下载源：{core.registry_label(core.registry_plan(cfg, reg_var.get())[0])}\n"
                    "会做：升级 → 核对 → 启动冒烟；起不来自动退回原版本。",
                    parent=dlg,
                ):
                    return
            args = argparse.Namespace(
                lane=lane, version=version, rollback=rollback, stop=True,
                no_boot_check=False, dry_run=dry_run, registry=reg_var.get(),
            )
            if not dry_run:
                self._save_registry_choice(reg_var, reg_remember)
            title = f"升级 {lane}" + ("（只看会做什么）" if dry_run else "")
            dlg.destroy()
            self.run_job(title, lambda: core.cmd_upgrade(core.load_config(), args))

        self.link_button(foot, "关闭", dlg.destroy, fg=MUTED).pack(side="right")
        self.link_button(foot, "先看会做什么", lambda: start(False, dry_run=True),
                         fg=ACCENT_DK).pack(side="right", padx=(0, 14))
        if data.get("previousVersion"):
            self.link_button(
                foot, f"退回上一版（{data['previousVersion']}）", lambda: start(True), fg=AMBER
            ).pack(side="right", padx=(0, 14))
        if not external:
            up = self.link_button(foot, "升级到这一版", lambda: start(False), fg=ACCENT_DK)
            up.pack(side="right", padx=(0, 14))
        else:
            tk.Label(foot, text="外部安装：本窗口不动它", bg=BG, fg=RED,
                     font=(FONT_UI, 9)).pack(side="right", padx=(0, 14))

        def load(choice: str) -> None:
            try:
                plan = core.registry_plan(cfg, choice)
                packument, reg = core.fetch_index(cfg, registries=plan)
                versions = core.sorted_versions(packument)
                tags = packument.get("dist-tags", {})
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: hint.configure(
                    text=f"查 registry 失败（{exc}）——自己手打版本号也行", fg=AMBER))
                return

            def apply() -> None:
                try:
                    if not dlg.winfo_exists():
                        return
                except tk.TclError:
                    return
                tag_names: dict[str, list[str]] = {}
                for tag, ver in tags.items():
                    tag_names.setdefault(ver, []).append(tag)
                current = str(data.get("version") or "")
                for ver in reversed(versions[-30:]):
                    marks = []
                    if ver == current:
                        marks.append("现在用的")
                    try:
                        if core.installed_version(core.paths(cfg)["versions"] / ver) == ver:
                            marks.append("本地已装")
                    except Exception:  # noqa: BLE001
                        pass
                    order.append(ver)
                    listbox.insert(
                        "end",
                        f"  {ver:<16}{','.join(tag_names.get(ver, [])) or '-':<12}"
                        + ("  ← " + "、".join(marks) if marks else ""),
                    )
                if versions:
                    var.set(versions[-1])
                    newest = versions[-1]
                    hint.configure(
                        text=f"registry 最新 {newest}"
                             + ("（就是你现在这个）" if newest == current else " ← 比你现在的新")
                             + f"　来源 {reg}",
                        fg=GRAY,
                    )

            self.root.after(0, apply)

        # 换源就重查一次版本列表（列表和「最新版」都跟着源走）。
        # Tk 变量只能在主线程读，所以在这里读好再交给子线程。
        def reload(*_a) -> None:
            choice = reg_var.get()
            threading.Thread(target=load, args=(choice,), daemon=True).start()

        reg_var.trace_add("write", reload)
        reload()


    def market_dialog(self, lane: str) -> None:
        """插件市场单独开关 + 更新。

        它值得单独一个对话框：关掉别的插件容易，关掉「插件市场」本身以前只能手改 YAML；
        而它恰恰是唯一需要"更新"的那个插件——更新别的插件是它的活儿，没人能更新它自己。
        更新走的是 dsh 自己的插件管理通道（dsh plugin → pnpm），所以和插件市场内部
        更新别的插件是同一条路：钉具体版本号、必要时带一次性绕过。
        """
        cfg = core.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("插件市场")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        w, h = 680, 470
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 60
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"插件市场（dshmarket）：lane「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Label(
            dlg,
            text=(
                "关掉它不会动别的插件，也不会动官方组合包；「打开」把启动器写的那一小段删掉就回来了。\n"
                "「更新」只更新插件市场自己：点名到版本地装（pnpm 的「新版本按住」只拦自动升级），"
                "装完启动一次确认能起来，起不来自动回滚。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=620,
        ).pack(anchor="w", padx=18)

        state = tk.Label(dlg, text="正在读状态（顺便查一次 registry）…", bg=BG, fg=TEXT,
                         font=(FONT_UI, 11, "bold"), anchor="w")
        state.pack(fill="x", padx=18, pady=(12, 0))
        detail = tk.Label(dlg, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9), justify="left",
                          anchor="w", wraplength=620)
        detail.pack(fill="x", padx=18, pady=(4, 0))
        note = tk.Label(dlg, text="", bg=BG, fg=AMBER, font=(FONT_UI, 9), justify="left",
                        anchor="w", wraplength=620)
        note.pack(fill="x", padx=18, pady=(8, 0))

        holder: dict = {"state": None, "latest": None}

        def fill() -> None:
            try:
                st = core.market_status(cfg, lane, with_registry=True)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: state.configure(text=f"读取失败：{exc}", fg=RED))
                return

            def apply() -> None:
                try:
                    if not dlg.winfo_exists():
                        return
                except tk.TclError:
                    return
                holder["state"] = st
                holder["latest"] = st.get("latest")
                off = core.market_disabled_rows(st)
                if not st["spec"] and not st["package_dir"].is_dir():
                    state.configure(text="这条 lane 没装插件市场", fg=AMBER)
                elif off and st["rows"] and len(off) == len(st["rows"]):
                    state.configure(text="已关掉（插件开关块在）", fg=GREEN)
                elif off:
                    state.configure(text=f"部分关着：{'、'.join(off)}", fg=AMBER)
                else:
                    state.configure(text="启用中", fg=TEXT)
                lines = [f"已装版本  {st['installed'] or '（没装）'}"
                         + (f"      profile 里写的是 {st['spec']}" if st["spec"] else ""),
                         f"行 id     {'、'.join(st['rows']) or '(没有补丁行)'}"]
                if st["latest"]:
                    same = st["latest"] == st["installed"]
                    lines.append(f"registry  最新 {st['latest']}"
                                 + ("（就是你现在这个）" if same else "  ← 有新版本"))
                elif st["registry_error"]:
                    lines.append(f"registry  查不到：{st['registry_error']}")
                else:
                    lines.append("registry  没查（离线）")
                if st["snapshots"]:
                    lines.append(f"更新快照  {len(st['snapshots'])} 个，最新是 "
                                 f"{st['snapshots'][0]['version'] or '?'}")
                detail.configure(text="\n".join(lines))
                if st["general_block"]:
                    note.configure(text="注意：这条 lane 的补丁层里还挂着一个"
                                        "「dsh-lanes 禁用第三方插件」块（启动器已不再维护它，"
                                        "要撤只能手删那几行）")
                elif st["latest"] and st["latest"] != st["installed"]:
                    note.configure(text=f"有新版 {st['latest']}：点「更新」即可（会先存快照）")
                else:
                    note.configure(text="")

            self.root.after(0, apply)

        threading.Thread(target=fill, daemon=True).start()

        boot_var = tk.BooleanVar(value=True)
        anyway_var = tk.BooleanVar(value=False)
        opt = tk.Frame(dlg, bg=BG)
        opt.pack(fill="x", padx=18, pady=(6, 0))
        tk.Checkbutton(opt, text="改动后启动一次确认还能起来（起不来就自动还原）", variable=boot_var,
                       bg=BG, fg=TEXT, selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                       font=(FONT_UI, 9), anchor="w").pack(anchor="w")
        tk.Checkbutton(opt, text="新版本被 pnpm 的 strict 按住也硬闯（--anyway，带一次性绕过）",
                       variable=anyway_var, bg=BG, fg=TEXT, selectcolor=CARD,
                       activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
                       anchor="w").pack(anchor="w")

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=18, pady=(10, 14))

        def do_off() -> None:
            args = argparse.Namespace(lane=lane, no_verify=False, no_boot_check=not boot_var.get())
            dlg.destroy()
            self.run_job(f"关掉插件市场：{lane}",
                         lambda: core.cmd_market_off(core.load_config(), args))

        def do_on() -> None:
            args = argparse.Namespace(lane=lane, no_verify=False)
            dlg.destroy()
            self.run_job(f"打开插件市场：{lane}",
                         lambda: core.cmd_market_on(core.load_config(), args))

        def do_update() -> None:
            st = holder["state"]
            if not st:
                messagebox.showwarning("插件市场", "状态还没读出来，稍等一下再点", parent=dlg)
                return
            latest = holder["latest"]
            if not latest:
                messagebox.showwarning(
                    "插件市场",
                    "没查到 registry 上的版本（网络？）。可以先用命令行指定版本：\n"
                    f"py dsh_lanes.py market-update {lane} --version <版本号>",
                    parent=dlg,
                )
                return
            if not messagebox.askyesno(
                "更新插件市场",
                f"把插件市场更新到 {latest}？\n\n"
                "· 会先把现在的包与清单存一份快照（失败能退回）\n"
                "· 走 dsh plugin → pnpm，钉死这个版本号\n"
                "· 装完启动一次确认能起来，起不来自动回滚\n"
                "· 更新完要重启这条 lane 才生效",
                parent=dlg,
            ):
                return
            args = argparse.Namespace(lane=lane, version=None, anyway=anyway_var.get(),
                                      force=False, offline=False, no_verify=False,
                                      no_boot_check=not boot_var.get())
            dlg.destroy()
            self.run_job(f"更新插件市场：{lane}",
                         lambda: core.cmd_market_update(core.load_config(), args))

        ttk.Button(btns, text="更新", command=do_update).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="打开", command=do_on).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="关掉", command=do_off).pack(side="left")

    def chat_dialog(self, lane: str) -> None:
        """备份对话：列出快照，可立即备份 / 恢复 / 删除。

        备份的是 <HOME>\\sessions —— 对话记录本体（每个会话一个 .zstd）。
        恢复之前会先**校验快照自身**（坏备份恢复只会更糟），再把现状另存一份（恢复错了能回来），
        恢复完逐文件核对指纹。
        """
        cfg = core.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("备份对话")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        w, h = 780, 540
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 50
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"备份对话：lane「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Label(
            dlg,
            text=(
                "备份的是这条 lane 的对话记录（DSH_HOME\\sessions，一个会话一个文件）。\n"
                "恢复前会先校验快照本身，再把现在的记录另存一份；恢复后逐文件比对指纹。\n"
                "这条 lane 正在运行时，恢复会先被拦住（先停它）。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=730,
        ).pack(anchor="w", padx=18)

        head = tk.Frame(dlg, bg=BG)
        head.pack(fill="x", padx=18, pady=(10, 0))
        tk.Label(head, text=f"{'名称':<30}{'时间':<20}{'会话':>5}{'大小':>11}", bg=BG, fg=MUTED,
                 font=(FONT_MONO, 9)).pack(anchor="w")

        wrap = tk.Frame(dlg, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=18, pady=(4, 6))
        scroll = ttk.Scrollbar(wrap, orient="vertical")
        box = tk.Listbox(wrap, bg=CARD, fg=TEXT, font=(FONT_MONO, 9), activestyle="none",
                         selectbackground=ACCENT, selectforeground="#FFFFFF",
                         highlightthickness=0, yscrollcommand=scroll.set)
        scroll.configure(command=box.yview)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)

        status = tk.Label(dlg, text="正在读取备份列表…", bg=BG, fg=GRAY, font=(FONT_UI, 9),
                          justify="left", anchor="w", wraplength=730)
        status.pack(fill="x", padx=18)
        items: list[dict] = []

        def load() -> None:
            try:
                snaps = core.chat_snapshots(cfg, lane)
                now = core.count_sessions(core.lane_home(cfg, lane))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: status.configure(text=f"读取失败：{exc}", fg=RED))
                return

            def apply() -> None:
                try:
                    if not dlg.winfo_exists():
                        return
                except tk.TclError:
                    return
                items.clear()
                items.extend(snaps)
                box.delete(0, "end")
                for it in snaps:
                    flag = "" if it["ok"] else "  [不可用：缺 manifest.json]"
                    note = "运行时备份" if it["running"] else (it["note"] or "")
                    box.insert(
                        "end",
                        f"{it['name'][:29]:<30}{str(it['created'])[:19]:<20}"
                        f"{it['sessions']:>5}{core.human_size(it['size']):>11}  {note}{flag}",
                    )
                if snaps:
                    total = sum(int(it["size"] or 0) for it in snaps)
                    status.configure(
                        text=f"{len(snaps)} 份备份 / {core.human_size(total)}；"
                             f"这条 lane 现在的会话文件数 {now}",
                        fg=GRAY,
                    )
                else:
                    status.configure(text=f"还没有备份。当前会话文件数 {now}，点「立即备份」来一份。",
                                     fg=AMBER)

            self.root.after(0, apply)

        threading.Thread(target=load, daemon=True).start()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=18, pady=(8, 14))

        def picked() -> dict | None:
            sel = box.curselection()
            if not sel:
                messagebox.showinfo("备份对话", "先在列表里选一份备份", parent=dlg)
                return None
            index = int(sel[0])
            if index >= len(items):
                return None
            return items[index]

        def do_backup() -> None:
            suffix = simpledialog.askstring(
                "备份对话", "给这份快照起个后缀名（可留空，例如 before-upgrade）：", parent=dlg
            )
            if suffix is None:
                return
            args = argparse.Namespace(lane=lane, name=(suffix.strip() or None))
            self.run_job(f"备份对话：{lane}",
                         lambda: core.cmd_chat_backup(core.load_config(), args),
                         on_done=load)

        def do_restore() -> None:
            target = picked()
            if not target:
                return
            if not target["ok"]:
                messagebox.showwarning("备份对话", "这份快照不完整（缺 manifest.json），不能用", parent=dlg)
                return
            running = bool(core.lane_any_running(cfg, lane))
            question = (
                f"把对话记录恢复到「{target['name']}」？\n\n"
                f"· 快照里 {target['sessions']} 个会话文件 / {core.human_size(target['bytes'])}\n"
                "· 先校验快照自身，再把现在的记录另存一份（恢复错了能回来）\n"
                "· 恢复完逐文件比对指纹"
            )
            if running:
                if not messagebox.askyesno(
                    "备份对话",
                    question + "\n\n这条 lane 正在运行：恢复会先把它停掉，之后请手动「打开」。继续？",
                    parent=dlg,
                ):
                    return
            elif not messagebox.askyesno("备份对话", question, parent=dlg):
                return
            args = argparse.Namespace(lane=lane, name=target["name"], stop=True, yes=True, force=False)
            self.run_job(f"恢复对话：{lane}",
                         lambda: core.cmd_chat_restore(core.load_config(), args),
                         on_done=load)

        def do_rm() -> None:
            target = picked()
            if not target:
                return
            if not messagebox.askyesno(
                "备份对话",
                f"删掉快照「{target['name']}」（{core.human_size(target['size'])}）？删了就没了。",
                parent=dlg,
            ):
                return
            args = argparse.Namespace(lane=lane, name=target["name"], yes=True)
            self.run_job(f"删除备份：{lane}",
                         lambda: core.cmd_chat_rm(core.load_config(), args),
                         on_done=load)

        ttk.Button(btns, text="关闭", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="立即备份", command=do_backup).pack(side="left")
        ttk.Button(btns, text="恢复选中的", command=do_restore).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="删除选中的", command=do_rm).pack(side="left", padx=(8, 0))

    def new_version_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("新建版本")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 540, 580
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 90
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text="新建一条 lane", bg=BG, fg=TEXT, font=(FONT_UI, 13, "bold")).pack(
            anchor="w", padx=20, pady=(16, 2)
        )
        tk.Label(
            dlg,
            text="会把该版本的 DSH 装进独立目录（不覆盖你正在用的版本），并分配独立 DSH_HOME。\n"
                 "端口不固定：每次打开时实时挑空闲端口；在卡片上可以继续「＋ 添加 Profile」开多个环境。",
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
            wraplength=470,
            justify="left",
        ).pack(anchor="w", padx=20)

        # 凭据说明：不再自动继承，用「凭据…」按条目复制
        keybox = tk.Frame(dlg, bg="#EEF1FF")
        keybox.pack(fill="x", padx=20, pady=(8, 0))
        tk.Label(
            keybox,
            text="API key：新 HOME 是空的。建好之后在卡片上点「凭据…」，",
            bg="#EEF1FF",
            fg=ACCENT_DK,
            font=(FONT_UI, 9),
            wraplength=460,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(7, 0))
        tk.Label(
            keybox,
            text="从已有版本按条目勾选复制（也可以启动后直接在 DSH 里填）。",
            bg="#EEF1FF",
            fg=MUTED,
            font=(FONT_UI, 8),
            wraplength=460,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 7))

        reg_var, reg_remember = self.registry_row(dlg, core.load_config(), pady=(12, 0))

        form = tk.Frame(dlg, bg=BG)
        form.pack(fill="x", padx=20, pady=14)
        form.columnconfigure(1, weight=1)

        tk.Label(form, text="名称", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(row=0, column=0, sticky="w", pady=6)
        name_var = tk.StringVar(value="")
        name_entry = ttk.Entry(form, textvariable=name_var)
        name_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)

        tk.Label(form, text="版本", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(row=1, column=0, sticky="w", pady=6)
        ver_var = tk.StringVar(value="")
        self.ver_combo = ttk.Combobox(form, textvariable=ver_var, values=[], width=30)
        self.ver_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=6)

        hint = tk.Label(dlg, text="正在查询 npm 上可用的版本…", bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        hint.pack(fill="x", padx=20)

        def base_name(ver: str) -> str:
            tags = {"0.1.7-rc.2": "next", "0.1.5-rc.3": "stable"}
            return tags.get(ver, "lane-" + re.sub(r"[^A-Za-z0-9]+", "-", ver).strip("-")[:12])

        def on_pick(_e=None) -> None:
            text = ver_var.get().strip()
            ver = text.split()[0] if text else ""
            if ver and not name_var.get().strip():
                name_var.set(base_name(ver))

        self.ver_combo.bind("<<ComboboxSelected>>", on_pick)

        def load_versions(choice: str) -> None:
            try:
                plan = core.registry_plan(core.load_config(), choice)
                packument, reg = core.fetch_index(core.load_config(), registries=plan)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: hint.configure(text=f"查询失败：{exc}（可直接手输版本号）", fg=RED))
                return
            tags = packument.get("dist-tags", {})
            tag_of: dict[str, list[str]] = {}
            for tag, ver in tags.items():
                tag_of.setdefault(ver, []).append(tag)
            versions = core.sorted_versions(packument)
            values = [f"{v}    ({','.join(tag_of[v])})" if v in tag_of else v for v in reversed(versions)]
            summary = "  ".join(f"{k}={v}" for k, v in sorted(tags.items()))

            def apply() -> None:
                self.ver_combo.configure(values=values)
                if values and not ver_var.get().strip():
                    ver_var.set(values[0])
                    on_pick()
                hint.configure(text=f"来源 {reg}｜共 {len(versions)} 个版本｜{summary}", fg=MUTED)

            self.root.after(0, apply)

        # 换源就重查一次（Tk 变量只在主线程读）
        def reload_versions(*_a) -> None:
            hint.configure(text="正在查询 npm 上可用的版本…", fg=MUTED)
            choice = reg_var.get()
            threading.Thread(target=load_versions, args=(choice,), daemon=True).start()

        def do_create() -> None:
            lane = name_var.get().strip()
            ver_text = ver_var.get().strip()
            ver = ver_text.split()[0] if ver_text else ""
            if not LANE_NAME_RE.match(lane):
                messagebox.showwarning("名称不合法", "名称只能用字母/数字/._-，且以字母或数字开头。", parent=dlg)
                return
            if not ver:
                messagebox.showwarning("缺少版本", "请选择或输入一个版本号（如 0.1.7-rc.2 或 next）。", parent=dlg)
                return
            if lane in core.load_config().get("lanes", {}):
                if not messagebox.askyesno("已存在", f"lane「{lane}」已存在，要覆盖它的版本吗？", parent=dlg):
                    return
            args = argparse.Namespace(lane=lane, version=ver, force=True, follow=True,
                                      registry=reg_var.get())
            self._save_registry_choice(reg_var, reg_remember)

            def job() -> None:
                core.cmd_create(core.load_config(), args)

            dlg.destroy()
            self.run_job(f"新建 {lane}（{ver}）", job)

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(4, 16), side="bottom")
        self.button(btns, "开始安装", do_create, primary=True).pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)
        self.button(btns, "重新查询版本", reload_versions).pack(side="left")

        reg_var.trace_add("write", reload_versions)
        reload_versions()

    # ── 关闭 ────────────────────────────────────────────────────────────────
    def _save_geometry(self) -> None:
        try:
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w < 400 or h < 300:  # 最小化/异常状态下不记
                return
            cfg = core.load_config()
            cfg["ui"] = {"w": w, "h": h, "x": self.root.winfo_x(), "y": self.root.winfo_y()}
            core.save_config(cfg)
        except Exception:
            pass

    def _on_close(self) -> None:
        """关窗口就只是关窗口 —— 绝不顺手停掉正在跑的 DSH。

        以前这里会弹「是＝顺手停掉它们并退出」，而 Windows 的 askyesnocancel 默认焦点在"是"，
        于是点 X 再按一下回车就把 `global`（你正在用的那套）给停了。想停请用顶部「全部停止」。
        """
        running = [
            f"{lane}/{prof}"
            for lane, profs in (self._snapshot(force=True)["states"] or {}).items()
            for prof, rt in (profs or {}).items()
            if rt
        ]
        if running:
            self._log_lines(
                [
                    (
                        f"已关闭启动器窗口。以下实例仍在后台运行（下次打开会自动认回来）："
                        f"{'、'.join(running)}",
                        "muted",
                    )
                ]
            )
        self._save_geometry()
        self.root.destroy()


def _safe_std_streams() -> None:
    """pythonw（无控制台）下 sys.stdout/stderr 是 None，任何 print 都会抛异常。
    核心模块是靠 print 输出日志的，所以必须先补上兜底流。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass


# 界面上的每个入口都必须真的挂在 LauncherApp 上。
# 为什么专门查这个：缩进 / 注释写错时，一个 def 可能被"吃掉"变成上一个函数的**局部**函数，
# 语法照样合法、窗口照样能建出来，只有点那个按钮时才炸（真的踩过一次：
# `def delete_lane` 被并进注释，整段落进上一个函数里，而 --selfcheck 报的是 OK）。
_REQUIRED_ACTIONS = (
    "refresh_lanes",
    "manual_refresh",
    "refresh_remote",
    "open_repo",
    "open_profile",
    "stop_profile",
    "stop_lane",
    "stop_all",
    "show_lane_log",
    "add_profile_dialog",
    "remove_profile",
    "credential_dialog",
    "delete_lane",
    "clone_lane",
    "upgrade_dialog",
    "verify_lane",
    "market_dialog",
    "chat_dialog",
    "new_version_dialog",
    "registry_row",
    "_save_registry_choice",
    "_attach_port_hint",
    "_parse_port_text",
    "adopt_install",
    "kill_instance",
    "open_url",
    "copy_text",
    "_snapshot",
    "_invalidate",
    "_on_close",
)


def _raise_timer_resolution() -> None:
    """把 Windows 系统计时器精度调到 1ms。

    默认粒度约 15.6ms，Tk 的 `after(8)` 会被拖到 ~15ms 一帧，动画就不够顺滑。
    调到 1ms 后 8ms 一帧才真的成立；进程退出时系统自动恢复，不必刻意 timeEndPeriod。
    """
    if os.name != "nt":
        return
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH 多版本启动器（图形界面版）")
    parser.add_argument("--selfcheck", action="store_true", help="构建窗口并刷新一次后退出（自动化自检）")
    args = parser.parse_args()

    _safe_std_streams()
    enable_dpi_awareness()
    _raise_timer_resolution()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    try:
        app = LauncherApp(root)
    except Exception as exc:  # noqa: BLE001
        # 绝不留一个"没有 mainloop 的半成品窗口"：那种窗口既不能正常用、也关不掉
        # （2026-09-27 的故障就是这样：__init__ 里写运行态被拒 → 弹错误框 → 窗口挂着不动）。
        detail = traceback.format_exc()
        try:
            messagebox.showerror(
                "DSH 多版本启动器 · 启动失败",
                f"{type(exc).__name__}: {exc}\n\n"
                f"{detail[-1200:]}\n"
                "窗口已关闭；完整信息也写到了 stderr（命令行里跑能看到）。",
            )
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        print(detail, file=sys.stderr, flush=True)
        return 1

    if args.selfcheck:
        root.withdraw()
        root.update()
        root.update_idletasks()
        missing = [m for m in _REQUIRED_ACTIONS if not callable(getattr(app, m, None))]
        lanes = core.load_config().get("lanes", {})
        if missing:
            print(f"[XX] selfcheck 失败：LauncherApp 上找不到这些方法：{'、'.join(missing)}")
            root.destroy()
            return 1
        print(
            f"selfcheck OK：窗口构建成功，{len(_REQUIRED_ACTIONS)} 个界面入口齐全，"
            f"lane 数量 {len(lanes)}：{', '.join(lanes) or '(无)'}"
        )
        root.destroy()
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
