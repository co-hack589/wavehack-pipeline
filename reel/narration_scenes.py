"""リールの4シーンそれぞれに合わせたナレーションを、予報データから定型で組み立てて Gemini TTS (Aoede) で音声化する。
シーンごとに別ファイルにすることで、各シーンの頭で再生でき、映像とズレない。キーは環境変数 GEMINI_API_KEY から読む。"""
from __future__ import annotations

import json, os, shutil, sys
sys.path.insert(0, os.path.dirname(__file__))
from narration import speak, speak_local  # noqa: E402

DAYJ = {"sat": "土曜", "sun": "日曜"}

def jtime(t: str) -> str:
    h = int(t[:2])
    return "正午" if h == 12 else f"午後{h - 12}時" if h > 12 else f"午前{h}時"

# アプリの waveSizeLabel (src/app/_shared/wave-size.ts) と同じ閾値。読み上げ用の言い回し
def size_words(m: float) -> str:
    cm = m * 100
    if cm < 50: return "すねからひざくらい"
    if cm < 80: return "ももから腰くらい"
    if cm < 120: return "腹から胸くらい"
    if cm < 180: return "肩から頭くらい"
    return "頭オーバー"

def lines(P: dict) -> list[str]:
    key, s = max(((k, x) for k in ("sat", "sun") for x in P["days"][k]), key=lambda kx: kx[1]["score"])
    other = "sun" if key == "sat" else "sat"
    o = max(P["days"][other], key=lambda x: x["score"])
    good = sum(1 for m in P["members"] if (m[key] or 0) >= 55)
    area = P["group"].replace("・", "")
    size = size_words(s["h"])
    both = abs(o["h"] - s["h"]) < 0.3
    return [
        (f"今週末の{area}エリアは、{len(P['members'])}地点中{good}地点がおすすめ。" if good
         else f"今週末の{area}エリアは、全体的に穏やかなコンディション。"),  # 「0地点がおすすめ」と言わない
        f"{P['name']}は{'土日とも' if both else DAYJ[key] + 'が'}{size}のサイズ。ベストは{DAYJ[key]}の{jtime(s['t'])}です。",
        f"うねりは{s['swell']}から、周期{round(s['per'])}秒。風は{s['wms']:.0f}メートルの{s['wind']}です。",
    ]

# 誘導の一言は全地点共通。1回だけ音声化して使い回す
CTA_TEXT = "詳しくはプロフィールのリンクから、ウェーブハックでチェックしてね！"
CTA_FILE = "common_cta.wav"

def generate(points: list[dict], out_dir: str, cta_dir: str, names: list[str] | None = None) -> None:
    """シーン1〜3はポイントごとに音声化、4（誘導）は cta_dir の共通音声を使い回す。"""
    cta_path, cta_meta = f"{cta_dir}/{CTA_FILE}", f"{cta_dir}/common_cta.json"
    if not (os.path.exists(cta_path) and os.path.exists(cta_meta) and json.load(open(cta_meta))["text"] == CTA_TEXT):
        json.dump({"text": CTA_TEXT, "seconds": round(speak(CTA_TEXT, cta_path), 2)}, open(cta_meta, "w"), ensure_ascii=False)
    cta = json.load(open(cta_meta))
    gemini_ok = True
    for P in points:
        if names and P["name"] not in names:
            continue
        prev = f"{out_dir}/{P['name']}_scenes.json"
        done = json.load(open(prev)) if os.path.exists(prev) else []
        texts = lines(P)
        meta, voice = [], "gemini"
        try:
            if not gemini_ok:
                raise SystemExit("Gemini TTS は今回の実行では使用不可")
            for i, text in enumerate(texts, 1):
                out = f"{out_dir}/{P['name']}_s{i}.wav"
                if os.path.exists(out) and len(done) >= i and done[i - 1]["text"] == text and done[i - 1].get("voice", "gemini") == "gemini":
                    sec = done[i - 1]["seconds"]  # 同じ原稿の音声は作り直さない（再実行時）
                else:
                    sec = speak(text, out)
                meta.append({"text": text, "seconds": round(sec, 2), "file": out, "voice": "gemini"})
                json.dump(meta, open(prev, "w"), ensure_ascii=False, indent=1)  # 途中経過も保存
            last = {"text": cta["text"], "seconds": cta["seconds"], "file": cta_path, "voice": "gemini"}
        except SystemExit as e:
            # 1本のリール内で声が混ざらないよう、このポイントは誘導も含め全部 Kyoko で作り直す
            print(P["name"], "Gemini TTS 失敗 → macOS Kyoko で代替:", str(e)[:120], flush=True)
            gemini_ok = False
            if not shutil.which("say"):  # クラウド(Linux)には代替音声が無い → このポイントは動画化しない
                if os.path.exists(prev):
                    os.remove(prev)
                print(P["name"], "代替音声なしのためスキップ", flush=True)
                continue
            voice, meta = "kyoko", []
            for i, text in enumerate(texts, 1):
                out = f"{out_dir}/{P['name']}_s{i}.wav"
                meta.append({"text": text, "seconds": round(speak_local(text, out), 2), "file": out, "voice": "kyoko"})
            kcta = f"{out_dir}/common_cta_kyoko.wav"
            if not os.path.exists(kcta):
                speak_local(CTA_TEXT, kcta)
            import wave
            with wave.open(kcta) as w:
                last = {"text": CTA_TEXT, "seconds": round(w.getnframes() / w.getframerate(), 2), "file": kcta, "voice": "kyoko"}
        for i, m in enumerate(meta, 1):
            print(P["name"], i, voice, f"{m['seconds']:.1f}s", m["text"], flush=True)
        json.dump(meta + [last], open(prev, "w"), ensure_ascii=False, indent=1)

def generate_silent(points: list[dict], out_dir: str, seconds: float = 3.0) -> None:
    """動作確認用: ナレーションの代わりに無音を置く（Gemini の枠を使わずに動画化まで試す）。"""
    import wave
    for P in points:
        meta = []
        for i, text in enumerate(lines(P) + [CTA_TEXT], 1):
            out = f"{out_dir}/{P['name']}_s{i}.wav"
            with wave.open(out, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(b"\x00\x00" * int(24000 * seconds))
            meta.append({"text": text, "seconds": seconds, "file": out, "voice": "silent"})
        json.dump(meta, open(f"{out_dir}/{P['name']}_scenes.json", "w"), ensure_ascii=False, indent=1)
        print(P["name"], "silent", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="build_reel_data.py の出力")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--cta-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "voice"))
    ap.add_argument("--silent", action="store_true", help="無音で代用（動作確認用）")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    if a.silent:
        points = json.load(open(a.data))["points"]
        generate_silent([p for p in points if not a.names or p["name"] in a.names], a.out_dir)
        raise SystemExit(0)
    generate(json.load(open(a.data))["points"], a.out_dir, a.cta_dir, a.names)
