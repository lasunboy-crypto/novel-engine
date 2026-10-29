"""G2(v1.1 wave 4 session 2):PlotThread.thread_type 字段 + store 往返单测。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.db.models import Base
from src.core.memory.store import create_plot_thread, get_plot_thread
from src.core.models.plot_thread import PlotThread


@pytest.fixture
def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_thread(
    *, thread_id: str = "thread-1", thread_type: str | None = None
) -> PlotThread:
    kwargs: dict[str, object] = dict(
        thread_id=thread_id,
        workspace_id="ws-1",
        title="暴雨象征",
        status="planted",
        importance=4,
        planted_chapter="ch-0003",
        description="第 3 章窗外暴雨象征主角内心失落。",
        created_at=datetime.now(timezone.utc),
    )
    if thread_type is not None:
        kwargs["thread_type"] = thread_type
    return PlotThread(**kwargs)


def test_thread_type_defaults_plot_event() -> None:
    assert _make_thread().thread_type == "plot_event"


def test_thread_type_explicit_value() -> None:
    assert _make_thread(thread_type="metaphor").thread_type == "metaphor"


def test_thread_type_invalid_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_thread(thread_type="not_a_valid_type")


def test_store_roundtrip_preserves_thread_type(_session: Session) -> None:
    create_plot_thread(_session, _make_thread(thread_type="foreshadowing"))
    _session.commit()
    fetched = get_plot_thread(_session, "thread-1")
    assert fetched is not None
    assert fetched.thread_type == "foreshadowing"


def test_store_roundtrip_default_thread_type(_session: Session) -> None:
    create_plot_thread(_session, _make_thread())
    _session.commit()
    fetched = get_plot_thread(_session, "thread-1")
    assert fetched is not None
    assert fetched.thread_type == "plot_event"
