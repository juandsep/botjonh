"""Worker entrypoint (assistant-worker).

Push subscriber for assistant-updates (user messages) and assistant-cron
(scheduled jobs). Cloud Run validates the Pub/Sub OIDC token before the request
reaches this route, so no unauthenticated caller gets here.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

app = FastAPI(title="assistant-worker")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/push")
async def push(request: Request) -> Response:
    # Decode the Pub/Sub message envelope, route updates vs cron jobs, call the
    # LLM with allowlisted tools, execute writes and reply via the channel.
    # Acknowledge by returning 2xx; Pub/Sub retries on failure, so writes must
    # be idempotent (update_id dedup).
    #    TODO: implement via assistant.llm.client and assistant.services.
    return Response(status_code=200)
