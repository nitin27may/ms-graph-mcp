# Microsoft Graph coverage

A workload-by-workload comparison of what `ms-graph-mcp` exposes against the official Microsoft
Graph **v1.0** surface.

**Scope of this analysis.** Two filters are applied throughout, and they remove a great deal of
Graph from consideration:

1. **Delegated access only.** This server acts for a signed-in user. Anything that only works with
   application permissions is out of scope by design — not a gap.
2. **v1.0 only.** `/beta` endpoints are excluded. Microsoft does not support them in production and
   changes them without notice.

Current surface: **55 tools** — 42 read, 4 write, 9 internal.

Last verified against Microsoft Learn: **2026-08-04**.

---

## Scorecard

| Workload | Tools | Coverage | Assessment |
|---|---:|---|---|
| Meetings & transcripts | 7 | ●●●●● | Best-covered area. Genuinely complete for the read path. |
| Directory (Entra ID) | 7 | ●●●○○ | Solid core. Missing direct reports, transitive membership, photos. |
| Email (Outlook) | 6 | ●●○○○ | Read + send only. No reply, forward, folders, or attachments on the agent surface. |
| Files (OneDrive) | 6 | ●●○○○ | Read-only discovery. All write/share/version operations are internal-tier or absent. |
| Tasks (Planner/To Do) | 6 | ●●○○○ | Read + create To Do task. No Planner writes, no task completion. |
| Calendar | 4 | ●●○○○ | Read only. No create/update/cancel, no scheduling assistance. |
| Teams | 4 | ●●○○○ | Channels only. **1:1 and group chats entirely absent.** |
| People | 3 | ●●●○○ | Reasonable for its size. |
| OneNote | 3 | ●●○○○ | Notebooks and sections; no page content read. |
| **SharePoint** | **0** | ○○○○○ | **Entirely absent.** |
| **Unified search** | **0** | ○○○○○ | **Entirely absent.** Highest leverage per tool in Graph. |
| Contacts | 0 | ○○○○○ | Absent. |
| Presence | 0 | ○○○○○ | Absent. |
| Excel workbook | 0 | ○○○○○ | Absent. |
| Change notifications / delta | 0 | ○○○○○ | Absent. Cross-cutting. |

---

## Delegated permissions actually in use

No permission matrix currently ships with this project — the single largest documentation gap (see
"Recommendations"). The set below is what the originating platform requests, and is a sound starting
point for an app registration:

```
User.Read                          Mail.Read                    Calendars.Read
User.ReadBasic.All                 Mail.Send                    Calendars.ReadWrite
User.Read.All                      Mail.ReadWrite               OnlineMeetings.Read
Group.Read.All                     Files.Read.All               OnlineMeetingTranscript.Read.All
GroupMember.Read.All               Files.ReadWrite.All          Chat.Read
Directory.Read.All                 Sites.Read.All               ChatMessage.Send
People.Read                        Notes.ReadWrite              ChannelMessage.Read.All
Tasks.Read / Tasks.ReadWrite       Team.ReadBasic.All
```

Two registrations are involved in the source platform: a **delegated** one for user sign-in and OBO,
and a **separate app-only** one holding `Group.Read.All` / `User.Read.All` for tenant-wide directory
reads that delegated permissions cannot cover. That second registration is what the
`X-Entra-App-Token` header carries.

---

## Per-workload detail

### Calendar — 4 tools

**Covered:** `get_upcoming_meetings`, `get_meeting_details`, `get_meeting_attendees`,
`get_calendar_events_range` — all over `/me/calendarView` and `/me/events/{id}`.

**Missing:**

| Gap | Endpoint | Why it matters |
|---|---|---|
| Create event | `POST /me/events` | An agent can read a calendar but cannot book anything. |
| Update / cancel event | `PATCH`, `DELETE /me/events/{id}`, `POST .../cancel` | No rescheduling. |
| Respond to invite | `POST /me/events/{id}/accept\|decline\|tentativelyAccept` | Cannot triage invitations. |
| **Find meeting times** | `POST /me/findMeetingTimes` | Graph does the hard scheduling work; without it an agent cannot suggest a slot. |
| **Free/busy lookup** | `POST /me/calendar/getSchedule` | Availability for other people. |
| List calendars | `GET /me/calendars`, `/calendarGroups` | Only the default calendar is reachable. |
| Rooms and places | `GET /places/microsoft.graph.room` | No room booking. |
| Recurring event instances | `GET /me/events/{id}/instances` | Series handling is invisible. |

**Assessment:** the most conspicuous functional gap after SharePoint. `findMeetingTimes` and
`getSchedule` together are what turn a read-only calendar into a scheduling assistant.

---

### Email (Outlook) — 6 tools

**Covered:** `search_emails`, `get_recent_emails`, `get_flagged_emails`, `get_email_thread`,
`send_email`, `propose_email`.

