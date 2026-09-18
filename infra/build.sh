#!/usr/bin/env bash
# Package the Lambda deployment zip: handler + tools + pure-Python dependencies.
# boto3 ships with the Lambda runtime, so it is deliberately not bundled.
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD="infra/build"
rm -rf "$BUILD" && mkdir -p "$BUILD/package"

python3 -m pip install \
  --quiet --target "$BUILD/package" \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: requests openpyxl python-dotenv

cp lambda_handler.py "$BUILD/package/"
mkdir -p "$BUILD/package/tools"
cp tools/*.py "$BUILD/package/tools/"

(cd "$BUILD/package" && zip -qr ../function.zip .)
echo "built $BUILD/function.zip ($(du -h "$BUILD/function.zip" | cut -f1))"
