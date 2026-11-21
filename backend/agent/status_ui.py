"""本机采集器状态窗口；只保留任务摘要，不保存凭据或文件二进制。"""

import copy
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.version import AGENT_VERSION

_FIELDS = {
    "asin", "asins", "title", "features", "description", "image_url", "source_url",
    "keyword", "query", "page_number", "page", "marketplace", "country", "week_end",
    "price", "price_text", "price_reliable", "price_basis", "currency", "offer_kind",
    "cards", "rows", "raw_rows", "result", "product", "products", "requests", "items",
    "total", "count", "fetched", "filename", "screenshot", "diagnostic", "error",
    "code", "message", "search_frequency_rank", "click_share", "conversion_share",
    "rank", "organic_rank", "position", "sponsored", "is_sponsored", "stop_reason",
    "truncated", "report_week_end", "columns", "term", "search_volume",
}


def preview(value: object, depth: int = 0) -> object:
    """Bounded allowlist: neither pairing tokens nor base64 file bodies enter the UI."""
    if depth > 7:
        return "…"
    if isinstance(value, dict):
        result = {str(k): preview(v, depth + 1) for k, v in value.items() if k in _FIELDS}
        files = value.get("files")
        if isinstance(files, dict):
            result["files"] = list(files)[:50]
        return result
    if isinstance(value, list):
        items = [preview(item, depth + 1) for item in value[:50]]
        if len(value) > 50:
            items.append(f"仅预览前 50 条，共 {len(value)} 条；完整结果已回传服务器")
        return items
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:200]


def task_label(path: str) -> str:
    for suffix, label in [
        ("/searches", "批量搜索商品"), ("/search", "搜索商品"),
        ("/products", "批量读取商品"), ("/product", "读取商品详情"),
        ("/aba/report", "抓取 ABA 周报"), ("/sellersprite/export", "导出卖家精灵词表"),
        ("/poe/download", "下载商机探测器数据"),
        ("/poe/intents", "搜索商机购买意图"),
    ]:
        if path.endswith(suffix):
            return label
    return ""


def result_summary(payload: dict[str, Any]) -> tuple[str, str]:
    error = payload.get("error")
    if error:
        detail = error.get("message") or error.get("code") if isinstance(error, dict) else error
        return "失败", str(detail)[:400]
    result = payload.get("result")
    if isinstance(result, list):
        failures = sum(bool(row.get("error")) for row in result if isinstance(row, dict))
        return ("部分失败" if failures else "完成", f"返回 {len(result)} 项，失败 {failures} 项")
    if isinstance(result, dict):
        for key, label in [("cards", "商品"), ("rows", "词条"), ("items", "记录")]:
            rows = result.get(key)
            if isinstance(rows, list):
                return "完成", f"抓取 {len(rows)} 条{label}"
        if result.get("filename"):
            count = f"，{result['count']} 条" if "count" in result else ""
            return "完成", str(result["filename"]) + count
        if result.get("asin"):
            price = f"，当前售价 {result['price']} {result.get('currency') or ''}" if result.get("price") is not None else ""
            return "完成", str(result["asin"]) + price
    return "完成", "数据已回传服务器"


