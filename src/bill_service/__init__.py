def main() -> None:
    import uvicorn

    uvicorn.run("bill_service.main:app", host="0.0.0.0", port=8001, reload=True)