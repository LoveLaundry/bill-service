from fastapi import FastAPI

app = FastAPI()


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "service": "bill-service", "probe": "minimal-root-app"}
