"""Graph People tools — people search, profiles, and current user info."""

from __future__ import annotations

import urllib.parse

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.tooling import tool

_SELECT_PERSON = "id,displayName,givenName,surname,emailAddresses,jobTitle,department,officeLocation,scoredEmailAddresses"
_SELECT_USER = "id,displayName,givenName,surname,mail,jobTitle,department,officeLocation,mobilePhone,businessPhones"


class SearchPeopleInput(BaseModel):
    query: str = Field(description="Name or keyword to search for people in the org")
    max_results: int = Field(10, description="Maximum people to return")


class GetPersonDetailsInput(BaseModel):
    email: str = Field(description="The person's email address")


class GetMyProfileInput(BaseModel):
    pass  # No parameters needed


@tool(
    description="Search for people in the organisation by name or keyword. Returns display name, email, title, and department."
)
async def search_people(params: SearchPeopleInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/people",
        **{
            "$search": params.query,
            "$select": _SELECT_PERSON,
            "$top": min(params.max_results, 20),
        },
    )
    return [_slim_person(p) for p in (data.get("value") or [])]


@tool(
    description="Get details about a specific person by their email address — title, department, phone."
)
async def get_person_details(params: GetPersonDetailsInput, context: dict) -> dict:
    token = context["access_token"]
    # Use the users endpoint for direct lookup
    data = await graph_get(
        token,
        f"/users/{urllib.parse.quote(params.email, safe='@.')}",
        **{"$select": _SELECT_USER},
    )
    return {
        "id": data.get("id", ""),
        "display_name": data.get("displayName", ""),
        "given_name": data.get("givenName", ""),
        "surname": data.get("surname", ""),
        "email": data.get("mail", ""),
        "job_title": data.get("jobTitle", ""),
        "department": data.get("department", ""),
        "office": data.get("officeLocation", ""),
        "mobile": data.get("mobilePhone", ""),
        "phone": (data.get("businessPhones") or [None])[0],
    }


@tool(description="Get the currently authenticated user's own profile information.")
async def get_my_profile(params: GetMyProfileInput, context: dict) -> dict:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me",
        **{"$select": _SELECT_USER},
    )
    return {
        "id": data.get("id", ""),
        "display_name": data.get("displayName", ""),
        "email": data.get("mail", ""),
        "job_title": data.get("jobTitle", ""),
        "department": data.get("department", ""),
        "office": data.get("officeLocation", ""),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slim_person(p: dict) -> dict:
    emails = p.get("scoredEmailAddresses") or p.get("emailAddresses") or []
    email = emails[0].get("address", "") if emails else ""
    return {
        "id": p.get("id", ""),
        "name": p.get("displayName", ""),
        "email": email,
        "job_title": p.get("jobTitle", ""),
        "department": p.get("department", ""),
        "office": p.get("officeLocation", ""),
    }
