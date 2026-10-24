#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comic.baby 漫画下载器
======================================================================
输入作品 id（或详情页 URL），自动遍历全部章节，把每一话存成一个 PDF，
再合并出一份合集 PDF：comic/<漫画名>/<序号>_<章节名>.pdf + <漫画名>_合集.pdf

关于「图片乱序」的处理（本脚本的核心，逆向自站点的 reader 前端）：
  1. 章节接口 /api/assets/chapters/{id}/read 会给每页一个 descrambleSegments，
     表示这张图被**纵向切成 N 段后整段倒序存放**（段高 = 总高//N，余数补在最后一段）。
     浏览器侧是写进 canvas 后用 drawImage 倒序重拼，本脚本用 PIL 做等价操作。
  2. 少数图片还叠加了 AES-256-CBC 加密（域名 img2*.mugua.live）。
     密钥/IV 硬编码在前端包里，本脚本直接复用；域名会被改写成 img*.mugua.live 作为降级。
  3. 重拼后段与段之间会有压缩造成的细线，前端用一段 YCbCr 接缝羽化抹掉，
     这里按同一算法实现（--no-seam-fix 可关闭）。

用法：
  python comic_dl.py                             # 打印清单，按序号选作品与章节
  python comic_dl.py m99y5au08f5aisdx7nvp62s4
  python comic_dl.py https://comic.baby/work/m99y5au08f5aisdx7nvp62s4
  python comic_dl.py <id> -i                     # 跳过搜索，直接选章节
  python comic_dl.py --search 斗破                # 只打印搜索结果清单
  python comic_dl.py <id> --out D:/comic --threads 8
  python comic_dl.py <id> --list                 # 只看章节列表
  python comic_dl.py <id> --only 1               # 只下第 1 话（章节序号）
  python comic_dl.py <id> --max-pages 5          # 每话只下前 5 页（调试用）
  python comic_dl.py <id> --force                # 忽略已存在的 PDF 重新生成

依赖：pip install requests pillow numpy pypdf cryptography
"""

import argparse
import io
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# pypdf 只在合并时用，缺失时给出友好提示
try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
    PdfReader = PdfWriter = None

# ------------------------------------------------------------------ 常量

BASE = "https://comic.baby"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
    # 前端给所有请求加的能力标记，图片加密相关的协商用
    "X-Client-Capabilities": "img-aes-cbc-v1",
}

# 前端包 _nuxt/DTEzeN18.js 里的常量
AES_KEY = b"8Z7X9f2kL3s7m8P9q0R5t6y7u8i9o0p8"
AES_IV = b"abcdef0987654321"

IMG_ENC_HOST = re.compile(r"^img2(-[a-z]+)?\.", re.I)
IMG_FALLBACK_HOST = re.compile(r"^img2(-[a-z]+)?\.", re.I)

# 接缝羽化算法参数（与前端一致）
SEAM_T = 4          # 接缝上下各取 4 行
SEAM_N = 8          # 窗口高 8 行
SEAM_MIN_BAND = 32  # 段高小于该值就不做羽化


# ------------------------------------------------------------------ 工具

def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def safe_name(name, maxlen=60):
    """把标题清洗成安全的文件名（Windows 也不出错）"""
    if not name:
        return "untitled"
    s = str(name)
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:maxlen].strip() or "untitled")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0


# ------------------------------------------------------------------ 网络

def build_session(pool=16):
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def api(session, path, **params):
    url = path if path.startswith("http") else BASE + path
    r = session.get(url, params=params or None, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("接口 %s 返回 HTTP %s" % (url, r.status_code))
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        # 404 时站点会回一张 HTML 错误页，提示得直白一点
        raise RuntimeError("接口 %s 没有返回 JSON（HTTP %s，%s）；"
                           "多半是 id 不存在或作品已下架"
                           % (url, r.status_code, ct or "无 content-type"))
    return r.json()


def fetch_bytes(session, url, tries=3):
    """下载单张图片；失败重试并支持 img2→img 降级"""
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=45)
            if r.status_code == 200 and r.content:
                return r.content
            last = "HTTP %s" % r.status_code
        except requests.RequestException as e:
            last = str(e)
        time.sleep(0.8 * (i + 1))
    # 加密域名下载失败时，改写成普通域名再试一次
    alt = img_fallback_url(url)
    if alt != url:
        try:
            r = session.get(alt, timeout=45)
            if r.status_code == 200 and r.content:
                return r.content
            last = "HTTP %s (fallback)" % r.status_code
        except requests.RequestException as e:
            last = str(e) + " (fallback)"
    raise RuntimeError("图片下载失败: %s -> %s" % (url, last))


# ------------------------------------------------------------------ 图片还原

def is_encrypted(url):
    try:
        host = requests.utils.urlparse(url).hostname or ""
    except Exception:
        host = ""
    return bool(IMG_ENC_HOST.match(host))


def img_fallback_url(url):
    def repl(m):
        return "img%s." % (m.group(1) or "")

    try:
        p = requests.utils.urlparse(url)
        host = IMG_FALLBACK_HOST.sub(repl, p.hostname or "", count=1)
        return p._replace(netloc=p.netloc.replace(p.hostname or "", host)).geturl()
    except Exception:
        return url


def aes_decrypt_cbc(data):
    """AES-256-CBC + PKCS7 去填充（密钥见常量）"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        raise RuntimeError("图片是 AES 加密的，请先安装依赖：pip install cryptography")

    dec = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).decryptor()
    plain = dec.update(data) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(plain) + unpadder.finalize()


