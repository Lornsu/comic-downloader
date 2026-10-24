#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comic.baby 漫画下载器 · 图形界面

界面从上到下：
  ① 搜索作品     输入 ID / 详情页链接 / 关键词，回车或点「搜索」
  ② 保存方式     下拉框选「全部保存」或「自定义保存章节」；选自定义时下方列出章节供勾选
  ③ 保存路径     默认 <程序目录>/comic/<漫画名>，可「浏览…」选目录（自动拼 comic/漫画名），
                 也可以直接改文字后点「保存」
  ④ 运行日志     实时滚动输出下载进度

下载内核直接复用 comic_dl.py，界面只负责收集参数和显示日志。
"""

import argparse
import json
import os
import queue
import sys
import textwrap
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import comic_dl as core

APP_TITLE = "comic.baby 漫画下载器"
MODE_ALL = "全部保存"
MODE_PICK = "自定义保存章节"

FONT = "Microsoft YaHei UI"
FONT_UI = (FONT, 10)
FONT_BOLD = (FONT, 10, "bold")
FONT_MONO = ("Consolas", 9)
CHAPTER_COLS = 4          # 章节勾选区的列数
CH_ROW_H = 28             # 章节单行最小行高
CH_MAX_H = 240            # 章节区最大高度，超出则出现滚动条
CH_HOVER = "#e8f1fb"      # 章节单元格悬停底色


def app_dir():
    """exe 运行时取 exe 所在目录，脚本运行时取脚本所在目录"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class StreamToQueue:
    """把 print / 错误输出塞进队列，由主线程写进日志框"""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.session = None
        self.work = None
        self.work_id = None
        self.title_text = ""
        self.chapters = []
        self.chapter_vars = []          # [(BooleanVar, chapter)]
        self.dot_labels = []
        self.lock_base = None
        self._syncing = False
        self.busy = False

        self.build_ui()
        self._drain_id = self.after(80, self._drain)
        self._sync_id = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 界面
    def build_ui(self):
        self.title(APP_TITLE)
        self.geometry("920x820")
        self.minsize(800, 700)
        self._setup_style()

        root = ttk.Frame(self, padding=(14, 12, 14, 12))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)      # 日志区

        self._build_search(root, 0)
        self._build_mode(root, 1)
        self._build_path(root, 2)
        self._build_action(root, 3)
        self._build_log(root, 4)

        self._append("就绪。在上方输入作品 ID / 详情页链接 / 关键词后点「搜索」。\n")
        self._append("提示：搜索到多部作品时会弹窗让你挑一本。\n")

    def _setup_style(self):
        st = ttk.Style(self)
        for name in ("vista", "winnative", "clam"):
            if name in st.theme_names():
                try:
                    st.theme_use(name)
                    break
                except tk.TclError:
                    continue
        st.configure("TLabel", font=FONT_UI)
        st.configure("TButton", font=FONT_UI)
        st.configure("TCheckbutton", font=FONT_UI)
        st.configure("TCombobox", font=FONT_UI)
        st.configure("TLabelframe.Label", font=FONT_BOLD)
        st.configure("Go.TButton", font=(FONT, 11, "bold"))

    # ---- ① 搜索 ----
    def _build_search(self, root, row):
        box = ttk.LabelFrame(root, text=" ① 搜索作品 ", padding=(10, 8))
        box.grid(row=row, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="作品 ID / 链接 / 关键词").grid(row=0, column=0, padx=(0, 8))
        self.var_query = tk.StringVar()
        self.ent_query = ttk.Entry(box, textvariable=self.var_query, font=FONT_UI)
        self.ent_query.grid(row=0, column=1, sticky="ew", ipady=2)
        self.ent_query.bind("<Return>", lambda e: self.on_search())
        self.btn_search = ttk.Button(box, text="搜索", width=10, command=self.on_search)
        self.btn_search.grid(row=0, column=2, padx=(8, 0))

        self.lbl_work = ttk.Label(box, text="尚未选择作品", foreground="#666")
        self.lbl_work.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ---- ② 保存方式 ----
    def _build_mode(self, root, row):
        box = ttk.LabelFrame(root, text=" ② 保存方式 ", padding=(10, 8))
        box.grid(row=row, column=0, sticky="nsew", pady=(10, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        top = ttk.Frame(box)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(2, weight=1)
        ttk.Label(top, text="选择").grid(row=0, column=0, padx=(0, 8))
        self.var_mode = tk.StringVar(value=MODE_ALL)
        self.cbo_mode = ttk.Combobox(top, textvariable=self.var_mode, state="disabled",
                                     values=[MODE_ALL, MODE_PICK], width=16, font=FONT_UI)
        self.cbo_mode.grid(row=0, column=1, sticky="w")
        self.cbo_mode.bind("<<ComboboxSelected>>", self._on_mode_change)
        self.lbl_hint = ttk.Label(top, text="下载全部章节，生成每话 PDF + 合集", foreground="#666")
        self.lbl_hint.grid(row=0, column=2, sticky="w", padx=(12, 0))

        # 章节勾选区（默认隐藏）
        self.frm_ch = ttk.Frame(box)
        self.frm_ch.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.frm_ch.columnconfigure(0, weight=1)
        self.frm_ch.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self.frm_ch, height=CH_ROW_H, bg="white",
                                highlightthickness=1, highlightbackground="#c8c8c8",
                                borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb = ttk.Scrollbar(self.frm_ch, orient="vertical", command=self.canvas.yview)
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.inner = tk.Frame(self.canvas, bg="white", borderwidth=0)
        self.win_item = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.inner.bind("<MouseWheel>", self._on_wheel)

        bar = ttk.Frame(self.frm_ch)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="全选", width=8,
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bar, text="全不选", width=8,
                   command=lambda: self._set_all(False)).pack(side="left", padx=(6, 0))
        self.lbl_sel = ttk.Label(bar, text="", foreground="#666")
        self.lbl_sel.pack(side="left", padx=(12, 0))

        self.frm_ch.grid_remove()

    # ---- ③ 路径 ----
    def _build_path(self, root, row):
        box = ttk.LabelFrame(root, text=" ③ 保存路径 ", padding=(10, 8))
        box.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        box.columnconfigure(1, weight=1)                 # 只有输入框吃掉多余宽度

        ttk.Label(box, text="目录").grid(row=0, column=0, padx=(0, 8))
        self.var_path = tk.StringVar()
        self.ent_path = ttk.Entry(box, textvariable=self.var_path, font=FONT_UI)
        self.ent_path.grid(row=0, column=1, sticky="ew", ipady=2)
        self.ent_path.bind("<Return>", lambda e: self.on_apply_path())
        # 三个按钮宽度与 ① 的「搜索」一致（width=10），不拉伸
        ttk.Button(box, text="浏览…", width=10,
                   command=self.on_browse).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(box, text="保存", width=10,
                   command=self.on_apply_path).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(box, text="默认", width=10,
                   command=self.on_reset_path).grid(row=0, column=4, padx=(8, 0))

        # 锁定行单独一层容器：不参与上一行的列宽分配，三个按钮才能等宽
        lockbar = ttk.Frame(box)
        lockbar.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        lockbar.columnconfigure(1, weight=1)

        self.var_lock = tk.BooleanVar(value=False)
        self.chk_lock = ttk.Checkbutton(lockbar, text="锁定父目录（只改漫画名）",
                                        variable=self.var_lock, command=self.on_toggle_lock)
        self.chk_lock.grid(row=0, column=0, sticky="w")

        self.lbl_path = ttk.Label(lockbar, text="默认：<程序目录>/comic/<漫画名>", foreground="#666")
        self.lbl_path.grid(row=0, column=1, sticky="e")

        self._load_cfg()

    # ---- 路径：锁定的父目录 ----
    def _default_base(self):
        return str(app_dir() / "comic")

    def _comic_folder(self, base):
        """父目录 + 漫画名，锁定后只有漫画名会变"""
        name = core.safe_name(self.title_text, 80) if self.title_text else "漫画"
        return str(Path(base) / name)

    def _cfg_path(self):
        return app_dir() / "comic_config.json"

    def _load_cfg(self):
        """读取上次的锁定设置（文件不存在则忽略）"""
        try:
            data = json.loads(self._cfg_path().read_text("utf-8"))
        except Exception:
            return
        base = (data.get("lock_base") or "").strip()
        if data.get("lock") and base:
            self.lock_base = base
            self.var_lock.set(True)
            self.ent_path.configure(state="readonly")
            self.lbl_path.configure(text="已锁定：%s" % base, foreground="#12539b")
            self.var_path.set(self._comic_folder(base))

    def _save_cfg(self):
        try:
            self._cfg_path().write_text(
                json.dumps({"lock": bool(self.var_lock.get()),
                            "lock_base": self.lock_base or ""},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    def on_toggle_lock(self):
        if self.var_lock.get():
            cur = self.var_path.get().strip().strip('"')
            base = str(Path(cur).parent) if cur else self._default_base()
            self.lock_base = base
            self.ent_path.configure(state="readonly")
            self.var_path.set(self._comic_folder(base))
            self.lbl_path.configure(text="已锁定：%s" % base, foreground="#12539b")
            self._log_line("已锁定父目录：%s（切换作品只改漫画名）" % base)
        else:
            self.lock_base = None
            self.ent_path.configure(state="normal")
            self.lbl_path.configure(text="默认：<程序目录>/comic/<漫画名>", foreground="#666")
            self._log_line("已取消锁定，路径可自由修改")
        self._save_cfg()

    def refresh_path(self):
        """按当前作品刷新路径：锁定则换漫画名，否则回到默认路径"""
        if self.var_lock.get() and self.lock_base:
            self.var_path.set(self._comic_folder(self.lock_base))
            self.lbl_path.configure(text="已锁定：%s" % self.lock_base, foreground="#12539b")
        else:
            self.var_path.set(self._default_path())
            self.lbl_path.configure(text="默认：<程序目录>/comic/<漫画名>", foreground="#666")

    # ---- ④ 按钮 ----
    def _build_action(self, root, row):
        bar = ttk.Frame(root)
        bar.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        bar.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(bar, mode="indeterminate", length=200)
        self.pb.grid(row=0, column=0, sticky="w")
        self.pb.grid_remove()               # 空闲时不占位
        self.btn_go = ttk.Button(bar, text="开始下载", style="Go.TButton",
                                 state="disabled", command=self.on_start)
        self.btn_go.grid(row=0, column=1, ipadx=24, ipady=4)

    # ---- ⑤ 日志 ----
    def _build_log(self, root, row):
        box = ttk.LabelFrame(root, text=" ④ 运行日志 ", padding=(8, 6))
        box.grid(row=row, column=0, sticky="nsew", pady=(12, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.txt = ScrolledText(box, height=12, font=FONT_MONO, wrap="word",
                                state="disabled", relief="flat", padx=8, pady=6,
                                bg="#1f1f1f", fg="#d6d6d6", insertbackground="#d6d6d6")
        self.txt.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------ 交互
    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.win_item, width=max(event.width - 2, 1))
        self._sync_chapter_geometry()

    def _sync_chapter_geometry(self):
        """按内容实际高度收紧画布，内容不超高就不留滚动余量"""
        if self._syncing or not self.winfo_exists():
            return
        self._syncing = True
        try:
            self.inner.update_idletasks()
            need = max(self.inner.winfo_reqheight(), CH_ROW_H)
            h = max(CH_ROW_H, min(CH_MAX_H, need))
            if int(self.canvas.cget("height")) != h:
                self.canvas.configure(height=h)
            self.canvas.itemconfigure(self.win_item, height=need)
            if need > h:
                if not self.vsb.winfo_ismapped():
                    self.vsb.grid()
            else:
                self.vsb.grid_remove()
                self.canvas.yview_moveto(0)
            self.canvas.configure(
                scrollregion=(0, 0, max(self.canvas.winfo_width(), 1), need))
        finally:
            self._syncing = False

    def _on_wheel(self, event):
        self.canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _on_mode_change(self, *_):
        if self.var_mode.get() == MODE_PICK:
            self.frm_ch.grid()
            self.lbl_hint.configure(text="只下载下方勾选的章节（点整格切换勾选，超出可滚动）")
            self._sync_id = self.after_idle(self._sync_chapter_geometry)
        else:
            self.frm_ch.grid_remove()
            self.lbl_hint.configure(text="下载全部章节，生成每话 PDF + 合集")

    # ---- 搜索 ----
    def on_search(self):
        if self.busy:
            return
        kw = self.var_query.get().strip()
        if not kw:
            messagebox.showwarning(APP_TITLE, "请先输入作品 ID、详情页链接或关键词")
            return
        self._log_line("搜索：%s" % kw)
        self._set_busy(True)
        threading.Thread(target=self._search_worker, args=(kw,), daemon=True).start()

    def _session(self):
        if self.session is None:
            self.session = core.build_session(12)
        return self.session

    def _search_worker(self, kw):
        try:
            sess = self._session()
            kind, val = core.Picker.classify(kw)
            if kind == "id":
                try:
                    w = core.api(sess, "/api/catalog/works/%s" % val)
                    self.q.put(("work", (val, w)))
                    return
                except Exception as e:
                    self.q.put(("log", "按 id 取作品失败：%s\n改为关键词搜索…\n" % e))

            d = core.api(sess, "/api/catalog/search",
                         q=kw, page=1, pageSize=20, sort="updatedAt")
            items = d.get("items") or []
            if not items:
                self.q.put(("error", "没有找到匹配「%s」的作品" % kw))
                return
            if len(items) == 1:
                wid = items[0]["id"]
                w = core.api(sess, "/api/catalog/works/%s" % wid)
                self.q.put(("work", (wid, w)))
                return
            self.q.put(("choose", items))
        except Exception as e:
            self.q.put(("error", "搜索失败：%s" % e))
        finally:
            self.q.put(("search_done", None))

    def _load_work_async(self, wid):
        try:
            w = core.api(self._session(), "/api/catalog/works/%s" % wid)
            self.q.put(("work", (wid, w)))
        except Exception as e:
            self.q.put(("error", "取作品失败：%s" % e))
        finally:
            self.q.put(("search_done", None))

    def _load_work(self, wid, w):
        self.work_id = wid
        self.work = w
        self.title_text = w.get("title") or wid
        chapters = [c for c in (w.get("chapters") or []) if c.get("id")]
        chapters.sort(key=lambda c: (c.get("chapterNumber") or 0, c.get("createdAt") or ""))
        self.chapters = chapters

        author = w.get("authorName") or "-"
        self.lbl_work.configure(
            text="《%s》　作者：%s　共 %d 话" % (self.title_text, author, len(chapters)),
            foreground="#111")
        self._build_chapter_list()
        self.cbo_mode.configure(state="readonly")
        self.btn_go.configure(state="normal")
        self.refresh_path()
        self._log_line("已加载《%s》，%d 话" % (self.title_text, len(chapters)))
        if not chapters:
            messagebox.showwarning(APP_TITLE, "这部作品没有可用章节（可能已下架或需要登录）")

    def _build_chapter_list(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.chapter_vars = []
        self.dot_labels = []

        n = len(self.chapters)
        cols = max(1, min(CHAPTER_COLS, n)) if n else CHAPTER_COLS
        rows = (n + cols - 1) // cols if n else 1

        # 列等宽撑满，行等高；没有章节的列不占宽度
        for c in range(CHAPTER_COLS):
            if c < cols:
                self.inner.columnconfigure(c, weight=1, uniform="chcol", minsize=0)
            else:
                self.inner.columnconfigure(c, weight=0, uniform="", minsize=0)
        for r in range(rows):
            self.inner.rowconfigure(r, weight=0, uniform="chrow", minsize=CH_ROW_H)

        if not n:
            tk.Label(self.inner, text="（这部作品没有可用章节）", anchor="w",
                     font=FONT_UI, bg="white", fg="#999", padx=8, pady=4
                     ).grid(row=0, column=0, columnspan=CHAPTER_COLS, sticky="nsew")

        for i, ch in enumerate(self.chapters):
            num = ch.get("chapterNumber")
            num = num if isinstance(num, int) else i + 1
            name = core._trunc(ch.get("title") or "(无标题)", 16)
            var = tk.BooleanVar(value=True)
            lbl = tk.Label(self.inner, anchor="w", cursor="hand2",
                           font=FONT_UI, bg="white", bd=0, padx=8, pady=3)
            lbl.grid(row=i // cols, column=i % cols, sticky="nsew")
            lbl.bind("<Button-1>",
                     lambda e, v=var, l=lbl, nn=num, t=name: self._toggle_dot(l, v, nn, t))
            lbl.bind("<MouseWheel>", self._on_wheel)
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=CH_HOVER))
            lbl.bind("<Leave>", lambda e, l=lbl: l.configure(bg="white"))
            self._paint_dot(lbl, var, num, name)
            self.chapter_vars.append((var, ch))
            self.dot_labels.append((lbl, var, num, name))
        self._update_sel()
        self.canvas.yview_moveto(0)
        self._sync_id = self.after_idle(self._sync_chapter_geometry)

    @staticmethod
    def _paint_dot(lbl, var, num, name):
        if var.get():
            lbl.configure(text="◉ %03d %s" % (num, name), fg="#12539b")
        else:
            lbl.configure(text="○ %03d %s" % (num, name), fg="#8a8a8a")

    def _toggle_dot(self, lbl, var, num, name):
        var.set(not var.get())
        self._paint_dot(lbl, var, num, name)
        self._update_sel()

    def _repaint_all(self):
        for lbl, var, num, name in getattr(self, "dot_labels", []):
            self._paint_dot(lbl, var, num, name)

    def _update_sel(self):
        n = sum(1 for v, _ in self.chapter_vars if v.get())
        self.lbl_sel.configure(text="已勾选 %d / %d 话" % (n, len(self.chapter_vars)))

    def _set_all(self, value):
        for v, _ in self.chapter_vars:
            v.set(value)
        self._repaint_all()
        self._update_sel()

    def _choose_work(self, items):
        win = tk.Toplevel(self)
        win.title("选择作品 · 共 %d 条" % len(items))
        win.geometry("660x430")
        win.transient(self)
        win.grab_set()

        ttk.Label(win, text="搜索到多部作品，选中后点「确定」（或双击）：",
                  font=FONT_UI).pack(anchor="w", padx=12, pady=(12, 8))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12)
        tv = ttk.Treeview(frame, columns=("title", "author", "ch", "pop"),
                          show="headings", selectmode="browse")
        for key, txt, width, anchor in (("title", "标题", 320, "w"),
                                        ("author", "作者", 150, "w"),
                                        ("ch", "话数", 60, "e"),
                                        ("pop", "人气", 80, "e")):
            tv.heading(key, text=txt)
            tv.column(key, width=width, anchor=anchor, stretch=(key == "title"))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        id_map = {}
        for idx, it in enumerate(items):
            iid = "i%d" % idx
            id_map[iid] = it["id"]
            tv.insert("", "end", iid=iid,
                      values=(it.get("title") or "-",
                              it.get("authorName") or "-",
                              it.get("chapterCount") or 0,
                              core.fmt_num(it.get("popularity"))))
        if items:
            first = "i0"
            tv.selection_set(first)
            tv.focus(first)

        def confirm(*_):
            sel = tv.selection()
            if not sel:
                return
            wid = id_map[sel[0]]
            win.destroy()
            self._log_line("已选择作品 id=%s" % wid)
            self._set_busy(True)
            threading.Thread(target=self._load_work_async, args=(wid,), daemon=True).start()

        tv.bind("<Double-1>", confirm)
        tv.bind("<Return>", confirm)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="确定", command=confirm).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=(0, 6))

        win.wait_window()

    # ---- 路径 ----
    def _default_path(self):
        if not self.title_text:
            return ""
        return str(app_dir() / "comic" / core.safe_name(self.title_text, 80))

    def on_reset_path(self):
        self.lock_base = self._default_base() if self.var_lock.get() else None
        self.refresh_path()
        if self.var_lock.get():
            self._save_cfg()

    def on_browse(self):
        if self.var_lock.get():
            d = filedialog.askdirectory(
                title="选择父目录（每个漫画在其下建子文件夹）",
                initialdir=self.lock_base or str(app_dir()))
            if not d:
                return
            self.lock_base = str(Path(d))
            self.var_path.set(self._comic_folder(self.lock_base))
            self.lbl_path.configure(text="已锁定：%s" % self.lock_base, foreground="#12539b")
            self._save_cfg()
            self.on_apply_path()
            return
        d = filedialog.askdirectory(title="选择漫画根目录（会自动拼接 comic/漫画名）",
                                    initialdir=str(app_dir()))
        if not d:
            return
        name = core.safe_name(self.title_text, 80) if self.title_text else "漫画"
        self.var_path.set(str(Path(d) / "comic" / name))
        self.on_apply_path()

    def on_apply_path(self):
        raw = self.var_path.get().strip().strip('"')
        if not raw:
            self.on_reset_path()
            return
        p = Path(raw).expanduser()
        if self.var_lock.get() and p.parent != Path(self.lock_base or ""):
            # 锁定时以 父目录/漫画名 为准，防止手改跑偏
            p = Path(self._comic_folder(self.lock_base or self._default_base()))
        self.var_path.set(str(p))
        try:
            p.mkdir(parents=True, exist_ok=True)
            self.lbl_path.configure(
                text="已锁定父目录，将保存到：%s" % self.var_path.get()
                if self.var_lock.get() else "将保存到：%s" % p,
                foreground="#0a7a0a")
            self._log_line("保存路径已设置：%s" % p)
        except Exception as e:
            self.lbl_path.configure(text="路径不可用：%s" % e, foreground="#b00020")
            messagebox.showerror(APP_TITLE, "路径不可用：%s" % e)

    # ---- 下载 ----
    def on_start(self):
        if self.busy:
            return
        if not self.work:
            messagebox.showwarning(APP_TITLE, "请先搜索并选择一部作品")
            return

        picked = None
        partial = False
        if self.var_mode.get() == MODE_PICK:
            picked = [ch for v, ch in self.chapter_vars if v.get()]
            if not picked:
                messagebox.showwarning(APP_TITLE, "请至少勾选一个章节")
                return
            partial = len(picked) < len(self.chapters)

        out = self.var_path.get().strip().strip('"') or self._default_path()
        self.var_path.set(out)
        out_dir = Path(out).expanduser()

        ns = argparse.Namespace(
            out=str(out_dir.parent), threads=6, dpi=150, quality=95, delay=1.0,
            only=None, max_pages=0, force=False, list=False,
            keep_pages=False, no_seam_fix=False,
            id=None, interactive=False, search=None,
        )

        total = len(picked) if picked else len(self.chapters)
        self._set_busy(True)
        self.pb.start(12)
        self._append("\n" + "=" * 62 + "\n")
        self._append("开始下载《%s》　共 %d 话\n" % (self.title_text, total))
        self._append("目标目录：%s\n" % out_dir)
        self._append("=" * 62 + "\n")

        threading.Thread(
            target=self._download_worker,
            args=(self.work_id, self.work, picked, partial, out_dir, ns),
            daemon=True,
        ).start()

    def _download_worker(self, work_id, work, picked, partial, out_dir, ns):
        sink = StreamToQueue(self.q)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = sink
        try:
            core.Downloader(ns).run(work_id, picked=picked, work=work,
                                    partial=partial, out_dir=out_dir)
            self.q.put(("done", "下载完成\n\n%s" % "\n".join(textwrap.wrap(str(out_dir), 46))))
        except SystemExit as e:
            self.q.put(("error", str(e) if str(e) else "已中止"))
        except Exception as e:
            self.q.put(("error", "%s：%s" % (type(e).__name__, e)))
            self.q.put(("log", traceback.format_exc()))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.q.put(("download_done", None))

    # ------------------------------------------------------------ 日志 / 队列
    def _append(self, text):
        self.txt.configure(state="normal")
        self.txt.insert("end", text)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _log_line(self, msg):
        self._append("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))

    def _drain(self):
        try:
            while True:
                tag, payload = self.q.get_nowait()
                if tag == "log":
                    self._append(payload)
                elif tag == "work":
                    self._load_work(*payload)
                elif tag == "choose":
                    self._choose_work(payload)
                elif tag == "error":
                    self._append("\n[错误] %s\n" % payload)
                    messagebox.showerror(APP_TITLE, payload)
                elif tag == "done":
                    self._set_busy(False)          # 先复位按钮/进度条，再弹模态框
                    self._append("\n[完成] %s\n" % payload.replace("\n\n", "\n"))
                    messagebox.showinfo(APP_TITLE, payload)
                elif tag in ("search_done", "download_done"):
                    self._set_busy(False)
                    self.pb.stop()
        except queue.Empty:
            pass
        try:
            self._drain_id = self.after(80, self._drain)
        except tk.TclError:
            pass

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_search.configure(state=state)
        self.btn_go.configure(state="normal" if (not busy and self.work) else "disabled")
        if busy:
            self.pb.grid()
        else:
            self.pb.stop()
            self.pb.grid_remove()

    def _on_close(self):
        if self.busy and not messagebox.askyesno(APP_TITLE, "正在下载，确定要退出吗？"):
            return
        for aid in (getattr(self, "_drain_id", None), getattr(self, "_sync_id", None)):
            if aid:
                try:
                    self.after_cancel(aid)
                except tk.TclError:
                    pass
        self.destroy()


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
