# shweta/zoho-crm - command reference

Twenty-nine commands. Seventeen reads, which run without approval. Twelve
writes, which stop at the airlock until a human approves the exact payload.

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
the current value of every field being written, plus `Modified_Time`, and hashes
them together.

`Modified_Time` is in the fingerprint but never in the payload. Without it the
hash covers only the fields being written, so an edit to any *other* column on a
matched record would pass unnoticed and the write would land on state nobody
reviewed. Reading it costs one extra column and closes that gap.

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
    {"id": "1352736000000533111", "before": {"Lead_Status": "Lost Lead", "Modified_Time": "2026-07-29T18:44:09+05:30"}},
    {"id": "1352736000000533106", "before": {"Lead_Status": "Lost Lead", "Modified_Time": "2026-07-28T11:02:55+05:30"}},
    {"id": "1352736000000533107", "before": {"Lead_Status": "Contacted", "Modified_Time": "2026-07-30T09:15:01+05:30"}}
  ],
  "summary": "3 records matched, 2 would actually change on Lead_Status. Run zoho.apply_update with the same module, query and changes to commit."
}
```

`count` is what matched; `would_change` excludes records already holding the
target value. The `records` array is your rollback data: it holds every prior
value, and `apply_update` writes it into the ledger so `plan_rollback` can read
it back long after the plan itself has expired.

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

## zoho.plan_rollback

Works out what undoing a previously applied bulk update would restore. Changes
nothing.

Takes the same three inputs that performed the apply. There is no rollback token:
a token would be an identifier, and receipts redact identifiers, so it could not
survive the round trip. The module, query and changes you already typed are the
address.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | the module that was applied |
| `query` | string | yes | the same COQL SELECT used for the apply |
| `changes` | object | yes | the same changes object used for the apply |

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
  "applied_at": "2026-08-01T07:02:19Z",
  "count": 2,
  "fields": ["Lead_Status"],
  "records": [
    {"id": "1352736000000533111", "restore_to": {"Lead_Status": "Lost Lead"}},
    {"id": "1352736000000533106", "restore_to": {"Lead_Status": "Lost Lead"}}
  ],
  "changed_again": [],
  "missing": [],
  "expires_in_minutes": 60,
  "summary": "Would restore 2 records on Lead_Status to their values before the apply of 2026-08-01T07:02:19Z. 0 have been changed again since and 0 no longer exist; apply_rollback refuses while any record has moved."
}
```

`changed_again` lists records that no longer hold the value the apply wrote,
meaning somebody has edited them since. `missing` lists records that no longer
exist. Both are reported rather than skipped: a blind restore would discard
whatever the other person did.

**Errors.** Raises if the ledger holds no applied change for these three inputs.
Also raises for entries written before 0.6.0, which do not carry prior values.

## zoho.plan_merge

Works out exactly what a merge would destroy, without merging anything.
Read-only.

Merge is the most destructive operation this module reaches, and the least
visible afterwards. Verified against a live org on 2026-08-03:

- the master's field values win; the loser's are lost with no record
- notes, tasks, calls, events and attachments are REPARENTED to the master
- the losing record is gone: a direct fetch returns 204, and it is invisible
  to COQL
- its recycle-bin entry has no `display_name` and no `deleted_by`, so an
  operator opening the bin sees an unidentifiable blank row
- `PUT /actions/restore` answers HTTP 200 with an empty body and does nothing

So there is no undo, no usable bin entry, and no attribution. And merging is
routine - housekeeping, done casually, by whoever notices the duplicate. This
preview is the only chance to notice the wrong record was chosen as master.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | `Leads` or `Contacts` |
| `master_id` | string | yes | the record that survives |
| `loser_ids` | array | yes | records to merge in, max 3 per call |

```json
{
  "module": "Leads",
  "master_id": "1352736000000533111",
  "loser_ids": ["1352736000000533106"]
}
```

