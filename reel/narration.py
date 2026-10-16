"""ポイントごとの週末予報から、Gemini Flash でナレーション原稿を作り、Aoede の声で音声化する。
音声化は Google Cloud Text-to-Speech（Chirp 3 HD、月100万文字まで無料）が既定。認証情報が無ければ Gemini TTS。
キー・トークンは環境変数から読む（出力しない）:
  GOOGLE_TTS_API_KEY   — Cloud TTS 用 API キー（Text-to-Speech API のみに制限したもの）
  GOOGLE_ACCESS_TOKEN  — キーの代わりに OAuth アクセストークン（GitHub Actions の Workload Identity 等）
  GEMINI_API_KEY       — Gemini TTS（TTS_BACKEND=gemini のとき、または Cloud TTS の認証情報が無いとき）"""
import base64, json, os, sys, time, urllib.error, urllib.request, wave

API = "https://generativelanguage.googleapis.com/v1beta/models"
TEXT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest"]  # 混雑(503)時は順に切替
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]  # 上限時は Lite に切替
VOICE = "Aoede"
MAX_CHARS = 85  # 明るめのテンポで約20秒

MIN_INTERVAL = 22.0  # 秒。無料枠の1分あたり上限に当たらないよう TTS 呼び出しの間隔をあける
_last_call = [0.0]

def call(model: str, body: dict) -> dict:
    # キーは strip する（Secrets にコピペした際の末尾改行でヘッダーが不正になるため）
    if "tts" in model:
        wait = _last_call[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    req = urllib.request.Request(f"{API}/{model}:generateContent", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"].strip()})
    for attempt in range(2):  # 503/429 は一時的なので1回だけ待って再試行
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 503) or attempt == 1:
                raise SystemExit(f"{model}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
            time.sleep(60)

def facts(P: dict) -> dict:
    best = max(((k, s) for k in ("sat", "sun") for s in P["days"][k]), key=lambda x: x[1]["score"])
    k, s = best
    h = int(s["t"][:2])
    good = sum(1 for m in P["members"] if (m[k] or 0) >= 55)
    return {"エリア": P["group"], "ポイント": P["name"], "おすすめ地点数": f"{len(P['members'])}地点中{good}地点（{'土' if k == 'sat' else '日'}曜）",
            "ベスト": f"{'土' if k == 'sat' else '日'}曜の{'午後' + str(h - 12) if h > 12 else '午前' + str(h) if h < 12 else '正午'}時".replace("正午時", "正午"),
            "うねり": f"{s['swell']}から{s['h']:.1f}メートル、周期{round(s['per'])}秒", "風": f"{s['wind']} {s['wms']}m/s", "スコア": s["score"]}

def write_script(P: dict) -> str:
    f = facts(P)
    prompt = (f"波予報サービス「ウェーブハック」の Instagram リール用ナレーション原稿を書いてください。\n"
              f"条件: 日本語{MAX_CHARS}文字以内／4文／1文目は「今週末の{f['エリア']}エリア」で始める／"
              f"最後は「詳しくはウェーブハックで！」で締める／数字は下の事実だけを使い、創作しない／絵文字・記号・かっこ禁止\n"
              f"事実: {json.dumps(f, ensure_ascii=False)}\n原稿のみを出力:")
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.7}}
    for i, model in enumerate(TEXT_MODELS):
        try:
            res = call(model, body)
            break
        except SystemExit:
            if i == len(TEXT_MODELS) - 1:
                raise
    text = res["candidates"][0]["content"]["parts"][0]["text"].strip().replace("\n", "")
    if len(text) > MAX_CHARS + 10:  # 長すぎたら事実ベースの定型に落とす
        text = f"今週末の{f['エリア']}エリア、波予想です！{f['ポイント']}のベストは{f['ベスト']}。{f['うねり'].split('、')[0]}のうねりで、スコアは{f['スコア']}！詳しくはウェーブハックで！"
    return text

