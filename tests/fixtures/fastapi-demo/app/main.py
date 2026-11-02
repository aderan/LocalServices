"""fastapi-demo：GET /health -> {"ok": true}。

运行（N04 验证时）：uv run --directory <本目录> uvicorn app.main:app --port 18212
"""

from fastapi import FastAPI

app = FastAPI(title="fastapi-demo")


@app.get("/health")
def health() -> dict:
    return {"ok": True}
