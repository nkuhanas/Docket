import importlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from docket.domain.errors import DocketError

router_module = importlib.import_module("docket.internal_api.router")


def _request(order: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                wake_discord_projection=lambda: order.append("wake"),
            )
        )
    )


def test_approval_wakes_projection_only_after_commit(monkeypatch) -> None:
    order: list[str] = []

    @contextmanager
    def fake_session_scope():
        order.append("begin")
        yield object()
        order.append("commit")

    class FakeApprovalService:
        def __init__(self, _session: object) -> None:
            pass

        def respond(self, _payload: object) -> dict[str, object]:
            order.append("respond")
            return {"ok": True}

    monkeypatch.setattr(router_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(router_module, "ApprovalService", FakeApprovalService)

    result = router_module.approval_response(_request(order), object())

    assert result == {"ok": True}
    assert order == ["begin", "respond", "commit", "wake"]


def test_rejected_approval_does_not_wake_projection(monkeypatch) -> None:
    order: list[str] = []

    @contextmanager
    def fake_session_scope():
        order.append("begin")
        yield object()
        order.append("commit")

    class RejectingApprovalService:
        def __init__(self, _session: object) -> None:
            pass

        def respond(self, _payload: object) -> dict[str, object]:
            order.append("respond")
            raise DocketError(code="approval_not_found", message="Missing")

    monkeypatch.setattr(router_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(router_module, "ApprovalService", RejectingApprovalService)

    with pytest.raises(HTTPException) as rejected:
        router_module.approval_response(_request(order), object())

    assert rejected.value.status_code == 404
    assert order == ["begin", "respond", "commit"]


def test_rolled_back_approval_does_not_wake_projection(monkeypatch) -> None:
    order: list[str] = []

    @contextmanager
    def fake_session_scope():
        order.append("begin")
        try:
            yield object()
        except Exception:
            order.append("rollback")
            raise
        order.append("commit")

    class FailingApprovalService:
        def __init__(self, _session: object) -> None:
            pass

        def respond(self, _payload: object) -> dict[str, object]:
            order.append("respond")
            raise RuntimeError("transaction failed")

    monkeypatch.setattr(router_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(router_module, "ApprovalService", FailingApprovalService)

    with pytest.raises(RuntimeError, match="transaction failed"):
        router_module.approval_response(_request(order), object())

    assert order == ["begin", "respond", "rollback"]


def test_local_action_wakes_projection_only_after_commit(monkeypatch) -> None:
    order: list[str] = []

    @contextmanager
    def fake_session_scope():
        order.append("begin")
        yield object()
        order.append("commit")

    class FakeLocalActionService:
        def __init__(self, _session: object) -> None:
            pass

        def respond(self, _payload: object) -> dict[str, object]:
            order.append("respond")
            return {"ok": True}

    monkeypatch.setattr(router_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(router_module, "LocalActionService", FakeLocalActionService)

    payload = SimpleNamespace(transition="local_action")
    result = router_module.local_action_response(_request(order), payload)

    assert result == {"ok": True}
    assert order == ["begin", "respond", "commit", "wake"]


def test_proposal_navigation_wakes_projection_only_after_commit(monkeypatch) -> None:
    order: list[str] = []

    @contextmanager
    def fake_session_scope():
        order.append("begin")
        yield object()
        order.append("commit")

    class FakeProposalControlService:
        def __init__(self, _session: object) -> None:
            pass

        def respond(
            self,
            _payload: object,
            *,
            refresh_started_at: object,
        ) -> dict[str, object]:
            assert refresh_started_at is None
            order.append("respond")
            return {"ok": True}

    monkeypatch.setattr(router_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        router_module,
        "ProposalControlService",
        FakeProposalControlService,
    )

    payload = SimpleNamespace(transition="proposal_review_navigate")
    result = router_module.local_action_response(_request(order), payload)

    assert result == {"ok": True}
    assert order == ["begin", "respond", "commit", "wake"]


def test_wake_failure_does_not_change_committed_response(monkeypatch) -> None:
    order: list[str] = []

    @contextmanager
    def fake_session_scope():
        order.append("begin")
        yield object()
        order.append("commit")

    class FakeApprovalService:
        def __init__(self, _session: object) -> None:
            pass

        def respond(self, _payload: object) -> dict[str, object]:
            order.append("respond")
            return {"ok": True}

    def failed_wake() -> None:
        order.append("wake")
        raise RuntimeError("lost local wake")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(wake_discord_projection=failed_wake))
    )
    monkeypatch.setattr(router_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(router_module, "ApprovalService", FakeApprovalService)

    assert router_module.approval_response(request, object()) == {"ok": True}
    assert order == ["begin", "respond", "commit", "wake"]