def band_ranges(height, segs):
    """按前端 st() 的规则切段：前 segs-1 段等高，余数全给最后一段"""
    step = height // segs
    rem = height % segs
    out, y = [], 0
    for i in range(segs):
        h = step + (rem if i == segs - 1 else 0)
        out.append((y, y + h))
        y += h
    return out


def descramble(im, segs):
    """把被整段倒序存放的图重拼回正常顺序"""
    if not segs or segs <= 1:
        return im
    w, h = im.size
    if segs > h:                      # 异常值保护
        return im
    bands = band_ranges(h, segs)
    out = Image.new(im.mode, (w, h))
    y = 0
    for i in range(segs - 1, -1, -1):  # 倒序贴回
        a, b = bands[i]
        out.paste(im.crop((0, a, w, b)), (0, y))
        y += b - a
    return out


def _ramp(d, s, i):
    """前端 x()：|d| <= s 时 1，|d| >= i 时 0，中间线性"""
    return np.clip((i - np.abs(d)) / float(i - s), 0.0, 1.0)


def _fix_window(win):
    """
    接缝羽化：把 8 行窗口内因分段压缩产生的色阶/色度跳变，按上下两端的插值抹平。
    完全照搬前端 it() 的 YCbCr 处理，只是用 numpy 向量化。
    """
    r, g, b = win[:, :, 0], win[:, :, 1], win[:, :, 2]
    Y = 0.299 * r + 0.587 * g + 0.114 * b
    Cb = -0.168736 * r - 0.331264 * g + 0.5 * b
    Cr = 0.5 * r - 0.418688 * g - 0.081312 * b

    h1, y6 = Y[1], Y[6]          # 接缝上下各 3 行的参考亮度
    y0, y7 = Y[0], Y[7]          # 窗口最外侧
    cb0, cb7 = Cb[0], Cb[7]
    cr0, cr7 = Cr[0], Cr[7]

    # 只有上下两端本身连续（是真内容而不是硬切痕）才做羽化
    same = (np.abs(h1 - y0) <= 28) & (np.abs(y6 - y7) <= 28)

    out = np.empty_like(win)
    for l in range(SEAM_N):
        v = l / float(SEAM_N - 1)
        I = _ramp(y0 + (y7 - y0) * v - Y[l], 40, 60)

        E = Y[l]
        if SEAM_N - 3 >= l >= 2:          # 接缝两侧 4 行
            z = h1 + (y6 - h1) * (l - 1) / 5.0 - Y[l]
            E = np.where(same, Y[l] + np.minimum(I, _ramp(z, 12, 28)) * z, Y[l])

        dcb = cb0 + (cb7 - cb0) * v - Cb[l]
        dcr = cr0 + (cr7 - cr0) * v - Cr[l]
        k = np.minimum(np.minimum(I, _ramp(dcb, 12, 28)), _ramp(dcr, 12, 28))
        P = Cb[l] + k * dcb
        W = Cr[l] + k * dcr

        out[l, :, 0] = E + 1.402 * W
        out[l, :, 1] = E - 0.344136 * P - 0.714136 * W
        out[l, :, 2] = E + 1.772 * P

    return np.clip(np.rint(out), 0, 255)


