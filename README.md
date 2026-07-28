# shweta/zoho-crm

Fourteen commands for Zoho CRM. Reads run straight through. Writes stop at the airlock and wait for a human. Bulk updates go further and check the records again before committing.

## Why

An approval binds to the inputs a person saw, not to the records they point at. Approve a change across 80 leads, someone edits nine while the approval sits there, and the write lands on state nobody reviewed. Zoho has no undo; the only recourse is a restore request.

`plan_update` snapshots the fields it is about to change and hashes them. `apply_update` re-runs the same query, re-hashes, and refuses if anything moved. The snapshot is also your rollback data, since it holds every prior value.

Written for teams too small to have a compliance function but regulated enough that someone will eventually ask who changed a client record and when.

## Install

```
railcall market install shweta/zoho-crm
```

## Credentials

Self Client at api-console.zoho.com. Scopes:

```
ZohoCRM.modules.ALL,ZohoCRM.settings.fields.READ,ZohoCRM.users.READ,ZohoCRM.coql.READ
```

`ZohoCRM.org.READ` is optional and only fills in org details on verify_connection.

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

Swap `.in` for your region. A token minted in one datacenter will not authenticate against another. Nothing is read from the process environment. Run `zoho.verify_connection` first.

## Example

```
zoho.plan_update
  module:  Leads
  query:   select Last_Name from Leads where Lead_Status = 'Lost Lead'
  changes: {"Lead_Status": "Contacted"}
```

```
3 records matched, 3 would actually change on Lead_Status
```

Someone edits one of those leads. Then:

```
zoho.apply_update  (same module, query and changes)
```

```
Refusing to apply. The records moved since the plan was made:
3 now match the query and the state fingerprint is sha256:e591e62d..., not sha256:a3f1c088...
```

## Commands

Read: `verify_connection` `describe_module` `search_records` `list_records` `get_record` `list_users` `plan_update`

Write, approval required: `apply_update` `create_record` `update_record` `upsert_records` `delete_record` `convert_lead` `add_note`

## Things that will bite you

COQL needs a WHERE clause. `select Email from Leads limit 3` returns 400 SYNTAX_ERROR with no explanation.

`ZohoCRM.coql.READ` is not covered by `modules.ALL`, and the failure looks like a bad query rather than a missing scope.

Zoho answers HTTP 200 when part of a batch fails. Read `succeeded` and `failed`, not the status code.

Writes are not retried after a 5xx. Zoho has no idempotency header, so a retried create leaves a duplicate.

Plans expire after an hour. Apply with exactly the module, query and changes you planned, or it will tell you no plan exists.

## Limits

100 records per write call. `delete_record` moves to the recycle bin, recoverable 60 days. Bulk and Notification APIs are not wrapped, being async and push-based. The manifest declares `subprocess: false`; enforcement is at the Python import layer, not a container.

Tested against Zoho CRM v8. All fourteen commands run against a live org.
