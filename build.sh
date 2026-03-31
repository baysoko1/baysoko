#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
pip install -r requirements.txt

mkdir -p \
  media/store_logos \
  media/store_covers \
  media/v1/chat_attachments \
  media/chat_attachments \
  media/listing_images \
  media/profile_pics \
  media/blog_images \
  static/images \
  static/css \
  static/js \
  templates/socialaccount \
  templates/account

echo "-> Build dependencies installed and runtime directories prepared"
