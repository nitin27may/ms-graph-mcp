"""Graph People tools — people search, profiles, and current user info."""

from __future__ import annotations

import urllib.parse

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.tooling import READ_ONLY, tool

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
    description=(
        "Find colleagues the signed-in user actually works with, ranked by relevance from their own "
        "mail and meeting history — so it finds the right 'Priya' without exact spelling. "
        "Returns name, email, title and department. Use directory_search_users to search the "
        "whole tenant instead, or people_list_contacts for the saved address book. "
        "Requires People.Read."
    ),
    annotations=READ_ONLY,
    aliases=("search_people",),
)
async def people_search(params: SearchPeopleInput, context: dict) -> list[dict]:
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
    description=(
        "Look up one person in the organisation by their exact email address and return their "
        "profile: display name, job title, department, office location and phone numbers. Use when "
        "the address is already known — people_search is the tool for finding someone by partial "
        "or approximate name. Fails if the address does not belong to a tenant account. "
        "Requires User.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_person_details",),
)
async def people_get(params: GetPersonDetailsInput, context: dict) -> dict:
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


@tool(
    description=(
        "Get the signed-in user's own profile — their name, email address, job title, department "
        "and office. Call this first when a request says 'me', 'my' or 'I' and the user's own "
        "identity or email address is needed to answer it, for example before filtering a calendar "
        "or addressing a message. Takes no arguments. Requires User.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_my_profile",),
)
async def people_get_my_profile(params: GetMyProfileInput, context: dict) -> dict:
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
