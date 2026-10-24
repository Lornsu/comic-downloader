#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包入口（boot）：从 .pyc 加载真正的程序，再启动图形界面。

这样 dist 里的 exe 不含 .py 源码 —— 打包流程是
    comic_dl.py / comic_gui.py  →  compileall  →  *.pyc  →  PyInstaller  →  *.exe

PyInstaller 只能静态分析 .py，看不到 pyc 里的 import，
所以下面把第三方依赖显式 import 一遍，让收集器把它们打进包里。
"""

import importlib.util
import os
import sys
import traceback


def _bundle_dir():
    """onefile 模式解压到 _MEIPASS；onedir / 直接跑脚本时用脚本所在目录"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _load_pyc(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法加载 %s（%s）" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ensure_deps():
    """
    让 PyInstaller 收集运行期才用到的模块。

    两个 .pyc 里的 import 语句它静态分析不到，所以这里把用到的模块全 import 一遍；
    tkinter 尤其要注意 —— 只 import tkinter 不够，filedialog / messagebox / ttk /
    scrolledtext 都是独立的 .py 文件，不显式引用就不会被打进包里。
    """
    # 标准库
    import argparse  # noqa: F401
    import concurrent.futures  # noqa: F401
    import ctypes  # noqa: F401
    import io  # noqa: F401
    import os  # noqa: F401
    import pathlib  # noqa: F401
    import queue  # noqa: F401
    import re  # noqa: F401
    import textwrap  # noqa: F401
    import threading  # noqa: F401
    import time  # noqa: F401
    import traceback  # noqa: F401
    import unicodedata  # noqa: F401

    # tkinter 全家桶
    import tkinter  # noqa: F401
    import tkinter.constants  # noqa: F401
    import tkinter.font  # noqa: F401
    import tkinter.ttk  # noqa: F401
    import tkinter.filedialog  # noqa: F401
    import tkinter.messagebox  # noqa: F401
    import tkinter.scrolledtext  # noqa: F401

    # 第三方
    import requests  # noqa: F401
    import PIL  # noqa: F401
    import PIL.Image  # noqa: F401
    import numpy  # noqa: F401
    import pypdf  # noqa: F401
    import cryptography  # noqa: F401
    from cryptography.hazmat.primitives import padding  # noqa: F401
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: F401


def _fatal(msg):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", msg)
        root.destroy()
    except Exception:
        sys.stderr.write(msg + "\n")


def main():
    base = _bundle_dir()
    try:
        _ensure_deps()
        _load_pyc("comic_dl", os.path.join(base, "comic_dl.pyc"))
        gui = _load_pyc("comic_gui", os.path.join(base, "comic_gui.pyc"))
    except Exception:
        _fatal("程序文件加载失败：\n\n%s" % traceback.format_exc())
        return 1

    gui.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