def seam_fix(im, segs):
    """对所有段边界做羽化；段太矮时跳过（与前端一致）"""
    if not segs or segs <= 1:
        return im
    w, h = im.size
    bands = band_ranges(h, segs)
    if any(b - a < SEAM_MIN_BAND for a, b in bands):
        return im

    arr = np.asarray(im.convert("RGB"), dtype=np.float32)
    acc = 0
    for i in range(segs - 1):
        a, b = bands[i]
        acc += b - a
        start = acc - SEAM_T
        if start < 0 or start + SEAM_N > h:
            continue
        arr[start:start + SEAM_N] = _fix_window(arr[start:start + SEAM_N].copy())
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def decode_page(data, segs, fix=True):
    """图片字节 → 还原后的 PIL.Image"""
    im = Image.open(io.BytesIO(data))
    im.load()
    # 统一成 RGB（保留了透明通道的图会是 RGBA，PDF 里需要不透明背景）
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    im = descramble(im, segs)
    if fix:
        im = seam_fix(im, segs)
    return im


# ------------------------------------------------------------------ PDF

def save_single_page_pdf(im, path, dpi, quality):
    im.save(str(path), "PDF", resolution=dpi, quality=quality)


def merge_pdfs(pdfs, out_path, title=None, labels=None, author="comic.baby"):
    """按顺序合并 PDF；labels 可给每个文件加一个书签（合集里便于跳章节）"""
    if not PdfWriter:
        raise RuntimeError("缺少 pypdf，请先安装：pip install pypdf")
    writer = PdfWriter()
    for i, p in enumerate(pdfs):
        reader = PdfReader(str(p))
        start = len(writer.pages)
        for page in reader.pages:
            writer.add_page(page)
        if labels and i < len(labels) and labels[i]:
            writer.add_outline_item(str(labels[i]), start)
    writer.add_metadata({
        "/Title": title or out_path.stem,
        "/Producer": "comic_dl.py",
        "/Author": author,
    })
    tmp = str(out_path) + ".tmp"
    with open(tmp, "wb") as f:
        writer.write(f)
    writer.close()
    os.replace(tmp, out_path)


# ------------------------------------------------------------------ 主流程

