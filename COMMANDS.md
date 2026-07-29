# shweta/zoho-crm - command reference

Eighteen commands. Nine reads, which run without approval. Nine writes, which
stop at the airlock until a human approves the exact payload.

Examples below use Zoho's own demo records, so you can follow along in a fresh
trial org.

## Conventions

Every command returns `ok: true` on success. Failures raise, and the receipt
records the reason rather than returning an error object.

**Receipts redact identifiers.** Record ids, emails and hashes come back as
`[account]` or `[redacted - identifier]` in receipt output. Your handler gets
the real values; the sealed receipt does not. That is the platform protecting
the audit trail, and it is why nothing in this module asks you to copy a value
out of one command's output into another's input.

**Every plan expires after 60 minutes** and is keyed on the exact inputs used.
Apply with anything different and it will tell you no plan exists.

---

# Reads

## zoho.verify_connection

Preflight. Probes every capability this module needs and reports which scopes
are granted, which are missing, and which commands a missing scope blocks.
Run it first: a missing scope otherwise surfaces much later as an unrelated
command failing with a bare 401.

No inputs.

```json
{}
```

```json
{
  "ok": true,
  "ready": true,
  "api_domain": "https://www.zohoapis.in",
  "api_version": "v8",
  "scopes": {
    "ZohoCRM.settings.fields.READ": "ok",
    "ZohoCRM.modules.ALL": "ok",
    "ZohoCRM.coql.READ": "ok",
    "ZohoCRM.users.READ": "ok",
    "ZohoCRM.org.READ": "ok"
  },
  "blocked_commands": [],
  "org_name": "Acme Consulting LLP",
  "summary": "Ready. All required scopes granted on https://www.zohoapis.in for Acme Consulting LLP."
}
```

With a scope missing:

```json
{
  "ready": false,
  "scopes": { "ZohoCRM.coql.READ": "MISSING" },
  "blocked_commands": ["and every apply_", "plan_delete", "plan_handover",
                       "plan_update", "search_records"],
  "summary": "Not ready. Missing ZohoCRM.coql.READ. Re-mint the refresh token in the Zoho API console with the full scope list from the README. Until then these will fail: ..."
}
```

`ZohoCRM.org.READ` is optional. Without it `ready` stays true, the scope reads
`missing (optional)`, and only the org name and id are blank.

**Errors.** If there is no vault entry at all, the error prints the exact JSON
shape to write, including which URLs change per datacenter.

## zoho.describe_module

Every field on a module, including custom ones, with data types and picklist
values. Call this before writing so field names are never guessed.

| input | type | required |
|---|---|---|
| `module` | string | yes |

```json
{ "module": "Leads" }
```

```json
{
  "ok": true,
  "module": "Leads",
  "field_count": 55,
  "fields": [
    {
      "api_name": "Lead_Status",
      "label": "Lead Status",
      "type": "picklist",
      "required": false,
      "read_only": false,
      "picklist_values": ["Attempted to Contact", "Contacted", "Junk Lead",
                          "Lost Lead", "Not Contacted", "Pre-Qualified"]
    }
  ]
}
```

The picklist values matter: writing `Lead_Status: "Qualified"` when your org
calls it `Pre-Qualified` fails per-record, not per-batch.

## zoho.search_records

Read-only COQL, Zoho's SQL-like query language.

| input | type | required |
|---|---|---|
| `query` | string | yes |

```json
{ "query": "select Last_Name, Company from Leads where Lead_Status = 'Lost Lead' limit 3" }
```

```json
{
  "ok": true,
  "count": 2,
  "more_records": false,
  "records": [
    {"id": "1352736000000533111", "Last_Name": "Maclead", "Company": "Rangoni Of Florence"},
    {"id": "1352736000000533106", "Last_Name": "Lace", "Company": "Printing Dimensions"}
  ]
}
```

**Errors.** Anything that is not a SELECT is rejected before it reaches Zoho.
A bare `400 SYNTAX_ERROR` near `where` usually means no WHERE clause, which
COQL requires. Three chained `!=` on one column also fails; use
`not in ('a','b','c')`.

## zoho.list_records

Pages through a module without writing a query.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | |
| `fields` | array | no | defaults to a core set per module |
| `page` | number | no | 1-based |
| `per_page` | number | no | max 200 |
| `sort_by` | string | no | field api_name |
| `sort_order` | string | no | `asc` or `desc` |

