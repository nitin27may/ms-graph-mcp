# Microsoft Graph coverage

A workload-by-workload comparison of what `ms-graph-mcp` exposes against the official Microsoft
Graph **v1.0** surface.

**Scope of this analysis.** Two filters are applied throughout, and they remove a great deal of
Graph from consideration:

1. **Delegated access only.** This server acts for a signed-in user. Anything that only works with
   application permissions is out of scope by design — not a gap.
2. **v1.0 only.** `/beta` endpoints are excluded. Microsoft does not support them in production and
   changes them without notice.

Current surface: **85 tools** — 53 read, 23 write, 9 internal.

The 9 internal tools are not part of the agent surface; they are reachable only by a shared-secret
machine principal. The number that matters to an agent is **76**.

Which permission each tool needs is in [permissions.md](permissions.md), generated from the tool
descriptions and checked in CI. What is planned is in [roadmap.md](roadmap.md).

---

## Scorecard

Counts are agent-visible tools — read plus write.

| Workload | Tools | Coverage | Assessment |
|---|---:|---|---|
| Meetings & transcripts | 7 | ●●●●● | Complete for the read path. The chain a transcript actually needs. |
| Email (Outlook) | 11 | ●●●●○ | Read, search, thread, attachments, send, reply, reply-all, forward, mark read. No folders or drafts. |
| Tasks (Planner/To Do) | 11 | ●●●●○ | Full read plus create, update and complete on both platforms. No plan or bucket creation. |
| Calendar | 10 | ●●●●○ | Read, create, update, cancel, RSVP, `findMeetingTimes`, `getSchedule`. No calendar list, no rooms. |
| Teams | 8 | ●●●●○ | Chats and channels, read plus send to chats. No channel posting, no replies, no presence. |
| Files (OneDrive/SharePoint) | 10 | ●●●○○ | Discovery, read, upload, update, folders, sharing links. No move/copy/delete, no delta. |
| Directory (Entra ID) | 7 | ●●●○○ | Solid core. Missing direct reports, transitive membership, photos. |
| People & contacts | 6 | ●●●●○ | Relevance search, profile, and the Outlook address book. |
| OneNote | 5 | ●●●●○ | Notebooks, sections, page list, page read, page create. No page update. |
| Unified search | 1 | ●●●●○ | `POST /search/query` across mail, events, files, sites, lists and people. |
| **SharePoint sites & lists** | **0** | ○○○○○ | **No structured access.** Reachable only as search hits. The largest remaining gap. |
| Presence | 0 | ○○○○○ | Absent. |
| Excel workbook | 0 | ○○○○○ | Absent. |
| Change notifications / delta | 0 | ○○○○○ | Absent. Cross-cutting. |

---

## Per-workload detail

### Meetings & transcripts — 7 tools

**Covered:** `meetings_get_transcript`, `meetings_list_transcripts`,
`meetings_list_with_transcripts`, `meetings_get_transcript_by_event`, `meetings_get_from_join_url`,
`meetings_list_past`, `meetings_get_attendance_report`.

**Missing:** creating an online meeting (`POST /me/onlineMeetings`); meeting recordings
(`/recordings`, requires `OnlineMeetingRecording.Read.All`); call records
(`/communications/callRecords`, application-only — **out of scope**).

**Assessment:** the strongest area, and the one that justifies the project's existence. Transcript
retrieval is genuinely fiddly in Graph — the event → onlineMeeting → transcript chain is three hops
with a join-URL filter in the middle — and this handles all of it.

---

### Email (Outlook) — 11 tools

**Read:** `mail_search`, `mail_list_recent`, `mail_list_flagged`, `mail_get_thread`,
`mail_list_attachments`.
**Write:** `mail_send`, `mail_reply`, `mail_reply_all`, `mail_forward`, `mail_mark_read`,
`mail_propose`.

`mail_propose` drafts a meeting email for the user to review and send themselves — it sends nothing.

