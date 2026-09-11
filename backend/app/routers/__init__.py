"""HTTP routers, plus the shared JSON body helper for POST endpoints.

The acceptance tests post JSON with `curl -X POST ... -d '{json}'` and no Content-Type header, so
curl sends `application/x-www-form-urlencoded`, which FastAPI's normal body parsing rejects with 422.
Every POST endpoint therefore reads the raw body and validates it with `parse_json_body`.
"""
from typing import TypeVar

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


async def parse_json_body(request: Request, model: type[T]) -> T:
    """Validate the raw request body as JSON for `model`, whatever the Content-Type header says.

    Raises HTTPException(422) with Pydantic's error list (the same shape FastAPI uses for its own
    validation errors) if the body is not valid JSON or does not match the model.
    """
    raw = await request.body()
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        # A body that is not valid UTF-8 comes back as raw bytes in "input"; decode it leniently
        # so encoding the error cannot fail (otherwise the client would get a 500, not a 422).
        detail = jsonable_encoder(
            exc.errors(include_url=False),
            custom_encoder={bytes: lambda b: b.decode("utf-8", errors="replace")},
        )
        raise HTTPException(status_code=422, detail=detail) from exc
