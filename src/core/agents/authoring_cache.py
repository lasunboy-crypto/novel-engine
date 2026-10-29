"""仅供选择式规划的已完成阶段复用；不把未审核候选当作通过。"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path

from pydantic import BaseModel
from src.core.agents.outline_review_packets import digest

_scope: ContextVar[tuple[Path, str] | None] = ContextVar("authoring_stage_cache", default=None)


@contextmanager
def completed_stage_scope(directory: Path, salt: str):
    token = _scope.set((directory, salt))
    try:
        yield
    finally:
        _scope.reset(token)


def completed_stage_key(request: dict) -> tuple[Path, str] | None:
    scope = _scope.get()
    if scope is None:
        return None
    directory, salt = scope
    key = digest({"request": request, "scope": salt, "cache_version": 1})
    return directory / f"{key}.json", key


def load_completed_stage(binding: tuple[Path, str] | None, model: type[BaseModel]) -> BaseModel | None:
    if binding is None or not binding[0].exists():
        return None
    path, key = binding
    data = json.loads(path.read_text())
    if data.get("request_hash") != key or digest(data["value"]) != data.get("value_hash"):
        raise ValueError("已完成阶段缓存来源损坏，不能按通过复用")
    return model.model_validate(data["value"])


def store_completed_stage(binding: tuple[Path, str] | None, result: BaseModel) -> None:
    if binding is None:
        return
    path, key = binding
    value = result.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"request_hash": key, "value_hash": digest(value), "value": value}, ensure_ascii=False))
    temporary.replace(path)
