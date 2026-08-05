"""To Do and Planner write tools.

Planner is the reason this file is long. Every write needs the task's current
ETag as ``If-Match``, so each tool is read-then-write, and the failure modes are
easy to get silently wrong:

  * no ETag sent -> Graph refuses the write outright
  * stale ETag   -> 412, meaning somebody edited it in between
  * PATCH        -> 204 with no body unless representation is requested

To Do has none of that and is tested more lightly.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.tasks_write import (
    CompletePlannerTaskInput,
    CompleteTodoInput,
    CreatePlannerTaskInput,
    TaskImportance,
    TodoStatus,
    UpdatePlannerTaskInput,
    UpdateTodoInput,
    tasks_complete_planner,
    tasks_complete_todo,
    tasks_create_planner,
    tasks_update_planner,
    tasks_update_todo,
)

_CTX = {"access_token": "tok"}
_ETAG = 'W/"JzEtVGFzayAgQEBAQEBAQEBAQEBAQEBAWCc="'


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://graph.microsoft.com/v1.0/planner/tasks/t1")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )


# ── Microsoft To Do ───────────────────────────────────────────────────────────


async def test_complete_todo_sets_status_only():
    with patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p:
        p.return_value = {"id": "t1", "title": "Ship it", "status": "completed"}
        result = await tasks_complete_todo(CompleteTodoInput(list_id="l1", task_id="t1"), _CTX)

    token, path, body = p.call_args.args[0], p.call_args.args[1], p.call_args.args[2]
    assert token == "tok"
    assert path == "/me/todo/lists/l1/tasks/t1"
    assert body == {"status": "completed"}
    assert result["status"] == "completed"


async def test_update_todo_sends_only_supplied_fields():
    with patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p:
        p.return_value = {"id": "t1"}
        await tasks_update_todo(
            UpdateTodoInput(
                list_id="l1", task_id="t1", title="New", importance=TaskImportance.high
            ),
            _CTX,
        )
    assert p.call_args.args[2] == {"title": "New", "importance": "high"}


async def test_update_todo_wraps_a_due_date_in_graphs_datetime_shape():
    with patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p:
        p.return_value = {}
        await tasks_update_todo(
            UpdateTodoInput(list_id="l1", task_id="t1", due_date="2026-08-20"), _CTX
        )
    assert p.call_args.args[2]["dueDateTime"] == {
        "dateTime": "2026-08-20T00:00:00",
        "timeZone": "UTC",
    }


async def test_update_todo_accepts_every_status_graph_defines():
    """notStarted/inProgress/completed are obvious; the other two are not."""
    for status in TodoStatus:
        with patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p:
            p.return_value = {}
            await tasks_update_todo(
                UpdateTodoInput(list_id="l1", task_id="t1", status=status), _CTX
            )
        assert p.call_args.args[2]["status"] == status.value


async def test_update_todo_rejects_an_empty_change():
    with patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p:
        result = await tasks_update_todo(UpdateTodoInput(list_id="l1", task_id="t1"), _CTX)
    p.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


async def test_todo_tools_reject_injected_ids():
    with pytest.raises(ValueError):
        await tasks_complete_todo(CompleteTodoInput(list_id="../../me", task_id="t1"), _CTX)


# ── Planner: the ETag dance ───────────────────────────────────────────────────


async def test_planner_update_reads_the_etag_then_writes_with_it():
    """Without If-Match, Graph refuses the write. This is the core behaviour."""
    with (
        patch(
            "ms_graph_mcp.tasks_write.graph_get",
            new=AsyncMock(return_value={"id": "t1", "@odata.etag": _ETAG}),
        ) as get,
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p,
    ):
        p.return_value = {"id": "t1", "title": "New", "percentComplete": 50}
        await tasks_update_planner(UpdatePlannerTaskInput(task_id="t1", title="New"), _CTX)

    assert get.call_args.args[1] == "/planner/tasks/t1", "must read the task to learn its ETag"
    headers = p.call_args.args[3]
    assert headers["If-Match"] == _ETAG
    # Planner answers 204 without this, which would lose the updated task.
    assert headers["Prefer"] == "return=representation"


async def test_planner_stale_etag_is_reported_as_a_retryable_conflict():
    """412 means somebody edited it between our read and our write."""
    with (
        patch(
            "ms_graph_mcp.tasks_write.graph_get",
            new=AsyncMock(return_value={"@odata.etag": _ETAG}),
        ),
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p,
    ):
        p.side_effect = _http_error(412)
        result = await tasks_update_planner(UpdatePlannerTaskInput(task_id="t1", title="x"), _CTX)

    assert result["error"] == "CONFLICT"
    assert result["retryable"] is True
    assert "Re-read" in result["message"] or "re-read" in result["message"]


async def test_planner_409_is_also_a_conflict():
    with (
        patch(
            "ms_graph_mcp.tasks_write.graph_get",
            new=AsyncMock(return_value={"@odata.etag": _ETAG}),
        ),
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p,
    ):
        p.side_effect = _http_error(409)
        result = await tasks_update_planner(UpdatePlannerTaskInput(task_id="t1", title="x"), _CTX)
    assert result["error"] == "CONFLICT"


async def test_planner_update_reports_not_found_when_the_read_fails():
    """No point attempting a write whose ETag we could never obtain."""
    with (
        patch("ms_graph_mcp.tasks_write.graph_get", new=AsyncMock()) as get,
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p,
    ):
        get.side_effect = _http_error(404)
        result = await tasks_update_planner(UpdatePlannerTaskInput(task_id="t1", title="x"), _CTX)
    p.assert_not_called()
    assert result["error"] == "NOT_FOUND"
    assert result["retryable"] is False


async def test_planner_403_still_reports_the_scope():
    with (
        patch(
            "ms_graph_mcp.tasks_write.graph_get",
            new=AsyncMock(return_value={"@odata.etag": _ETAG}),
        ),
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p,
    ):
        p.side_effect = _http_error(403)
        result = await tasks_update_planner(UpdatePlannerTaskInput(task_id="t1", title="x"), _CTX)
    assert result["error"] == "SCOPE_DENIED"
    assert result["scope"] == "Tasks.ReadWrite"


async def test_planner_204_response_still_reports_success():
    """graph_patch returns {} on 204; the tool must not present that as a task."""
    with (
        patch(
            "ms_graph_mcp.tasks_write.graph_get",
            new=AsyncMock(return_value={"@odata.etag": _ETAG}),
        ),
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock(return_value={})),
    ):
        result = await tasks_complete_planner(CompletePlannerTaskInput(task_id="t1"), _CTX)
    assert result == {"id": "t1", "status": "updated"}


async def test_complete_planner_sets_percent_complete_to_100():
    """Planner has no 'done' flag — 100% is how completion is represented."""
    with (
        patch(
            "ms_graph_mcp.tasks_write.graph_get",
            new=AsyncMock(return_value={"@odata.etag": _ETAG}),
        ),
        patch("ms_graph_mcp.tasks_write.graph_patch", new=AsyncMock()) as p,
    ):
        p.return_value = {"id": "t1", "percentComplete": 100}
        await tasks_complete_planner(CompletePlannerTaskInput(task_id="t1"), _CTX)
    assert p.call_args.args[2] == {"percentComplete": 100}


async def test_planner_update_validates_the_percentage():
    with patch("ms_graph_mcp.tasks_write.graph_get", new=AsyncMock()) as get:
        result = await tasks_update_planner(
            UpdatePlannerTaskInput(task_id="t1", percent_complete=150), _CTX
        )
    get.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


async def test_planner_update_rejects_an_empty_change():
    with patch("ms_graph_mcp.tasks_write.graph_get", new=AsyncMock()) as get:
        result = await tasks_update_planner(UpdatePlannerTaskInput(task_id="t1"), _CTX)
    get.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


# ── Planner create ────────────────────────────────────────────────────────────


async def test_create_planner_builds_the_assignment_open_dict():
    """Assignments are keyed by user id and each value needs @odata.type.

    Omitting the type makes Graph reject the whole request, and the error does
    not say which field was at fault.
    """
    with patch("ms_graph_mcp.tasks_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {"id": "t1", "title": "Review"}
        await tasks_create_planner(
            CreatePlannerTaskInput(
                plan_id="p1",
                title="Review",
                bucket_id="b1",
                assigned_to_user_ids=["user-a", "user-b"],
                due_date="2026-08-20",
            ),
            _CTX,
        )

    body = post.call_args.args[2]
    assert body["planId"] == "p1"
    assert body["bucketId"] == "b1"
    assert body["dueDateTime"] == "2026-08-20T00:00:00Z"
    assert set(body["assignments"]) == {"user-a", "user-b"}
    for assignment in body["assignments"].values():
        assert assignment["@odata.type"] == "#microsoft.graph.plannerAssignment"
        assert assignment["orderHint"]


async def test_create_planner_omits_optional_fields_entirely():
    with patch("ms_graph_mcp.tasks_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {}
        await tasks_create_planner(CreatePlannerTaskInput(plan_id="p1", title="T"), _CTX)
    body = post.call_args.args[2]
    assert body == {"planId": "p1", "title": "T"}


async def test_create_planner_surfaces_the_etag_for_a_later_update():
    """Returning it saves the caller a read before their next write."""
    with patch(
        "ms_graph_mcp.tasks_write.graph_post",
        new=AsyncMock(return_value={"id": "t1", "@odata.etag": _ETAG}),
    ):
        result = await tasks_create_planner(CreatePlannerTaskInput(plan_id="p1", title="T"), _CTX)
    assert result["etag"] == _ETAG


# ── tier placement ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "tasks_complete_todo",
        "tasks_update_todo",
        "tasks_create_planner",
        "tasks_update_planner",
        "tasks_complete_planner",
    ],
)
async def test_task_writes_are_refused_without_scope(name, call_tool):
    cv = current_request_context.set({"access_token": "tok", "write_scope": False})
    try:
        result = await call_tool(name, {})
    finally:
        current_request_context.reset(cv)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "write_scope_required"


def test_no_task_write_is_marked_destructive():
    """Completing or editing a task is reversible; none of these destroy data."""
    from ms_graph_mcp.tooling import get_registry

    registry = get_registry()
    for name in (
        "tasks_complete_todo",
        "tasks_update_todo",
        "tasks_create_planner",
        "tasks_update_planner",
        "tasks_complete_planner",
    ):
        assert registry.get(name).annotations.destructive is False
