#!/bin/bash
# 在 Mac 上直接執行下載器（第一次會自動安裝需要的套件）
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "第一次執行，安裝中…"
  python3 -m venv .venv || { echo "找不到 Python，請先到 https://www.python.org 下載安裝"; read -p "按 Enter 結束"; exit 1; }
  .venv/bin/pip install -q playwright
fi
.venv/bin/python naver_downloader.py