```json
{
  "ok": true,
  "module": "Leads",
  "master_id": "1352736000000533111",
  "count": 1,
  "conflicting_fields": 2,
  "related_records_moving": 12,
  "irreversible": true,
  "losers": [
    {
      "id": "1352736000000533106",
      "conflicts": [
        {"field": "Phone", "master": "+91 98200 11111", "loser": "+91 99300 22222"},
        {"field": "Lead_Status", "master": "Contacted", "loser": "Qualified"}
      ],
      "only_on_loser": [{"field": "Secondary_Email", "loser": "ada.l@acme.com"}],
      "related": {
        "Notes": {"count": 9, "titles": ["Call 12 Mar", "Renewal terms"]},
        "Tasks": {"count": 3, "titles": ["Follow up"]},
        "Calls": {"count": 0, "titles": []},
        "Events": {"count": 0, "titles": []},
        "Attachments": {"count": 0, "titles": []}
      },
      "modified_time": "2026-07-29T18:44:09+05:30",
      "modified_by": "Shweta D"
    }
  ]
}
```

`conflicts` is the point of the command. The master wins silently, so an
operator has to see the phone number that is about to disappear while they can
still swap which record is the master. `only_on_loser` is the opposite case:
values the master lacks, which survive the merge.

**System stamps are filtered out.** `Created_Time`, `Modified_Time`,
`Last_Activity_Time`, `Change_Log_Time__s`, anything in Zoho's `$` namespace
and anything ending `_Time` always differ between two records and are never
something an operator can act on. A live preview of two test records reported
seven conflicts, four of which were timestamps - enough noise to bury the
three that mattered. They are counted in `system_fields_differing` rather than
dropped silently.

**Restricted to Leads and Contacts.** Merge is verified on those two. Accounts
and Deals may behave differently and this command will not guess about an
operation that cannot be undone.

**Related lists are read through COQL**, not the nested REST route. On v8,
`GET Leads/{id}/Notes` and its siblings answer 400 REQUIRED_PARAM_MISSING;
`select id from Tasks where What_Id = '...'` works. A related list that cannot
be read is reported as `count: null` rather than silently counted as zero.

**Errors.** Raises if the module is not Leads or Contacts, if `master_id`
appears in `loser_ids`, if `loser_ids` holds duplicates or more than three ids,
or if any record does not exist.

## zoho.plan_upsert

Works out which records a bulk upsert would create and which it would update,
without writing anything. Read-only.

`upsert_records` handles one call of up to 100. This is the governed form for a
larger set: it pages the duplicate-check lookup to completion, reports the
split, and fingerprints the records that already exist so `apply_upsert` can
refuse if they move.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | module API name |
| `records` | array | yes | objects to insert or update, up to 2000 |
| `duplicate_check_fields` | array | yes | fields Zoho matches on |

```json
{
  "module": "Leads",
  "records": [
    {"Email": "ada@acme.com", "Last_Name": "Lovelace", "Lead_Status": "Contacted"},
    {"Email": "grace@acme.com", "Last_Name": "Hopper", "Lead_Status": "New"}
  ],
  "duplicate_check_fields": ["Email"]
}
```

```json
{
  "ok": true,
  "module": "Leads",
  "duplicate_check_fields": ["Email"],
  "total": 2,
  "will_update": 1,
  "will_create": 1,
  "calls_required": 1,
  "updates": [
    {"id": "1352736000000533111", "matched_on": "Email",
     "matched_value": "ada@acme.com",
     "modified_time": "2026-07-29T18:44:09+05:30"}
  ],
  "creates": [{"Email": "grace@acme.com"}],
  "drift_covers": "the 1 records that already exist; the 1 being created have no prior state to move"
}
```

**The drift guarantee here is narrower than `plan_update`'s, and the output
says so.** A record that does not exist yet has no prior state, so there is
nothing to fingerprint and nothing that can move. `apply_upsert` re-checks the
records in `updates`; the ones in `creates` are unguarded by construction.

Every record must carry the duplicate-check fields. Without them the plan
cannot tell an insert from an update, so the command refuses rather than
guessing.

The lookup is one COQL per check field rather than one per record: a set of 500
would otherwise be 500 round trips.

**Errors.** Raises if `duplicate_check_fields` is missing, if any record lacks
one of them, or if the set exceeds 2000 records.

## zoho.hygiene_scan

Finds what is quietly rotting in the CRM. Read-only, changes nothing.

Zoho's own reports will give you these numbers. What they will not do is hand
the result to a governed write. Every finding here names the command that fixes
it and carries the COQL that produced it - in the report file, for the reason
below - and that command still plans, still fingerprints, still refuses on
drift.

