"""template/reel.html を Chrome で録画し、シーンごとのナレーションを合成して MP4 (1080x1920, H.264/AAC) にする。

使い方:
  uv run --with playwright --with imageio-ffmpeg python reel/render_reel.py \
      --data reel_data.json --voice-dir voice/ --out-dir videos/ [ポイント名 ...]
録画は実時間。各ポイント約25秒。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template" / "reel.html"
PROJECTION = HERE / "assets" / "maps" / "projection.json"
VOICE_DELAY = 0.25  # 各シーン開始からナレーションまでの間（テンプレートの say() と同じ）
FF = imageio_ffmpeg.get_ffmpeg_exe()


def reel_payload(data: dict, voice_dir: Path) -> dict:
    narr = {}
    for p in data["points"]:
        f = voice_dir / f"{p['name']}_scenes.json"
        if not f.exists():  # ナレーション未生成のポイントは録画対象外
            continue
        meta = json.loads(f.read_text(encoding="utf-8"))
        narr[p["name"]] = [{"src": m["file"], "seconds": m["seconds"], "text": m["text"]} for m in meta]
    return {"points": data["points"], "narr": narr, "proj": json.loads(PROJECTION.read_text(encoding="utf-8")),
            "dayLabel": data["dayLabel"], "period": data["period"]}


def render(browser, payload: dict, point: str, out: Path) -> float:
    tmp = Path(tempfile.mkdtemp())
    ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=1,
                              record_video_dir=str(tmp), record_video_size={"width": 1080, "height": 1920})
    ctx.add_init_script(f"window.__REEL__ = {json.dumps(payload, ensure_ascii=False)};")
    t_ctx = time.monotonic()
    page = ctx.new_page()
    page.goto(f"{TEMPLATE.as_uri()}?render=1&point={urllib.parse.quote(point)}")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(1500)  # 地図・背景画像の読み込み待ち
    t_start = time.monotonic() - t_ctx
    page.evaluate("window.startRender()")
    info = page.evaluate("window.__render")
    page.wait_for_timeout(info["end"] + 300)
    ctx.close()
    webm = next(tmp.glob("*.webm"))
    total = info["end"] / 1000
    inputs, filters = [], []
    for i, clip in enumerate(payload["narr"][point]):
        inputs += ["-i", clip["src"]]
        ms = int((info["starts"][i] / 1000 + VOICE_DELAY) * 1000)
        filters.append(f"[{i + 1}:a]adelay={ms}|{ms},aresample=48000[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(inputs) // 2))
    filters.append(f"{mix}amix=inputs={len(inputs) // 2}:normalize=0,apad[aout]")
    subprocess.run([FF, "-y", "-ss", f"{t_start:.3f}", "-i", str(webm), *inputs,
                    "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[aout]",
                    "-t", f"{total:.3f}", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
                    "-crf", "20", "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True, capture_output=True)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--voice-dir", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    data = json.loads(a.data.read_text(encoding="utf-8"))
    payload = reel_payload(data, a.voice_dir)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for pt in data["points"]:
            if (a.names and pt["name"] not in a.names) or pt["name"] not in payload["narr"]:
                continue
            out = a.out_dir / f"{pt['name']}.mp4"
            sec = render(browser, payload, pt["name"], out)
            print(out, f"{sec:.1f}s", flush=True)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
