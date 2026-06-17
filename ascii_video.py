#!/usr/bin/env python3
"""
ASCII Video Player - 终端播放彩色视频/GIF
支持: Kitty图形协议(真图像) / Sixel / Unicode半块(兼容)
依赖: pip install "imageio[ffmpeg]" Pillow rich numpy
"""

import sys
import io
import time
import argparse
import os
import base64
import shutil

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from rich.console import Console

# Win32 GPU 加速: 直接写入控制台
_win32_write = None
if sys.platform == "win32":
    try:
        import ctypes
        _kernel32 = ctypes.windll.kernel32
        _GetStdHandle = _kernel32.GetStdHandle
        _WriteConsoleW = _kernel32.WriteConsoleW
        _GetConsoleMode = _kernel32.GetConsoleMode
        _SetConsoleMode = _kernel32.SetConsoleMode
        # 启用 Virtual Terminal Processing (ANSI)
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = _GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        _GetConsoleMode(handle, ctypes.byref(mode))
        _SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        _console_handle = handle
        _win32_write = True
    except Exception:
        _win32_write = None

try:
    import imageio.v3 as iio
except ImportError:
    print("请先安装: pip install \"imageio[ffmpeg]\" Pillow rich numpy")
    sys.exit(1)


console = Console()


def fast_write(data):
    """快速写入终端 (Win32 API 或 buffer)"""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    if _win32_write:
        # WriteConsoleW 需要 UTF-16-LE
        try:
            text = data.decode("utf-8", errors="replace")
            utf16 = text.encode("utf-16-le")
            written = ctypes.c_ulong()
            _WriteConsoleW(_console_handle, utf16, len(text), ctypes.byref(written), None)
        except Exception:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def get_terminal_size():
    """实时获取终端尺寸"""
    s = shutil.get_terminal_size((80, 24))
    return s.columns, s.lines


def detect_terminal():
    term = os.environ.get("TERM_PROGRAM", "").lower()
    term_type = os.environ.get("TERM", "").lower()
    if "kitty" in term:
        return "kitty"
    if "wezterm" in term:
        return "kitty"
    if "iterm" in term:
        return "sixel"
    if "sixel" in term_type or "mlterm" in term_type or "foot" in term_type:
        return "sixel"
    return "block"


def is_gif(path):
    return os.path.splitext(path)[1].lower() == ".gif"


def read_frames(path):
    if is_gif(path):
        pil = Image.open(path)
        frames, durations = [], []
        try:
            while True:
                frames.append(pil.convert("RGB").copy())
                durations.append(max(pil.info.get("duration", 100), 20) / 1000.0)
                pil.seek(pil.tell() + 1)
        except EOFError:
            pass
        fps = 1.0 / durations[0] if durations else 30.0
        return frames, durations, "GIF", fps, pil.size
    else:
        raw = list(iio.imiter(path, plugin="pyav"))
        meta = iio.immeta(path, plugin="pyav")
        fps = meta.get("fps", 30.0)
        frames = [Image.fromarray(f) if isinstance(f, np.ndarray) else f for f in raw]
        orig_size = frames[0].size if frames else (0, 0)
        return frames, [1.0 / fps] * len(frames), "VIDEO", fps, orig_size


def calc_optimal_size(orig_w, orig_h, term_cols, term_rows):
    """根据原始尺寸和终端尺寸计算最佳输出尺寸"""
    avail_rows = (term_rows - 2) * 2

    # 目标: 填满终端但不溢出, 最多放大3倍
    scale_w = term_cols / orig_w
    scale_h = avail_rows / (orig_h * 0.5)
    scale = min(scale_w, scale_h, 3.0)
    scale = max(scale, 1.0)

    char_w = int(orig_w * scale)
    char_h = int(orig_w * scale * (orig_h / orig_w) * 0.5)
    if char_h % 2 != 0:
        char_h += 1

    char_w = min(char_w, term_cols)
    char_h = min(char_h, avail_rows)
    if char_h % 2 != 0:
        char_h -= 1

    return max(40, char_w), max(2, char_h)


def enhance_small_image(pil_img, orig_w, orig_h, target_w):
    """小图快速放大 (只做LANCZOS, 跳过慢速滤镜)"""
    scale = target_w / orig_w
    if scale <= 1.0:
        return pil_img
    new_h = max(2, int(pil_img.size[1] * scale))
    if new_h % 2 != 0:
        new_h += 1
    return pil_img.resize((target_w, new_h), Image.LANCZOS)


def resize_pil(img, char_w, char_h):
    """快速调整 (BILINEAR比LANCZOS快5x, 终端分辨率下质量无差别)"""
    return img.resize((char_w, char_h), Image.BILINEAR)