```json
{ "module": "Deals", "per_page": 2, "sort_by": "Modified_Time" }
```

```json
{
  "ok": true,
  "module": "Deals",
  "count": 2,
  "page": 1,
  "more_records": true,
  "records": [
    {"id": "1352736000000541001", "Deal_Name": "King", "Stage": "Id. Decision Makers", "Amount": 60000}
  ]
}
```

If `fields` is omitted for a module with no built-in default, the metadata API
is queried and the first fifty readable fields are used.

## zoho.get_record

One record by id.

| input | type | required |
|---|---|---|
| `module` | string | yes |
| `record_id` | string | yes |
| `fields` | array | no |

```json
{ "module": "Leads", "record_id": "1352736000000533111" }
```

```json
{ "ok": true, "module": "Leads", "record": { "id": "1352736000000533111", "Last_Name": "Maclead" } }
```

**Errors.** A non-numeric `record_id` is rejected before the call. A record in
the recycle bin returns "no record found".

## zoho.list_users

Org users and their ids. Ownership fields take an id, not a name or an email.

| input | type | required | notes |
|---|---|---|---|
| `type` | string | no | defaults to `ActiveConfirmedUsers` |
| `page` | number | no | |
| `per_page` | number | no | max 200 |

Valid `type` values: `AllUsers`, `ActiveUsers`, `DeactiveUsers`,
`ConfirmedUsers`, `NotConfirmedUsers`, `DeletedUsers`, `ActiveConfirmedUsers`,
`AdminUsers`, `ActiveConfirmedAdmins`, `CurrentUser`.

```json
{ "type": "ActiveConfirmedUsers" }
```

```json
{
  "ok": true,
  "type": "ActiveConfirmedUsers",
  "count": 2,
  "users": [
    {"id": "1352736000000462001", "full_name": "Priya R", "email": "priya@acme.in",
     "role": "CEO", "profile": "Administrator", "status": "active"}
  ]
}
```

**Errors.** This endpoint needs `ZohoCRM.users.READ`, which the record
endpoints do not. If the scope is missing the error says so explicitly rather
than surfacing a bare 401.

## zoho.plan_update

Works out what a bulk update would change, without changing anything. Snapshots
the current value of every field being written and hashes them.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | |
| `query` | string | yes | COQL SELECT choosing the records |
| `changes` | object | yes | field api_name to new value |
| `max_records` | number | no | refuse past this, max 100 |

```json
{
  "module": "Leads",
  "query": "select Last_Name from Leads where Lead_Status = 'Lost Lead'",
  "changes": { "Lead_Status": "Contacted" }
}
```

```json
{
  "ok": true,
  "module": "Leads",
  "count": 3,
  "would_change": 2,
  "fields": ["Lead_Status"],
  "expires_in_minutes": 60,
  "records": [
    {"id": "1352736000000533111", "before": {"Lead_Status": "Lost Lead"}},
    {"id": "1352736000000533106", "before": {"Lead_Status": "Lost Lead"}},
    {"id": "1352736000000533107", "before": {"Lead_Status": "Contacted"}}
  ],
  "summary": "3 records matched, 2 would actually change on Lead_Status. Run zoho.apply_update with the same module, query and changes to commit."
}
```

`count` is what matched; `would_change` excludes records already holding the
target value. The `records` array is your rollback data: it holds every prior
value.

**Errors.** A query matching nothing raises rather than planning an empty
change. Field names must be plain api_names; anything else is refused before it
reaches COQL.

## zoho.plan_delete

The same idea for deletions. Snapshots `Modified_Time` on every match, so a
record edited between the plan and the approval blocks the delete.

| input | type | required |
|---|---|---|
| `module` | string | yes |
| `query` | string | yes |

```json
{ "module": "Leads", "query": "select Last_Name from Leads where Lead_Status = 'Junk Lead'" }
```

```json
{
  "ok": true,
  "module": "Leads",
  "count": 4,
  "expires_in_minutes": 60,
  "records": [{"id": "1352736000000533120", "before": {"Modified_Time": "2026-07-29T05:10:34+05:30"}}],
  "summary": "4 records in Leads would go to the recycle bin, recoverable for 60 days. Run zoho.apply_delete with the same module and query to commit."
}
```

## zoho.plan_handover

Reports everything a departing user owns across Leads, Deals, Contacts and
Accounts. Closed deals are held back unless asked for, and the excluded count
is reported so nothing is hidden.

