"""Graph OneNote tools — notebooks, sections, and page creation."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.config import get_config
from ms_graph_mcp.tooling import tool


class GetNotebooksInput(BaseModel):
    max_results: int = Field(10, description="Maximum notebooks to return")


class GetSectionsInput(BaseModel):
    notebook_id: str = Field(description="The notebook ID to list sections for")


class SaveToOnenoteInput(BaseModel):
    notebook_id: str = Field(description="Target notebook ID")
    section_id: str = Field(description="Target section ID")
    page_title: str = Field(description="Title of the new OneNote page")
    content_html: str = Field(description="HTML content for the page body")


@tool(description="Get the user's OneNote notebooks.")
async def get_notebooks(params: GetNotebooksInput, context: dict) -> list[dict]:
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


@tool(description="Get sections in a OneNote notebook.")
async def get_sections(params: GetSectionsInput, context: dict) -> list[dict]:
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

    import httpx

    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        resp = await client.post(
            f"https://graph.microsoft.com/v1.0/me/onenote/sections/{section_id}/pages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/html",
            },
            content=html_body.encode("utf-8"),
        )
        resp.raise_for_status()
        return resp.json()


@tool(
    description="Save content as a new page in a OneNote notebook section. Content must be valid HTML."
)
async def save_to_onenote(params: SaveToOnenoteInput, context: dict) -> dict:
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
