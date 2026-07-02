"""気象庁 潮汐推算データを取得して JSON キャッシュに保存する。

使い方:
    python scripts/fetch_tide.py                          # 当年・宮崎/油津
    python scripts/fetch_tide.py --year 2027 --ports MG AB
    python scripts/fetch_tide.py --output output/tide_jma.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wavehack_pipeline.tide import (
    PORT_ABURATSU,
    PORT_MIYAZAKI,
    fetch_jma_tide,
    parse_jma_text,
    to_jsonable,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument(
        "--ports",
        nargs="+",
        default=[PORT_MIYAZAKI, PORT_ABURATSU],
        help="気象庁港コード（2文字）。既定: MG AB",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / "tide_jma.json",
    )
    args = parser.parse_args()

    payload: dict = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "year": args.year,
        "source": "jma_suisan_txt",
        "ports": {},
    }

    for port in args.ports:
        try:
            text = fetch_jma_tide(args.year, port)
        except Exception as e:
            print(f"  ERROR: fetch {port}: {e}", file=sys.stderr)
            continue
        table = parse_jma_text(text)
        payload["ports"][port] = to_jsonable(table)
        print(f"  OK: {port} {args.year} = {len(table)} days")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
