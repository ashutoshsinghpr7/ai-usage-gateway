"""Fake OpenAI-compatible upstream used by scripts/verify_local.sh.

Returns a deterministic chat completion with a usage block so the gateway's
metering path can be verified end to end without spending real API money.
Also supports stream=true so the streaming relay path is exercised.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "fake-model")

    if body.get("stream"):
        async def events():
            for piece in ["po", "ng from ", "fake up", "stream"]:
                payload = {"choices": [{"delta": {"content": piece}}], "model": model}
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return {
        "id": "chatcmpl-fake-1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong from fake upstream"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }
