"""Model pictures: PNG encoding, white border cropping and the tool."""

import struct
import zlib

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.adapters.picture import crop, encode_png, picture_png, rgb_rows
from lira_sapr_mcp.server import build_server

W, K = b"\xff\xff\xff\xff", b"\x00\x00\x00\xff"  # white and black BGRA pixels


def decode(png: bytes) -> tuple[int, int, bytes]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    pos, idat = 8, b""
    while pos < len(png):
        (size,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + size]
        crc = struct.unpack(">I", png[pos + 8 + size : pos + 12 + size])[0]
        assert zlib.crc32(kind + data) == crc
        idat += data if kind == b"IDAT" else b""
        pos += 12 + size
    return width, height, zlib.decompress(idat)


def test_png_holds_the_pixels_in_rgb():
    blue_red = b"\xff\x00\x00\xff" + b"\x00\x00\xff\xff"  # BGRA blue, red
    width, height, raw = decode(encode_png(rgb_rows(2, 1, blue_red)))
    assert (width, height) == (2, 1)
    assert raw == b"\x00" + b"\x00\x00\xff" + b"\xff\x00\x00"


def test_crop_keeps_the_drawing_and_a_margin():
    # 6 x 5 white picture with one black pixel at (3, 2).
    pixels = bytearray(W * 30)
    pixels[(2 * 6 + 3) * 4 : (2 * 6 + 3) * 4 + 4] = K
    rows = crop(rgb_rows(6, 5, bytes(pixels)), margin=1)
    assert len(rows) == 3 and {len(row) for row in rows} == {9}
    assert rows[1][3:6] == b"\x00\x00\x00"  # the centre of the 3 x 3 result
    assert rows[0] == rows[2] == b"\xff" * 9


def test_crop_finds_the_drawing_edges_on_any_row():
    # Ink at the left on row 1 and at the right on row 3 of a 10 x 5 picture.
    pixels = bytearray(W * 50)
    for x, y in ((2, 1), (7, 3)):
        pixels[(y * 10 + x) * 4 : (y * 10 + x) * 4 + 4] = K
    rows = crop(rgb_rows(10, 5, bytes(pixels)), margin=0)
    assert len(rows) == 3 and len(rows[0]) == 6 * 3
    assert rows[0][:3] == rows[2][-3:] == b"\x00\x00\x00"


def test_crop_leaves_a_blank_picture_as_it_is():
    rows = rgb_rows(2, 2, W * 4)
    assert crop(rows) == rows


def test_a_4k_picture_is_made_quickly():
    import time

    width, height = 3840, 2160
    pixels = bytearray(W * (width * height))
    for y in range(500, 1500):
        pixels[(y * width + 1000) * 4 : (y * width + 2800) * 4] = K * 1800
    started = time.perf_counter()
    png = picture_png(width, height, bytes(pixels))
    assert time.perf_counter() - started < 3
    assert struct.unpack(">II", png[16:24]) == (1800 + 24, 1000 + 24)


@pytest.mark.anyio
async def test_picture_tool_returns_an_image_and_saves_it(tmp_path):
    session, _ = fake_session(tmp_path)
    model = tmp_path / "frame.lir"
    model.write_bytes(b"lir")
    target = tmp_path / "pics" / "frame.png"
    async with Client(build_server(session)) as client:
        await client.call_tool("lira_open_model", {"path": str(model)})
        res = await client.call_tool("lira_model_picture", {"save_path": str(target)})
        bad = await client.call_tool("lira_model_picture", {"save_path": "frame.png"})
    assert not res.is_error, res.content
    image = res.content[0]
    assert image.type == "image" and image.mime_type == "image/png"
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "saved to" in res.content[1].text
    assert bad.is_error and "absolute .png" in bad.content[0].text
