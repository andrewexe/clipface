"""Draws the placeholder app icon. Run: uv run --with pillow scripts/make_icon.py out.png
Then: cd app && pnpm tauri icon out.png
"""

import sys

from PIL import Image, ImageDraw

SIZE = 1024
image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((64, 64, SIZE - 64, SIZE - 64), radius=210, fill=(124, 108, 246, 255))
# A 9:16 frame with a play mark: vertical clips.
draw.rounded_rectangle((372, 232, 652, 792), radius=48, outline=(255, 255, 255, 255), width=44)
draw.polygon([(468, 422), (468, 602), (592, 512)], fill=(255, 255, 255, 255))
image.save(sys.argv[1])
