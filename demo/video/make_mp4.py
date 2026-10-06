"""Turns a recorded take into the mp4 for upload.

    python demo/video/make_mp4.py                  # out/kioku-demo.webm   -> out/kioku-demo.mp4   (record.mjs)
    python demo/video/make_mp4.py kioku-paypal     # out/kioku-paypal.webm -> out/kioku-paypal.mp4 (record-paypal.mjs)

The take starts a moment before the opening card is up and ends with the browser closing, so the mp4 is cut
to start on the opening card and end on the closing card, with a short fade at each end. It needs ffmpeg with
libx264: the copy that comes with the imageio-ffmpeg package if it is installed, otherwise ffmpeg on the PATH.
The length printed last is read back from the finished mp4 (both hackathons ask for under three minutes).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / 'out'
FPS = 25
CARD = (18, 55, 46)  # #12372e, the background of the title cards


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which('ffmpeg') or sys.exit('ffmpeg not found')


def card_frames(ff: str, src: Path) -> list[bool]:
    """For each frame, whether a title card fills the screen (a small patch left of centre: a light panel in the app)."""
    raw = subprocess.run([ff, '-v', 'error', '-i', str(src), '-vf', f'fps={FPS},crop=8:8:96:446,scale=1:1',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
    return [max(abs(a - b) for a, b in zip(raw[i:i + 3], CARD)) <= 8 for i in range(0, len(raw) - 2, 3)]


def duration(ff: str, path: Path) -> float:
    """The length of a finished video, as ffmpeg reads it from the file."""
    info = subprocess.run([ff, '-hide_banner', '-i', str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r'Duration: (\d+):(\d+):([\d.]+)', info).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def main(name: str = 'kioku-demo') -> None:
    src, dst = OUT / f'{name}.webm', OUT / f'{name}.mp4'
    ff = ffmpeg()
    cards = card_frames(ff, src)
    if True not in cards:
        sys.exit('no title card found in the take')
    first = cards.index(True)
    last = len(cards) - 1 - cards[::-1].index(True)
    start, length = first / FPS, (last + 1 - first) / FPS
    subprocess.run([ff, '-v', 'error', '-y', '-ss', f'{start:.3f}', '-i', str(src), '-t', f'{length:.3f}',
                    '-vf', f'fps={FPS},fade=t=in:st=0:d=0.4,fade=t=out:st={length - 0.8:.3f}:d=0.8',
                    '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p',
                    '-movflags', '+faststart', '-an', str(dst)], check=True)
    print(f'{dst}  {length:.1f} s (cut {start:.2f} s from the start, {(len(cards) - last - 1) / FPS:.2f} s from the end)')
    final = duration(ff, dst)
    print(f'length of the mp4: {int(final // 60)}:{final % 60:05.2f} ({final:.2f} s)'
          + ('  — over three minutes!' if final >= 180 else ''))


if __name__ == '__main__':
    main(*sys.argv[1:2])