| Gap | Endpoint |
|---|---|
| Draft lifecycle | `POST /me/messages`, `PATCH`, `POST .../send` |
| Move / copy message | `POST /me/messages/{id}/move`, `/copy` |
| Mail folders | `GET /me/mailFolders`, create/rename |
| Download an attachment's bytes | `GET /me/messages/{id}/attachments/{id}/$value` — listing exists, content is internal-tier |
| Flag / categorise | `PATCH /me/messages/{id}` beyond read state |
| Inbox rules | `GET/POST /me/mailFolders/inbox/messageRules` |
| Mailbox settings, automatic replies | `GET/PATCH /me/mailboxSettings` |
| Delta sync | `GET /me/messages/delta` |

**Recipient-domain gate.** `mail_send` and `mail_forward` check
`GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` before the Graph call, because the caller chooses the
recipients. `mail_reply` and `mail_reply_all` are not gated — the thread already fixes who they go
to.

**Assessment:** the common actions are all present. Folders and drafts are what remain, and drafts
matter more than they look: an agent that can prepare a message without sending it is a different
safety proposition from one that can only send.

---

### Tasks — Planner and To Do — 11 tools

**Read:** `tasks_list_planner_plans`, `tasks_list_planner_buckets`, `tasks_list_planner_tasks`,
`tasks_list_todo_lists`, `tasks_list_todo`.
**Write:** `tasks_create_todo`, `tasks_update_todo`, `tasks_complete_todo`, `tasks_create_planner`,
`tasks_update_planner`, `tasks_complete_planner`.

| Gap | Endpoint |
|---|---|
| Delete a task | `DELETE /planner/tasks/{id}`, `DELETE /me/todo/lists/{id}/tasks/{id}` |
| Assign a Planner task | `PATCH` with `assignments` |
| Task details — checklist, description, references | `GET/PATCH /planner/tasks/{id}/details` |
| Create plan / bucket | `POST /planner/plans`, `/planner/buckets` |
| Tasks assigned to me across plans | `GET /me/planner/tasks` |
| To Do checklist items and linked resources | `.../checklistItems`, `.../linkedResources` |
| Create a To Do list | `POST /me/todo/lists` |

**Two hard constraints from the official docs:**

1. **Planner Premium plans and tasks are not available through the Graph API at all.** Only basic
   plans. Any Project-for-the-web content is unreachable and always will be through this API.
2. **Every Planner `POST` / `PATCH` / `DELETE` requires an `If-Match` header** carrying the last
   known `@odata.etag`, and returns `409` / `412` on conflict. The three Planner writes here are
   read-then-write and return a retryable `CONFLICT` rather than silently overwriting a concurrent
   edit.

**Assessment:** completion works on both platforms, which was the conspicuous omission. What is left
is organisational — creating plans and buckets — and rarer than acting on tasks that already exist.

---

### Calendar — 10 tools

**Read:** `calendar_list_upcoming_events`, `calendar_list_events_in_range`, `calendar_get_event`,
`calendar_get_event_attendees`, `calendar_find_meeting_times`, `calendar_get_free_busy`.
**Write:** `calendar_create_event`, `calendar_update_event`, `calendar_cancel_event`,
`calendar_respond_to_event`.

`calendar_find_meeting_times` and `calendar_get_free_busy` are POST requests — their bodies are too
large for a query string — but they mutate nothing and are read-tier accordingly.

| Gap | Endpoint | Why it matters |
|---|---|---|
| List calendars | `GET /me/calendars`, `/calendarGroups` | Only the default calendar is reachable. |
| Rooms and places | `GET /places/microsoft.graph.room` | No room booking. |
| Recurring event instances | `GET /me/events/{id}/instances` | Series handling is invisible. |
| Event attachments | `GET /me/events/{id}/attachments` | |
| Delta sync | `GET /me/events/delta` | |

**Assessment:** scheduling works end to end — find a slot, book it, reschedule it, cancel it, reply
to an invitation. The remaining gaps are about *which* calendar and *which* room, not about whether
an agent can act at all.

---

### Teams — 8 tools

**Chats:** `chat_list`, `chat_list_messages`, `chat_list_members`, `chat_send_message`.
**Channels:** `chat_list_teams`, `chat_list_channels`, `chat_list_channel_messages`.
**Across both:** `chat_search_messages`.

