"""波レポートのリールを書き出す（メディアの下ごしらえ → template.html を 1 コマずつ撮影 → ナレーションと合成して MP4）。

  uv run --with playwright --with imageio-ffmpeg python report_reel/reel_render.py --ep ep.json --media main.mp4 --kind video \
      --out out.mp4 [--silent | --voice-dir voice/]
1080x1920 / 30fps / H.264 + AAC。ページの時計は reel/render_reel.py の CLOCK_JS で手動で進める（負荷に関係なく同じ動画になる）。
  --silent     TTS を使わず、原稿の長さから見積もった無音で尺を決める（見た目の確認用）
  --voice-dir  そのフォルダの scenes.json（edu/render_edu.narrate の形）をそのまま使う。原稿と一致しなければ止まる
どちらも無ければ edu/render_edu.narrate で音声化する（Gemini TTS Aoede → 失敗時 Cloud TTS Chirp 3 HD Autonoe）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "reel"))
sys.path.insert(0, str(ROOT / "edu"))
from narration import to_speech  # noqa: E402
from render_reel import CLOCK_JS, FF, FPS, VOICE_DELAY  # noqa: E402

CARD_W, CARD_H = 900, 506  # 映像カード（16:9）
PHOTO_W, PHOTO_H = 1080, 608  # 写真は少し大きく用意して Ken Burns で動かす
MAX_VIDEO_S = 20


def prepare_media(src: Path, kind: str, work: Path) -> dict:
    """work/media/ に template が読む形で置く。video は 30fps の連番 JPEG（16:9 に中央で切り抜き）、photo は 1 枚。"""
    media = work / "media"
    shutil.rmtree(media, ignore_errors=True)
    if kind == "photo":
        media.mkdir(parents=True)
        subprocess.run([FF, "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={PHOTO_W}:{PHOTO_H}:force_original_aspect_ratio=increase,crop={PHOTO_W}:{PHOTO_H}",
                        "-q:v", "3", str(media / "photo.jpg")], check=True, capture_output=True)
        return {"kind": "photo", "frames": 1, "fps": FPS}
    frames = media / "frames"
    frames.mkdir(parents=True)
    subprocess.run([FF, "-v", "error", "-y", "-i", str(src), "-t", str(MAX_VIDEO_S), "-an",
                    "-vf", f"fps={FPS},scale={CARD_W}:{CARD_H}:force_original_aspect_ratio=increase,crop={CARD_W}:{CARD_H}",
                    "-start_number", "0", "-q:v", "3", str(frames / "%04d.jpg")], check=True, capture_output=True)
    n = len(list(frames.glob("*.jpg")))
    if n == 0:
        raise SystemExit("動画からコマを取り出せませんでした")
    return {"kind": "video", "frames": n, "fps": FPS}


def silent(ep: dict, out: Path) -> list[dict]:
    import render_edu  # playwright を読むので必要なときだけ
    return render_edu.silent(ep, out)


def cached_voice(ep: dict, vdir: Path) -> list[dict]:
    """既にある音声（scenes.json）を使う。原稿・読みが一致しないシーンがあれば止める（勝手に TTS は呼ばない）。"""
    rd = ep.get("readings") or {}
    narr = json.loads((vdir / "scenes.json").read_text(encoding="utf-8"))
    bad = [i + 1 for i, s in enumerate(ep["scenes"])
           if i >= len(narr) or narr[i]["text"] != s["say"] or narr[i].get("speech", to_speech(s["say"], rd)) != to_speech(s["say"], rd)
           or not Path(narr[i]["file"]).exists()]
    if len(narr) != len(ep["scenes"]) or bad:
        raise SystemExit(f"{vdir}/scenes.json が原稿と一致しません（シーン {bad}）")
    return narr


def narrate(ep: dict, out: Path) -> list[dict]:
    import render_edu
    return render_edu.narrate(ep, out)


def render(ep: dict, narr: list[dict], media: dict, work: Path, out: Path) -> float:
    from playwright.sync_api import sync_playwright

    page_file = work / "reel.html"
    shutil.copyfile(HERE / "template.html", page_file)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        ctx.add_init_script(f"window.__EDU__ = {json.dumps({'ep': ep, 'narr': narr, 'media': media}, ensure_ascii=False)};")
        ctx.add_init_script(CLOCK_JS)
        page = ctx.new_page()
        page.goto(page_file.as_uri() + "?render=1")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(800)
        page.evaluate("window.startRender()")
        info = page.evaluate("window.__render")
        frames = Path(tempfile.mkdtemp())
        for i in range(int(info["end"] / 1000 * FPS) + 1):
            page.evaluate("async t => { window.__seek(t); await window.__vidSync(); }", i * 1000 / FPS)
            page.screenshot(path=str(frames / f"{i:05d}.jpg"), type="jpeg", quality=94)
        browser.close()
    total = info["end"] / 1000
    inputs, filters = [], []
    for i, clip in enumerate(narr):
        inputs += ["-i", clip["file"]]
        ms = int((info["starts"][i] / 1000 + VOICE_DELAY) * 1000)
        filters.append(f"[{i + 1}:a]adelay={ms}|{ms},aresample=48000[a{i}]")
    filters.append("".join(f"[a{i}]" for i in range(len(narr))) + f"amix=inputs={len(narr)}:normalize=0,apad[aout]")
    filters.append("[0:v]scale=out_range=tv,format=yuv420p[vout]")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FF, "-y", "-framerate", str(FPS), "-i", str(frames / "%05d.jpg"), *inputs,
                    "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                    "-t", f"{total:.3f}", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
                    "-crf", "20", "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True, capture_output=True)
    shutil.rmtree(frames, ignore_errors=True)
    return total


def alt_version(video: Path) -> Path | None:
    """Instagram の処理エラーに備え、別の設定で変換し直した版（publish_edu.publish が交互に送る）。"""
    alt = video.with_name(video.stem + "-alt.mp4")
    r = subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
                        "-pix_fmt", "yuv420p", "-r", "30", "-g", "60", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                        "-ar", "44100", "-movflags", "+faststart", str(alt)], capture_output=True)
    return alt if r.returncode == 0 and alt.exists() else None


def build_video(ep: dict, media_file: Path, kind: str, out: Path, *, silent_mode: bool = False,
                voice_dir: Path | None = None, work: Path | None = None) -> tuple[float, list[dict]]:
    work = work or Path(tempfile.mkdtemp(prefix="report-reel-"))
    work.mkdir(parents=True, exist_ok=True)
    media = prepare_media(media_file, kind, work)
    if voice_dir:
        narr = cached_voice(ep, voice_dir)
    elif silent_mode:
        narr = silent(ep, work / "voice")
    else:
        narr = narrate(ep, work / "voice")
    return render(ep, narr, media, work, out), narr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", type=Path, required=True)
    ap.add_argument("--media", type=Path, required=True)
    ap.add_argument("--kind", choices=["video", "photo"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--voice-dir", type=Path)
    ap.add_argument("--work", type=Path)
    a = ap.parse_args()
    ep = json.loads(a.ep.read_text(encoding="utf-8"))
    total, narr = build_video(ep, a.media, a.kind, a.out, silent_mode=a.silent, voice_dir=a.voice_dir, work=a.work)
    for m in narr:
        print(m.get("voice"), f"{m['seconds']:.1f}s", flush=True)
    print(a.out, f"{total:.1f}s")


if __name__ == "__main__":
    main()
