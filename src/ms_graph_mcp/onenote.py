"""Graph OneNote tools — notebooks, sections, and page creation."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post_raw
from ms_graph_mcp.tooling import READ_ONLY, WRITE_CREATE, tool


class GetNotebooksInput(BaseModel):
    max_results: int = Field(10, description="Maximum notebooks to return")


class GetSectionsInput(BaseModel):
    notebook_id: str = Field(description="The notebook ID to list sections for")


class SaveToOnenoteInput(BaseModel):
    notebook_id: str = Field(description="Target notebook ID")
    section_id: str = Field(description="Target section ID")
    page_title: str = Field(description="Title of the new OneNote page")
    content_html: str = Field(description="HTML content for the page body")


@tool(
    description=(
        "List the signed-in user's OneNote notebooks, with id, name, last-modified date and web "
        "URL. Call this first when saving or finding a note — a notebook id is what "
        "notes_list_sections takes, and a section id from there is what notes_create_page needs. "
        "Covers notebooks the user owns and ones shared with them. Requires Notes.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_notebooks",),
)
async def notes_list_notebooks(params: GetNotebooksInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/onenote/notebooks",
        **{"$select": "id,displayName,lastModifiedDateTime,links", "$top": params.max_results},
    )
    return [
        {
            "id": nb.get("id", ""),
            "name": nb.get("displayName", ""),
            "last_modified": nb.get("lastModifiedDateTime", ""),
            "url": (nb.get("links") or {}).get("oneNoteWebUrl", {}).get("href", ""),
        }
        for nb in (data.get("value") or [])
    ]


@tool(
    description=(
        "List the sections inside one OneNote notebook, with id, name and last-modified date. "
        "Takes a notebook id from notes_list_notebooks. Sections are the tabs within a notebook "
        "and are what pages actually live in, so a section id from here is required before "
        "notes_create_page can write anything. Requires Notes.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_sections",),
)
async def notes_list_sections(params: GetSectionsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/me/onenote/notebooks/{params.notebook_id}/sections",
        **{"$select": "id,displayName,lastModifiedDateTime"},
    )
    return [
        {
            "id": s.get("id", ""),
            "name": s.get("displayName", ""),
            "last_modified": s.get("lastModifiedDateTime", ""),
        }
        for s in (data.get("value") or [])
    ]


async def create_onenote_page(
    token: str, *, section_id: str, page_title: str, content_html: str
) -> dict:
    """Create a OneNote page in a section and return the RAW Graph page object.

    The single OneNote page-create HTTP — callers (the ``save_to_onenote`` tool,
    the action-sink, REST routes) all go through here instead of hand-rolling the
    POST, so the content-type (text/html) and page wrapper stay consistent.
    """
    html_body = f"""<!DOCTYPE html>
<html>
<head><title>{page_title}</title></head>
<body>{content_html}</body>
</html>"""

    return await graph_post_raw(
        token,
        f"/me/onenote/sections/{section_id}/pages",
        html_body.encode("utf-8"),
        "text/html",
    )


@tool(
    description=(
        "Create a new page in a OneNote section and return its id and web URL. Takes a section id "
        "from notes_list_sections, a title, and the body as valid HTML — plain text will not "
        "render correctly. Use for saving meeting notes, summaries or research into the user's "
        "notebooks. Always adds a new page; it cannot edit an existing one. "
        "Requires Notes.Create."
    ),
    annotations=WRITE_CREATE,
    aliases=("save_to_onenote",),
)
async def notes_create_page(params: SaveToOnenoteInput, context: dict) -> dict:
    data = await create_onenote_page(
        context["access_token"],
        section_id=params.section_id,
        page_title=params.page_title,
        content_html=params.content_html,
    )
    return {
        "id": data.get("id", ""),
        "title": data.get("title", ""),
        "url": (data.get("links") or {}).get("oneNoteWebUrl", {}).get("href", ""),
        "created_at": data.get("createdDateTime", ""),
    }