class Downloader:
    def __init__(self, args):
        self.args = args
        self.session = build_session(args.threads + 4)
        self.root = Path(args.out).expanduser().resolve()

    # ---- 数据获取 ----
    def work(self, work_id):
        return api(self.session, "/api/catalog/works/%s" % work_id)

    def chapter(self, chapter_id):
        d = api(self.session, "/api/assets/chapters/%s/read" % chapter_id)
        if not d.get("success") or not isinstance(d.get("pages"), list):
            raise RuntimeError("章节 %s 没有取到页面数据" % chapter_id)
        return d

    def resolve_id(self, raw):
        raw = raw.strip()
        m = re.search(r"/work/([A-Za-z0-9]+)", raw)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9]{6,}", raw):
            return raw
        raise SystemExit("无法识别作品 id：%s（可传详情页 URL 或纯 id）" % raw)

    # ---- 单话处理 ----
    def process_chapter(self, idx, ch, work_dir, total):
        cid = ch["id"]
        ch_title = ch.get("title") or ("第%s话" % (ch.get("chapterNumber") or idx))
        try:
            num = int(ch.get("chapterNumber"))
        except (TypeError, ValueError):
            num = idx
        pdf_path = work_dir / ("%04d_%s.pdf" % (num, safe_name(ch_title, 40)))

        if pdf_path.exists() and pdf_path.stat().st_size > 1024 and not self.args.force:
            print("[%d/%d] %s 已存在，跳过（--force 可重下）" % (idx, total, pdf_path.name))
            return pdf_path

        data = self.chapter(cid)
        pages = sorted(data["pages"], key=lambda p: p.get("index") or 0)
        if self.args.max_pages:
            pages = pages[:self.args.max_pages]
        if not pages:
            print("[%d/%d] %s 没有页面，跳过" % (idx, total, ch_title))
            return None

        print("[%d/%d] %s：%d 页" % (idx, total, ch_title, len(pages)), flush=True)

        tmp_dir = work_dir / "_tmp" / ("ch%04d" % num)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        keep_dir = work_dir / "_pages" / ("%04d" % num) if self.args.keep_pages else None
        if keep_dir:
            keep_dir.mkdir(parents=True, exist_ok=True)

        page_pdfs = []
        done = 0
        batch = max(2, self.args.threads * 3)

        for start in range(0, len(pages), batch):
            chunk = pages[start:start + batch]
            with ThreadPoolExecutor(max_workers=self.args.threads) as ex:
                futures = [ex.submit(self._download_one, p) for p in chunk]
                results = [f.result() for f in futures]      # 保持页序

            for p, (raw, segs) in zip(chunk, results):
                im = decode_page(raw, segs, fix=not self.args.no_seam_fix)
                out_pdf = tmp_dir / ("p%05d.pdf" % (p.get("index", 0) + 1))
                save_single_page_pdf(im, out_pdf, self.args.dpi, self.args.quality)
                page_pdfs.append(out_pdf)
                if keep_dir:
                    im.save(keep_dir / ("%04d.jpg" % (p.get("index", 0) + 1)), quality=92)
                done += 1
                if done % 5 == 0 or done == len(pages):
                    print("    %d/%d 页" % (done, len(pages)), flush=True)

        tmp_pdf = work_dir / ("_tmp_%04d.pdf" % num)
        merge_pdfs(page_pdfs, tmp_pdf, title=ch_title)
        os.replace(tmp_pdf, pdf_path)

        # 清理分页 PDF
        for p in page_pdfs:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
            tmp_dir.parent.rmdir()      # 最后一话时把空壳 _tmp 也清掉
        except OSError:
            pass

        print("    → %s（%s）" % (pdf_path.name, human_size(pdf_path.stat().st_size)))
        return pdf_path

    def _download_one(self, page):
        url = page.get("imageUrl") or ""
        if not url:
            raise RuntimeError("页面缺少 imageUrl")
        raw = fetch_bytes(self.session, url)
        segs = int(page.get("descrambleSegments") or 0)

        if is_encrypted(url):
            try:
                raw = aes_decrypt_cbc(raw)
            except Exception as e:
                # 解密失败就退回普通域名拿原图（前端也是这么降级的）
                eprint("    加密图解密失败（%s），改用降级域名" % e)
                raw = fetch_bytes(self.session, img_fallback_url(url))
        return raw, segs

    # ---- 入口 ----
    def run(self, raw_id, picked=None, work=None, partial=False, out_dir=None):
        """
        picked  : 交互模式已选好的章节列表（给定时忽略 --only）
        work    : 已经取到的作品 JSON，避免重复请求
        partial : 只选了一部分章节 → 合集文件名加标记，别覆盖完整合集
        out_dir : 直接指定输出目录（GUI 用），给定时不再拼 self.root/漫画名
        """
        work_id = self.resolve_id(raw_id)
        w = work if work is not None else self.work(work_id)
        title = w.get("title") or work_id
        chapters = [c for c in (w.get("chapters") or []) if c.get("id")]
        chapters.sort(key=lambda c: (c.get("chapterNumber") or 0, c.get("createdAt") or ""))

        if not chapters:
            raise SystemExit("该作品没有可用章节（可能已下架或需要登录）")

        if len(chapters) < int(w.get("chapterCount") or 0):
            eprint("提示：作品标注 %s 话，接口只返回 %d 话，可能被截断"
                   % (w.get("chapterCount"), len(chapters)))

        if self.args.list:
            print("《%s》共 %d 话" % (title, len(chapters)))
            for c in chapters:
                print("  %4s  %-40s %4sP  %s" % (
                    c.get("chapterNumber"), c.get("title"),
                    c.get("pageCount"), c.get("id")))
            return

        if picked is not None:
            chapters = picked
            if not chapters:
                raise SystemExit("没有选中任何章节")
        elif self.args.only:
            picked = [c for c in chapters
                      if str(c.get("chapterNumber")) == str(self.args.only)]
            if not picked:
                try:
                    picked = [chapters[int(self.args.only) - 1]]
                except (ValueError, IndexError):
                    raise SystemExit("--only 指定的章节不存在：%s" % self.args.only)
            chapters = picked
            partial = True        # 单话下载也不该覆盖完整合集

        work_dir = (Path(out_dir).expanduser().resolve() if out_dir
                    else self.root / safe_name(title, 80))
        work_dir.mkdir(parents=True, exist_ok=True)
        print("作品：《%s》  id=%s" % (title, work_id))
        print("输出：%s" % work_dir)
        print("章节：%d 话%s" % (len(chapters), "" if not self.args.max_pages
                                else "（每话只取前 %d 页）" % self.args.max_pages))

        t0 = time.time()
        items = []
        for i, ch in enumerate(chapters, 1):
            label = ch.get("title") or ("第%s话" % ch.get("chapterNumber"))
            try:
                p = self.process_chapter(i, ch, work_dir, len(chapters))
            except Exception as e:
                eprint("[%d/%d] 失败：%s（%s）" % (i, len(chapters), ch.get("title"), e))
                continue
            if p and p.exists():
                items.append((p, label))
            if i < len(chapters):
                time.sleep(self.args.delay)

        if not items:
            raise SystemExit("没有成功下载任何章节")

        # 合集：按文件名（章节号）排序后合并，并给每话加书签
        items.sort(key=lambda t: t[0].name)
        pdfs = [p for p, _ in items]
        labels = [lb for _, lb in items]
        if partial:
            total_pdf = work_dir / ("%s_合集_已选%d话.pdf"
                                    % (safe_name(title, 50), len(chapters)))
        else:
            total_pdf = work_dir / ("%s_合集.pdf" % safe_name(title, 60))
        merge_pdfs(pdfs, total_pdf, title=title, labels=labels)
        print("\n完成，用时 %.1fs" % (time.time() - t0))
        print("  单话 PDF：%d 个" % len(pdfs))
        print("  合集 PDF：%s（%s）" % (total_pdf.name, human_size(total_pdf.stat().st_size)))


