import qrcode
import io
import math
from PIL import Image, ImageDraw, ImageFont
from django.db.models import QuerySet


def generate_asset_qr_grid(queryset: QuerySet, columns: int = 5):
    """
    Generates a single PNG image containing QR codes for all assets in the queryset.
    Tiles them in a grid with 'columns' columns.
    """
    # Configuration
    qr_size = 300
    padding = 40
    text_height = 50
    cell_width = qr_size + (padding * 2)
    cell_height = qr_size + text_height + (padding * 2)

    assets = list(queryset)
    count = len(assets)
    if count == 0:
        return None

    rows = math.ceil(count / columns)
    
    canvas_width = columns * cell_width
    canvas_height = rows * cell_height

    # Create a white canvas
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)

    # Try to load a font, fallback to default
    try:
        # Common Mac font path
        font = ImageFont.truetype("/System/Library/Fonts/Geneva.ttf", 24)
    except Exception:
        try:
            # Another common font path (Linux)
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except Exception:
            font = ImageFont.load_default()

    for index, asset in enumerate(assets):
        row = index // columns
        col = index % columns

        # 1. Generate QR Code
        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(f"cubosapp://container/{asset.id}")
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_img = qr_img.resize((qr_size, qr_size))

        # 2. Calculate position
        x = col * cell_width + padding
        y = row * cell_height + padding

        # 3. Paste QR onto canvas
        canvas.paste(qr_img, (x, y))

        # 4. Draw Label (Asset Name)
        label_text = f"{asset.name}"
        # Measure text to center it
        try:
            bbox = draw.textbbox((0, 0), label_text, font=font)
            text_width = bbox[2] - bbox[0]
        except AttributeError:
            # Fallback for older Pillow versions
            text_width, _ = draw.textsize(label_text, font=font)

        text_x = x + (qr_size - text_width) // 2
        text_y = y + qr_size + 10
        draw.text((text_x, text_y), label_text, fill="black", font=font)

    # Save to buffer
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
