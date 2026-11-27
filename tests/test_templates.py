"""動画テンプレート（HTML）の中の JavaScript が構文として正しいかを確かめる。

11/19: reel/template/reel.html の1行の中に // コメントを置いたせいで後ろの処理が消え、
週次リールの書き出しが全地点で止まった（window.startRender is not a function）。同じ種類の事故を出す前に止める。
node が無い環境ではスキップする（GitHub Actions の ubuntu-latest には入っている）。
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 書き出しに使うテンプレートは全部見る（11/19: cover.html を一覧から漏らして同じ壊れを見逃した）
TEMPLATES = sorted(str(p.relative_to(ROOT)) for p in [*ROOT.glob("reel/template/*.html"), *ROOT.glob("edu/*.html"), *ROOT.glob("report_reel/*.html")])
SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def syntax_errors(html: str) -> list[str]:
    errs = []
    for i, js in enumerate(SCRIPT.findall(html)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        Path(f.name).unlink(missing_ok=True)
        if r.returncode:
            lines = r.stderr.strip().splitlines()
            errs.append(f"script[{i}]: " + next((x for x in lines if "Error" in x), lines[-1] if lines else "?"))
    return errs


@unittest.skipUnless(shutil.which("node"), "node がない")
class TemplateSyntaxTests(unittest.TestCase):
    def test_templates_parse(self):
        for t in TEMPLATES:
            with self.subTest(template=t):
                html = (ROOT / t).read_text(encoding="utf-8")
                self.assertTrue(SCRIPT.findall(html), f"{t}: script が見つからない")
                self.assertEqual(syntax_errors(html), [], t)

    def test_catches_line_comment_bug(self):
        bad = "<script>const f = (m) => { const c = m; // メモ return c; };\nwindow.go = () => f(1);</script>"
        self.assertTrue(syntax_errors(bad))


if __name__ == "__main__":
    unittest.main()