| input | type | required | notes |
|---|---|---|---|
| `stale_days` | number | no | days without a change that counts as stale, default 90 |
| `include` | array | no | restrict to named checks; omit to run all |
| `sample` | number | no | example records per finding, max 25, default 5 |

Checks run:

| key | what it finds | why it matters |
|---|---|---|
| `stale_leads` | leads untouched for `stale_days` | dead, or being ignored |
| `stale_deals` | open deals untouched for `stale_days` | a forecast that is quietly wrong |
| `overdue_deals` | deals past their closing date, still open | the pipeline and the calendar disagree |
| `leads_no_email` | leads with no email | nothing automated can reach them |
| `contacts_no_email` | contacts with no email | same, on records that matter more |
| `orphaned_*` | records owned by a deactivated user | nobody is working these and nobody knows |

```json
{ "stale_days": 120, "sample": 3 }
```

```json
{
  "ok": true,
  "stale_days": 120,
  "issues_found": 2,
  "records_affected": 47,
  "deactivated_users": ["Priya N"],
  "report_path": "/home/you/.railcall/station/.railcall_workspace/hygiene_scan.20260807T151227Z.json",
  "findings": [
    {
      "check": "orphaned_leads",
      "module": "Leads",
      "label": "Leads owned by a deactivated user",
      "why_it_matters": "Owned by Priya N, who is no longer active. Nobody is working these and nobody knows it.",
      "count": 31,
      "fix_with": "zoho.plan_handover"
    }
  ],
  "unavailable": []
}
```

**The COQL and the matching record ids are in the file, not in the response.**
`redact_output` scrubs identifiers *and* date-shaped values before sealing a
receipt: a sample row comes back as `"id": "[account]"`,
`"Closing_Date": "[date]"`, and a query loses its date literal - which makes it
unpastable, and pasting it into `plan_update` is the one thing it is for.
Verified on station v0.61.

So the counts, labels and reasoning stay inline where an operator can scan
them, and the actionable half goes to `report_path`. The file holds each
finding's full `query` and `sample` rows, unredacted.

**A check that cannot run is reported, not counted as zero.** If a field name
is rejected by your org, the check lands in `unavailable` with the error. A
hygiene report that quietly under-reports is worse than one that admits a gap.

Checks with no matches are omitted entirely - silence is the healthy case, and
a list of zeroes buries the findings that matter.

`fix_with` names `zoho.plan_handover` for orphaned records and
`zoho.plan_update` for the rest. Open the report file and feed the finding's
`query` straight to that plan command.

**Errors.** Raises if `stale_days` is below 1. Note that `0` is rejected
rather than treated as absent.

## zoho.scan_changes

Reports records changed since the last run, and which of those nobody governed.
Read-only, changes nothing.

This exists to narrow the ledger's largest stated limit. The ledger sees only
what this module wrote, so an edit made in the Zoho UI is invisible to it.
`Modified_Time` is not, so a scan can name changes that never passed through an
approval.

A change counts as governed when this module recorded the exact post-write
`Modified_Time` Zoho returned for it. Zoho sends that value on every SUCCESS row
and it is byte-identical to a later read, so the match is exact rather than a
tolerance window - an edit landing one second after an approved write is still
reported instead of hiding behind it.

Designed for the station's incremental scheduler. The station holds the
position and injects `since`; the module stores no watermark of its own, so it
cannot silently skip a window with nothing in a receipt to show for it. The
command still runs by hand, in which case it scans everything.

| input | type | required | notes |
|---|---|---|---|
| `modules` | array | no | Zoho API names to scan, default Leads, Contacts, Accounts, Deals |
| `limit` | number | no | a safety cap per module, default 500 - not a selector |
| `since` | string | no | ISO-8601 UTC, injected by the scheduler |
| `exclude_ids` | array | no | change refs already delivered, injected by the scheduler |

```json
{ "modules": ["Leads", "Deals"] }
```

```json
{
  "ok": true,
  "count": 44,
  "since": null,
  "rows_scanned": 44,
  "skipped_already_delivered": 0,
  "ungoverned_count": 44,
  "truncated": false,
  "report_path": "/home/you/.railcall/station/.railcall_workspace/scan_changes.20260808T182136Z.json",
  "ledger_covers_from": "2026-08-01T07:02:19Z",
  "unmatchable_ledger_entries": 6,
  "summary": "44 changes since the start of the scan window. 44 have no matching ledger entry."
}
```

