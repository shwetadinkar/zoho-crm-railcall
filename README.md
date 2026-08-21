# shweta/zoho-crm

Thirty commands for Zoho CRM. Reads run straight through. Writes stop at the airlock, and anything changing a set re-checks it first.

## Why

An approval binds to the inputs a person saw, not the records they point at. Approve a change over 80 leads, someone edits nine while it waits, and the write lands on state nobody reviewed.

`plan_update`, `plan_delete`, `plan_handover` and `plan_merge` snapshot state and hash it first. The matching `apply_` re-reads, re-hashes and refuses if anything moved. Scans page to completion, so a set is never half-reported.

Zoho has no undo, so the module keeps its own: every applied change goes into a hash-chained ledger holding the prior values, so `plan_rollback` restores them and `audit_pack` shows every refusal. A write Zoho never answered is kept too; `reconcile_writes` reports what can be told about it.

Merging is the sharpest case: routine, irreversible, and Zoho's bin entry has no name and no deleted-by. `apply_merge` archives the loser's full record first - afterwards it is the only readable copy.

For teams too small for a compliance function but regulated enough someone eventually asks who changed a record.

## Install

```
railcall market install shweta/zoho-crm
```

## Credentials

Self Client at api-console.zoho.com, scopes:

```
ZohoCRM.modules.ALL,ZohoCRM.settings.fields.READ,ZohoCRM.users.READ,ZohoCRM.coql.READ
```

Exchange the grant code for a refresh token, then save a vault entry named `zoho` with `refresh_token`, `client_id`, `client_secret`, `token_url`, `instance_url`. Swap `.in` for your region: a token minted in one datacenter will not work in another.

Run `zoho.verify_connection` first: it names any missing scope and what it blocks.

## Example

Run `zoho.plan_update` with a module, query and `changes`. Someone edits one of those leads. Then `zoho.apply_update`, same inputs:

```
Refusing to apply. The records moved since the plan was made:
the state fingerprint is sha256:e591e62d..., not sha256:a3f1c088...
```

## Commands

Read: `verify_connection` `describe_module` `search_records` `list_records` `get_record` `list_users` `plan_update` `plan_delete` `plan_handover` `plan_rollback` `plan_merge` `plan_upsert` `hygiene_scan` `scan_changes` `check_readiness` `verify_ledger` `audit_pack` `reconcile_writes`

Write, approval required: `apply_update` `apply_delete` `apply_handover` `apply_rollback` `apply_merge` `apply_upsert` `create_record` `update_record` `upsert_records` `delete_record` `convert_lead` `add_note`

`hygiene_scan` answers a question rather than wrapping an endpoint: stale records, overdue deals, records owned by someone who left.

`scan_changes` runs on a schedule and names records changed since the last run with no approval behind them. The ledger sees only this module's writes; `Modified_Time` does not, so UI edits become visible. Ungoverned *edits* only: a deletion leaves nothing to poll.

Both return counts inline and send records to a file: receipts redact identifiers.

Every command, with examples and errors: [COMMANDS.md](COMMANDS.md)

## Limits

100 records per write call, 2000 per scan. `plan_upsert` pages larger sets; its drift check covers existing records only. Deletes go to the recycle bin for 60 days; rollback covers updates only. Bulk and Notification APIs are not wrapped. The manifest declares `subprocess: false` and an empty `allowed_destinations`: a signed declaration nothing here calls an LLM.

Any write can be capped per day in `rate_limits.json`; a block does not consume its approval.

Tested against Zoho CRM v8, twenty-nine of the thirty against a live org.
