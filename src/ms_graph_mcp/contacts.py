"""The signed-in user's personal address book — ``/me/contacts``.

**Not the same as ``people.py``, and the distinction matters enough that the
tool descriptions have to state it.** Three tools in this package look similar
and read three different sources:

  ``people_search``            ``/me/people``   relevance-ranked colleagues,
                                                derived from the user's own mail
                                                and meeting history
  ``directory_search_users``   ``/users``       every account in the Entra tenant
  ``people_list_contacts``     ``/me/contacts`` the user's own saved address book

Only the last one holds external contacts — a supplier, a client, a plumber —
who have no tenant account at all and are therefore invisible to the other two.
"What is Jane's mobile number" usually only works here.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post
from ms_graph_mcp.errors import graph_error_response, invalid_arguments
from ms_graph_mcp.odata import escape_odata_string
from ms_graph_mcp.tooling import READ_ONLY, WRITE_CREATE, tool

_CONTACT_SELECT = (
    "id,displayName,givenName,surname,emailAddresses,businessPhones,mobilePhone,"
    "companyName,jobTitle"
)


class ListContactsInput(BaseModel):
    max_results: int = Field(50, description="Maximum contacts to return (1-100)")


class SearchContactsInput(BaseModel):
    query: str = Field(description="Name or company to look for in the address book")
    max_results: int = Field(20, description="Maximum contacts to return (1-100)")


class CreateContactInput(BaseModel):
    given_name: str = Field(description="First name")
    surname: str = Field(default="", description="Last name")
    email: str = Field(default="", description="Primary email address")
    mobile_phone: str = Field(default="", description="Mobile number")
    business_phone: str = Field(default="", description="Work number")
    company: str = Field(default="", description="Company name")
    job_title: str = Field(default="", description="Job title")


def _slim_contact(c: dict) -> dict:
    emails = c.get("emailAddresses") or []
    return {
        "id": c.get("id", ""),
        "name": c.get("displayName", ""),
        "email": emails[0].get("address", "") if emails else "",
        "all_emails": [e.get("address", "") for e in emails],
        "mobile": c.get("mobilePhone", "") or "",
        "business_phone": (c.get("businessPhones") or [""])[0],
        "company": c.get("companyName", ""),
        "job_title": c.get("jobTitle", ""),
    }


@tool(
    description=(
        "List the signed-in user's saved Outlook contacts, with names, email addresses and phone "
        "numbers. This is the user's personal address book and is the only place external "
        "contacts live — people with no account in the tenant, invisible to people_search and "
        "directory_search_users. Use it for phone numbers. Requires Contacts.Read."
    ),
    annotations=READ_ONLY,
)
async def people_list_contacts(params: ListContactsInput, context: dict) -> list[dict] | dict:
    token = context["access_token"]
    try:
        data = await graph_get(
            token,
            "/me/contacts",
            **{
                "$select": _CONTACT_SELECT,
                "$top": min(max(params.max_results, 1), 100),
                "$orderby": "displayName",
            },
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Contacts.Read", tool="people_list_contacts")
    return [_slim_contact(c) for c in (data.get("value") or [])]


@tool(
    description=(
        "Search the signed-in user's saved Outlook contacts by name or company, returning emails "
        "and phone numbers. Searches the personal address book only — use people_search for "
        "colleagues the user works with, or directory_search_users to search the whole tenant. "
        "This is the one that finds external contacts. Requires Contacts.Read."
    ),
    annotations=READ_ONLY,
)
async def people_search_contacts(params: SearchContactsInput, context: dict) -> list[dict] | dict:
    token = context["access_token"]
    if not params.query.strip():
        return invalid_arguments("Supply a name or company to search for.")
    term = escape_odata_string(params.query.strip())
    try:
        data = await graph_get(
            token,
            "/me/contacts",
            **{
                "$filter": (
                    f"startswith(displayName,'{term}') or startswith(surname,'{term}') "
                    f"or startswith(companyName,'{term}')"
                ),
                "$select": _CONTACT_SELECT,
                "$top": min(max(params.max_results, 1), 100),
            },
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Contacts.Read", tool="people_search_contacts")
    return [_slim_contact(c) for c in (data.get("value") or [])]


@tool(
    description=(
        "Save a new contact to the signed-in user's Outlook address book, with name and optional "
        "email, phone numbers, company and job title. Use when the user wants to remember someone "
        "who is not in the company directory. Adds to their private contacts only and does not "
        "affect the tenant directory. Requires Contacts.ReadWrite."
    ),
    annotations=WRITE_CREATE,
)
async def people_create_contact(params: CreateContactInput, context: dict) -> dict:
    token = context["access_token"]
    if not params.given_name.strip():
        return invalid_arguments("A contact needs at least a first name.")
    body: dict = {"givenName": params.given_name}
    if params.surname:
        body["surname"] = params.surname
    if params.email:
        # Graph wants a list of {address, name} even for a single address.
        display = " ".join(p for p in (params.given_name, params.surname) if p)
        body["emailAddresses"] = [{"address": params.email, "name": display}]
    if params.mobile_phone:
        body["mobilePhone"] = params.mobile_phone
    if params.business_phone:
        body["businessPhones"] = [params.business_phone]
    if params.company:
        body["companyName"] = params.company
    if params.job_title:
        body["jobTitle"] = params.job_title
    try:
        created = await graph_post(token, "/me/contacts", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Contacts.ReadWrite", tool="people_create_contact")
    return _slim_contact(created)


__all__ = ["people_create_contact", "people_list_contacts", "people_search_contacts"]
