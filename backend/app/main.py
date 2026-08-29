from fastapi import FastAPI

app = FastAPI(title="RepoScope API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "reposcope-api"}
