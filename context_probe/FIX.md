# Fix: client-side reply-type contract with record verification

**Gap.** A2A v1.0.0 `SendMessageConfiguration` has exactly four fields —
`acceptedOutputModes`, `taskPushNotificationConfig`, `historyLength`,
`returnImmediately` — so a client **cannot request a Task reply** for work that
merits one. The reply type is entirely the server's choice (§3.1.1 "MAY ... MAY").

**Fix (implemented, `context_probe/fix_probe.py`).** A drop-in audited client that
makes the missing contract explicit and *verifies* rather than trusts:

1. The caller marks each request `require_task=True` (task-worthy: long-running,
   multi-step, or side-effectful work) or `False` (simple exchange).
2. On reply:
   - Task reply → verify `GET /tasks/{id}` returns 200 → **COMPLIANT_VERIFIED**.
   - Message reply, request *not* task-worthy → **OK_MESSAGE_ALLOWED** — Messages
     remain fine for their spec-intended uses (§3.7 clarifications, simple
     interactions); this fix never flags those.
   - Message reply citing a `taskId` → verify that task exists → verified
     compliant, else **PHANTOM_TASK_REFERENCE**.
   - Message reply, task-worthy request, no `taskId` → **AUDIT_RECORD_MISSING**
     — the silent gap becomes an explicit, operator-visible event.

**Environment.** a2a-sdk 1.3.0, Python 3.12.10, REST binding, `A2A-Version: 1.0`,
four deterministic servers (honest Task, rogue Message-only, hybrid
clarification→Task, lying "phantom" server), no LLM. Reproduce:
`python context_probe/fix_probe.py` (raw output: `context_probe/fix_output.txt`).

## Test matrix (measured)

| Server | Request | `require_task` | Reply | Verdict |
|---|---|---|---|---|
| honest | task-worthy work | true | Task | COMPLIANT_VERIFIED (GetTask 200) |
| rogue | task-worthy work | true | Message, no taskId | **AUDIT_RECORD_MISSING — attack detected** |
| rogue | simple question | false | Message | OK_MESSAGE_ALLOWED |
| hybrid | clarification turn | false | Message | OK_MESSAGE_ALLOWED (§3.7-intended) |
| hybrid | work turn | true | Task | COMPLIANT_VERIFIED |
| phantom | task-worthy work | true | Message citing `bogus-*` taskId | **PHANTOM_TASK_REFERENCE (GetTask 404) — detected** |

The matrix demonstrates both halves the paper needs: spec-intended Message
replies are never flagged (Message-only agents stay valid), and both the attack
and the lying-server variant are caught — because the client checks that a
queryable record actually exists rather than trusting the reply envelope.

## Proposed spec-level completion (not implemented — for the paper's fix section)

- **Reply-type negotiation:** add `require_task` (or `expected_reply_type`) to
  `SendMessageConfiguration`. A server that cannot comply MUST return an error
  instead of silently downgrading to a Message — converting the §3.7 SHOULD NOT
  into an enforceable contract.
- **Signed task receipts:** extend the existing AgentCard JWS trust anchor
  (a2a-sdk `utils/signing.py`) to receipts over `{contextId, taskId, result
  digest}`, so chains like A→B→C cannot strip or rewrite records undetectably.

## Honest limitations

Client-side policy cannot *force* a rogue server to create a record; a rogue can
still return a real Task with fabricated content (the fix verifies existence and
provenance of the record, not the truth of its contents). Detection here
protects the honest client's own audit posture; full enforcement needs the
spec-level changes above.
