# Phase 3 notification delivery and fatigue review — 2026-07-13

This is the durable P3.S record for one explicitly authorized notification
canary. It records only non-sensitive synthetic inputs and hashed recipient and
receipt identities. It contains no email address, API key, raw provider
response, retained P3.4 output, or production case data.

## Boundary reviewed

The manual command used `automation.run_notification_canary` with `--live`, a
new isolated ignored output root, and the SHA-256 fingerprint of the existing
approved test recipient. It constructed one grouped digest from three fixed
synthetic case states:

- a seven-day `no_pdf` weekly item;
- a 30-day `no_public_list` monthly item; and
- an 84-day `unknown_download_source` dormant item.

All three use synthetic venue IDs, year 2099, synthetic evidence IDs, and a
synthetic run ID. The subject and first body lines label the message as a P3.S
synthetic canary that represents no real conference or retained case. The
command accepted no upstream event, notification intent, case database, or
P3.4 path.

## Delivery evidence

- Generated at: `2026-07-13T23:30:14.819421Z`.
- Notification ID:
  `notification:digest:f389da9924ac595a2687d240374ac65a4fad75ab2f5f423df5a38820c59634f9`.
- Approved recipient SHA-256:
  `6e45a5ac75e628cfdbc0ff485e61f0aff2c8de946b067f0f89dac8ed318428e8`.
- Provider receipt SHA-256:
  `821b27f1fa52fd9f922d91ce5ad1c876310067a0c54bfceca89fa270d82e2b9b`.
- External delivery requests: `1`.
- Durable delivery attempts: `1`.
- Durable status: `delivered`; failure category: none.
- Secret scan: the retained marker/result JSON contained neither the approved
  address nor the Resend key.

Here `delivered` means the Resend send endpoint returned a valid provider
receipt and the P3.3 coordinator durably completed that accepted attempt. The
review made no second provider request and did not independently inspect the
recipient mailbox, so final mailbox placement is not claimed.

The adapter used the stable notification ID as Resend's `Idempotency-Key`.
Current provider documentation states that the send endpoint accepts that
header and that provider idempotency lasts 24 hours; durable local delivered
state remains the longer-lived replay guard. See the official
[send-email API](https://resend.com/docs/api-reference/emails/send-email) and
[idempotency documentation](https://resend.com/docs/dashboard/emails/idempotency-keys).

## Fatigue review

The delivered plain-text message had these measured properties:

- subject: 58 characters;
- body: 1,334 characters across 36 lines;
- due items: 3 in 3 groups; and
- group order: weekly, monthly, dormant.

Each group had a visible urgency heading and count. Each item showed one case
identity, venue/year, blocker/status, age/slot, due time, short summary, and
evidence reference. A single run-reference section appeared after all groups.
The sampled message was concise enough to scan and made the synthetic boundary
unambiguous. Grouping three otherwise separate reminders into one delivery is
consistent with the intended fatigue policy.

This canary does not establish readability near the 100-item or 100,000-byte
contract bounds, inbox rendering across clients, accessibility of an HTML
variant, or real long-term recipient fatigue. Those remain explicit rollout
limitations; this package does not change message or case semantics in
response.

## Failure and replay evidence

Before the live attempt, the command ran a local no-network transport drill
against a separate temporary schema-version-3 database. One typed
`rate_limited` failure produced one durable `retryable` attempt, retained no
raw error text, and left the case listing empty. Removing the temporary root at
the end of the drill proved that no production or P3.4 state participated.

After provider acceptance, the completed canary root was reopened with a fake
transport whose `send` method fails if called. P3.3 returned the existing
`delivered` outcome and the fake recorded zero calls. No provider retry was
made.

Focused tests additionally cover timeout, unavailable, authentication,
payload, protocol, invalid-recipient, malformed/oversized response,
foreign-root, approval-mismatch, missing-`--live`, and refusal to retry a
consumed `retryable` canary root with fakes and temporary storage.

## Validation

After the cross-invocation one-request guard was added, the final repository
checks were:

- `python -m unittest discover -s automation/tests -v`: 193 tests passed;
- `python -m compileall -q automation`: passed;
- `python postprocessing/generate_statistics.py --check`: generated coverage
  was current; and
- `git diff --check`: passed.

The focused P3.S transport/command suite and the P3.2-P3.4/control-state
regressions are included in that automation run. No shared scraper,
configuration, validator, utility, or conference data changed, so the
repository instructions did not require the unrelated core suite.

## Rollback evidence

The live canary changed no tracked or production runtime configuration. A
rollback rehearsal removed the canary sender/recipient variables and invoked
the live command against a fresh temporary root. It exited with refusal code 2
before constructing a transport, making a request, or creating the root.

Operational rollback is therefore to unset `RESEND_KEY`,
`OPENPAPERS_CANARY_EMAIL_FROM`, and `OPENPAPERS_CANARY_EMAIL_TO` for the manual
process and withhold `--live`. The ignored completed root may be retained for
audit or removed after this durable record is accepted. There is no deployment
to roll back, no schema migration to downgrade, no scheduler to disable, and
no production case state to restore. The already accepted synthetic email
cannot be recalled.

## Non-effects and conclusion

The run did not open or deliver any retained P3.4 shadow notification, change
a case or reminder rule, write production/GCS state, invoke Prefect or Cloud
Run, configure a scheduler or recipient, queue a job, run a scraper, contact a
Mac worker or Codex, publish data, deploy MustCite, or begin Phase 4.

P3.S passes its bounded shadow gate. Phase 3 is `Shadow`, not `Implemented`:
the real adapter is reachable only through the explicit synthetic-only canary,
while production event delivery and operational rollout remain absent.
