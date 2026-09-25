"""Generate the original antilagphotoshop lightning icon; standard library only."""
from pathlib import Path
import math
import struct
import zlib

BOLT = [(0.57, 0.17), (0.29, 0.55), (0.46, 0.55), (0.40, 0.84),
        (0.73, 0.43), (0.55, 0.43), (0.64, 0.17)]


def in_bolt(x, y):
    inside = False
    for (ax, ay), (bx, by) in zip(BOLT, BOLT[1:] + BOLT[:1]):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def color(x, y):
    # Rounded-square silhouette, cool gradient and a crisp ivory bolt.
    dx = max(abs(x - 0.5) - 0.27, 0)
    dy = max(abs(y - 0.5) - 0.27, 0)
    if math.hypot(dx, dy) > 0.19:
        return (0, 0, 0, 0)
    if in_bolt(x, y):
        return (247, 255, 249, 255)
    blend = min(1, max(0, 0.18 + 0.70 * y + 0.22 * x))
    glow = max(0, 1 - math.hypot(x - 0.20, y - 0.15) * 1.5)
    return (int(25 + 99 * blend), int(167 - 99 * blend + 35 * glow), int(232 + 16 * blend), 255)


def chunk(name, payload):
    return struct.pack('>I', len(payload)) + name + payload + struct.pack('>I', zlib.crc32(name + payload) & 0xffffffff)


def png(size):
    rows = bytearray()
    samples = 3
    for row in range(size):
        rows.append(0)
        for column in range(size):
            colors = [color((column + (sx + 0.5) / samples) / size,
                            (row + (sy + 0.5) / samples) / size)
                      for sy in range(samples) for sx in range(samples)]
            alpha_sum = sum(c[3] for c in colors)
            rgb = [round(sum(c[channel] * c[3] for c in colors) / alpha_sum) if alpha_sum else 0 for channel in range(3)]
            rows.extend((*rgb, round(alpha_sum / len(colors))))
    header = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b'')


def build():
    directory = Path(__file__).resolve().parent
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [png(size) for size in sizes]
    offset = 6 + 16 * len(sizes)
    entries = bytearray()
    for size, image in zip(sizes, images):
        entries.extend(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    (directory / 'antilagphotoshop.ico').write_bytes(struct.pack('<HHH', 0, 1, len(sizes)) + entries + b''.join(images))
    (directory / 'antilagphotoshop.png').write_bytes(images[-1])
    print('Icon generated: 7 sizes (16-256 px), plus PNG preview.')


if __name__ == '__main__':
    build()