| Gap | Endpoint | Notes |
|---|---|---|
| Send a channel message | `POST /teams/{id}/channels/{id}/messages` | Channels are read-only; chats are not. |
| Message replies | `.../messages/{id}/replies` | Threading is invisible in both. |
| **Presence** | `GET /me/presence`, `POST /communications/getPresencesByUserId` | "Is this person free right now?" |
| Team members | `GET /teams/{id}/members` | Chat members are covered; team members are not. |
| Team and channel creation | `POST /teams`, `POST /teams/{id}/channels` | |
| Tabs and installed apps | `.../tabs`, `.../installedApps` | |
| Message delta | `GET /chats/{id}/messages/delta` | The supported way to poll. |
| Shifts / schedules | `/teams/{id}/schedule` | Frontline scenarios. |

**Gotcha from the official docs:** Microsoft imposes an explicit **polling limit of once per day**
for Teams resources; anything more frequent must use change notifications, and violating this is
treated as a breach of the API terms of use. Any future sync feature must use subscriptions, not
polling.

**Assessment:** chats were the hole and are now covered, including sending. The asymmetry left is
that an agent can post to a chat but not to a channel.

---

### Files — OneDrive and SharePoint document libraries — 10 tools + 7 internal

**Read:** `files_search`, `files_list_recent`, `files_list_trending`, `files_list_shared_with_me`,
`files_get_content`, `files_get_group_drive`.
**Write:** `files_upload`, `files_update_content`, `files_create_folder`,
`files_create_sharing_link`.
**Internal only:** walk descendants, ensure folder, upload, update content, download, resolve a
group drive, plus the `graph_request` passthrough.

`files_*` covers OneDrive **and** SharePoint document libraries — they are the same `driveItem`
resource underneath, which is why the namespace is not split by product.

Graph's `driveItem` exposes roughly **35 operations**. Missing from every tier:

| Gap | Endpoint | Notes |
|---|---|---|
| **Move, copy, delete, restore** | `PATCH`, `POST .../copy`, `DELETE`, `.../restore` | An agent can create but not reorganise. |
| **Delta (change tracking)** | `GET /drive/root/delta` | Required for any sync scenario. |
| List / revoke permissions | `GET`, `DELETE .../permissions` | Links can be created but not audited or withdrawn. |
| Invite / grant access | `POST /drive/items/{id}/invite` | |
| File versions | `GET /drive/items/{id}/versions` | |
| **Large-file upload session** | `POST .../createUploadSession` | Current upload is single-shot base64, so size is bounded. |
| Thumbnails / preview | `.../thumbnails`, `POST .../preview` | |
| Check in / check out | `.../checkin`, `.../checkout` | SharePoint document libraries. |
| Sensitivity / retention labels | `.../assignSensitivityLabel`, `.../setRetentionLabel` | Compliance scenarios. |
| Item analytics / activities | `.../analytics`, `.../activities` | |

**Assessment:** an agent can find, read, create and share a file. It cannot move one, delete one,
take back a sharing link, or upload anything large. Revoking a link is the one that stands out —
handing out access without being able to withdraw it is an odd shape for a permission tool.

---

### Directory / Entra ID — 7 tools

**Covered:** `directory_search_users`, `directory_get_user`, `directory_get_user_manager`,
`directory_list_user_groups`, `directory_search_groups`, `directory_list_group_members`,
`directory_get_group`.

| Gap | Endpoint | Notes |
|---|---|---|
| **Direct reports** | `GET /users/{id}/directReports` | Manager works; the inverse does not. Org-chart traversal is half-built. |
| **Transitive membership** | `GET /users/{id}/transitiveMemberOf` | Nested groups are invisible today. |
| **Profile photo** | `GET /users/{id}/photo/$value` | |
| Membership check | `POST /me/checkMemberGroups`, `getMemberGroups` | Efficient authorization checks. |
| Group owners | `GET /groups/{id}/owners` | |
| Licence details | `GET /users/{id}/licenseDetails` | |
| Directory roles | `GET /directoryRoles` | Admin-scoped. |
| Administrative units | `GET /directory/administrativeUnits` | |
| Resolve many objects at once | `POST /directoryObjects/getByIds` | Cheap batch resolution. |
| Organization info | `GET /organization` | |
| App role assignments | `GET /users/{id}/appRoleAssignments` | |

**Delegated constraints worth knowing:**

