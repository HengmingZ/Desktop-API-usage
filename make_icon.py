"""Generate the application icon (icon.ico).

Design: dark rounded square, white outer ring, inner ring split into
three arcs using the service accent colors (Kimi blue / ChatGPT green /
Antigravity red) — mirroring the app's dual-ring widget.

Run: uv run python make_icon.py
"""

from PIL import Image, ImageDraw

SIZE = 512  # render large, let the ico container downscale
BG = (27, 29, 35, 255)  # #1b1d23
WHITE = (255, 255, 255, 255)
COLORS = [
    (77, 107, 254, 255),  # Kimi   #4d6bfe
    (16, 163, 127, 255),  # ChatGPT #10a37f
    (234, 67, 53, 255),   # Antigravity #ea4335
]


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = SIZE // 32
    d.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=SIZE // 5,
        fill=BG,
    )

    # outer ring (white)
    outer_w = SIZE // 22
    outer_box = [SIZE * 0.16, SIZE * 0.16, SIZE * 0.84, SIZE * 0.84]
    d.arc(outer_box, start=0, end=360, fill=WHITE, width=outer_w)

    # inner ring: three colored arcs with small gaps
    inner_w = SIZE // 26
    inner_box = [SIZE * 0.30, SIZE * 0.30, SIZE * 0.70, SIZE * 0.70]
    gap = 14
    for i, color in enumerate(COLORS):
        start = -90 + i * 120 + gap / 2
        d.arc(inner_box, start=start, end=start + 120 - gap, fill=color, width=inner_w)

    img.save(
        "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    img.resize((256, 256)).save("icon.png")
    print("icon.ico + icon.png written")


if __name__ == "__main__":
    main()
