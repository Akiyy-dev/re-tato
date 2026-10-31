"""Human-operated captcha dialogs. No OCR, automatic solving or challenge bypass."""

import base64
import binascii
import io
import math
from urllib.parse import urlsplit
from urllib.request import urlopen

MAX_IMAGE_BYTES = 4_000_000


def slider_answer(display_x, display_width):
    if not all(math.isfinite(v) for v in (display_x, display_width)) or display_width <= 0:
        raise ValueError('invalid slider dimensions')
    if not 0 <= display_x <= display_width:
        raise ValueError('slider outside challenge image')
    return {'x': float(display_x / display_width * 960.0)}


def slider_y(normalized_y, canvas_height):
    if not all(math.isfinite(v) for v in (normalized_y, canvas_height)) or canvas_height <= 0:
        raise ValueError('invalid slider y coordinates')
    return normalized_y / 540.0 * canvas_height


def decode_image_data(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES:
        raise ValueError('invalid or oversized image data')
    if value.startswith('data:image/png;base64,'):
        value = value.split(',', 1)[1]
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('challenge image must contain embedded base64 PNG data') from None


def _image(value, *, remote=False):
    from PIL import Image
    if remote:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('unsupported challenge image URL')
        with urlopen(value, timeout=8) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError('challenge image too large')
    else:
        data = decode_image_data(value)
    image = Image.open(io.BytesIO(data))
    if image.width * image.height > 2_000_000:
        raise ValueError('challenge image dimensions too large')
    image.load()
    return image.convert('RGBA')


def solve_challenge(challenge):
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageTk

    kind, parameters = challenge['type'], challenge['parameters']
    if kind not in (1, 2):
        raise NotImplementedError('This login verifier does not yet implement the official web captcha bridge')
    if not isinstance(parameters, dict):
        raise ValueError('unsupported captcha parameter structure')
    if kind == 2:
        background = _image(parameters['data'])
        foreground = None
    else:
        background = _image(parameters['bg'], remote=True)
        foreground = _image(parameters['fg'], remote=True)
    original_width, original_height = background.size
    scale = min(1.0, 640 / background.width, 380 / background.height)
    width, height = max(1, round(background.width * scale)), max(1, round(background.height * scale))
    if kind == 1:
        height = max(1, round(width * 9 / 16))
    background = background.resize((width, height), Image.Resampling.LANCZOS)
    root = tk.Tk()
    root.title('Tatotalk — 人工验证码')
    root.resizable(False, False)
    root.attributes('-topmost', True)
    frame = ttk.Frame(root, padding=14)
    frame.pack()
    ttk.Label(frame, text='请手动完成服务端验证码', font=('Microsoft YaHei UI', 12)).pack(pady=(0, 10))
    canvas = tk.Canvas(frame, width=width, height=height, highlightthickness=0)
    canvas.pack()
    background_photo = ImageTk.PhotoImage(background, master=root)
    canvas.create_image(0, 0, image=background_photo, anchor='nw')
    answer = [None]
    foreground_photo = None
    if kind == 2:
        ttk.Label(frame, text='输入图片中的文字：').pack(pady=(12, 4))
        entry = ttk.Entry(frame, width=28, font=('Segoe UI', 13))
        entry.pack()
        entry.focus_set()

        def submit():
            text = entry.get().strip()
            if text:
                answer[0] = text
                root.destroy()
    else:
        foreground = foreground.resize((max(1, round(foreground.width / original_width * width)), max(1, round(foreground.height / original_height * height))), Image.Resampling.LANCZOS)
        foreground_photo = ImageTk.PhotoImage(foreground, master=root)
        # Protocol coordinates are normalized to a 960-wide image.
        y = slider_y(float(parameters['y']), height)
        piece = canvas.create_image(0, y, image=foreground_photo, anchor='nw')
        position = tk.DoubleVar(root, 0.0)

        def move(value):
            canvas.coords(piece, float(value), y)

        ttk.Label(frame, text='拖动下方滑块，将拼图移到缺口位置：').pack(pady=(12, 4))
        tk.Scale(frame, from_=0, to=max(0, width - foreground.width), orient='horizontal',
                 variable=position, command=move, length=width, resolution=0.1).pack()

        def submit():
            answer[0] = slider_answer(position.get(), width)
            root.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(pady=(14, 0))
    ttk.Button(buttons, text='提交人工答案', command=submit).pack(side='left', padx=6)
    ttk.Button(buttons, text='取消', command=root.destroy).pack(side='left', padx=6)
    root.bind('<Return>', lambda _: submit())
    root.protocol('WM_DELETE_WINDOW', root.destroy)
    root.mainloop()
    # Keep image references alive for the duration of this dialog only.
    del foreground_photo, background_photo
    return answer[0]


def main():
    """Keep Tk on this child process's main thread; communicate via stdio only."""
    import json
    import sys
    try:
        raw = sys.stdin.buffer.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('oversized captcha challenge')
        challenge = json.loads(raw.decode('utf-8'))
        answer = solve_challenge(challenge)
        print(json.dumps(answer))
        return 0
    except NotImplementedError:
        return 4
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
