# Findings: what a2a-python retains for Message-only server replies

**Question.** What does the context machinery in a2a-python actually retain when a
server replies with a direct Message (no Task)? Metadata or content? And is any of
it exposed through an A2A method?

**Answer in one line.** In a2a-sdk 1.3.0 there is **no context service at all** —
`contextId` is an opaque matching string, the only server-side persistence is the
task store, and a Message-only exchange writes **zero rows / zero objects
anywhere**, so there is nothing to expose. Multi-turn state still works: the
executor holds it in its own memory keyed by the `contextId` it echoes.

## Environment (pinned)

| Component | Version |
|---|---|
| a2a-sdk (`pip install a2a-sdk[http-server,sqlite]==1.3.0`) | 1.3.0 |
| Python | 3.12.10 (Windows) |
| protobuf | 7.36.2 |
| starlette / uvicorn / httpx | 1.10.0 / 0.54.0 / 0.28.1 |
| sqlalchemy / aiosqlite (DatabaseTaskStore) | 2.1.8 / 0.22.1 |
| Protocol binding under test | REST (HTTP+JSON), `A2A-Version: 1.0` header required |

Reproduce: `context_probe/.venv` + `python context_probe/probe.py`
(full output saved in `context_probe/probe_output.txt`).

## Source evidence (a2a-sdk 1.3.0, paths relative to site-packages)

1. **No context service exists.** `grep -rn "ContextService|context_service|save_context|get_context|create_context" a2a/` →
   0 hits. The only "context" on the server is `a2a/server/context.py:14-25`
   (`ServerCallContext`), a per-request object carrying `state`/`user`/`tenant` —
   created fresh per call, never persisted, never keyed by `contextId`.

2. **`contextId` is opaque and matching-only.** The handler threads
   `params.message.context_id` through (`a2a/server/request_handlers/default_request_handler.py:307-312`)
   and `TaskManager` uses it solely to *validate consistency* between a request and
   task events, adopting it if unset (`a2a/server/tasks/task_manager.py:235-240`).
   Nothing is ever stored keyed by it.

3. **Only Task events reach persistence.** `TaskManager.process` persists an event
   only if it is a `Task`, `TaskStatusUpdateEvent`, or `TaskArtifactUpdateEvent`
   (`a2a/server/tasks/task_manager.py:341-344`). A `Message` event is returned to
   the caller verbatim — no store call exists on that path
   (`default_request_handler.py:397-464`).

4. **Even the client's request Message is not stored unless a Task exists.** The
   incoming message is appended to history only when a task was already loaded
   (`default_request_handler.py:316-324` → `task_manager.py:392-410`). In a
   Message-only flow the request is discarded after execution.

5. **`historyLength` is Task-anchored.** It is applied only when the reply is a
   Task (`default_request_handler.py:459-462`) and operates on `Task.history`
   (`a2a/utils/task.py:35-49`, spec §3.2.4). A Message reply has no history field.

6. **No A2A method returns a past Message; none touches contexts.** The complete
   REST route surface (`a2a/server/routes/rest_routes.py:83-111`) is:
   `POST /message:send`, `POST /message:stream`, `GET /tasks`, `GET /tasks/{id}`,
   `POST /tasks/{id}:cancel`, `GET|POST /tasks/{id}:subscribe`,
   `*/tasks/{id}/pushNotificationConfigs*`, `GET /extendedAgentCard`.
   Everything queryable is `tasks/*`.

7. **No reply-type negotiation exists.** `SendMessageConfiguration` fields are
   exactly `acceptedOutputModes`, `taskPushNotificationConfig`, `historyLength`,
   `returnImmediately` — a client cannot request Task-only replies.

8. **The handler runtime holds no context persistence object.** Live dump of
   `DefaultRequestHandler` attributes: `task_store`, `_push_config_store`,
   `_active_task_registry`, queue/event internals, card — nothing else. (With the
   v1.0 proto there is also no `final` field on `TaskStatusUpdateEvent` anymore;
   terminal state conveys finality.)

## Live probe results (`probe.py`, deterministic executors, no LLM)

| Audit surface after the interaction | A: rogue Message-only (3 turns, 1 contextId) | B: honest Task reply (1 turn) | C: rogue + durable `DatabaseTaskStore` (sqlite) |
|---|---|---|---|
| Reply envelope | `{"message": …}` incl. `contextId` | `{"task": …}` `TASK_STATE_COMPLETED` | `{"message": …}` |
| Multi-turn state | **works** — same `contextId` across turns 1→3, executor memory `turns={ctx: 3}` | via task history | works, same contextId |
| `GET /a2a/tasks` | `totalSize: 0` | 1 task listed | `totalSize: 0` |
| `GET /a2a/tasks/{id}` | 404 `TASK_NOT_FOUND` (for contextId and for messageId) | 200; `history_len=2` (request+reply), answer in `status.message` | 404 |
| In-memory task store dump | `{}` — 0 tasks | 1 task, retrievable | n/a |
| sqlite `tasks` table | n/a | n/a | **0 rows** |
| INFO+ SDK logs during exchange | 0 content-bearing records | — | 0 |

## Implication for the paper

- The rebuttal "the context service keeps a record / just query the context" does
  not apply to a2a-python 1.3.0: there is no context service, and `contextId`
  correlation leaves zero protocol-visible state. Statefulness and record-keeping
  are fully decoupled — the rogue server keeps the first and skips the second.
- Even the SDK's own durable, SQL-backed store (`DatabaseTaskStore`) receives no
  rows for message-only flows; switching stores does not close the gap.
- Reproduction gotcha: the REST v1.0 handler requires the `A2A-Version: 1.0`
  header; without it the server assumes 0.3 and rejects with
  `VERSION_NOT_SUPPORTED`.
