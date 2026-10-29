"""Persist parsed, unverified mini-plan responses outside canonical outline files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from src.core.agents._generation_checkpoint import GenerationCheckpointError
from src.core.llm.base import LLMResponse


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


class OutlineMiniCandidateStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise GenerationCheckpointError("小纲待审缓存的请求指纹无效")
        return self.root / f"{key}.json"

    def _read(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except (OSError, ValueError) as exc:
            raise GenerationCheckpointError("小纲待审缓存损坏，不能自动放行或重新付费") from exc

    def _write(self, path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False, prefix=".candidate-") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
                temporary = file.name
            os.replace(temporary, path)
        except OSError as exc:
            raise GenerationCheckpointError("小纲待审缓存保存失败，保留现场后再续跑") from exc

    def load(self, request_hash: str) -> LLMResponse | None:
        data = self._read(self._path(request_hash))
        if data is None:
            return None
        if data.get("version") != 1 or data.get("request_hash") != request_hash:
            raise GenerationCheckpointError("小纲待审缓存来源不一致")
        if data.get("status") == "rejected":
            return None
        if data.get("status") != "unverified":
            raise GenerationCheckpointError("小纲待审缓存状态无效")
        response_hash = data.get("response_hash")
        if not isinstance(response_hash, str):
            raise GenerationCheckpointError("小纲待审缓存缺少响应指纹")
        response = self._read(self._path(response_hash))
        if response is None or _digest(response) != response_hash:
            raise GenerationCheckpointError("小纲待审缓存响应损坏")
        try:
            return LLMResponse.model_validate(response)
        except ValueError as exc:
            raise GenerationCheckpointError("小纲待审缓存响应结构无效") from exc

    def store(self, request_hash: str, response: LLMResponse) -> None:
        payload = response.model_dump(mode="json")
        response_hash = _digest(payload)
        response_path = self._path(response_hash)
        if not response_path.exists():
            self._write(response_path, payload)
        elif self._read(response_path) != payload:
            raise GenerationCheckpointError("小纲待审响应证据损坏")
        self._write(self._path(request_hash), {"version": 1, "request_hash": request_hash,
            "status": "unverified", "response_hash": response_hash})

    def reject(self, request_hash: str) -> None:
        path = self._path(request_hash)
        data = self._read(path)
        if data is not None:
            self._write(path, {**data, "status": "rejected"})
