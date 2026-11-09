#!/bin/bash
# 在 Mac 上執行一次，產生 dist/NaverDownloader.app，把它壓縮後傳給其他 Mac 使用者
cd "$(dirname "$0")"
python3 -m venv .venv-build
.venv-build/bin/pip install -q playwright pyinstaller
.venv-build/bin/pyinstaller --noconfirm --windowed --name "NaverDownloader" naver_downloader.py
cd dist && ditto -c -k --keepParent NaverDownloader.app NaverDownloader-mac.zip
echo
echo "完成！要傳給別人的檔案：dist/NaverDownloader-mac.zip"
read -p "按 Enter 結束"
