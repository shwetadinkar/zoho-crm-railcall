# shweta/zoho-crm

Eighteen commands for Zoho CRM. Reads run straight through. Writes stop at the airlock, and anything changing a set of records checks that set again before committing.

## Why

An approval binds to the inputs a person saw, not to the records they point at. Approve a change across 80 leads, someone edits nine while it sits there, and the write lands on state nobody reviewed. Zoho has no undo.

`plan_update`, `plan_delete` and `plan_handover` snapshot state and hash it first. The matching `apply_` re-reads, re-hashes, and refuses if anything moved. Scans page to completion, so a set is never half-reported.

For teams too small for a compliance function but regulated enough that someone will eventually ask who changed a client record.

## Install

```
railcall market install shweta/zoho-crm
```

## Credentials

Self Client at api-console.zoho.com. Scopes:

```
ZohoCRM.modules.ALL,ZohoCRM.settings.fields.READ,ZohoCRM.users.READ,ZohoCRM.coql.READ
```

`ZohoCRM.org.READ` is optional; it only fills in org details.

Exchange the grant code for a refresh token, then save a vault entry named `zoho`:

```json
{
  "refresh_token": "1000....",
  "client_id": "1000....",
  "client_secret": "....",
  "token_url": "https://accounts.zoho.in/oauth/v2/token",
  "instance_url": "https://www.zohoapis.in"
}
```

Swap `.in` for your region. A token minted in one datacenter will not authenticate against another. Nothing is read from the process environment.

Run `zoho.verify_connection` first. It probes every scope and names any that are missing, and which commands they block, so a bad setup surfaces immediately rather than as a confusing error three commands later.

## Example

```
zoho.plan_update
  module:  Leads
  query:   select Last_Name from Leads where Lead_Status = 'Lost Lead'
  changes: {"Lead_Status": "Contacted"}
```

Someone edits one of those leads. Then `zoho.apply_update`, same three inputs:

```
Refusing to apply. The records moved since the plan was made: 3 now match
the query and the state fingerprint is sha256:e591e62d..., not sha256:a3f1c088...
```

## Commands

Read: `verify_connection` `describe_module` `search_records` `list_records` `get_record` `list_users` `plan_update` `plan_delete` `plan_handover`

Write, approval required: `apply_update` `apply_delete` `apply_handover` `create_record` `update_record` `upsert_records` `delete_record` `convert_lead` `add_note`

`delete_record` and `convert_lead` act on ids you pass directly; `plan_delete` is the query-driven form.

Every command, with examples and errors: [COMMANDS.md](COMMANDS.md)

## Gotchas

COQL needs a WHERE clause. `select Email from Leads limit 3` returns 400 SYNTAX_ERROR with no hint.

COQL also rejects three chained `!=` on one column; use `not in ('a','b','c')`.

`ZohoCRM.coql.READ` is not covered by `modules.ALL`, and the failure looks like a bad query.

Zoho answers HTTP 200 when part of a batch fails. Read `succeeded` and `failed`.

Writes are not retried after a 5xx: Zoho has no idempotency header, so a retry duplicates.

Plans expire after an hour, and apply must repeat the planned inputs exactly.

## Limits

100 records per write call, 2000 per scan before it refuses rather than half-reporting. Deletes go to the recycle bin, recoverable 60 days. Bulk and Notification APIs are not wrapped, being async and push-based. Activities, attachments and tags are not covered. The manifest declares `subprocess: false`; enforcement is at the Python import layer, not a container.

Tested against Zoho CRM v8. All eighteen commands run against a live org.
