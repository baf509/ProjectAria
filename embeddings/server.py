"""
ARIA - Embedding Microservice

Serves voyage-4-nano embeddings via an OpenAI-compatible /v1/embeddings endpoint.
Runs on CPU using sentence-transformers.
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

TRUNCATE_DIM = 1024
MODEL_NAME = "voyageai/voyage-4-nano"

model: SentenceTransformer | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    model = SentenceTransformer(MODEL_NAME, truncate_dim=TRUNCATE_DIM)
    yield
    model = None


app = FastAPI(title="ARIA Embeddings", lifespan=lifespan)


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str = MODEL_NAME


class EmbeddingData(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict


@app.post("/v1/embeddings")
async def create_embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    texts = [request.input] if isinstance(request.input, str) else request.input

    embeddings = model.encode(texts, normalize_embeddings=True)

    data = [
        EmbeddingData(embedding=emb.tolist(), index=i)
        for i, emb in enumerate(embeddings)
    ]

    return EmbeddingResponse(
        data=data,
        model=request.model,
        usage={"prompt_tokens": sum(len(t.split()) for t in texts), "total_tokens": sum(len(t.split()) for t in texts)},
    )


@app.get("/health")
async def health():
    return {"status": "healthy", "model": MODEL_NAME, "dimensions": TRUNCATE_DIM}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