- **Guest users cannot enumerate** `/users` or `/groups` at all. They can read a specific object by
  id and follow navigation links, but a search returning more than one result fails. Anyone running
  this in a tenant where they are a guest will see `directory_search_users` fail regardless of
  consent — Microsoft blocks the query, not the permission.
- `User.ReadBasic.All` restricts other users to eight properties (displayName, givenName, id, mail,
  photo, securityIdentifier, surname, userPrincipalName). Richer fields need `User.Read.All`.
- Several `user` properties are stored outside the directory (`skills`, `aboutMe`, `birthday`,
  `interests`, `mailboxSettings`, …) and never appear in delta queries.
- Group lookups prefer an app-only token supplied as `X-Entra-App-Token`, because delegated
  permissions cannot cover tenant-wide group reads. It is optional, and there is a delegated
  fallback.

**Assessment:** unchanged since the first release, and now the second-largest gap. `directReports`
and `transitiveMemberOf` are the two that most change what an agent can answer.

---

### People and contacts — 6 tools

**Read:** `people_search`, `people_get`, `people_get_my_profile`, `people_list_contacts`,
`people_search_contacts`.
**Write:** `people_create_contact`.

Three different data sources, deliberately not merged:

| Tool | Source | Finds |
|---|---|---|
| `people_search` | `/me/people` relevance graph | Colleagues you actually work with, ranked by your own mail and meeting history — tolerates fuzzy spelling |
| `directory_search_users` | Entra ID tenant directory | Any account in the tenant, exact-ish match |
| `people_list_contacts` / `people_search_contacts` | Outlook personal address book | External contacts, invisible to both of the above |

**Missing:** `GET /me/insights/used` and `/shared` (only `/trending` is wired, via
`files_list_trending`); `GET /me/profile`, the richer profile resource; contact folders
(`/me/contactFolders`); updating or deleting a contact.

---

### OneNote — 5 tools

**Read:** `notes_list_notebooks`, `notes_list_sections`, `notes_list_pages`,
`notes_get_page_content`.
**Write:** `notes_create_page`.

| Gap | Endpoint |
|---|---|
| Update page content | `PATCH /me/onenote/pages/{id}/content` |
| Delete page | `DELETE /me/onenote/pages/{id}` |
| Section groups | `GET /me/onenote/sectionGroups` |
| Copy page / section | `POST .../copyToSection`, `copyToNotebook` |
| Group / SharePoint-hosted notebooks | `/groups/{id}/onenote/…`, `/sites/{id}/onenote/…` |

**Notable:** the OneNote API **does not support app-only authentication at all** — delegated is the
only option. That is unusual in Graph, and it means this workload is fully in scope for this server
by construction.

---

### Unified search — 1 tool

`search_query` issues `POST /search/query` across `message`, `event`, `driveItem`, `drive`, `list`,
`listItem`, `site` and `person` in one call. It is the highest-leverage single endpoint in Graph and
the right first move for a vague question.

**Missing:** the Copilot-connector entity types — `externalItem`, `bookmark`, `acronym`, `qna` —
which reach data no other endpoint exposes.

---

### SharePoint sites and lists — 0 tools

`search_query` returns sites and list items as *search results*, and `files_*` reaches document
libraries. There is no structured access: you cannot enumerate a site's lists, read a list's items
with their fields, or write one back.

| Capability | Endpoint |
|---|---|
| Find a site | `GET /sites?search={query}` |
| Get site by path | `GET /sites/{hostname}:/{server-relative-path}` |
| Root and sub-sites | `GET /sites/root`, `/sites/{id}/sites` |
| Document libraries | `GET /sites/{id}/drives`, `/sites/{id}/drive` |
| **Lists** | `GET /sites/{id}/lists` |
| **List items + fields** | `GET /sites/{id}/lists/{id}/items?expand=fields` |
| Create / update list items | `POST`, `PATCH .../items` |
| Column and content-type schema | `.../columns`, `.../contentTypes` |
| Site pages | `GET /sites/{id}/pages` |

**Constraint worth documenting:** Graph gives **read-only access to `site` resources** — you cannot
create a site through it. `list`, `listItem` and `driveItem` are read-write.

**Assessment:** the largest remaining gap. SharePoint lists are where a great deal of enterprise
structured data actually lives, and `listItem` with `?expand=fields` is a general-purpose data
reader. `Sites.Read.All` is already in the read consent set.