**The records are in the file, not the response.** `redact_output` scrubs
identifiers before sealing a receipt, so a record id comes back `[account]` and
the finding stops being actionable. Counts stay inline; the file holds each
ungoverned change with its id, `modified_at`, the raw offset Zoho sent, and
`modified_by`.

**The cursor identifies a change, not a record.** `change_ref` is
`{module}:{id}:{modified_at}`, so a record edited twice is two items. Keying on
the record id alone would have the scheduler suppress the second edit as
already delivered - which in an active org is the common case, not an edge.

**Ordering is ascending and mandatory.** The cap truncates and the station
advances its watermark to the newest row returned; in any other order a
truncated page strands older rows behind the mark and they are never seen again.
Hitting the cap sets `truncated: true`, and the station then refuses to advance
rather than stepping over work that was never returned.

**Timestamps are normalised in the module.** Zoho renders an explicit offset
(`2026-08-07T21:33:58+05:30`); the watermark is ISO-8601 UTC. Stripping the
offset rather than converting it would make every record look 5.5 hours newer,
drag the mark into the future, and permanently drop everything modified in that
window. An unreadable timestamp surfaces the row rather than skipping it: a row
that cannot be proved old must never silently become a row that is ignored.

**Limits, all of them.**

This finds ungoverned *edits*. A record deleted in the UI leaves nothing to
poll, and a UI merge makes the loser invisible to COQL.

Coverage starts at `ledger_covers_from`, the earliest entry in the live ledger
chain. The chain rotates past 5000 entries into sealed archives, which this does
not read.

`unmatchable_ledger_entries` counts applied entries with no recorded
`Modified_Time`: everything written before the ledger began recording it, plus
every merge, since Zoho's merge response carries no timestamp for the master.
Those are real approvals this cannot match, so the count is reported rather than
their records being quietly called ungoverned.

`modified_by` is Zoho's record of who last touched the record. For writes this
module made it is always the OAuth user, so it names a person only for changes
the module did not make.

**Errors.** Raises if `modules` is not a list, or if `since` is not a readable
ISO-8601 timestamp - scanning from the beginning of time on a bad value would
look like success while spending the day's API budget.

## zoho.check_readiness

Checks one record against the fields the module says are required, plus any you
name. Read-only.

Zoho enforces required fields on its own forms. It does **not** enforce them on
an API write, and it does not enforce your rules at all - the ones that are not
about validity but about a record being ready to act on. A deal with no closing
date saves happily and then sits in a forecast being wrong.

| input | type | required | notes |
|---|---|---|---|
| `module` | string | yes | module API name |
| `record_id` | string | yes | the record to check |
| `require` | array | no | extra field api_names that must be filled |

```json
{
  "module": "Deals",
  "record_id": "1352736000000533152",
  "require": ["Closing_Date", "Amount", "Contact_Name"]
}
```

```json
{
  "ok": true,
  "module": "Deals",
  "record_id": "1352736000000533152",
  "ready": false,
  "missing_required": ["Stage"],
  "missing_requested": [
    {"field": "Closing_Date", "note": "empty"},
    {"field": "Contract_Ref", "note": "not a field on this module"}
  ],
  "required_fields_checked": 4
}
```

Read-only fields are skipped even when the module marks them mandatory: a field
nobody can fill in is not a readiness problem.

A requested field that does not exist on the module is reported as such rather
than as empty, so a typo in `require` surfaces instead of failing silently.

## zoho.verify_ledger

Recomputes every link in the local change ledger and reports whether it holds.
Read-only.

Each ledger entry carries the hash of the entry before it, so editing or removing
any entry breaks every link after it. This walks the chain from the start and
names the first entry that fails.

No inputs.

```json
{}
```

```json
{
  "ok": true,
  "intact": true,
  "entries_verified": 14,
  "first_broken_entry": null,
  "applied": 11,
  "refused": 3,
  "unresolved": 1,
  "covers": "changes made through this module only; edits made in the Zoho UI are not visible here. An unresolved entry records a write whose outcome Zoho never confirmed - it is neither an applied change nor a refused one",
  "summary": "Ledger intact. 15 entries verified, 11 applied, 3 refused and 1 unresolved. Tamper-evident, not tamper-proof: this proves no entry was altered in place. The 1 unresolved entry is a write Zoho never returned a verdict on; the entry holds what was attempted and the prior values, so it can be checked by hand."
}
```

