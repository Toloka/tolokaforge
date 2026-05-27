from fastapi import FastAPI

from routers import holds, reports

app = FastAPI(title="Billing Service")

app.include_router(holds.router)
app.include_router(reports.router)


@app.get("/health")
def health():
    return {"status": "ok"}