# ─── Kitty 图形协议 ──────────────────────────────────────────

def img_to_kitty(pil_img):
    import zlib
    term_w, term_h = get_terminal_size()
    max_px_w = term_w * 8
    max_px_h = (term_h - 2) * 16
    pil_img.thumbnail((max_px_w, max_px_h), Image.LANCZOS)

    png_data = io.BytesIO()
    pil_img.save(png_data, format="PNG")
    compressed = zlib.compress(png_data.getvalue())

    payload = base64.b64encode(compressed).decode("ascii")
    chunks = [payload[i:i+4096] for i in range(0, len(payload), 4096)]
    if len(chunks) == 1:
        return f"\033_Ga=T,f=100,t=d;{chunks[0]}\033\\"
    parts = []
    for i, chunk in enumerate(chunks):
        if i == 0:
            parts.append(f"\033_Ga=T,f=100,t=d,m=1;{chunk}")
        elif i == len(chunks) - 1:
            parts.append(f"\033_G m=0;{chunk}\033\\")
        else:
            parts.append(f"\033_G m=1;{chunk}")
    return "\n".join(parts) + "\033\\"


# ─── Sixel 协议 ──────────────────────────────────────────────

def img_to_sixel(pil_img):
    term_w, term_h = get_terminal_size()
    max_px_w = term_w * 8
    max_px_h = (term_h - 2) * 16
    pil_img.thumbnail((max_px_w, max_px_h), Image.LANCZOS)
    pil_img = pil_img.convert("P", palette=Image.ADAPTIVE, colors=256)
    w, h = pil_img.size
    pixels = list(pil_img.getdata())
    palette = pil_img.getpalette() or [0] * 768

    out = "\033Pq"
    for i in range(256):
        r, g, b = palette[i*3], palette[i*3+1], palette[i*3+2]
        out += f"#{i};2;{r*100//255};{g*100//255};{b*100//255}"
    for y in range(0, h, 6):
        for color_id in range(256):
            has_pixel = False
            band = []
            for dy in range(6):
                yy = y + dy
                row = []
                for x in range(w):
                    p = pixels[yy * w + x] if yy < h else 0
                    row.append(1 if p == color_id else 0)
                band.append(row)
                if any(row):
                    has_pixel = True
            if not has_pixel:
                continue
            out += f"#{color_id}"
            for dy in range(6):
                row = band[dy]
                i = 0
                while i < len(row):
                    val = row[i]
                    count = 1
                    while i + count < len(row) and row[i + count] == val:
                        count += 1
                    out += f"!{count}\x3f" if val else f"!{count}\x24"
                    i += count
            out += "$"
        if y + 6 < h:
            out += "-"
    out += "\033\\"
    return out


# ─── Unicode 半块 ────────────────────────────────────────────

def img_to_block(pil_img):
    rgb = pil_img.convert("RGB")
    w, h = rgb.size
    pixels = list(rgb.getdata())
    lines = []
    for y in range(0, h, 2):
        row = []
        for x in range(w):
            r1, g1, b1 = pixels[y * w + x]
            r2, g2, b2 = pixels[(y + 1) * w + x] if y + 1 < h else (0, 0, 0)
            row.append(f"\033[38;2;{r1};{g1};{b1}m\033[48;2;{r2};{g2};{b2}m\u2580")
        lines.append("".join(row))
    return "\n".join(lines)


HALF_CHAR = "\u2580".encode("utf-8")
RESET = b"\033[0m"
ESC = b"\033"