### The three outcome classes

`applied` and `refused` are settled: the change happened, or the module stopped
it. `unresolved` is the third case, and it is not a failure.

Zoho has no idempotency key. When a write returns no HTTP status at all, or a
5xx, there is no way to retry it safely and no way to ask afterwards whether it
landed - applied, not applied, and partially applied are all still live. The
module has always refused to retry and told you to go and check. Now it also
records what it attempted before it lost contact: the intended values, the
records in the payload, each one's prior values, and each one's `Modified_Time`
from immediately before the call.

That last field is the useful one. An untouched `Modified_Time` on a record you
re-read afterwards is near proof the write never landed, and it is the only
signal strong enough to tell "nothing happened" apart from "it happened and was
changed back".

An unresolved entry never counts towards `records_changed`. Nothing is known to
have changed.

**Three honest limits, all stated in the output rather than only here.**

It is *tamper-evident*, not tamper-proof. It proves no entry was altered in
place. Anyone who can write the file can rewrite the whole chain from any point.

It covers changes made *through this module*. Someone editing in the Zoho UI is
invisible to it. It is a record of governed changes, not an audit trail of the
org.

An unresolved entry records an *attempt*, not an outcome. Reading one tells you
what the module tried to do and what the records looked like beforehand. It does
not tell you what happened, and nothing in this module will claim otherwise.

## zoho.audit_pack

Writes the ledger out as a file for review, and returns the path and the counts.
Read-only. Covers all three outcome classes - see verify_ledger above for what
`unresolved` means.

The contents are not returned inline. Receipts redact identifiers, so anything
returned in the output would come back with its record ids stripped, which is
precisely what makes a pack worth reading. A human opens the file.

| input | type | required | notes |
|---|---|---|---|
| `since` | string | no | ISO date or datetime; omit for everything |
| `until` | string | no | ISO date or datetime; omit for everything |
| `module` | string | no | restrict to one module api_name |
| `outcome` | string | no | `applied`, `refused` or `unresolved`; omit for all three |

```json
{ "since": "2026-07-01", "outcome": "refused" }
```

```json
{
  "ok": true,
  "pack_path": "/home/you/.railcall/station/.railcall_workspace/audit_pack.20260801T071029Z.json",
  "entries": 3,
  "applied": 0,
  "refused": 3,
  "unresolved": 0,
  "records_changed": 0,
  "chain_intact": true,
  "summary": "Wrote 3 ledger entries to audit_pack.20260801T071029Z.json - 0 applied, 3 refused, 0 unresolved, 0 records changed. Chain intact. Refusals are the useful half: they are the evidence the control fired. The unresolved count is writes Zoho never confirmed either way, and is not included in records changed."
}
```

Each entry in the file holds the outcome, command, module, timestamp, the two
fingerprints where they differ, and the chain hashes. Applied entries also carry
the prior values.

An unresolved entry carries `reason` (Zoho's failure as the module saw it),
`attempted_at`, `intent`, and a `targets` list holding each record's id, its
prior values, and its `before_modified_time`. It also carries `verdict_basis`,
which says how the outcome could be established at all: `value` means compare
the recorded fields against what the record holds now; `existence` means there
is no value to compare and the question is whether the record still exists
(delete, merge); `modified_time` means the prior values were never read, so only
movement can be judged, not what it moved to (upsert).

Where a command writes in batches - `apply_upsert` and `apply_handover` - the
entry also records how far it got before contact was lost, so the batches that
definitely committed are not re-examined alongside the one that is genuinely in
doubt.

`entries_verified` in the file's `chain` block counts the *whole* ledger, not the
filtered subset, because integrity is a property of the chain rather than of your
query.

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
  "ledger_seq": 14,
  "origin": {"initiated_via": "railcall-airlock", "stamp": "2026-07-29T15:22:15Z"}
}
```

`ledger_seq` is this change's position in the local ledger. The entry holds every
prior value, which is what `zoho.plan_rollback` reads.

On drift:

```
Refusing to apply. The records moved since the plan was made: 3 now match the
query and the state fingerprint is sha256:e591e62d..., not sha256:a3f1c088...
Re-run zoho.plan_update and review the new plan.
```

A record that newly matches the filter counts as drift too, since the approved
set is no longer the set in front of you. So does an edit to a field this command
is not writing: `Modified_Time` is part of the fingerprint, so any change to a
matched record refuses.

The refusal is written to the ledger as well as the success. A stopped write is
the only direct evidence the control works, so it is kept.

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

## zoho.apply_rollback

Commits a plan from `plan_rollback`, restoring the prior values. Inputs are the
same three: `module`, `query`, `changes`.

A rollback is a write like any other and gets no special dispensation. It
re-reads, re-hashes, and refuses if the records moved since the rollback plan was
made. Undoing onto records somebody has since edited is the exact failure this
module exists to stop.

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
  "action": "roll back Leads",
  "succeeded": 2,
  "failed": 0,
  "records_restored": 2,
  "fingerprint_verified": "sha256:127366c7...",
  "ledger_seq": 15,
  "errors": []
}
```

