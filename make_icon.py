"""Generate phonedeck.ico — a phone + external-display motif."""
import os
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# diagonal blue->cyan gradient
grad = Image.new("RGBA", (S, S))
gp = grad.load()
c1, c2 = (37, 99, 235), (6, 182, 212)   # #2563eb -> #06b6d4
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        gp[x, y] = (int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t), 255)

# rounded-square mask for the background
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([10, 10, S - 10, S - 10], radius=52, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
W = (255, 255, 255, 255)

# external display (landscape window), upper-right
d.rounded_rectangle([96, 74, 196, 150], radius=12, outline=W, width=11)
d.line([120, 168, 172, 168], fill=W, width=11)          # display stand base
d.line([146, 150, 146, 168], fill=W, width=11)          # stand neck

# phone, lower-left, overlapping — with a "cast" play triangle on its screen
d.rounded_rectangle([64, 118, 128, 214], radius=16, fill=(15, 18, 24, 255),
                    outline=W, width=10)
d.polygon([(88, 150), (88, 186), (114, 168)], fill=W)   # play/cast glyph

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phonedeck.ico")
img.save(out, format="ICO", sizes=sizes)
print("wrote", out)
