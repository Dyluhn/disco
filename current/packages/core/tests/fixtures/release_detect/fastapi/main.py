from fastapi import FastAPI

app = FastAPI()


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/")
def index() -> dict[str, str]:
    return {"hello": "world"}
