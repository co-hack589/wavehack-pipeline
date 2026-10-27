"""ポイントごとの週末予報から、Gemini Flash でナレーション原稿を作り、Aoede の声で音声化する。
音声化は Gemini TTS（Aoede）が既定。無料枠が尽きたときは Google Cloud Text-to-Speech（Chirp 3 HD Autonoe、
月100万文字まで無料）で代替する（narration_scenes.py が地点ごとに切り替え、1本の中で声を混ぜない）。
キー・トークンは環境変数から読む（出力しない）:
  GOOGLE_TTS_API_KEY   — Cloud TTS 用 API キー（Text-to-Speech API のみに制限したもの）
  GOOGLE_ACCESS_TOKEN  — キーの代わりに OAuth アクセストークン（GitHub Actions の Workload Identity 等）
  GEMINI_API_KEY       — Gemini TTS
  TTS_BACKEND=gcloud   — 最初から Cloud TTS だけで作る（動作確認用）"""
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
        except (urllib.error.URLError, TimeoutError) as e:  # 通信断も失敗扱いにして代替へ回す
            if attempt == 1:
                raise SystemExit(f"{model}: {e}")
            time.sleep(20)

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
CLOUD_VOICE = "ja-JP-Chirp3-HD-Autonoe"  # 2026-10-16 聞き比べで選定（Chirp 3 HD の Aoede は Gemini の Aoede と声が違う）

def backend() -> str:
    """最初に使う方式。"gemini"（既定）か "gcloud"（TTS_BACKEND=gcloud のとき）。"""
    return os.environ.get("TTS_BACKEND", "").strip() or "gemini"

def cloud_available() -> bool:
    """Cloud TTS の認証情報があるか（Gemini の代替に使えるか）。"""
    return bool(os.environ.get("GOOGLE_TTS_API_KEY", "").strip() or os.environ.get("GOOGLE_ACCESS_TOKEN", "").strip())

# TTS が誤読しやすい地名・用語の読み（音声化の直前に置き換える。画面表示・キャプションは漢字のまま）
READINGS = {
    "鵠沼": "くげぬま", "東浪見": "とらみ", "夷隅": "いすみ", "部原": "へばら", "御宿": "おんじゅく",
    "白渚": "しらすか", "平砂浦": "へいさうら", "由比ガ浜": "ゆいがはま", "七里ガ浜": "しちりがはま",
    "多々戸浜": "たたどはま", "入田浜": "いりたはま", "百々": "どど", "国府ノ浜": "こうのはま",
    "市後浜": "いちごはま", "生見": "ぬくみ", "内妻": "うちづま", "宍喰": "ししくい", "一ツ葉": "ひとつば",
    "お倉ヶ浜": "おくらがはま", "伊比井": "いびい", "大堂津": "おおどうつ", "三苫": "みとま", "芥屋": "けや",
    "五十嵐浜": "いからしはま", "郷津": "ごうづ", "難波江": "なばえ", "白兎": "はくと", "小沢見": "おざみ",
    "羽伏浦": "はぶしうら", "毘砂別": "びしゃべつ", "河原子": "かわらご", "剃金": "そりがね",
    "椎名内": "しいなうち", "鉄浜": "かねはま", "西高東低": "せいこうとうてい",
}


def to_speech(text: str) -> str:
    for k in sorted(READINGS, key=len, reverse=True):
        text = text.replace(k, READINGS[k])
    return text


def speak_cloud(text: str, out: str) -> float:
    text = to_speech(text)
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
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise SystemExit(f"Cloud TTS: {e}")
            time.sleep(20)
    if data[:4] == b"RIFF":  # LINEAR16 は WAV ヘッダー付きで返る
        open(out, "wb").write(data)
    else:
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    with wave.open(out) as w:
        return w.getnframes() / w.getframerate()

def speak(text: str, out: str) -> float:
    text = to_speech(text)
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
    text = to_speech(text)
    """Gemini TTS が使えないとき用の macOS 標準音声（Kyoko）。24kHz/mono/16bit WAV で出力。"""
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as d:
        aiff = os.path.join(d, "v.aiff")
        subprocess.run(["say", "-v", voice, "-r", "210", "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@24000", "-c", "1", aiff, out], check=True)
    with wave.open(out) as w:
        return w.getnframes() / w.getframerate()