**Missing:**

| Gap | Endpoint |
|---|---|
| Reply / reply-all / forward | `POST /me/messages/{id}/reply`, `replyAll`, `forward` |
| Draft lifecycle | `POST /me/messages`, `PATCH`, `POST .../send` |
| Mark read/unread, flag | `PATCH /me/messages/{id}` |
| Move / copy message | `POST /me/messages/{id}/move`, `/copy` |
| Mail folders | `GET /me/mailFolders`, create/rename |
| Attachments (agent surface) | `GET /me/messages/{id}/attachments` — exists but **internal-tier only** |
| Inbox rules | `GET/POST /me/mailFolders/inbox/messageRules` |
| Categories | `GET /me/outlook/masterCategories` |
| Mailbox settings, automatic replies | `GET/PATCH /me/mailboxSettings` |
| Delta sync | `GET /me/messages/delta` |

**Assessment:** `send_email` exists but `reply` does not, which is backwards for an assistant —
replying in-thread is the more common and safer action. `fetch_message_attachments` is already
implemented in the internal tier and could be promoted to a read tool cheaply.

---

### Meetings & transcripts — 7 tools

**Covered:** `get_meeting_transcript`, `list_meeting_transcripts`, `get_meetings_with_transcripts`,
`get_transcript_by_event_id`, `get_online_meeting_from_event`, `get_past_meetings`,
`get_attendance_report`.

**Missing:** creating an online meeting (`POST /me/onlineMeetings`); meeting recordings
(`/recordings`, requires `OnlineMeetingRecording.Read.All`); call records (`/communications/callRecords`,
application-only — **out of scope**).

**Assessment:** the strongest area, and the one that justifies the project's existence. Transcript
retrieval is genuinely fiddly in Graph and this handles the event → onlineMeeting → transcript chain.

---

### Files (OneDrive) — 6 tools + 5 internal

**Covered (agent):** `search_files`, `get_recent_files`, `get_trending_files`, `get_shared_files`,
`get_file_content`, `get_group_drive`.
**Covered (internal only):** walk descendants, ensure folder, upload, update content, download.

Graph's `driveItem` exposes roughly **35 operations**. Missing from every tier:

| Gap | Endpoint | Notes |
|---|---|---|
| **Create sharing link** | `POST /drive/items/{id}/createLink` | The single most requested file action. |
| **Invite / grant access** | `POST /drive/items/{id}/invite` | |
| List / delete permissions | `GET`, `DELETE .../permissions` | |
| Move, copy, delete, restore | `PATCH`, `POST .../copy`, `DELETE`, `.../restore` | |
| **Delta (change tracking)** | `GET /drive/root/delta` | Required for any sync scenario. |
| File versions | `GET /drive/items/{id}/versions` | |
| Thumbnails / preview | `.../thumbnails`, `POST .../preview` | |
| Check in / check out | `.../checkin`, `.../checkout` | SharePoint document libraries. |
| Large-file upload session | `POST .../createUploadSession` | Current upload is single-shot base64. |
| Sensitivity / retention labels | `.../assignSensitivityLabel`, `.../setRetentionLabel` | Compliance scenarios. |
| Item analytics / activities | `.../analytics`, `.../activities` | |

**Assessment:** an agent can find a file but cannot share it, move it, or track changes to it. The
write primitives exist in the internal tier and are deliberately withheld from agents — promoting a
curated subset to the write tier is a smaller job than it looks.

---

### SharePoint — 0 tools

**Nothing is exposed.** The entire `site` / `list` / `listItem` surface is absent.

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
| Site-hosted OneNote | `GET /sites/{id}/onenote/notebooks` |

**Constraint worth documenting:** Graph gives **read-only access to `site` resources** — you cannot
create a site through it. `list`, `listItem`, and `driveItem` are read-write.

**Assessment:** the largest single gap. SharePoint lists are where a great deal of enterprise
structured data actually lives, and `listItem` with `?expand=fields` is a general-purpose data
reader. `Sites.Read.All` is already in the permission set.

---

### Teams — 4 tools

**Covered:** `get_joined_teams`, `get_team_channels`, `get_channel_messages`,
`search_teams_messages`.

You asked me to double-check Teams specifically. It is **not** as complete as it looks — the
coverage is channel-shaped, and most Teams conversation happens in chats:

