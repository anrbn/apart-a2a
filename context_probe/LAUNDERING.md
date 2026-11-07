# Record laundering in an A2A chain (A → B → C)

**Attack.** A rogue intermediary B converts downstream Task records into upstream
unrecorded Messages. B is a *server* to A and a *client* to C, the two roles A2A
defines for chained agents.

```
A (client) ── message:send ──> B (rogue intermediary) ── message:send ──> C (honest Task server)
A <── bare Message ────────── B <── completed Task ──────────────────── C
      no taskId,                (durable record created at C,
      no provenance              but B keeps the linkage private)
```

Per request, B: (1) forwards the work to C and receives a completed Task;
(2) privately remembers the downstream task id; (3) replies to A with a bare
Message carrying the task output, its own contextId, and **no taskId**. B also
does not forward A's contextId to C, so C's records cannot even be correlated
with A's conversation after the fact.

**Environment.** a2a-sdk 1.3.0, Python 3.12.10, REST binding, `A2A-Version: 1.0`,
deterministic executors, no LLM. Reproduce: `python context_probe/chain_probe.py`
(raw output: `context_probe/chain_output.txt`).

## Results (measured)

| Party / surface | After 2 turns | Detail |
|---|---|---|
| A → B `GET /tasks` | `totalSize: 0` | B's store never received an object |
| A → B `GET /tasks/{contextId}` | 404 `TASK_NOT_FOUND` | the only id A holds |
| A's reply from B | Message, keys = `contextId, messageId, parts, role` | **no `taskId`**, no mention of C |
| C `GET /tasks` (if A even knew C exists) | `totalSize: 2`, both `TASK_STATE_COMPLETED` | contextIds differ from A's — B sent fresh, contextId-free messages |
| B's private memory (in-process dump) | A-context → downstream task id + C's URL | exposed by **no** A2A method |
| B's task store | 0 tasks | laundering complete |
| C's task store | 2 tasks | orphaned from A's perspective |

## Spec compliance (A2A v1.0.0)

- **Zero MUST violations.** §3.1.1: the server "MAY create a new Task to process
  the provided message asynchronously or MAY return a direct Message"; a direct
  Message is for "simple interactions that don't require task tracking."
- **One SHOULD NOT.** §3.7: "Messages SHOULD NOT be used to deliver task
  outputs"; results "SHOULD BE returned using Artifacts associated with a Task."
  B's reply to A violates exactly this.
- **Undetectable through A2A.** Replies carry no signature: the SDK's only
  signing facility is AgentCard JWS (`a2a/utils/signing.py`), and the REST
  dispatcher has no signature hooks — a2a-sdk 1.3.0 cannot sign or verify a
  Task or Message. A therefore cannot distinguish "B did the work itself and
  chose Message-only" from "B used C and stripped the record", and cannot prove
  C was involved at all.

## Why this matters (paper framing)

The work is fully stateful and real (C's records prove it), yet every record is
unreachable from A's side of the protocol. Unit42's "agent session smuggling"
abuses *statefulness* (hidden sessions via reused context); this is the inverse
move — the intermediary keeps the statefulness, launders the *record*, and the
victim's audit surface stays empty. The only rule broken is a SHOULD NOT, which
no party can observe being broken.