The restore is itself recorded, with the values that were there before it ran, so
a rollback can be rolled back.

**Not available for deletes.** `plan_rollback` covers `apply_update` only. A
deleted record cannot be restored by a write; the ledger entry for a delete is a
record of what was removed, not rollback data. Zoho's recycle bin holds deletions
for 60 days.

## zoho.apply_merge

Commits a plan from `zoho.plan_merge`. Inputs are the same three: `module`,
`master_id`, `loser_ids`.

Re-reads every record involved - the master included, since its values are the
ones that win - re-hashes, and refuses if anything moved. Drift matters more
here than anywhere else in this module: an update can be rolled back and a
delete sits in the recycle bin for 60 days, but a merge cannot be taken back at
all.

```json
{
  "ok": true,
  "action": "merge into Leads/1352736000000533111",
  "master_id": "1352736000000533111",
  "succeeded": 1,
  "failed": 0,
  "merged": ["1352736000000533106"],
  "errors": [],
  "fingerprint_verified": "sha256:127366c7...",
  "ledger_seq": 41,
  "recoverable": false
}
```

**The ledger entry is written before the merge fires.** Everywhere else in this
module the ledger writes after the API call, because recording a change that
never happened is worse than missing one. Here the reasoning inverts: a merge
with no record of what the loser held is unrecoverable in a way an unrecorded
update is not, and Zoho's own bin entry is useless. If the ledger write fails,
the merge does not proceed.

That entry holds the loser's complete record and its related-list counts. After
the merge it is the only readable copy anywhere.

**One loser per API call.** Zoho accepts a list under `data[]`, but a batched
failure halfway through leaves no way to say which records were merged and
which were not. `succeeded`, `failed` and `errors` are per record.

**No rollback.** `plan_rollback` covers `apply_update` only. The ledger entry
is a reference - someone can look up the value that vanished and re-enter it by
hand - not an undo. The record itself cannot be restored, and the reparented
notes cannot be unpicked.

## zoho.apply_upsert

Commits a plan from `zoho.plan_upsert`. Inputs are the same three: `module`,
`records`, `duplicate_check_fields`.

Re-reads the records that already existed, re-hashes, and refuses if any of
them moved. Then writes in batches of 100, Zoho's ceiling.

```json
{
  "ok": true,
  "action": "upsert 250 records into Leads",
  "succeeded": 248,
  "failed": 2,
  "errors": [{"code": "DUPLICATE_DATA", "message": "duplicate data"}],
  "planned_updates": 90,
  "planned_creates": 160,
  "fingerprint_verified": "sha256:127366c7...",
  "ledger_seq": 44
}
```

**`succeeded` and `failed` come from the response body, not the status code.**
Zoho answers HTTP 200 even when some rows in a batch fail. A caller reading
only the status would report a clean run on a batch where forty records were
rejected.

On drift the refusal is written to the ledger and the command raises, same as
every other apply in this module.

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
duplicate. Check Zoho, then re-approve. The message also names a ledger entry:
the attempt is recorded with the intended values, the records in the payload,
and each one's `Modified_Time` from just before the call. Open it with
`zoho.audit_pack --outcome unresolved` before you go looking - a record whose
`Modified_Time` still matches the recorded one was almost certainly not
written.

**"No current plan"** - apply must repeat the planned inputs exactly. Plans
expire after 60 minutes.

**"No applied change in the ledger"** - `plan_rollback` addresses an apply by the
same module, query and changes that performed it. Run `zoho.audit_pack` to see
what is on record.

