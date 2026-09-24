"""Scoped API for deterministic rarity watch collection management."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()
HOME = Path(__file__).resolve().parents[3]
MANAGER_PATH = HOME / "scripts" / "rarity_watch_manager.py"


def _manager():
    spec = importlib.util.spec_from_file_location("rarity_watch_manager_backend", MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return _manager().execute(body, **kwargs)


@router.get("/health")
async def health():
    return {"ok": True, "mode": "deterministic-zero-llm", "live_add": True}


@router.post("/execute")
async def execute(body: dict[str, Any]):
    try:
        return execute_request(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
