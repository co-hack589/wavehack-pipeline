"""教育シリーズ「波予報の読み方」のリールを ep*.json から作る（ナレーション → コマ撮り → MP4）。

  uv run --with playwright --with imageio-ffmpeg python edu/render_edu.py edu/episodes/ep01.json [--silent]
出力: edu/out/ep01.mp4（1080x1920, H.264/AAC）, edu/out/ep01_voice/。--silent は TTS を使わない見た目確認用
声は週次リールと同じ Gemini Aoede。失敗したら全シーン Cloud TTS Autonoe（GOOGLE_ACCESS_TOKEN があれば）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reel"))
from narration import cloud_available, speak, speak_cloud  # noqa: E402
from render_reel import CLOCK_JS, FF, FPS, VOICE_DELAY  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402


def silent(ep: dict, out: Path) -> list[dict]:
    """見た目確認用。原稿の長さから秒数を見積もった無音を置く（TTS の枠を使わない）。"""
    import wave
    out.mkdir(parents=True, exist_ok=True)
    meta = []
    for i, s in enumerate(ep["scenes"], 1):
        sec, f = round(len(s["say"]) * 0.13 + 0.6, 2), out / f"silent{i}.wav"
        with wave.open(str(f), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\0\0" * int(24000 * sec))
        meta.append({"text": s["say"], "file": str(f), "seconds": sec, "voice": "silent"})
    return meta


def narrate(ep: dict, out: Path) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    meta_f = out / "scenes.json"
    old = json.loads(meta_f.read_text()) if meta_f.exists() else []
    texts = [s["say"] for s in ep["scenes"]]
    if len(old) == len(texts) and all(o["text"] == t for o, t in zip(old, texts)):
        return old  # 原稿が同じなら作り直さない
    try:
        meta = [{"text": t, "file": str(out / f"s{i}.wav"), "seconds": round(speak(t, str(out / f"s{i}.wav")), 2), "voice": "gemini"}
                for i, t in enumerate(texts, 1)]
    except SystemExit as e:
        if not cloud_available():
            raise
        print("Gemini TTS 失敗 → Cloud TTS (Autonoe):", str(e)[:120], flush=True)
        meta = [{"text": t, "file": str(out / f"s{i}.wav"), "seconds": round(speak_cloud(t, str(out / f"s{i}.wav")), 2), "voice": "gcloud"}
                for i, t in enumerate(texts, 1)]
    meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def render(ep: dict, narr: list[dict], out: Path) -> float:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        ctx.add_init_script(f"window.__EDU__ = {json.dumps({'ep': ep, 'narr': narr}, ensure_ascii=False)};")
        ctx.add_init_script(CLOCK_JS)
        page = ctx.new_page()
        page.goto((HERE / "template.html").as_uri() + "?render=1")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(1000)
        page.evaluate("window.startRender()")
        info = page.evaluate("window.__render")
        frames = Path(tempfile.mkdtemp())
        for i in range(int(info["end"] / 1000 * FPS) + 1):
            page.evaluate("t => window.__seek(t)", i * 1000 / FPS)
            page.screenshot(path=str(frames / f"{i:05d}.jpg"), type="jpeg", quality=92)
        browser.close()
    total = info["end"] / 1000
    inputs, filters = [], []
    for i, clip in enumerate(narr):
        inputs += ["-i", clip["file"]]
        ms = int((info["starts"][i] / 1000 + VOICE_DELAY) * 1000)
        filters.append(f"[{i + 1}:a]adelay={ms}|{ms},aresample=48000[a{i}]")
    filters.append("".join(f"[a{i}]" for i in range(len(narr))) + f"amix=inputs={len(narr)}:normalize=0,apad[aout]")
    filters.append("[0:v]scale=out_range=tv,format=yuv420p[vout]")
    subprocess.run([FF, "-y", "-framerate", str(FPS), "-i", str(frames / "%05d.jpg"), *inputs,
                    "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                    "-t", f"{total:.3f}", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
                    "-crf", "20", "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True, capture_output=True)
    shutil.rmtree(frames, ignore_errors=True)
    return total


def main() -> None:
    src = Path(sys.argv[1]).resolve()
    ep = json.loads(src.read_text(encoding="utf-8"))
    (HERE / "out").mkdir(exist_ok=True)
    quiet = "--silent" in sys.argv
    narr = (silent if quiet else narrate)(ep, HERE / "out" / f"{src.stem}_voice")
    for m in narr:
        print(m["voice"], f"{m['seconds']:.1f}s", m["text"], flush=True)
    out = HERE / "out" / f"{src.stem}{'_silent' if quiet else ''}.mp4"
    print(out, f"{render(ep, narr, out):.1f}s")


if __name__ == "__main__":
    main()
