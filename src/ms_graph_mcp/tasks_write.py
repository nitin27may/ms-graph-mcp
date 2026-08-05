"""Task write tools for Microsoft To Do and Planner.

The two platforms behave very differently on writes, and the difference is the
whole difficulty of this module.

**To Do** is an ordinary REST resource: PATCH the task, done.

**Planner** versions every resource with an ETag and requires it back as
``If-Match`` on any write. A PATCH without one is refused, and a PATCH with a
stale one returns 412. So every Planner write here is read-then-write: fetch the
task to learn its current ETag, then send the change with that ETag. A 409/412
still means someone else changed it in between, which is reported as a
retryable conflict rather than silently overwritten.

Planner also answers PATCH with ``204 No Content`` unless asked otherwise, so
these send ``Prefer: return=representation`` to get the updated task back
instead of having to re-read it.

Planner **Premium** plans are not reachable through the Graph API at all — only
basic plans. That is a Microsoft limitation, not something to work around.
"""

from __future__ import annotations

from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_patch, graph_post
from ms_graph_mcp.errors import conflict, graph_error_response, invalid_arguments, not_found
from ms_graph_mcp.odata import validate_graph_id
from ms_graph_mcp.tooling import WRITE_CREATE, WRITE_UPDATE, tool

# Planner returns 204 on PATCH by default; this asks for the updated object.
_RETURN_REPRESENTATION = {"Prefer": "return=representation"}


class TodoStatus(StrEnum):
    not_started = "notStarted"
    in_progress = "inProgress"
    completed = "completed"
    waiting_on_others = "waitingOnOthers"
    deferred = "deferred"


