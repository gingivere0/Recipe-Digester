"""Converts a raw Nook framebuffer page (1024x600 RGB565, landscape) into a
portrait PNG as the user sees it. Usage: fb2png.py in.raw out.png [page]"""
import struct
import sys
import zlib

W, H = 1024, 600
data = open(sys.argv[1], "rb").read()
page = int(sys.argv[3]) if len(sys.argv) > 3 else 0
data = data[page * W * H * 2:(page + 1) * W * H * 2]

# portrait (x, y) lives at panel row 599 - x, column y
rows = []
for y in range(W):
    r = bytearray([0])
    for x in range(H):
        o = ((H - 1 - x) * W + y) * 2
        v = data[o] | data[o + 1] << 8
        r += bytes(((v >> 11) << 3, ((v >> 5) & 63) << 2, (v & 31) << 3))
    rows.append(bytes(r))


def chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", H, W, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
open(sys.argv[2], "wb").write(png)
