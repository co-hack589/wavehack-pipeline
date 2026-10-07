"""template/reel.html を Chrome で1コマずつ撮影し、シーンごとのナレーションを合成して MP4 (1080x1920, H.264/AAC) にする。

使い方:
  uv run --with playwright --with imageio-ffmpeg python reel/render_reel.py \
      --data reel_data.json --voice-dir voice/ --out-dir videos/ [ポイント名 ...]
実時間の画面録画（record_video）は CPU が混むとコマが届かず数秒単位で静止し、
ヘッドレス Chrome のウィンドウが 1920px に満たないと下端が灰色で埋まる。
そこでページの時計（setTimeout / rAF / performance.now / Web Animations）を差し替え、
30fps で1コマずつ進めてスクリーンショットを撮る。負荷に関係なく毎回同じ動画になる。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template" / "reel.html"
PROJECTION = HERE / "assets" / "maps" / "projection.json"
VOICE_DELAY = 0.25  # 各シーン開始からナレーションまでの間（テンプレートの say() と同じ）
FF = imageio_ffmpeg.get_ffmpeg_exe()
FPS = 30
# ページの時計を手動で進めるための差し替え。テンプレートの later()/countUp()/波の canvas/全アニメーションが対象。
# アニメーションは生成時に止め、__seek(ms) のたびに「生成からの経過時間」を currentTime に入れる
CLOCK_JS = """
(() => {
  let now = 0, seq = 0, rafs = [];
  const timers = [], anims = [], animate = Element.prototype.animate;
  performance.now = () => now;
  window.setTimeout = (fn, d = 0, ...args) => { timers.push({id: ++seq, at: now + Math.max(0, +d || 0), fn: () => fn(...args)}); return seq; };
  window.clearTimeout = id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
  window.requestAnimationFrame = fn => { rafs.push(fn); return rafs.length; };
  Element.prototype.animate = function (kf, opt) {
    const a = animate.call(this, kf, opt);
    a.pause(); a.currentTime = 0; anims.push({a, born: now});
    return a;
  };
  window.__seek = to => {
    for (;;) {
      timers.sort((x, y) => x.at - y.at || x.id - y.id);
      if (!timers.length || timers[0].at > to) break;
      const t = timers.shift(); now = t.at; t.fn();
    }
    now = to;
    const cbs = rafs; rafs = []; cbs.forEach(f => f(now));
    anims.forEach(({a, born}) => { a.currentTime = now - born; });
  };
})();
"""


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
    ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
    ctx.add_init_script(f"window.__REEL__ = {json.dumps(payload, ensure_ascii=False)};")
    ctx.add_init_script(CLOCK_JS)
    page = ctx.new_page()
    page.goto(f"{TEMPLATE.as_uri()}?render=1&point={urllib.parse.quote(point)}")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(1500)  # 地図・背景画像の読み込み待ち（ページの時計は止まったまま）
    page.evaluate("window.startRender()")
    info = page.evaluate("window.__render")
    frames = Path(tempfile.mkdtemp())
    for i in range(int(info["end"] / 1000 * FPS) + 1):
        page.evaluate("t => window.__seek(t)", i * 1000 / FPS)
        page.screenshot(path=str(frames / f"{i:05d}.jpg"), type="jpeg", quality=92)
    ctx.close()
    total = info["end"] / 1000
    inputs, filters = [], []
    for i, clip in enumerate(payload["narr"][point]):
        inputs += ["-i", clip["src"]]
        ms = int((info["starts"][i] / 1000 + VOICE_DELAY) * 1000)
        filters.append(f"[{i + 1}:a]adelay={ms}|{ms},aresample=48000[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(inputs) // 2))
    filters.append(f"{mix}amix=inputs={len(inputs) // 2}:normalize=0,apad[aout]")
    filters.append("[0:v]scale=out_range=tv,format=yuv420p[vout]")  # JPEG のフルレンジを通常の動画レンジへ
    subprocess.run([FF, "-y", "-framerate", str(FPS), "-i", str(frames / "%05d.jpg"), *inputs,
                    "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                    "-t", f"{total:.3f}", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
                    "-crf", "20", "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True, capture_output=True)
    shutil.rmtree(frames, ignore_errors=True)
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