| Gap | Endpoint | Notes |
|---|---|---|
| **List the user's chats** | `GET /me/chats` | 1:1 and group chats. Absent. |
| **Chat messages** | `GET /chats/{id}/messages` | Absent. |
| **Send a chat message** | `POST /chats/{id}/messages` | `ChatMessage.Send` is already requested. |
| Send a channel message | `POST /teams/{id}/channels/{id}/messages` | Read-only today. |
| Message replies | `.../messages/{id}/replies` | Threading is invisible. |
| Chat / team members | `GET /chats/{id}/members`, `/teams/{id}/members` | |
| **Presence** | `GET /me/presence`, `POST /communications/getPresencesByUserId` | "Is this person free?" |
| Team and channel creation | `POST /teams`, `POST /teams/{id}/channels` | |
| Tabs and installed apps | `.../tabs`, `.../installedApps` | |
| Message delta | `GET /chats/{id}/messages/delta` | The supported way to poll. |
| Shifts / schedules | `/teams/{id}/schedule` | Frontline scenarios. |

**Gotcha from the official docs:** Microsoft imposes an explicit **polling limit of once per day**
for Teams resources; anything more frequent must use change notifications, and violating this is
treated as a breach of the API terms of use. Any future sync feature must use subscriptions, not
polling.

**Assessment:** chats are the notable omission. `Chat.Read` and `ChatMessage.Send` are already in the
permission set, so this is a pure implementation gap.

---

### Directory / Entra ID — 7 tools

**Covered:** `search_users`, `get_user_details`, `get_user_manager`, `get_user_groups`,
`search_groups`, `get_group_members`, `get_group_details`.

| Gap | Endpoint | Notes |
|---|---|---|
| **Direct reports** | `GET /users/{id}/directReports` | Manager works; the inverse does not. Org-chart traversal is half-built. |
| **Profile photo** | `GET /users/{id}/photo/$value` | |
| Transitive membership | `GET /users/{id}/transitiveMemberOf` | Nested groups are invisible today. |
| Membership check | `POST /me/checkMemberGroups`, `getMemberGroups` | Efficient authorization checks. |
| Group owners | `GET /groups/{id}/owners` | |
| Licence details | `GET /users/{id}/licenseDetails` | |
| Directory roles | `GET /directoryRoles` | Admin-scoped. |
| Administrative units | `GET /directory/administrativeUnits` | |
| Resolve many objects at once | `POST /directoryObjects/getByIds` | Cheap batch resolution. |
| Organization info | `GET /organization` | |
| App role assignments | `GET /users/{id}/appRoleAssignments` | |

**Delegated constraints worth documenting:**
- **Guest users cannot enumerate** `/users` or `/groups` at all. They can read a specific object by
  id, and can follow navigation links, but a search returning more than one result fails. Anyone
  running this in a tenant where they are a guest will see `search_users` fail in a way the current
  error message does not explain.
- `User.ReadBasic.All` restricts other users to eight properties (displayName, givenName, id, mail,
  photo, securityIdentifier, surname, userPrincipalName). Richer fields need `User.Read.All`.
- Several `user` properties are stored outside the directory (`skills`, `aboutMe`, `birthday`,
  `interests`, `mailboxSettings`, …) and never appear in delta queries.

**Assessment:** the core is sound; `directReports` and `transitiveMemberOf` are the two that most
change what an agent can answer.

---

### Tasks — Planner and To Do — 6 tools

**Covered:** `get_planner_plans`, `get_planner_buckets`, `get_planner_tasks`, `get_todo_lists`,
`get_todo_tasks`, `create_todo_task`.

| Gap | Endpoint |
|---|---|
| **Create / update / delete a Planner task** | `POST /planner/tasks`, `PATCH`, `DELETE` |
| **Complete a task** (`percentComplete: 100`) | `PATCH /planner/tasks/{id}` |
| Assign a task | `PATCH` with `assignments` |
| Task details — checklist, description, references | `GET/PATCH /planner/tasks/{id}/details` |
| Create plan / bucket | `POST /planner/plans`, `/planner/buckets` |
| Tasks assigned to me across plans | `GET /me/planner/tasks` |
| Update / complete / delete a To Do task | `PATCH`, `DELETE /me/todo/lists/{id}/tasks/{id}` |
| To Do checklist items and linked resources | `.../checklistItems`, `.../linkedResources` |
| Create a To Do list | `POST /me/todo/lists` |

**Two hard constraints from the official docs:**

1. **Planner Premium plans and tasks are not available through the Graph API at all.** Only basic
   plans. Any Project-for-the-web content is unreachable and always will be through this API.
2. **Every Planner `POST` / `PATCH` / `DELETE` requires an `If-Match` header** carrying the last
   known `@odata.etag`, and returns `409` / `412` on conflict. Any write implementation must
   read-then-write and handle a retry. `client.py:graph_patch` already accepts `extra_headers` for
   exactly this.

**Assessment:** `create_todo_task` is the only write. Marking a task complete — arguably the single
most useful task action — is missing on both platforms.

---

### OneNote — 3 tools

**Covered:** `get_notebooks`, `get_sections`, `save_to_onenote`.