---

## Absent workloads

| Workload | Key endpoints | Verdict |
|---|---|---|
| **Presence** | `GET /me/presence`, `POST /communications/getPresencesByUserId` | Small, high perceived value. Pairs naturally with `calendar_get_free_busy`. |
| **Excel workbook** | `/drive/items/{id}/workbook/worksheets`, `/tables`, `/range` | Large API. Turns spreadsheets into queryable data. |
| **Change notifications** | `POST /subscriptions` (webhooks), `…/delta` on most resources | Cross-cutting. Needed for any sync, and the *only* sanctioned way to track Teams messages. |
| **Batching** | `POST /$batch` | Up to 20 requests per round trip. A pure efficiency win. |

---

## Out of scope — and why

Documenting these matters as much as the gaps, so they are not repeatedly re-proposed.

- **Microsoft Loop.** There is **no generally available Loop API**. Loop pages live in SharePoint
  Embedded containers; the first Microsoft Graph APIs for Loop were slated for H1 CY2026 and are
  **workspace and lifecycle management only, not content access**. A beta `workspace` resource
  exists. Nothing stable to build on. Revisit when a v1.0 Loop API ships.
- **Planner Premium / Project for the web.** Explicitly excluded by Microsoft from the Planner API.
- **Call records** (`/communications/callRecords`) — application permissions only.
- **Security, Intune, Identity Protection, audit logs, usage reports** — administrative surfaces
  requiring app-only permissions or admin roles. A different product.
- **Directory writes** (create/delete users, reset passwords, group management) — deliberately out
  of scope. This is an assistant, not an IAM tool, and the sensitive-action matrix around them is
  substantial.

---

## What is worth building next

Ranked by value delivered per unit of work. This list is the source for [roadmap.md](roadmap.md) —
keep the two in step.

| # | Item | Tools | Rationale |
|---|---|---:|---|
| 1 | **SharePoint sites and lists** — find site, list lists, read/write list items | 5–6 | Largest absent workload. `listItem` with `?expand=fields` is a general-purpose structured-data reader, and `Sites.Read.All` is already consented. |
| 2 | **Directory completion** — `directReports`, `transitiveMemberOf`, photo, `checkMemberGroups` | 3–4 | Completes org-chart traversal. `directReports` is the inverse of a tool that already exists. |
| 3 | **File operations** — move, copy, delete, list/revoke permissions, versions | 4–5 | An agent that can create a sharing link should be able to take it back. |
| 4 | **Large-file upload session** — `createUploadSession` | 1 | Removes the size ceiling on the upload that already exists. |
| 5 | **Mail folders and drafts** | 4–5 | A draft an agent prepares but does not send is a materially safer default than send. |
| 6 | **Presence** | 1–2 | Small, and the answer to a question people ask constantly. |
| 7 | **Channel posting and message replies** | 2–3 | Closes the chat/channel asymmetry. |
| 8 | Excel workbook, delta/webhooks, `$batch` | — | Larger, cross-cutting, lower urgency. |

---

## Sources

Graph endpoint references verified against Microsoft Learn on **2026-08-04**. Tool inventory derived
from `src/ms_graph_mcp/allowlists.py` on **2026-08-05**.

- [Microsoft Graph REST API v1.0 reference](https://learn.microsoft.com/en-us/graph/api/overview?view=graph-rest-1.0)
- [Working with SharePoint sites](https://learn.microsoft.com/en-us/graph/api/resources/sharepoint?view=graph-rest-1.0)
- [driveItem resource type](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0)
- [Teams API overview](https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview?view=graph-rest-1.0)
- [Working with users](https://learn.microsoft.com/en-us/graph/api/resources/users?view=graph-rest-1.0)
- [Planner API overview](https://learn.microsoft.com/en-us/graph/api/resources/planner-overview?view=graph-rest-1.0)
- [OneNote API overview](https://learn.microsoft.com/en-us/graph/api/resources/onenote-api-overview?view=graph-rest-1.0)
- [Microsoft Search API overview](https://learn.microsoft.com/en-us/graph/search-concept-overview)
- [Outlook contacts API](https://learn.microsoft.com/en-us/graph/api/resources/contact?view=graph-rest-1.0)
- [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
