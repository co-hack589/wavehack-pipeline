"""Upload pipeline artefacts to the configured remote target.

Env vars (per target):

    OUTPUT_TARGET=supabase
      SUPABASE_URL           https://<project>.supabase.co
      SUPABASE_SERVICE_KEY   service-role key
      SUPABASE_BUCKET        default: wavehack
      SUPABASE_OBJECT_PATH   default: dashboard.json

    OUTPUT_TARGET=s3
      S3_BUCKET              e.g. wavehack-data-prod
      S3_KEY                 default: processed/dashboard.json
      AWS_REGION             default: ap-northeast-1

    OUTPUT_TARGET=local (default)
      no-op

Called from scripts/run_all.sh after generate_dashboard.py.
Kept dependency-light so it can run in either Fargate (with boto3 baked in)
or GitHub Actions (with just `requests`).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def upload_supabase(path: Path) -> None:
    import requests  # already a `gfs` extra dep

    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    bucket = os.environ.get("SUPABASE_BUCKET", "wavehack")
    obj = os.environ.get("SUPABASE_OBJECT_PATH", "dashboard.json")

    endpoint = f"{url}/storage/v1/object/{bucket}/{obj}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "x-upsert": "true",  # overwrite existing
        "cache-control": "max-age=0, no-cache, must-revalidate",
    }
    with path.open("rb") as f:
        r = requests.post(endpoint, headers=headers, data=f.read(), timeout=60)
    if r.status_code >= 400:
        raise SystemExit(f"Supabase upload failed: {r.status_code} {r.text}")
    print(f"[upload] supabase ok: {endpoint}")


def upload_s3(path: Path) -> None:
    try:
        import boto3  # not in `gfs` extras — install in Fargate image only
    except ImportError as e:
        raise SystemExit(
            "boto3 not installed. Add it to a `[s3]` extra or install in the container."
        ) from e

    bucket = os.environ["S3_BUCKET"]
    key = os.environ.get("S3_KEY", "processed/dashboard.json")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")

    s3 = boto3.client("s3", region_name=region)
    s3.upload_file(
        Filename=str(path),
        Bucket=bucket,
        Key=key,
        ExtraArgs={
            "ContentType": "application/json",
            "CacheControl": "max-age=0, no-cache, must-revalidate",
        },
    )
    print(f"[upload] s3 ok: s3://{bucket}/{key}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument(
        "--target",
        default=os.environ.get("OUTPUT_TARGET", "local"),
        choices=["local", "supabase", "s3"],
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"[upload] file missing: {args.file}", file=sys.stderr)
        return 1

    if args.target == "local":
        print("[upload] target=local, no-op")
        return 0
    if args.target == "supabase":
        upload_supabase(args.file)
        return 0
    if args.target == "s3":
        upload_s3(args.file)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