class CollectorStatus:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.show_requested = threading.Event()
        self.hide_requested = threading.Event()
        self.shutdown_requested = threading.Event()
        self.update_requested = threading.Event()
        self.reopen_after_update = False
        self.inflight: set[str] = set()
        self.window_revision = ""
        self.ui_token = secrets.token_urlsafe(32)
        self.binding: dict[str, str] = {}
        self.history_path: Path | None = None
        self.connection = "正在连接"
        self.connection_detail = "等待服务器确认"
        self.update = "启动时及空闲时自动检查"
        self.update_phase = "idle"
        self.runtime: dict[str, Any] = {}
        self.store = "正在读取店铺"
        self.events: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def configure(self, config: dict[str, Any], *, persist_history: bool = False) -> None:
        import hashlib
        import hmac

        with self.lock:
            self.binding = {"server": str(config.get("server") or ""), "device_id": str(config.get("device_id") or ""),
                            "computer": os.environ.get("COMPUTERNAME", "本机"), "version": AGENT_VERSION}
            if config.get("token"):
                self.ui_token = hmac.new(str(config["token"]).encode(), b"collector-local-status-ui", hashlib.sha256).hexdigest()
            if persist_history:
                self.history_path = Path(os.environ.get("APPDATA") or str(Path.home())) / "KeywordLibraryAgent" / "ui-history.json"
                try:
                    history = json.loads(self.history_path.read_text(encoding="utf-8"))
                    for event in history.get("events", [])[-60:]:
                        if isinstance(event, dict) and event.get("id") and event.get("state") != "抓取中":
                            self.events.setdefault(str(event["id"]), event)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass

    def set_connection(self, state: str, detail: str) -> None:
        with self.lock:
            self.connection, self.connection_detail = state, detail

    def set_update(self, text: str, phase: str = "idle") -> None:
        with self.lock:
            self.update = text
            self.update_phase = phase

    def begin(self, request_id: str, path: str, body: object) -> bool:
        with self.lock:
            self.inflight.add(request_id)
        label = task_label(path)
        if not label:
            return False
        request = preview(body)
        detail = json.dumps(request, ensure_ascii=False)
        with self.lock:
            self.events[request_id] = {
                "id": request_id, "time": datetime.now().strftime("%H:%M:%S"),
                "task": label, "state": "抓取中", "summary": detail[:250],
                "input": request, "output": None, "started": time.monotonic(), "seconds": None,
            }
            # Never remove an active task while concurrent requests finish.
            for key in list(self.events):
                if len(self.events) <= 60:
                    break
                if self.events[key]["state"] != "抓取中":
                    del self.events[key]
        return True

    def finish(self, request_id: str, payload: dict[str, Any]) -> None:
        state, summary = result_summary(payload)
        with self.lock:
            self.inflight.discard(request_id)
            event = self.events.get(request_id)
            if event is None:
                return
            event.update(state=state, summary=summary, output=preview(payload),
                         seconds=round(time.monotonic() - event["started"], 1))
        if self.history_path is not None:
            try:
                with self.lock:
                    history = {"events": [event for event in self.events.values() if event["state"] != "抓取中"][-60:]}
                    text = json.dumps(history, ensure_ascii=False)
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.history_path.with_suffix(".tmp")
                temporary.write_text(text, encoding="utf-8")
                temporary.replace(self.history_path)
            except OSError:
                pass

    def progress(self, request_id: str, payload: dict[str, Any], summary: str) -> None:
        with self.lock:
            event = self.events.get(request_id)
            if event is not None and event["state"] == "抓取中":
                event.update(output=preview(payload), summary=summary)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            events = list(self.events.values())
            for event in events:
                event["elapsed_seconds"] = event["seconds"] if event["seconds"] is not None else round(time.monotonic() - event["started"], 1)
            return copy.deepcopy({"connection": self.connection, "connection_detail": self.connection_detail, "binding": self.binding,
                                  "update": self.update, "update_phase": self.update_phase, "window_revision": self.window_revision, "runtime": self.runtime, "store": self.store,
                                  "events": events})


STATUS = CollectorStatus()


def poll_local_status(stop: threading.Event) -> None:
    import httpx

    next_store = 0.0
    while not stop.is_set():
        try:
            response = httpx.get("http://127.0.0.1:7100/v1/status", timeout=2, trust_env=False)
            response.raise_for_status()
            with STATUS.lock:
                STATUS.runtime = response.json()
        except (httpx.HTTPError, ValueError):
            with STATUS.lock:
                STATUS.runtime = {"state": "offline"}
        if time.monotonic() >= next_store:
            try:
                response = httpx.get("http://127.0.0.1:7100/v1/ziniao/stores", timeout=8, trust_env=False)
                data = response.json()
                current = str(data.get("current") or "")
                name = next((str(row.get("name") or "") for row in data.get("stores", []) if str(row.get("id")) == current), "")
                store = f"{name} · {current}" if name else current or str(data.get("message") or "未选择店铺，请在网页选择")
                with STATUS.lock:
                    STATUS.store = store
            except (httpx.HTTPError, ValueError, TypeError):
                with STATUS.lock:
                    STATUS.store = "店铺状态读取失败，请检查紫鸟"
            next_store = time.monotonic() + 15
        stop.wait(2)


