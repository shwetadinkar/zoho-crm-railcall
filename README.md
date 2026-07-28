# shweta/zoho-crm

Twelve commands for Zoho CRM. Reads run straight through. Writes stop at the airlock and wait for a human.

## Why

Zoho Flow and Zapier will both run your automation. Neither will stop it, and neither leaves a record of who said yes. That gap matters, because Zoho has no undo: one bad filter on a mass update rewrites 400 client records and your only option is a restore request.

Written for teams too small to have a compliance function but regulated enough that someone will eventually ask who changed a client record and when. Also for anyone handing an AI agent write access to a CRM.

## Install

```
railcall market install shweta/zoho-crm
```

## Credentials

Create a Self Client at api-console.zoho.com. Scopes:

```
ZohoCRM.modules.ALL,ZohoCRM.settings.fields.READ,ZohoCRM.users.READ,ZohoCRM.coql.READ
```

`ZohoCRM.org.READ` is optional; it only fills in org details on verify_connection.

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

Swap `.in` for your region (`.com`, `.eu`, `.com.au`, `.jp`). A token minted in one datacenter will not authenticate against another. Nothing is read from the process environment.

Run `zoho.verify_connection` first. It tells you whether the problem is the token or the region.

## Example

```
zoho.search_records
query: select Last_Name, Company from Leads where Last_Name is not null limit 2
```

```json
{"ok": true, "count": 2, "more_records": true,
 "records": [
   {"id": "1352736000000533111", "Last_Name": "Maclead", "Company": "Rangoni Of Florence"},
   {"id": "1352736000000533106", "Last_Name": "Lace", "Company": "Printing Dimensions"}]}
```

## Commands

Read: `verify_connection` `describe_module` `search_records` `list_records` `get_record` `list_users`

Write, approval required: `create_record` `update_record` `upsert_records` `delete_record` `convert_lead` `add_note`

`describe_module` is the one to call first from an agent. It returns the real api_name of every field including custom ones, so writes stop guessing.

## Things that will bite you

COQL needs a WHERE clause. `select Email from Leads limit 3` comes back 400 SYNTAX_ERROR with no explanation. Add `where Last_Name is not null`.

`ZohoCRM.coql.READ` is not covered by `modules.ALL`. Easy to miss, and the failure looks like a bad query rather than a missing scope.

Zoho answers HTTP 200 when part of a batch fails. Read `succeeded` and `failed` in the response, not the status code.

Writes are not retried after a 5xx. Zoho has no idempotency header, so a retried create leaves a duplicate. You get a loud error and re-approve instead.

## Limits

100 records per write call. `delete_record` moves to the recycle bin, recoverable 60 days; it does not purge. Bulk and Notification APIs are not wrapped, being async and push-based respectively.

Tested against Zoho CRM v8 on the India datacenter. All twelve commands run against a live org.