**"Refusing to roll back"** - the records moved between the rollback plan and the
rollback apply. Re-plan and review.

**"Refusing to merge"** - one of the records moved between the merge plan and
the apply. A merge cannot be undone, so re-plan and read the new diff.

**"Merge is verified on Leads and Contacts only"** - the module declines to
guess how merge behaves on Accounts or Deals.

**"Refusing to upsert"** - one of the records that already existed moved
between the plan and the apply. Re-plan and review the new split.

**"Every record must carry the duplicate-check fields"** - without them the
plan cannot separate inserts from updates.

**"'stale_days' must be at least 1"** - `hygiene_scan` rejects 0 rather than
quietly falling back to the 90-day default.

**"matches more than 2000 records"** - a scan refuses rather than acting on a
partial set. Narrow the query.

# What this module is allowed to call

The manifest carries an empty `allowed_destinations` array:

```json
"allowed_destinations": []
```

That is a declaration, not a restriction being accepted. This module talks to
one place - the Zoho CRM REST API, over urllib - and calls no language model,
no gateway, and no third party. The empty array says exactly that.

It matters because of how the station reads the field. A module with **no**
`allowed_destinations` entry is treated as unrestricted, for backward
compatibility with everything published before station v0.45. An **empty**
array resolves to an empty set of permitted hosts. Silence and an empty
declaration are not the same claim, and this module makes the second one.

The manifest is signed at publish time, so the declaration is covered by the
module signature. A publisher cannot claim after the fact to have declared
something they did not.

Being precise about the limits: the check runs inside `station.llm.complete()`,
which this module never calls, so nothing here will ever exercise it at
runtime. The value is that a reviewer reading the manifest can see the claim
and verify it against the signature. It is a contract about intent, not a
sandbox.


# Capping how often a write can run

Every write command here stops at the airlock, but nothing stops a runaway
client from approving and firing the same command a thousand times. The
station caps that, and the cap is yours to set.

Create or edit `rate_limits.json` in the station workspace:

```
~/.railcall/station/.railcall_workspace/rate_limits.json
```

```json
{
  "zoho.apply_delete":   {"per_day": 5},
  "zoho.apply_handover": {"per_day": 3},
  "zoho.apply_update":   {"per_day": 20},
  "zoho.apply_rollback": {"per_day": 20},
  "zoho.delete_record":  {"per_day": 10}
}
```

Counters roll over at UTC midnight. Setting `per_day` to `0` disables that
command for the rest of the day, which is a usable panic switch.

A blocked attempt comes back as `blocked_by_policy`, and **the approval is not
consumed**. Raise the cap and the same approval still executes; you do not have
to review the change again.

Read-only commands are not capped.

The station ships defaults for its built-in commands only. A module's commands
have no cap until you write one, so if you care about a ceiling on
`apply_delete` or `apply_handover`, set it deliberately rather than assuming
one exists.

Verified on station v0.44.


# Limits

100 records per write call. 200 per COQL page, paged automatically to 2000.
Bulk and Notification APIs are not wrapped: both are asynchronous or
push-based and do not fit a synchronous handler. Activities, attachments and
tags are not covered yet.

The ledger is local to the station and rotates past 5000 entries, sealing the
old file under a timestamped name and starting a fresh chain from the last
sealed hash. Ledger entries hold record ids and prior field values, so the file
is personal data and lives in the station workspace beside the plans.

Ledger writes happen after the API call, not before, with one deliberate
exception: `apply_merge` archives the losing records first, because after the
merge there is no readable copy left anywhere. A write that lands and is then
followed by a failed ledger write is real and unrecorded. The alternative would
be recording changes that never happened, which is worse for an evidence file.
The ledger does not claim completeness; it claims not to lie about what it
holds.

An `unresolved` entry is the one case where the ledger records something that
may not have happened, and it is explicit about it: the outcome class says so,
the entry holds an attempt rather than a change, and its records are excluded
from every "changed" count. Recording nothing was the old behaviour and it was
worse - the module knew exactly what it had tried to do and threw that away at
the moment it became most useful.

`apply_delete` and `apply_handover` write a ledger entry only in this
unresolved case. A successful delete or handover is not yet recorded, so
`scan_changes` reports both as ungoverned. That is a real gap in coverage, named
here rather than left to be discovered.