# ------------------------------------------------------------------ print 清单式选择
#
# 设计原则：全部用 print 往屏幕上打，末尾给一行输入提示，输入编号即可。
# 没有全屏重绘、没有翻屏网格、没有按键组合——就是普通脚本的输出节奏。

class Cancelled(Exception):
    """用户主动取消"""


def _dwidth(s):
    """终端显示宽度：CJK / 全角算 2 列"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _trunc(s, width):
    """按显示宽度截断，超出补省略号"""
    if _dwidth(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


def _pad(s, width, align="left"):
    gap = width - _dwidth(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def fmt_num(n):
    """人气数字转 万 / 亿"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000.0)
    if n >= 10000:
        return "%.1f万" % (n / 10000.0)
    return str(n)


def _term_width():
    try:
        return max(60, min(96, os.get_terminal_size().columns - 1))
    except Exception:
        return 72


def rule(ch="-", width=None):
    print(ch * (width or _term_width()))


def step(no, total, text):
    """打印一个步骤标题块"""
    print()
    rule("=")
    print("[%d/%d] %s" % (no, total, text))
    rule("-")


def banner():
    print()
    rule("=")
    print(" comic.baby 漫画下载器")
    print(" 选作品 → 选章节 → 每话一份 PDF + 全书合集 PDF")
    rule("=")


def print_table(headers, rows, aligns=None):
    """打印一张按显示宽度对齐的表格"""
    cols = len(headers)
    aligns = aligns or ["left"] * cols
    widths = []
    for i in range(cols):
        w = _dwidth(headers[i])
        for r in rows:
            w = max(w, _dwidth(r[i]))
        widths.append(w)

    # 总宽超出终端时，先挤窄非标题列，最后才动标题列
    total = sum(widths) + 3 * (cols - 1) + 2
    over = total - _term_width()
    order = [i for i in range(cols) if i not in (0, 1)] + [1]
    for i in order:
        if over <= 0:
            break
        room = max(0, widths[i] - 6)
        cut = min(over, room)
        widths[i] -= cut
        over -= cut

    head = "   ".join(_pad(_trunc(headers[i], widths[i]), widths[i], aligns[i])
                      for i in range(cols))
    print("  " + head.rstrip())
    print("  " + "-" * (sum(widths) + 3 * (cols - 1)))
    for r in rows:
        print(("  " + "   ".join(_pad(_trunc(r[i], widths[i]), widths[i], aligns[i])
                                 for i in range(cols))).rstrip())


