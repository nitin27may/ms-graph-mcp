"""Graph Tasks tools — Microsoft To Do and Planner task management."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post
from ms_graph_mcp.odata import validate_task_status
from ms_graph_mcp.tooling import tool


class GetPlannerPlansInput(BaseModel):
    max_results: int = Field(20, description="Maximum plans to return")


class GetPlannerBucketsInput(BaseModel):
    plan_id: str = Field(description="The Planner plan ID to list buckets for")


class GetTodoListsInput(BaseModel):
    max_results: int = Field(20, description="Maximum lists to return")


class CreateTodoTaskInput(BaseModel):
    title: str = Field(description="Task title")
    due_date: str | None = Field(None, description="Due date in ISO 8601 format (e.g. 2024-01-20)")
    notes: str | None = Field(None, description="Additional notes or description")
    importance: str = Field("normal", description="Task importance: low | normal | high")
    list_id: str | None = Field(
        None, description="To Do list ID. If omitted, uses the default Tasks list."
    )


class GetTodoTasksInput(BaseModel):
    list_id: str | None = Field(
        None, description="Specific list ID. If omitted, returns tasks across all lists."
    )
    status: str = Field(
        "notStarted", description="Filter by status: notStarted | inProgress | completed | all"
    )
    max_results: int = Field(20, description="Maximum tasks to return")


class GetPlannerTasksInput(BaseModel):
    plan_id: str = Field(description="The Planner plan ID to fetch tasks from")
    max_results: int = Field(25, description="Maximum tasks to return")


@tool(description="Get the user's Planner plans. Returns a list of plans with id and title.")
async def get_planner_plans(params: GetPlannerPlansInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/planner/plans",
        **{"$select": "id,title", "$top": min(params.max_results, 50)},
    )
    return [{"id": p.get("id", ""), "title": p.get("title", "")} for p in (data.get("value") or [])]


@tool(description="Get buckets in a Planner plan. Returns a list of buckets with id and name.")
async def get_planner_buckets(params: GetPlannerBucketsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/planner/plans/{params.plan_id}/buckets",
        **{"$select": "id,name"},
    )
    return [{"id": b.get("id", ""), "name": b.get("name", "")} for b in (data.get("value") or [])]


@tool(
    description="Get the user's Microsoft To Do lists. Returns a list of lists with id and displayName."
)
async def get_todo_lists(params: GetTodoListsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/todo/lists",
        **{"$select": "id,displayName", "$top": min(params.max_results, 50)},
    )
    return [
        {"id": lst.get("id", ""), "displayName": lst.get("displayName", "")}
        for lst in (data.get("value") or [])
    ]


@tool(description="Create a task in Microsoft To Do. Returns the created task ID.")
async def create_todo_task(params: CreateTodoTaskInput, context: dict) -> dict:
    token = context["access_token"]

    # Find or use default task list
    list_id = params.list_id
    if not list_id:
        lists = await graph_get(
            token, "/me/todo/lists", **{"$filter": "wellknownListName eq 'defaultList'"}
        )
        items = lists.get("value") or []
        list_id = items[0]["id"] if items else None

    if not list_id:
        return {"error": "Could not find default To Do list"}

    body: dict = {"title": params.title, "importance": params.importance}
    if params.due_date:
        body["dueDateTime"] = {"dateTime": f"{params.due_date}T00:00:00.0000000", "timeZone": "UTC"}
    if params.notes:
        body["body"] = {"content": params.notes, "contentType": "text"}

    task = await graph_post(token, f"/me/todo/lists/{list_id}/tasks", body)
    return {"id": task.get("id", ""), "title": task.get("title", ""), "list_id": list_id}


@tool(description="Get Microsoft To Do tasks. Optionally filter by list and status.")
async def get_todo_tasks(params: GetTodoTasksInput, context: dict) -> list[dict]:
    token = context["access_token"]

    if params.list_id:
        list_ids = [params.list_id]
    else:
        lists_data = await graph_get(token, "/me/todo/lists", **{"$select": "id,displayName"})
        list_ids = [lst["id"] for lst in (lists_data.get("value") or [])]

    all_tasks: list[dict] = []
    for lid in list_ids:
        status = validate_task_status(params.status)
        filt = "" if status == "all" else f"status eq '{status}'"
        kwargs: dict = {
            "$select": "id,title,status,importance,dueDateTime,createdDateTime,body",
            "$top": min(params.max_results, 100),
        }
        if filt:
            kwargs["$filter"] = filt
        data = await graph_get(token, f"/me/todo/lists/{lid}/tasks", **kwargs)
        for t in data.get("value") or []:
            all_tasks.append(
                {
                    "id": t.get("id", ""),
                    "title": t.get("title", ""),
                    "status": t.get("status", ""),
                    "importance": t.get("importance", "normal"),
                    "due": (t.get("dueDateTime") or {}).get("dateTime", ""),
                    "created": t.get("createdDateTime", ""),
                    "list_id": lid,
                }
            )
        if len(all_tasks) >= params.max_results:
            break

    return all_tasks[: params.max_results]


@tool(description="Get tasks from a Microsoft Planner plan.")
async def get_planner_tasks(params: GetPlannerTasksInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/planner/plans/{params.plan_id}/tasks",
        **{
            "$select": "id,title,percentComplete,dueDateTime,assignments,bucketId,createdDateTime",
            "$top": min(params.max_results, 50),
        },
    )
    return [
        {
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "percent_complete": t.get("percentComplete", 0),
            "due": t.get("dueDateTime", ""),
            "bucket_id": t.get("bucketId", ""),
            "created": t.get("createdDateTime", ""),
        }
        for t in (data.get("value") or [])
    ]