class StatusWindow:
    def __init__(self, config: dict[str, Any], *, background: bool = False) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        self.tk, self.messagebox = tk, messagebox
        self.root = tk.Tk()
        self.root.title("关键词库采集器")
        self.root.geometry("1080x800")
        self.root.minsize(900, 700)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        bg, surface, ink, muted, line, accent = "#F4F6F9", "#FFFFFF", "#172033", "#526174", "#DFE5EE", "#11766E"
        self.root.configure(background=bg)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=bg)
        style.configure("Surface.TFrame", background=surface)
        style.configure("TLabel", background=bg, foreground=ink, font=("Microsoft YaHei UI", 10))
        style.configure("Surface.TLabel", background=surface, foreground=ink)
        style.configure("Muted.TLabel", foreground=muted)
        style.configure("SurfaceMuted.TLabel", background=surface, foreground=muted)
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", background=surface, foreground=ink, padding=(13, 8), bordercolor=line, borderwidth=1, focusthickness=1, focuscolor=accent)
        style.map("TButton", background=[("pressed", "#E3ECEA"), ("active", "#EFF4F3")], foreground=[("disabled", "#76817F")])
        style.configure("Primary.TButton", background=accent, foreground=surface, bordercolor=accent, focuscolor=surface)
        style.map("Primary.TButton", background=[("pressed", "#084C47"), ("active", "#0B5E57")], foreground=[("disabled", "#B8D3CF")])
        style.configure("Quiet.TButton", background=bg, foreground=muted, borderwidth=0, bordercolor=bg)
        style.map("Quiet.TButton", background=[("active", "#E8EDF2"), ("pressed", "#DFE5EE")])
        style.configure("Binding.TEntry", fieldbackground=surface, foreground=ink, borderwidth=0, padding=(0, 3))
        style.map("Binding.TEntry", fieldbackground=[("readonly", surface)], foreground=[("readonly", ink)])
        style.configure("Treeview", background=surface, fieldbackground=surface, foreground=ink, borderwidth=0, rowheight=32, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#F1F4F7", foreground=muted, font=("Microsoft YaHei UI", 9), padding=(8, 9), relief="flat")
        style.map("Treeview", background=[("selected", "#DDF0EC")], foreground=[("selected", "#0A514A")])
        style.map("Treeview.Heading", background=[("active", "#E8EDF2")])
        style.configure("TPanedwindow", background=line)
        style.configure("Vertical.TScrollbar", background="#D9E0E9", troughcolor=surface, borderwidth=0, arrowsize=12)
        self.config = {"服务器": str(config.get("server") or ""),
                       "设备编号": str(config.get("device_id") or "未绑定"),
                       "本机名称": os.environ.get("COMPUTERNAME", "本机"), "采集器版本": AGENT_VERSION}
        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        ttk.Label(header, text="关键词库采集器", font=("Microsoft YaHei UI", 19, "bold")).pack(side="left")
        ttk.Button(header, text="后台运行", style="Primary.TButton", command=self.root.withdraw).pack(side="right")
        ttk.Label(header, text="v" + AGENT_VERSION, style="Muted.TLabel").pack(side="right", padx=16)
        connection_bar = ttk.Frame(frame, style="Surface.TFrame", padding=(18, 14))
        connection_bar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        connection_bar.columnconfigure(0, weight=1)
        self.connection = ttk.Label(connection_bar, style="Surface.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        self.connection.grid(row=0, column=0, sticky="w")
        self.current = ttk.Label(connection_bar, style="Surface.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        self.current.grid(row=0, column=1, sticky="e", padx=(18, 0))
        self.connection_detail = ttk.Label(connection_bar, style="SurfaceMuted.TLabel", wraplength=920)
        self.connection_detail.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        binding = ttk.Frame(frame, style="Surface.TFrame", padding=(18, 14))
        binding.grid(row=2, column=0, sticky="ew")
        ttk.Label(binding, text="设备绑定", style="Surface.TLabel", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.values: dict[str, Any] = {}
        positions = {"服务器": (1, 0), "采集器版本": (1, 2), "设备编号": (2, 0), "本机名称": (2, 2)}
        for label, value in self.config.items():
            row, column = positions[label]
            ttk.Label(binding, text=label, style="SurfaceMuted.TLabel", width=10).grid(row=row, column=column, sticky="w", pady=3)
            variable = tk.StringVar(value=value)
            self.values[label] = variable
            ttk.Entry(binding, textvariable=variable, state="readonly", style="Binding.TEntry", width=22 if column else 54).grid(row=row, column=column + 1, sticky="ew", pady=3)
        ttk.Label(binding, text="紫鸟店铺", style="SurfaceMuted.TLabel", width=10).grid(row=3, column=0, sticky="w", pady=3)
        self.store = ttk.Label(binding, style="Surface.TLabel", wraplength=850)
        self.store.grid(row=3, column=1, columnspan=3, sticky="w", pady=3)
        binding.columnconfigure(1, weight=1)
        binding.columnconfigure(2, pad=26)
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=3, column=0, sticky="ew", pady=(16, 10))
        ttk.Label(toolbar, text="采集记录", style="Heading.TLabel").pack(side="left")
        ttk.Button(toolbar, text="复制绑定信息", command=self.copy_binding).pack(side="right")
        ttk.Button(toolbar, text="打开数据目录", command=self.open_data).pack(side="right", padx=(0, 8))
        panes = ttk.Panedwindow(frame, orient="horizontal")
        panes.grid(row=4, column=0, sticky="nsew")
        history = ttk.Frame(panes, style="Surface.TFrame", padding=(0, 0, 0, 8))
        self.tree = ttk.Treeview(history, columns=("time", "task", "state", "summary"), show="headings", height=6, selectmode="browse")
        for key, label, width in [("time", "时间", 74), ("task", "任务", 120), ("state", "状态", 68), ("summary", "抓取结果", 230)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=60, stretch=key == "summary")
        scroll = ttk.Scrollbar(history, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.empty = ttk.Label(history, text="还没有抓取记录\n任务开始后会在这里实时显示", style="SurfaceMuted.TLabel", justify="center")
        self.empty.place(relx=0.5, rely=0.5, anchor="center")
        details = ttk.Frame(panes, style="Surface.TFrame", padding=16)
        ttk.Label(details, text="抓取信息", style="Surface.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(details, text="选择左侧记录，查看输入和实际返回数据", style="SurfaceMuted.TLabel").pack(anchor="w", pady=(4, 12))
        self.details = ScrolledText(details, wrap="word", width=38, height=8, font=("Microsoft YaHei UI", 10), relief="flat", background=surface, foreground=ink, padx=0, pady=0, borderwidth=0, highlightthickness=0)
        self.details.pack(fill="both", expand=True)
        self.details.insert("1.0", "等待采集任务\n\n收到任务后，将显示关键词、ASIN、商品售价和抓取条数等实际信息。\n\n此处预览每组前 50 条，完整数据由工作流保存。")
        self.details.configure(state="disabled")
        panes.add(history, weight=3)
        panes.add(details, weight=2)
        self.events: dict[str, dict[str, Any]] = {}
        self.last_detail = ""
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.render_details())
        footer = ttk.Frame(frame)
        footer.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self.update = ttk.Label(footer, style="Muted.TLabel")
        self.update.grid(row=0, column=0, sticky="w")
        ttk.Label(footer, text="关闭窗口继续后台采集，再次打开即可查看", style="Muted.TLabel", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(footer, text="退出采集器", style="Quiet.TButton", command=self.quit).grid(row=0, column=1, rowspan=2, sticky="e")
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        if background:
            self.root.withdraw()
        else:
            STATUS.show_requested.set()
        self.root.after(100, self.refresh)

    def open_data(self) -> None:
        directory = Path(os.environ.get("APPDATA") or str(Path.home())) / "KeywordLibraryAgent" / "data"
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(directory))

    def copy_binding(self) -> None:
        text = "\n".join(f"{key}：{value}" for key, value in self.config.items())
        text += "\n紫鸟店铺：" + STATUS.snapshot()["store"]
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def quit(self) -> None:
        if self.messagebox.askyesno("退出采集器", "退出后本机将停止接收抓取任务。确认退出？", parent=self.root):
            os._exit(0)

    def render_details(self) -> None:
        selection = self.tree.selection()
        if not selection or selection[0] not in self.events:
            return
        event = self.events[selection[0]]
        elapsed = f" · 耗时 {event['seconds']} 秒" if event["seconds"] is not None else ""
        detail = f"{event['task']} · {event['state']}{elapsed}\n{event['summary']}\n\n抓取输入\n"
        detail += json.dumps(event["input"], ensure_ascii=False, indent=2)
        detail += "\n\n实际返回数据\n" + (json.dumps(event["output"], ensure_ascii=False, indent=2) if event["output"] is not None else "正在抓取，等待返回…")
        if detail != self.last_detail:
            self.details.configure(state="normal")
            self.details.delete("1.0", "end")
            self.details.insert("1.0", detail)
            self.details.configure(state="disabled")
            self.last_detail = detail

    def refresh(self) -> None:
        state = STATUS.snapshot()
        self.connection.configure(text=state["connection"], foreground="#15803d" if state["connection"] == "已连接服务器" else "#9a3412")
        runtime = state["runtime"]
        ready = "本机运行时就绪" if runtime.get("state") in {"online", "busy"} else "本机运行时尚未就绪"
        if runtime.get("version") and runtime["version"] != AGENT_VERSION:
            ready = f"本机运行时仍为 {runtime['version']}，请重新运行安装器核验"
        self.connection_detail.configure(text=state["connection_detail"] + " · " + ready)
        self.store.configure(text=state["store"], wraplength=760)
        active = [event for event in state["events"] if event["state"] == "抓取中"]
        running = "、".join(event["task"] for event in active)
        if active:
            text = f"抓取中 · {len(active)} 个任务：{running}"
        elif runtime.get("state") == "busy":
            text = "抓取中 · " + str(runtime.get("operation") or "正在处理浏览器任务")
        else:
            text = "空闲 · 等待服务器下发任务"
        self.current.configure(text=text)
        self.update.configure(text="自动更新：" + state["update"])
        current_ids = set()
        for event in state["events"]:
            key = event["id"]
            current_ids.add(key)
            self.events[key] = event
            values = (event["time"], event["task"], event["state"], event["summary"])
            if self.tree.exists(key):
                self.tree.item(key, values=values)
            else:
                self.tree.insert("", "end", iid=key, values=values)
                self.tree.selection_set(key)
                self.tree.see(key)
        if state["events"]:
            self.empty.place_forget()
        for key in self.tree.get_children():
            if key not in current_ids:
                self.tree.delete(key)
                self.events.pop(key, None)
        self.render_details()
        if STATUS.show_requested.is_set():
            STATUS.show_requested.clear()
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.after(500, self.refresh)


def run_status_window(config: dict[str, Any], *, background: bool = False) -> None:
    from agent.window_host import browser_executable, run_browser_window

    STATUS.configure(config, persist_history=True)
    stop = threading.Event()
    threading.Thread(target=poll_local_status, args=(stop,), daemon=True, name="collector-status").start()
    if os.name == "nt" and browser_executable() is not None:
        try:
            run_browser_window(background=background)
        finally:
            stop.set()
        return
    window = StatusWindow(config, background=background)
    try:
        window.root.mainloop()
    finally:
        stop.set()
