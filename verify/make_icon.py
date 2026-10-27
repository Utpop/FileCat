# -*- coding: utf-8 -*-
"""
verify/make_icon.py —— 把一张方形插画加工成程序图标（PNG + ICO）。

用法：
    python verify/make_icon.py <源图> [输出目录]

做四件事：
1. 从四条边做洪水填充，把外部纯白背景变成透明（猫自己身上的白色被黑色轮廓
   围住，不会被吃掉）；
2. 裁掉透明边距，按内容重新居中成正方形，留 6% 呼吸空间；
3. 导出 512×512 的 `app_icon.png` 给 Qt 用；
4. 导出多尺寸 `app_icon.ico`（16/24/32/48/64/128/256）给桌面快捷方式用。
   Qt 不支持写 ICO，这里按 ICO 规范自己拼容器，条目用 PNG 压缩（Vista+ 支持）。
"""

from __future__ import annotations

import os
import struct
import sys
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
VENDOR = os.path.join(ROOT, "_vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt   # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter                     # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

WHITE = 246          # 三通道都不低于这个值就算"背景白"
HALO = 225           # 贴边的浅灰（JPEG 振铃）一并清掉
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
MASTER = 512
MARGIN = 0.06        # 内容四周留 6% 空白，图标不贴边


def strip_background(img: QImage) -> QImage:
    """从四条边洪水填充，把与边界连通的白色变透明。"""
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()

    def is_bg(x: int, y: int) -> bool:
        c = img.pixelColor(x, y)
        return c.red() >= WHITE and c.green() >= WHITE and c.blue() >= WHITE

    seen = bytearray(w * h)
    queue: deque = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not seen[y * w + x] and is_bg(x, y):
                seen[y * w + x] = 1
                queue.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not seen[y * w + x] and is_bg(x, y):
                seen[y * w + x] = 1
                queue.append((x, y))

    transparent = QColor(255, 255, 255, 0)
    while queue:
        x, y = queue.popleft()
        img.setPixelColor(x, y, transparent)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                seen[ny * w + nx] = 1
                if is_bg(nx, ny):
                    queue.append((nx, ny))

    # JPEG 在轮廓边上留了一圈浅灰振铃，填充会在剪影外侧留下白点。
    # 再走两遍：贴着透明区、又足够浅的像素也一起清掉，边缘才干净。
    for _ in range(2):
        kill = []
        for y in range(h):
            for x in range(w):
                c = img.pixelColor(x, y)
                if c.alpha() == 0 or min(c.red(), c.green(), c.blue()) < HALO:
                    continue
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < w and 0 <= ny < h and img.pixelColor(nx, ny).alpha() == 0:
                        kill.append((x, y))
                        break
        for x, y in kill:
            img.setPixelColor(x, y, transparent)
    return img


def content_rect(img: QImage) -> QRect:
    """不透明像素的包围盒。"""
    left, top = img.width(), img.height()
    right = bottom = -1
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8:
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
    if right < 0:
        return QRect(0, 0, img.width(), img.height())
    return QRect(left, top, right - left + 1, bottom - top + 1)


def to_square(img: QImage, box: QRect, size: int) -> QImage:
    """把内容居中放进正方形画布，四周按 MARGIN 留白，再平滑缩放到 size。"""
    side = int(max(box.width(), box.height()) * (1 + 2 * MARGIN))
    canvas = QImage(side, side, QImage.Format_ARGB32)
    canvas.fill(QColor(0, 0, 0, 0))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    dx = (side - box.width()) // 2
    dy = (side - box.height()) // 2
    painter.drawImage(QRect(dx, dy, box.width(), box.height()), img, box)
    painter.end()

    out = QImage(size, size, QImage.Format_ARGB32)
    out.fill(QColor(0, 0, 0, 0))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawImage(QRect(0, 0, size, size), canvas)
    painter.end()
    return out


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path: str, images) -> None:
    """按 ICO 规范拼容器；每个条目是一段完整 PNG。"""
    payloads = [png_bytes(im) for im in images]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = []
    for im, data in zip(images, payloads):
        w = 0 if im.width() >= 256 else im.width()
        h = 0 if im.height() >= 256 else im.height()
        entries.append(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32,
                                   len(data), offset))
        offset += len(data)
    with open(path, "wb") as fh:
        fh.write(header + b"".join(entries) + b"".join(payloads))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    src = argv[0]
    out_dir = argv[1] if len(argv) > 1 else os.path.join(ROOT, "assets")
    os.makedirs(out_dir, exist_ok=True)

    app = QApplication.instance() or QApplication([])   # noqa: F841

    img = QImage(src)
    if img.isNull():
        print("读不到图片：%s" % src)
        return 1
    img = strip_background(img)
    box = content_rect(img)
    master = to_square(img, box, MASTER)
    png_path = os.path.join(out_dir, "app_icon.png")
    master.save(png_path, "PNG")

    ico_path = os.path.join(out_dir, "app_icon.ico")
    write_ico(ico_path, [to_square(img, box, s) for s in ICO_SIZES])

    print("源图：%s" % src)
    print("内容框：%dx%d @ (%d,%d)" % (box.width(), box.height(), box.x(), box.y()))
    print("PNG：%s（%d×%d）" % (png_path, master.width(), master.height()))
    print("ICO：%s（%s）" % (ico_path, " / ".join(str(s) for s in ICO_SIZES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