class Picker:
    """print 清单 + 输入序号：步骤 1 选作品，步骤 2 选章节，步骤 3 确认"""

    PAGE_ITEMS = 20          # 搜索结果每页条数

    def __init__(self, session, args):
        self.s = session
        self.args = args
        self.sort = "updatedAt"

    # ---- 基础 ----
    @staticmethod
    def classify(raw):
        """判断输入是作品 id 还是搜索关键词"""
        m = re.search(r"/work/([A-Za-z0-9]+)", raw)
        if m:
            return "id", m.group(1)
        if re.fullmatch(r"[A-Za-z0-9]{16,}", raw):
            return "id", raw
        return "kw", raw

    def ask(self, label):
        try:
            return input("%s: " % label).strip()
        except (EOFError, KeyboardInterrupt, RuntimeError):
            # RuntimeError 覆盖 input(): lost sys.stdin（编辑器/无标准输入的环境）
            raise Cancelled()

    def get_work(self, wid):
        try:
            return api(self.s, "/api/catalog/works/%s" % wid)
        except Exception as e:
            print("  取作品失败：%s" % e)
            return None

    def search(self, keyword, page):
        return api(self.s, "/api/catalog/search", q=keyword, page=page,
                   pageSize=self.PAGE_ITEMS, sort=self.sort)

    # ---- 步骤 1：搜索并选作品 ----
    def pick_work(self, preset=None):
        keyword = None
        page = 1

        if preset:
            kind, val = self.classify(preset.strip())
            if kind == "id":
                w = self.get_work(val)
                if w:
                    return val, w
                print("  给定的 id 取不到作品，改为按关键词搜索")
                keyword = preset.strip()
            else:
                keyword = preset.strip()

        while True:
            if keyword is None:
                step(1, 3, "搜索作品")
                print("  输入关键词；也可直接粘贴作品 id 或 https://comic.baby/work/<id>")
                keyword = self.ask("关键词")
                if not keyword:
                    continue
                if keyword.lower() in ("q", "quit", "exit"):
                    raise Cancelled()
                kind, val = self.classify(keyword)
                if kind == "id":
                    w = self.get_work(val)
                    if w:
                        return val, w
                    keyword = None
                    continue
                page = 1

            try:
                d = self.search(keyword, page)
            except Exception as e:
                print("  搜索失败：%s" % e)
                keyword = None
                continue

            items = d.get("items") or []
            total = int(d.get("total") or 0)
            if not items:
                if page > 1:
                    page = 1
                    continue
                print("  没有匹配「%s」的作品，换个词试试" % keyword)
                keyword = None
                continue

            pages = max(1, (total + self.PAGE_ITEMS - 1) // self.PAGE_ITEMS)
            lo = (page - 1) * self.PAGE_ITEMS + 1
            hi = lo + len(items) - 1

            print()
            print("  「%s」共 %d 条 · 第 %d/%d 页（%d~%d）· 按%s"
                  % (keyword, total, page, pages, lo, hi,
                     "最近更新" if self.sort == "updatedAt" else "人气"))
            self.print_works(items, lo)
            print()
            print("  输入序号选择作品")
            print("  回车=下一页 · p=上一页 · s=换排序 · 直接输关键词=重搜 · q=退出")

            cmd = self.ask("序号")
            low = cmd.lower()

            if low in ("q", "quit", "exit"):
                raise Cancelled()
            if cmd.isdigit():
                i = int(cmd)
                if lo <= i <= hi:
                    wid = items[i - lo]["id"]
                    w = self.get_work(wid)
                    if w:
                        return wid, w
                    continue
                print("  序号超出范围（%d~%d）" % (lo, hi))
                continue
            if low in ("", "n", "next"):
                if page < pages:
                    page += 1
                else:
                    print("  已经是最后一页")
                continue
            if low in ("p", "prev"):
                if page > 1:
                    page -= 1
                else:
                    print("  已经是第一页")
                continue
            if low in ("s", "sort"):
                self.sort = "popular" if self.sort == "updatedAt" else "updatedAt"
                page = 1
                continue
            keyword = cmd
            page = 1

    def print_works(self, items, start=1):
        rows = []
        for i, it in enumerate(items, start):
            rows.append([
                "[%d]" % i,
                it.get("title") or "(无标题)",
                _trunc(it.get("authorName") or "-", 16),
                "%s话" % (it.get("chapterCount") or 0),
                fmt_num(it.get("popularity")),
                it.get("serialCode") or "-",
            ])
        print_table(["#", "标题", "作者", "话数", "人气", "编号"], rows,
                    ["right", "left", "left", "right", "right", "left"])

    # ---- 步骤 2：选章节 ----
    def pick_chapters(self, work, work_id):
        title = work.get("title") or work_id
        chapters = [c for c in (work.get("chapters") or []) if c.get("id")]
        chapters.sort(key=lambda c: (c.get("chapterNumber") or 0,
                                     c.get("createdAt") or ""))
        if not chapters:
            raise SystemExit("该作品没有可用章节（可能已下架或需要登录）")

        n = len(chapters)
        step(2, 3, "选择章节 ·《%s》共 %d 话" % (title, n))
        self.print_chapters(chapters)
        print()
        print("  输入要下载的序号：1  |  1-5  |  1,3,5-7")
        print("  直接回车 = 全部 %d 话 · q = 退出" % n)

        while True:
            cmd = self.ask("章节")
            low = cmd.lower()

            if low in ("q", "quit", "exit"):
                raise Cancelled()
            if low == "":
                sel = list(range(n))
            else:
                idxs = self._parse_index(cmd, n)
                if idxs is None:
                    print("  看不懂「%s」：示例 1 / 1-5 / 1,3,5-7，范围 1~%d" % (cmd, n))
                    continue
                sel = sorted(idxs)
                if not sel:
                    print("  没有选中任何章节")
                    continue

            if self._confirm(work, work_id, chapters, sel):
                return [chapters[i] for i in sel], len(sel) < n

            # 返回修改：重打一遍清单，让用户重新输入
            print()
            print("  重新选择：")
            self.print_chapters(chapters)
            print()
            print("  输入要下载的序号：1  |  1-5  |  1,3,5-7　（回车=全部）")

    def print_chapters(self, chapters):
        rows = []
        for i, ch in enumerate(chapters, 1):
            num = ch.get("chapterNumber")
            num = num if isinstance(num, int) else i
            pc = ch.get("pageCount")
            rows.append([
                "[%d]" % i,
                "%03d" % num,
                ch.get("title") or "(无标题)",
                ("%dP" % pc) if pc else "-",
            ])
        print_table(["#", "话", "标题", "页数"], rows,
                    ["right", "right", "left", "right"])

    @staticmethod
    def _parse_index(text, n):
        """解析 3 / 1-5 / 1,3,5-7 / 1- 为 0 基下标集合；非法返回 None"""
        text = text.replace("，", ",").replace("~", "-").replace("－", "-").replace(" ", "")
        out = set()
        for part in text.split(","):
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    lo = int(a) if a else 1
                    hi = int(b) if b else n
                except ValueError:
                    return None
                if lo > hi:
                    lo, hi = hi, lo
                if lo < 1 or hi > n:
                    return None
                out.update(range(lo - 1, hi))
            else:
                if not part.isdigit():
                    return None
                v = int(part)
                if v < 1 or v > n:
                    return None
                out.add(v - 1)
        return out

    # ---- 步骤 3：确认 ----
    def _confirm(self, work, work_id, chapters, sel):
        title = work.get("title") or work_id
        total_pages = 0
        for i in sel:
            pc = int(chapters[i].get("pageCount") or 0)
            if self.args.max_pages:
                pc = min(pc, self.args.max_pages)
            total_pages += pc
        work_dir = Path(self.args.out).expanduser().resolve() / safe_name(title, 80)

        step(3, 3, "确认下载")
        print("  作品: 《%s》" % title)
        print("  章节: %d / %d 话" % (len(sel), len(chapters)))
        print("  页数: 约 %d 页" % total_pages)
        print("  输出: %s" % work_dir)
        print("  并发: %d 线程，章节间隔 %.1fs" % (self.args.threads, self.args.delay))
        rule("-")
        print("  回车 = 开始下载 · b = 重新选章节 · q = 取消")

        cmd = self.ask("继续")
        low = cmd.lower()
        if low in ("q", "quit", "exit"):
            raise Cancelled()
        return low in ("", "y", "yes", "ok", "d")


def interactive(args):
    """无 id 时的默认入口：print 清单 → 输入序号 → 下载"""
    session = build_session(max(8, args.threads + 4))
    banner()
    picker = Picker(session, args)

    while True:
        work_id, work = picker.pick_work(args.id)
        picked, partial = picker.pick_chapters(work, work_id)
        Downloader(args).run(work_id, picked=picked, work=work, partial=partial)

        args.id = None                    # 下一轮从搜索开始
        print()
        print("  回车 = 退出 · 输入 y = 再挑一本")
        try:
            if picker.ask("继续").lower() not in ("y", "yes"):
                return 0
        except Cancelled:
            return 0


def search_only(args, keyword):
    """只打印搜索结果清单，不进入任何输入等待"""
    session = build_session(8)
    picker = Picker(session, args)
    rule("=")
    print(" 搜索「%s」（按%s）" % (keyword, "最近更新" if picker.sort == "updatedAt" else "人气"))
    rule("=")
    d = picker.search(keyword, 1)
    items = d.get("items") or []
    total = int(d.get("total") or 0)
    if not items:
        print("  没有匹配结果")
        return 0
    print(" 共 %d 条，显示前 %d 条：" % (total, len(items)))
    picker.print_works(items, 1)
    print()
    print(" 下载某一本：python comic_dl.py %s" % items[0]["id"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="comic.baby 漫画下载器：id → 每话 PDF + 合集 PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python comic_dl.py                      打印清单后按序号选作品与章节\n"
               "  python comic_dl.py m99y5au08f5aisdx7nvp62s4\n"
               "  python comic_dl.py <id> -i              直接对这本选章节\n"
               "  python comic_dl.py --search 斗破         只打印搜索结果清单\n",
    )
    ap.add_argument("id", nargs="?", help="作品 id 或 https://comic.baby/work/<id>（省略则打印清单供选择）")
    ap.add_argument("-i", "--interactive", action="store_true", help="打印清单并选择作品与章节")
    ap.add_argument("--search", metavar="关键词", help="只打印搜索结果清单，不下载")
    ap.add_argument("--out", default="comic", help="输出根目录（默认 ./comic）")
    ap.add_argument("--threads", type=int, default=6, help="图片并发数（默认 6）")
    ap.add_argument("--dpi", type=int, default=150, help="PDF 每英寸像素（默认 150）")
    ap.add_argument("--quality", type=int, default=95, help="JPEG 质量 1-100（默认 95）")
    ap.add_argument("--delay", type=float, default=1.0, help="章节间隔秒数（默认 1.0）")
    ap.add_argument("--only", help="只处理指定章节序号（chapterNumber）")
    ap.add_argument("--max-pages", type=int, default=0, help="每话最多取几页（0=全部）")
    ap.add_argument("--force", action="store_true", help="忽略已存在的 PDF，重新生成")
    ap.add_argument("--list", action="store_true", help="只列出章节，不下载")
    ap.add_argument("--keep-pages", action="store_true", help="额外保留每页 JPG 便于校对")
    ap.add_argument("--no-seam-fix", action="store_true", help="关闭接缝羽化")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.search:
        return search_only(args, args.search)

    # 编辑器（PyCharm / VS Code）的 Run 窗口不是真正的 TTY，但同样可以 input()，
    # 所以这里不按 isatty 拒绝；读不到输入时 Picker.ask 会抛 Cancelled 正常退出。
    want_tui = bool(args.interactive or not args.id)

    try:
        if want_tui:
            interactive(args)
        else:
            Downloader(args).run(args.id)
    except Cancelled:
        print("\n已取消，已完成的文件会保留")
        return 130
    except KeyboardInterrupt:
        eprint("\n已中断（已完成的 PDF 会保留，重跑会跳过）")
        return 130
    except SystemExit:
        raise
    except RuntimeError as e:
        eprint("错误：%s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
