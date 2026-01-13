"""
Create a multi-resolution .ico file from a source image.
Place your high-resolution source image (PNG, 256x256 or larger) as 'icon_source.png'
then run this script.
"""
from PIL import Image

# Source image - use a high-res PNG (256x256 or larger recommended)
source_image = "icon_source.png"  # Change this to your source image

# Icon sizes needed for Windows
sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

# Open source image
img = Image.open(source_image)

# Ensure RGBA mode for transparency support
if img.mode != 'RGBA':
    img = img.convert('RGBA')

# Create resized versions
icons = []
for size in sizes:
    resized = img.resize(size, Image.Resampling.LANCZOS)
    icons.append(resized)

# Save as multi-resolution ICO
icons[0].save(
    "FV_icon.ico",
    format='ICO',
    sizes=sizes,
    append_images=icons[1:]
)

print("Created FV_icon.ico with resolutions:", [f"{s[0]}x{s[1]}" for s in sizes])