def img_to_block_centered(pil_img):
    """24位真彩色半块渲染 (numpy全向量化)"""
    term_w, _ = get_terminal_size()
    arr = np.array(pil_img.convert("RGB"))
    h, w, _ = arr.shape
    pad = max(0, (term_w - w) // 2)

    if h % 2 != 0:
        arr = arr[:h-1]
        h -= 1

    top = arr[0::2]
    bot = arr[1::2]
    rows = h // 2

    pad_bytes = (" " * pad).encode("utf-8") if pad else b""

    lines = []
    for y in range(rows):
        t = top[y]
        b = bot[y]
        # 批量: R;G;B 字符串
        fg_rgb = np.char.add(t[:,0].astype(str), ";" + np.char.add(t[:,1].astype(str), ";" + t[:,2].astype(str)))
        bg_rgb = np.char.add(b[:,0].astype(str), ";" + np.char.add(b[:,1].astype(str), ";" + b[:,2].astype(str)))

        fg_parts = "\033[38;2;" + fg_rgb + "m"
        bg_parts = "\033[48;2;" + bg_rgb + "m\u2580"
        pixels = fg_parts + bg_parts

        line = pad_bytes + "".join(pixels).encode("utf-8") + b"\033[0m\n"
        lines.append(line)

    return b"".join(lines).decode("utf-8", errors="replace")


RENDERERS = {
    "kitty": lambda img: img_to_kitty(img),
    "sixel": lambda img: img_to_sixel(img),
    "block": lambda img: img_to_block_centered(img),
}


ASCII_CHARS = "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. "

def build_ascii_render(pil_img):
    term_w, _ = get_terminal_size()
    gray = pil_img.convert("L")
    w, h = gray.size
    pixels = list(gray.getdata())
    n = len(ASCII_CHARS)
    pad = max(0, (term_w - w) // 2)
    pad_str = " " * pad
    lines = []
    for y in range(h):
        lines.append(pad_str + "".join(ASCII_CHARS[min(pixels[y * w + x] * n // 256, n - 1)] for x in range(w)))
    return "\n".join(lines)



def play_video(video_path, mode, width):
    try:
        frames, durations, ftype, fps, orig_size = read_frames(video_path)
    except Exception as e:
        console.print(f"[red]无法读取: {e}[/red]")
        sys.exit(1)

    orig_w, orig_h = orig_size
    term_w, term_h = get_terminal_size()

    if width:
        char_w = min(width, term_w)
        char_h = max(2, int(char_w * (orig_h / orig_w) * 0.5))
        if char_h % 2 != 0:
            char_h += 1
    else:
        char_w, char_h = calc_optimal_size(orig_w, orig_h, term_w, term_h)

    proto = detect_terminal()
    if mode == "ascii":
        proto = "block"

    total = sum(durations)
    proto_name = {"kitty": "Kitty真图像", "sixel": "Sixel", "block": "Unicode半块"}
    scale_info = f"{orig_w}x{orig_h} -> {char_w}x{char_h}"

    console.print(f"[cyan]{ftype}:[/cyan] {len(frames)}帧 | {total:.1f}s | FPS:{fps:.1f} | {scale_info} | {proto_name.get(proto, proto)}")
    console.print("[dim]Ctrl+C 停止[/dim]")

    render = build_ascii_render if mode == "ascii" else RENDERERS[proto]
    need_enhance = orig_w < 200 and orig_h < 200

    # 预处理所有帧
    processed_frames = []
    for frame in frames:
        if need_enhance:
            p = enhance_small_image(frame, orig_w, orig_h, char_w)
            p = resize_pil(p, char_w, char_h)
        else:
            p = resize_pil(frame, char_w, char_h)
        processed_frames.append(p)

    # 清屏
    fast_write(b"\033[2J")

    # 第一行: 显示信息
    info = f"{ftype} | {len(frames)}帧 | {fps:.1f}fps | {orig_w}x{orig_h}->{char_w}x{char_h} | Ctrl+C停止"
    fast_write(f"\033[1;1H\033[K{info}\n".encode())

    # 视频区域从第2行开始, 垂直居中
    display_lines = (char_h + 1) // 2
    max_video_rows = term_h - 2  # 留首行+末行
    start_row = max(2, 2 + (max_video_rows - display_lines) // 2)

    # 清除视频区域
    clear_cmd = b""
    for i in range(display_lines):
        clear_cmd += f"\033[{start_row + i};1H\033[2K".encode()
    fast_write(clear_cmd)

    frame_count = 0
    cursor_pos = f"\033[{start_row};1H".encode()
    try:
        while True:
            for processed, dur in zip(processed_frames, durations):
                t0 = time.time()
                out = render(processed)
                if isinstance(out, str):
                    out = out.encode("utf-8", errors="replace")
                fast_write(cursor_pos + out)
                frame_count += 1
                elapsed = time.time() - t0
                sleep_time = dur - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
    except KeyboardInterrupt:
        pass
    finally:
        # 只清除视频区域, 保留info行
        clear_cmd = b""
        for i in range(display_lines):
            clear_cmd += f"\033[{start_row + i};1H\033[2K".encode()
        fast_write(clear_cmd)
        fast_write(f"\033[{start_row};1H\033[?25h\033[0m".encode())
        console.print(f"[green]共播放 {frame_count} 帧[/green]")


def main():
    parser = argparse.ArgumentParser(description="终端彩色视频/GIF播放器")
    parser.add_argument("file", help="视频或GIF文件路径")
    parser.add_argument("--mode", choices=["color", "ascii"], default="color",
                        help="color(默认) / ascii(灰度)")
    parser.add_argument("--width", type=int, default=0,
                        help="输出宽度 (默认0=自动适配终端)")

    args = parser.parse_args()
    if not os.path.exists(args.file):
        console.print(f"[red]文件不存在: {args.file}[/red]")
        sys.exit(1)

    play_video(args.file, args.mode, args.width if args.width > 0 else None)


if __name__ == "__main__":
    main()