| input | type | required | notes |
|---|---|---|---|
| `from_user` | string | yes | user id, email, or full name |
| `to_user` | string | yes | same |
| `modules` | array | no | defaults to the four above |
| `closed_deals` | string | no | `skip` (default) or `include` |

```json
{ "from_user": "priya@acme.in", "to_user": "sam@acme.in" }
```

```json
{
  "ok": true,
  "from_user": "Priya R",
  "to_user": "Sam T",
  "modules": ["Leads", "Deals", "Contacts", "Accounts"],
  "counts": {"Leads": 5, "Deals": 8, "Contacts": 14, "Accounts": 14},
  "total": 41,
  "closed_deals_excluded": 2,
  "expires_in_minutes": 60,
  "summary": "Priya R owns 5 leads, 8 deals, 14 contacts, 14 accounts (41 records) to move to Sam T. 2 closed deals were excluded; re-run with closed_deals=include to move them."
}
```

Users resolve by id, exact email, or a unique substring of name or email. An
ambiguous match refuses rather than guessing, and asks for the id.

**Errors.** Same person for both raises. A user who owns nothing raises rather
than planning a no-op.

---

# Writes

Every command below is `write_requires_approval`. The cycle is
preview, approve, execute. Executing without a matching approval returns
`blocked_by_policy` with "no human approval bound to this exact payload".

## zoho.apply_update

Commits a plan from `plan_update`. Re-runs the query, re-hashes the current
state, and refuses if anything moved.

Inputs are identical to `plan_update` minus `max_records`: `module`, `query`,
`changes`.

```json
{
  "module": "Leads",
  "query": "select Last_Name from Leads where Lead_Status = 'Lost Lead'",
  "changes": { "Lead_Status": "Contacted" }
}
```

```json
{
  "ok": true,
  "action": "apply plan to Leads",
  "succeeded": 3,
  "failed": 0,
  "records_applied": 3,
  "ids": ["1352736000000533111", "1352736000000533106", "1352736000000533107"],
  "errors": [],
  "origin": {"initiated_via": "railcall-airlock", "stamp": "2026-07-29T15:22:15Z"}
}
```

On drift:

```
Refusing to apply. The records moved since the plan was made: 3 now match the
query and the state fingerprint is sha256:e591e62d..., not sha256:a3f1c088...
Re-run zoho.plan_update and review the new plan.
```

A record that newly matches the filter counts as drift too, since the approved
set is no longer the set in front of you.

## zoho.apply_delete

Commits a plan from `plan_delete`. Inputs: `module`, `query`.

```json
{ "module": "Leads", "query": "select Last_Name from Leads where Lead_Status = 'Junk Lead'" }
```

```json
{ "ok": true, "action": "apply delete plan to Leads", "succeeded": 4, "failed": 0, "records_deleted": 4 }
```

Records go to the recycle bin and are recoverable for 60 days. This does not
purge.

## zoho.apply_handover

Commits a plan from `plan_handover`. Re-scans what the leaver owns and refuses
if the set changed. Inputs are identical to the plan: `from_user`, `to_user`,
and optionally `modules` and `closed_deals`, which must match what was planned.

```json
{ "from_user": "priya@acme.in", "to_user": "sam@acme.in" }
```

```json
{
  "ok": true,
  "action": "handover Priya R to Sam T",
  "moved": 41,
  "failed": 0,
  "per_module": {"Leads": 5, "Deals": 8, "Contacts": 14, "Accounts": 14},
  "errors": [],
  "origin": {"initiated_via": "railcall-airlock", "stamp": "2026-07-29T15:22:15Z"}
}
```

Reassignment runs in batches of 100 per module. The receipt is your handover
record: who moved what, to whom, approved by whom, when.

## zoho.create_record

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | |
| `records` | array | yes | up to 100 objects keyed by field api_name |
| `trigger` | array | no | `workflow`, `approval`, `blueprint`; empty array suppresses all |

```json
{
  "module": "Leads",
  "records": [{ "Last_Name": "Rao", "Company": "Vertex", "Email": "p.rao@vertex.co" }],
  "trigger": []
}
```

```json
{ "ok": true, "action": "create Leads", "succeeded": 1, "failed": 0,
  "ids": ["1352736000000542001"], "errors": [] }
```

