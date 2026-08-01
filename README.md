# shweta/zoho-crm

Twenty-two commands for Zoho CRM. Reads run straight through. Writes stop at the airlock, and anything changing a set of records checks that set again before committing.

## Why

An approval binds to the inputs a person saw, not to the records they point at. Approve a change across 80 leads, someone edits nine while it sits there, and the write lands on state nobody reviewed. Zoho has no undo.

`plan_update`, `plan_delete` and `plan_handover` snapshot state and hash it first. The matching `apply_` re-reads, re-hashes, and refuses if anything moved. Scans page to completion, so a set is never half-reported.

The fingerprint covers `Modified_Time` too, so an edit to *any* column on a matched record refuses the write, not just to the field being changed.

And since Zoho has no undo, the module keeps its own. Every applied change goes into a local hash-chained ledger holding the prior values, so `plan_rollback` restores them and `audit_pack` shows every change and every refusal. The refusals are the useful half: they are the evidence the control fired.

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

`ZohoCRM.org.READ` is optional.

Exchange the grant code for a refresh token, then save a vault entry named `zoho` holding `refresh_token`, `client_id`, `client_secret`, `token_url` and `instance_url`.

Swap `.in` for your region: a token minted in one datacenter will not authenticate against another. Nothing is read from the process environment.

Run `zoho.verify_connection` first. It probes every scope and names any that are missing and which commands they block, so a bad setup surfaces immediately.

## Example

Run `zoho.plan_update` on Leads with a query and a `changes` object. Someone edits one of those leads. Then `zoho.apply_update`, same three inputs:

```
Refusing to apply. The records moved since the plan was made: 3 now match
the query and the state fingerprint is sha256:e591e62d..., not sha256:a3f1c088...
```

## Commands

Read: `verify_connection` `describe_module` `search_records` `list_records` `get_record` `list_users` `plan_update` `plan_delete` `plan_handover` `plan_rollback` `verify_ledger` `audit_pack`

Write, approval required: `apply_update` `apply_delete` `apply_handover` `apply_rollback` `create_record` `update_record` `upsert_records` `delete_record` `convert_lead` `add_note`

`delete_record` and `convert_lead` act on ids you pass directly; `plan_delete` is the query-driven form. `plan_rollback` undoes a previous `apply_update`, and refuses on drift like any other write.

Every command, with examples, errors and COQL gotchas: [COMMANDS.md](COMMANDS.md)

## Limits

100 records per write call, 2000 per scan before it refuses rather than half-reporting. Deletes go to the recycle bin, recoverable 60 days; rollback covers updates, not deletes. Bulk and Notification APIs, Activities, attachments and tags are not covered. The manifest declares `subprocess: false`; enforcement is at the Python import layer, not a container.

Any write can be capped per day in the station's `rate_limits.json`. Module commands have no cap until you set one, and a blocked attempt does not consume its approval.

Tested against Zoho CRM v8. All twenty-two commands run against a live org.
