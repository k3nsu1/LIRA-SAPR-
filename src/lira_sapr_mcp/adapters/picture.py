"""A picture of a model: LIRA's view window captured through Win32, saved as PNG.

LIRA's API has no picture export, and copying the view to the clipboard would overwrite
the user's clipboard, so the server asks the view window to draw itself into a bitmap
(PrintWindow) and encodes the PNG itself with zlib. Capturing runs on the COM thread;
cropping and encoding (``picture_png``) run off it, so LIRA is not kept busy.
"""

import ctypes
import struct
import zlib

# PrintWindow flags: the client area only, and content drawn by the GPU as well.
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 1, 2
DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
WHITE_BYTE = b"\xff"
BI_RGB, DIB_RGB_COLORS = 0, 0


def capture_window(hwnd: int) -> tuple[int, int, bytes]:
    """Width, height and the BGRA pixels (top row first) of a window's client area.

    The thread is made DPI-aware for the capture: otherwise, with display scaling, Windows
    reports a scaled-down size and the bitmap holds only the top-left part of the view.
    """
    user32 = ctypes.windll.user32
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p  # a handle, not an int
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    previous = user32.SetThreadDpiAwarenessContext(DPI_PER_MONITOR_AWARE_V2)
    try:
        return _capture(hwnd)
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)


def _capture(hwnd: int) -> tuple[int, int, bytes]:
    import win32gui
    import win32ui

    _, _, width, height = win32gui.GetClientRect(hwnd)
    if width <= 0 or height <= 0:
        raise ValueError("the view window has no visible area (is LIRA minimized?)")
    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        previous = memory.SelectObject(bitmap)
        flags = PW_CLIENTONLY | PW_RENDERFULLCONTENT
        drawn = ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), flags)
        memory.SelectObject(previous)  # GetDIBits needs the bitmap out of the DC
        if not drawn:
            raise ValueError("Windows could not draw LIRA's view window")
        return width, height, _pixels(memory.GetSafeHdc(), bitmap.GetHandle(), width, height)
    finally:
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)
        win32gui.DeleteObject(bitmap.GetHandle())


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _pixels(hdc: int, hbitmap: int, width: int, height: int) -> bytes:
    """The bitmap as 32-bit BGRA rows, top row first, whatever the screen's colour depth
    (a 16-bit remote desktop too): GetDIBits converts it."""
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]  # fmt: skip
    header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, BI_RGB)
    # BITMAPINFO: the header, then room for a colour table GetDIBits may fill.
    info = ctypes.create_string_buffer(bytes(header), ctypes.sizeof(header) + 256 * 4)
    pixels = ctypes.create_string_buffer(width * height * 4)
    if gdi32.GetDIBits(hdc, hbitmap, 0, height, pixels, info, DIB_RGB_COLORS) != height:
        raise ValueError("Windows could not read the picture of LIRA's view window")
    return pixels.raw


def picture_png(width: int, height: int, bgra: bytes, margin: int = 12) -> bytes:
    """The captured window as a PNG, its white border cut to ``margin`` pixels."""
    return encode_png(crop(rgb_rows(width, height, bgra), margin))


def rgb_rows(width: int, height: int, bgra: bytes) -> list[bytes]:
    """BGRA pixels (top row first) as rows of RGB bytes."""
    stride = width * 4
    rows = []
    for y in range(height):
        row = bgra[y * stride : (y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        rows.append(bytes(rgb))
    return rows


def crop(rows: list[bytes], margin: int = 12) -> list[bytes]:
    """Cut the white border around the drawing, keeping ``margin`` pixels. Whole rows are
    compared at C speed (strip), not pixel by pixel."""
    inked = [y for y, row in enumerate(rows) if row.strip(WHITE_BYTE)]
    if not inked:
        return rows
    width = len(rows[0]) // 3
    top, bottom = max(inked[0] - margin, 0), min(inked[-1] + margin + 1, len(rows))
    drawn = rows[inked[0] : inked[-1] + 1]
    first = min(len(row) - len(row.lstrip(WHITE_BYTE)) for row in drawn) // 3
    last = (max(len(row.rstrip(WHITE_BYTE)) for row in drawn) - 1) // 3
    left, right = max(first - margin, 0), min(last + margin + 1, width)
    return [row[left * 3 : right * 3] for row in rows[top:bottom]]


def encode_png(rows: list[bytes]) -> bytes:
    """An 8-bit RGB PNG from rows of RGB bytes."""
    width, height = len(rows[0]) // 3, len(rows)
    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 for each row

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
