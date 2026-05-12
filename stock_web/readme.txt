# 1. 安裝依賴(只需做一次)
pip3 install --user fastapi uvicorn

# 2. 在 Claude/ 目錄下啟動
pip3 install --user anthropic  # 已裝
export ANTHROPIC_API_KEY=sk-ant-...
python3 -m uvicorn stock_web.app:app --host 0.0.0.0 --port 8000

# 3. 瀏覽器開
http://localhost:8000