Passing `"trigger": []` stops Zoho's own workflows firing, which matters when
importing historical data.

## zoho.update_record

Same shape as `create_record`, including the optional `trigger` array, but
every record must carry an `id`.

```json
{
  "module": "Leads",
  "records": [{ "id": "1352736000000542001", "Lead_Status": "Contacted" }]
}
```

```json
{ "ok": true, "action": "update Leads", "succeeded": 1, "failed": 0 }
```

This is the direct form and has no drift check. Use `plan_update` when the
target is a query rather than a list of ids you already hold.

## zoho.upsert_records

Inserts, or updates in place when a duplicate is found. Safe to re-run.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | |
| `records` | array | yes | |
| `duplicate_check_fields` | array | no | defaults to the module's unique fields |

```json
{
  "module": "Leads",
  "records": [{ "Last_Name": "Rao", "Company": "Vertex Ltd", "Email": "p.rao@vertex.co" }],
  "duplicate_check_fields": ["Email"]
}
```

```json
{ "ok": true, "action": "upsert Leads", "succeeded": 1, "failed": 0 }
```

Run twice with the same email and you get one record, updated. That is the
difference between this and `create_record`.

## zoho.delete_record

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | |
| `record_ids` | array | yes | up to 100 numeric ids |

```json
{ "module": "Leads", "record_ids": ["1352736000000542001"] }
```

```json
{ "ok": true, "action": "delete Leads", "succeeded": 1, "failed": 0 }
```

Recycle bin, not purge. This is the direct form; `plan_delete` is the
query-driven one with a drift check.

## zoho.convert_lead

Turns a qualified Lead into a Contact and Account, optionally opening a Deal.

| input | type | required | notes |
|---|---|---|---|
| `lead_id` | string | yes | |
| `assign_to` | string | no | user id; use `list_users` to find one |
| `deal` | object | no | supply to open a Deal |

```json
{
  "lead_id": "1352736000000533104",
  "deal": { "Deal_Name": "Vertex renewal", "Closing_Date": "2026-12-31", "Stage": "Qualification" }
}
```

```json
{
  "ok": true,
  "lead_id": "1352736000000533104",
  "contact_id": "1352736000000546002",
  "contact_name": "Michael Ruta",
  "account_id": "1352736000000546001",
  "account_name": "Buckley Miller & Wright",
  "deal_id": "",
  "deal_name": "",
  "message": "The record has been converted successfully"
}
```

Conversion is not reversible. `Closing_Date` must be `YYYY-MM-DD` and `Stage`
must match a stage in your pipeline, or the whole conversion fails.

## zoho.add_note

Writes an automation's reasoning back onto a record so a human can audit it
later.

| input | type | required |
|---|---|---|
| `module` | string | yes |
| `record_id` | string | yes |
| `content` | string | yes |
| `title` | string | no |

```json
{
  "module": "Leads",
  "record_id": "1352736000000533111",
  "title": "Scored by pipeline",
  "content": "Moved to Contacted: opened the last three emails, no reply in 14 days."
}
```

```json
{ "ok": true, "action": "add note to Leads/1352736000000533111", "succeeded": 1, "failed": 0 }
```

Note bodies are truncated at 32,000 characters.

---

# Errors you will actually hit

**`SYNTAX_ERROR` near `where`** - COQL requires a WHERE clause on every query,
and rejects three chained `!=` on one column. Use `not in ('a','b','c')`.

**`OAUTH_SCOPE_MISMATCH`** - `ZohoCRM.coql.READ` is not covered by
`modules.ALL`, and `ZohoCRM.users.READ` is separate again. The error names the
missing scope.

**HTTP 200 with failures inside** - Zoho reports partial batch failure with a
success status. Read `succeeded` and `failed`, never the status code. Per-record
reasons are in `errors`.

**"was not retried"** - a write that hit a 5xx or a dropped connection is not
retried, because Zoho has no idempotency header and a retried create leaves a
duplicate. Check Zoho, then re-approve.

**"No current plan"** - apply must repeat the planned inputs exactly. Plans
expire after 60 minutes.

**"matches more than 2000 records"** - a scan refuses rather than acting on a
partial set. Narrow the query.

# Limits

100 records per write call. 200 per COQL page, paged automatically to 2000.
Bulk and Notification APIs are not wrapped: both are asynchronous or
push-based and do not fit a synchronous handler. Activities, attachments and
tags are not covered yet.
