# Third-party licenses（social/ が取り込んだ外部の素材）

social/ の教育リールのネタ出し・台本検査は、次の2つの MIT ライセンスのリポジトリの一部を取り込んでいる。
元のスクリプトは取り込んでいない（実行もしていない）。取り込んだのは辞書のデータと、考え方・正規表現の一部だけ。

## 取り込んだもの

| 取り込み先 | 元 | 扱い |
|---|---|---|
| social/data/slop_ja.json | instagram-agent-skill-ja `skills/ig-human/slop.json` | そのまま写した。先頭にキー `_source`（出典）を足しただけ。使わない項目は social/edu_rules.py の `STYLE_EXCLUDE` で外す |
| social/edu_rules.py の `HOOK_DEALBREAKERS`・`HOOK_WEAK_OPENERS` | instagram-agent-skill-ja `skills/ig-reel/hookscore.py` の `DEALBREAKERS`・`WEAK_OPENERS` | 正規表現と語の一覧を写し、説明文を書き直した。点数づけ（5つの性質の採点）は取り込んでいない。当たったら不合格にする使い方に変えた |
| social/edu_rules.py の `lint_style` | instagram-agent-skill-ja `skills/ig-human/slop.json` の words・phrases・structures（regex） | 辞書を読んで当たりを返すだけの自前の実装（元の detect.py・humanize.py は使っていない） |
| social/ideas_agent.py の `HOOK_TYPES` | instagram-agent-skill-ja `skills/ig-reel/hooks.json` の id 16・3・12・7・11・10・14・23 | 教育向けの6型だけを選び、形・例・失敗の仕方を波予報の解説向けに書き直した（「よくある思い込みを正す」は 3・12 を元にした改変、「呼びかけ・〜なら、どう読む？」は 10・14 をまとめた）。煽り系の型は入れていない |
| social/ideas_agent.py の write_prompt の「1場面1アイデア」「sum で1場面目の問いの言葉を回収して答える」 | instagram-agent-skill `skills/ig-reel/SKILL.md`（One idea per Reel / THE LOOP: echo one word from the hook） | 考え方だけを日本語の指示に書き直した |

元のリポジトリ:
- instagram-agent-skill（英語版）: https://github.com/Jakeschincariol/instagram-agent-skill （commit d03c56bb598be770c60b201f94237e5d1a4268a6）
- instagram-agent-skill-ja（日本語版）: https://github.com/Alman-creator-uslog/instagram-agent-skill-ja （commit 8e924d6f1c2d5082560daf3347500165b4ea4f51）

## instagram-agent-skill（英語版）

```
MIT License

Copyright (c) 2026 Jake Schincariol

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## instagram-agent-skill-ja（日本語版）

```
MIT License

Copyright (c) 2026 Jake Schincariol
Copyright (c) 2026 あるまん (Japanese adaptation)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