class TaskImportance(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


def _slim_todo(t: dict) -> dict:
    return {
        "id": t.get("id", ""),
        "title": t.get("title", ""),
        "status": t.get("status", ""),
        "importance": t.get("importance", ""),
        "due": (t.get("dueDateTime") or {}).get("dateTime", ""),
        "completed_at": (t.get("completedDateTime") or {}).get("dateTime", ""),
    }


def _slim_planner(t: dict) -> dict:
    return {
        "id": t.get("id", ""),
        "title": t.get("title", ""),
        "plan_id": t.get("planId", ""),
        "bucket_id": t.get("bucketId", ""),
        "percent_complete": t.get("percentComplete", 0),
        "due": t.get("dueDateTime", ""),
        "assigned_to": list((t.get("assignments") or {}).keys()),
        "etag": t.get("@odata.etag", ""),
    }


async def _planner_etag(token: str, task_id: str) -> str | None:
    """Read a Planner task's current ETag.

    Returns ``None`` when the task cannot be read, which the caller reports as
    not-found rather than attempting a write that would fail anyway.
    """
    try:
        task = await graph_get(token, f"/planner/tasks/{task_id}")
    except httpx.HTTPStatusError:
        return None
    return task.get("@odata.etag")


async def _patch_planner_task(token: str, task_id: str, body: dict, tool_name: str) -> dict:
    """Read-then-write a Planner task, honouring its ETag.

    Shared by every Planner write because the sequence is identical and getting
    it wrong is silent: without ``If-Match`` Graph refuses, and with a stale one
    it returns 412 rather than clobbering a concurrent edit.
    """
    etag = await _planner_etag(token, task_id)
    if etag is None:
        return not_found(f"Planner task '{task_id}'")
    try:
        updated = await graph_patch(
            token,
            f"/planner/tasks/{task_id}",
            body,
            {"If-Match": etag, **_RETURN_REPRESENTATION},
        )
    except httpx.HTTPStatusError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (409, 412):
            return conflict(
                f"Planner task '{task_id}'",
                recovery=(
                    "Someone else edited it between reading and writing. Re-read the task and "
                    "apply the change again."
                ),
            )
        return graph_error_response(exc, scope="Tasks.ReadWrite", tool=tool_name)
    # A 204 leaves graph_patch returning {} — report success without pretending
    # to know the new state.
    return _slim_planner(updated) if updated else {"id": task_id, "status": "updated"}


# ── Microsoft To Do ───────────────────────────────────────────────────────────


class CompleteTodoInput(BaseModel):
    list_id: str = Field(description="The To Do list id containing the task")
    task_id: str = Field(description="The task id to mark complete")


class UpdateTodoInput(BaseModel):
    list_id: str = Field(description="The To Do list id containing the task")
    task_id: str = Field(description="The task id to change")
    title: str = Field(default="", description="New title. Omit to leave unchanged.")
    status: TodoStatus | None = Field(default=None, description="New status")
    importance: TaskImportance | None = Field(default=None, description="New importance")
    due_date: str = Field(default="", description="New due date, ISO 8601 (2026-08-20)")
    notes: str = Field(default="", description="Replacement notes")


@tool(
    description=(
        "Mark a Microsoft To Do task as done. Takes a list id from tasks_list_todo_lists and a "
        "task id from tasks_list_todo. This is the single most common task action and is separate "
        "from tasks_update_todo so it needs no other arguments. Affects the signed-in user's own "
        "private list, not a shared Planner board. Requires Tasks.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
)
async def tasks_complete_todo(params: CompleteTodoInput, context: dict) -> dict:
    token = context["access_token"]
    list_id = validate_graph_id(params.list_id, "list_id")
    task_id = validate_graph_id(params.task_id, "task_id")
    try:
        updated = await graph_patch(
            token,
            f"/me/todo/lists/{list_id}/tasks/{task_id}",
            {"status": TodoStatus.completed.value},
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Tasks.ReadWrite", tool="tasks_complete_todo")
    return _slim_todo(updated)


@tool(
    description=(
        "Change a Microsoft To Do task — its title, status, importance, due date or notes. Only "
        "the fields supplied are altered. Use tasks_complete_todo for the common case of simply "
        "marking something done. Status may be notStarted, inProgress, completed, waitingOnOthers "
        "or deferred. Requires Tasks.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
)
async def tasks_update_todo(params: UpdateTodoInput, context: dict) -> dict:
    token = context["access_token"]
    list_id = validate_graph_id(params.list_id, "list_id")
    task_id = validate_graph_id(params.task_id, "task_id")
    body: dict = {}
    if params.title:
        body["title"] = params.title
    if params.status is not None:
        body["status"] = params.status.value
    if params.importance is not None:
        body["importance"] = params.importance.value
    if params.due_date:
        body["dueDateTime"] = {"dateTime": f"{params.due_date}T00:00:00", "timeZone": "UTC"}
    if params.notes:
        body["body"] = {"content": params.notes, "contentType": "text"}
    if not body:
        return invalid_arguments("Nothing to update — supply at least one field to change.")
    try:
        updated = await graph_patch(token, f"/me/todo/lists/{list_id}/tasks/{task_id}", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Tasks.ReadWrite", tool="tasks_update_todo")
    return _slim_todo(updated)


# ── Planner ───────────────────────────────────────────────────────────────────


class CreatePlannerTaskInput(BaseModel):
    plan_id: str = Field(description="The plan to add the task to, from tasks_list_planner_plans")
    title: str = Field(description="Task title")
    bucket_id: str = Field(
        default="", description="Bucket (column) id from tasks_list_planner_buckets"
    )
    assigned_to_user_ids: list[str] = Field(
        default_factory=list,
        description="Entra user ids to assign. Use directory_search_users to resolve a name to an id.",
    )
    due_date: str = Field(default="", description="Due date, ISO 8601 (2026-08-20)")


class UpdatePlannerTaskInput(BaseModel):
    task_id: str = Field(description="The Planner task id to change")
    title: str = Field(default="", description="New title")
    percent_complete: int | None = Field(
        default=None, description="Progress 0-100. 0 is not started, 50 in progress, 100 complete."
    )
    bucket_id: str = Field(default="", description="Move the task to this bucket")
    due_date: str = Field(default="", description="New due date, ISO 8601")


class CompletePlannerTaskInput(BaseModel):
    task_id: str = Field(description="The Planner task id to mark complete")


@tool(
    description=(
        "Create a task on a shared Planner board. Takes a plan id from tasks_list_planner_plans, "
        "a title, and optionally a bucket, a due date and Entra user ids to assign it to. Planner "
        "is for team work; tasks_create_todo adds to the user's own private list instead. Premium "
        "plans are not reachable through the Graph API. Requires Tasks.ReadWrite."
    ),
    annotations=WRITE_CREATE,
)
async def tasks_create_planner(params: CreatePlannerTaskInput, context: dict) -> dict:
    token = context["access_token"]
    body: dict = {
        "planId": validate_graph_id(params.plan_id, "plan_id"),
        "title": params.title,
    }
    if params.bucket_id:
        body["bucketId"] = validate_graph_id(params.bucket_id, "bucket_id")
    if params.due_date:
        body["dueDateTime"] = f"{params.due_date}T00:00:00Z"
    if params.assigned_to_user_ids:
        # Planner assignments are an open dict keyed by user id, and each value
        # must carry the @odata.type or Graph rejects the whole request.
        body["assignments"] = {
            validate_graph_id(uid, "assigned_to_user_ids"): {
                "@odata.type": "#microsoft.graph.plannerAssignment",
                "orderHint": " !",
            }
            for uid in params.assigned_to_user_ids
        }
    try:
        created = await graph_post(token, "/planner/tasks", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Tasks.ReadWrite", tool="tasks_create_planner")
    return _slim_planner(created)


@tool(
    description=(
        "Change a task on a Planner board — its title, progress percentage, bucket or due date. "
        "Only the fields supplied are altered. Reads the task first to obtain the ETag Planner "
        "requires, so a concurrent edit is reported as a conflict rather than silently "
        "overwritten. Requires Tasks.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
)
async def tasks_update_planner(params: UpdatePlannerTaskInput, context: dict) -> dict:
    token = context["access_token"]
    task_id = validate_graph_id(params.task_id, "task_id")
    if params.percent_complete is not None and not 0 <= params.percent_complete <= 100:
        return invalid_arguments("percent_complete must be between 0 and 100.")
    body: dict = {}
    if params.title:
        body["title"] = params.title
    if params.percent_complete is not None:
        body["percentComplete"] = params.percent_complete
    if params.bucket_id:
        body["bucketId"] = validate_graph_id(params.bucket_id, "bucket_id")
    if params.due_date:
        body["dueDateTime"] = f"{params.due_date}T00:00:00Z"
    if not body:
        return invalid_arguments("Nothing to update — supply at least one field to change.")
    return await _patch_planner_task(token, task_id, body, "tasks_update_planner")


@tool(
    description=(
        "Mark a Planner task as done by setting its progress to 100 percent, which is how Planner "
        "represents completion. Takes a task id from tasks_list_planner_tasks. Separate from "
        "tasks_update_planner so the most common action needs no other arguments. Handles the "
        "ETag Planner requires on writes. Requires Tasks.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
)
async def tasks_complete_planner(params: CompletePlannerTaskInput, context: dict) -> dict:
    token = context["access_token"]
    task_id = validate_graph_id(params.task_id, "task_id")
    return await _patch_planner_task(
        token, task_id, {"percentComplete": 100}, "tasks_complete_planner"
    )


__all__ = [
    "tasks_complete_planner",
    "tasks_complete_todo",
    "tasks_create_planner",
    "tasks_update_planner",
    "tasks_update_todo",
]