| Gap | Endpoint |
|---|---|
| **List pages** | `GET /me/onenote/pages`, `/sections/{id}/pages` |
| **Read page content** | `GET /me/onenote/pages/{id}/content` | 
| Update page content | `PATCH /me/onenote/pages/{id}/content` |
| Section groups | `GET /me/onenote/sectionGroups` |
| Copy page / section | `POST .../copyToSection`, `copyToNotebook` |
| Delete page | `DELETE /me/onenote/pages/{id}` |
| Group / SharePoint-hosted notebooks | `/groups/{id}/onenote/…`, `/sites/{id}/onenote/…` |

**Notable:** the OneNote API **does not support app-only authentication at all** — delegated is the
only option. That is unusual in Graph, and it means this workload is fully in scope for this server
by construction.

**Assessment:** the server can write a page into OneNote but cannot read one back. Listing and
reading page content is the obvious next step.

---

### People — 3 tools

**Covered:** `search_people`, `get_person_details`, `get_my_profile`.

**Missing:** `GET /me/insights/used` and `/shared` (only `/trending` is wired, via
`get_trending_files`); `GET /me/profile` (the richer profile resource); org contacts
(`/contacts` — see below).

---

### Absent workloads

| Workload | Key endpoints | Verdict |
|---|---|---|
| **Unified search** | `POST /search/query` over `message`, `event`, `driveItem`, `drive`, `list`, `listItem`, `site`, `person`, `externalItem`, `bookmark`, `acronym`, `qna` | **Highest leverage in Graph.** One endpoint searches mail, calendar, files, sites and people, and reaches Copilot-connector external data that no other endpoint exposes. |
| **Personal contacts** | `GET /me/contacts`, `/me/contactFolders` | Straightforward, commonly wanted. |
| **Presence** | `GET /me/presence`, `POST /communications/getPresencesByUserId` | Small, high perceived value. |
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
- **Directory writes** (create/delete users, reset passwords, group management) — deliberately out of
  scope. This is an assistant, not an IAM tool, and the sensitive-action matrix around them is
  substantial.

---

## Recommendations

Ranked by value delivered per unit of work.

| # | Item | Tools | Rationale |
|---|---|---:|---|
| 1 | **Unified search** — `POST /search/query` | 1–2 | One endpoint spans six entity types. Nothing else in Graph gives this much reach per tool. |
| 2 | **Teams chats** — list chats, read messages, send message | 3–4 | The biggest hole in a workload that looks covered. Permissions already requested. |
| 3 | **Calendar write** — create/update/cancel, `findMeetingTimes`, `getSchedule` | 5–6 | Turns a read-only calendar into a scheduling assistant. |
| 4 | **SharePoint** — sites, lists, list items | 5–6 | Largest absent workload; `listItem` is a general-purpose structured-data reader. |
| 5 | **Email actions** — reply, reply-all, forward, mark read; promote attachments to read tier | 4–5 | Reply is more common and safer than compose-and-send, which already exists. |
| 6 | **Task completion** — Planner and To Do updates | 3–4 | Must handle `If-Match` etags and 409/412. |
| 7 | **OneNote page read** — list pages, get content | 2 | The server writes pages it cannot read back. |
| 8 | **Directory** — `directReports`, `transitiveMemberOf`, photo | 3 | Completes org-chart traversal. |
| 9 | **File sharing** — `createLink`, `invite`, permissions | 3 | Most requested file action. |
| 10 | Contacts, presence, delta/webhooks, `$batch` | — | Genuine but lower priority. |

**Doing 1–5 adds roughly 20 tools and closes every gap a user is likely to notice first.** It would
also take the surface past 70 tools, which is what makes the planned toolset-profile work a
prerequisite rather than an optimisation.

### The non-tool gap

Worth stating plainly: **the most valuable single addition is not a tool at all.** There is currently
no documentation of which delegated permissions each tool requires, and no app-registration guide. A
new user cannot obtain a working token from what ships today. The permission set above is the raw
material for that document.

---

## Sources

All verified against Microsoft Learn on 2026-08-04.

- [Microsoft Graph REST API v1.0 reference](https://learn.microsoft.com/en-us/graph/api/overview?view=graph-rest-1.0)
- [Working with SharePoint sites](https://learn.microsoft.com/en-us/graph/api/resources/sharepoint?view=graph-rest-1.0)
- [driveItem resource type](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0)
- [Teams API overview](https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview?view=graph-rest-1.0)
- [Working with users](https://learn.microsoft.com/en-us/graph/api/resources/users?view=graph-rest-1.0)
- [Planner API overview](https://learn.microsoft.com/en-us/graph/api/resources/planner-overview?view=graph-rest-1.0)
- [OneNote API overview](https://learn.microsoft.com/en-us/graph/api/resources/onenote-api-overview?view=graph-rest-1.0)
- [Microsoft Search API overview](https://learn.microsoft.com/en-us/graph/search-concept-overview)
- [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