CLOUD_TTS = "https://texttospeech.googleapis.com/v1/text:synthesize"
CLOUD_VOICE = f"ja-JP-Chirp3-HD-{VOICE}"

def backend() -> str:
    """"gcloud"（Cloud TTS）か "gemini"。TTS_BACKEND で明示しなければ、Cloud TTS の認証情報があればそちらを使う。"""
    b = os.environ.get("TTS_BACKEND", "").strip()
    if b:
        return b
    return "gcloud" if (os.environ.get("GOOGLE_TTS_API_KEY") or os.environ.get("GOOGLE_ACCESS_TOKEN")) else "gemini"

def speak_cloud(text: str, out: str) -> float:
    body = {"input": {"text": text}, "voice": {"languageCode": "ja-JP", "name": CLOUD_VOICE},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}
    headers = {"content-type": "application/json"}
    if os.environ.get("GOOGLE_TTS_API_KEY"):
        headers["x-goog-api-key"] = os.environ["GOOGLE_TTS_API_KEY"].strip()
    else:
        headers["authorization"] = "Bearer " + os.environ["GOOGLE_ACCESS_TOKEN"].strip()
        if os.environ.get("GOOGLE_CLOUD_PROJECT"):  # ユーザー認証(ADC)のときは課金・割り当て先のプロジェクトが必要
            headers["x-goog-user-project"] = os.environ["GOOGLE_CLOUD_PROJECT"].strip()
    req = urllib.request.Request(CLOUD_TTS, data=json.dumps(body).encode(), headers=headers)
    for attempt in range(3):  # 429/5xx は一時的なので待って再試行
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = base64.b64decode(json.load(r)["audioContent"])
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 503) or attempt == 2:
                raise SystemExit(f"Cloud TTS: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
            time.sleep(20)
    if data[:4] == b"RIFF":  # LINEAR16 は WAV ヘッダー付きで返る
        open(out, "wb").write(data)
    else:
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    with wave.open(out) as w:
        return w.getnframes() / w.getframerate()

def speak(text: str, out: str) -> float:
    if backend() == "gcloud":
        return speak_cloud(text, out)
    body = {"contents": [{"parts": [{"text": text}]}],  # 演技指示を混ぜると読み上げられるため原稿のみ
                           "generationConfig": {"responseModalities": ["AUDIO"],
                                                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
    for i, model in enumerate(TTS_MODELS):
        try:
            res = call(model, body)
            break
        except SystemExit:
            if i == len(TTS_MODELS) - 1:
                raise
    data = base64.b64decode(res["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    if data[:4] == b"RIFF":
        open(out, "wb").write(data)
    else:
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    with wave.open(out) as w:
        return w.getnframes() / w.getframerate()

if __name__ == "__main__":
    points, out_dir, names = json.load(open(sys.argv[1])), sys.argv[2], [a for a in sys.argv[3:] if not a.startswith("--")]
    for P in points:
        if names and P["name"] not in names:
            continue
        cached = f"{out_dir}/narration_{P['name']}.json"
        text = json.load(open(cached))["text"] if "--reuse" in sys.argv and os.path.exists(cached) else write_script(P)
        sec = speak(text, f"{out_dir}/narration_{P['name']}.wav")
        json.dump({"text": text, "seconds": round(sec, 1)}, open(f"{out_dir}/narration_{P['name']}.json", "w"), ensure_ascii=False)
        print(P["name"], f"{sec:.1f}s", len(text), text)


def speak_local(text: str, out: str, voice: str = "Kyoko") -> float:
    """Gemini TTS が使えないとき用の macOS 標準音声（Kyoko）。24kHz/mono/16bit WAV で出力。"""
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as d:
        aiff = os.path.join(d, "v.aiff")
        subprocess.run(["say", "-v", voice, "-r", "210", "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@24000", "-c", "1", aiff, out], check=True)
    with wave.open(out) as w:
        return w.getnframes() / w.getframerate()
