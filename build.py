#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包脚本：comic_dl.py / comic_gui.py  →  .pyc  →  ComicDownloader.exe

用法：
    python build.py            # 完整打包
    python build.py --pyc      # 只生成 pyc，不打包

步骤：
  1. 用当前解释器把两个模块编译成 build/pyc/*.pyc
  2. 生成一个简单的程序图标 build/icon.ico
  3. 用 PyInstaller 打包 boot.py，并把两个 pyc 作为数据文件塞进 exe
     （boot.py 从 pyc 加载真正的程序，所以 exe 里不含 .py 源码）
"""

import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
PYC_DIR = BUILD / "pyc"
DIST = ROOT                 # exe 直接产在项目根目录（仓库里只放这一份成品）
ICON = BUILD / "icon.ico"
APP_NAME = "ComicDownloader"
MODULES = ("comic_dl.py", "comic_gui.py")


def log(msg):
    print("[build] %s" % msg, flush=True)


def compile_pyc():
    if PYC_DIR.exists():
        shutil.rmtree(PYC_DIR)
    PYC_DIR.mkdir(parents=True)
    for name in MODULES:
        src = ROOT / name
        dst = PYC_DIR / (src.stem + ".pyc")
        py_compile.compile(str(src), cfile=str(dst), doraise=True)
        log("pyc: %s (%d 字节)" % (dst.name, dst.stat().st_size))
    return PYC_DIR


def make_icon():
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log("未安装 Pillow，跳过图标")
        return None

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, 250, 250], radius=54, fill=(26, 84, 152, 255))
    d.rounded_rectangle([114, 52, 142, 146], radius=12, fill=(255, 255, 255, 255))
    d.polygon([(78, 138), (178, 138), (128, 206)], fill=(255, 255, 255, 255))
    d.rounded_rectangle([78, 220, 178, 232], radius=6, fill=(255, 255, 255, 210))

    BUILD.mkdir(parents=True, exist_ok=True)
    img.save(str(ICON), format="ICO",
             sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    log("icon: %s" % ICON)
    return ICON


def build_exe():
    exe = Path(sys.executable).parent / "pyinstaller.exe"
    if not exe.exists():
        exe = Path(sys.executable).parent / "pyinstaller"
    cmd = [
        str(exe),
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", APP_NAME,
        "--add-data", "%s%s." % (PYC_DIR / "comic_dl.pyc", os.pathsep),
        "--add-data", "%s%s." % (PYC_DIR / "comic_gui.pyc", os.pathsep),
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
    ]
    if ICON.exists():
        cmd += ["--icon", str(ICON)]
    cmd.append(str(ROOT / "boot.py"))

    log("pyinstaller 打包中（首次约 1~2 分钟）…")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit("PyInstaller 失败，返回码 %d" % r.returncode)

    out = DIST / ("%s.exe" % APP_NAME)
    if not out.exists():
        raise SystemExit("未找到产物：%s" % out)
    log("完成：%s（%.1f MB）" % (out, out.stat().st_size / 1048576))
    return out


def main():
    only_pyc = "--pyc" in sys.argv
    compile_pyc()
    if only_pyc:
        return 0
    make_icon()
    build_exe()
    return 0


if __name__ == "__main__":
    sys.exit(main())
