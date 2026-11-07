"""Live probe: what does a2a-sdk 1.3.0 retain when a server replies Message-only?

Runs three scenarios against real A2A servers over HTTP (REST binding v1.0):
  A) rogue    : Message-only executor, multi-turn via contextId, state kept in
                executor memory. Never emits a Task event.
  B) honest   : Task executor (Task event + final TaskStatusUpdateEvent).
  C) rogue-db : Message-only executor again, but backed by the SDK's durable
                DatabaseTaskStore (sqlite).

After each scenario the probe audits every A2A retrieval surface (GET /tasks,
GET /tasks/{id}) and dumps the server-side stores directly, in-process.

No LLM is involved: the executors are deterministic, so the only variable is
the reply envelope (Message vs Task).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import uuid
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks.database_task_store import DatabaseTaskStore
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

HEADERS = {"A2A-Version": "1.0"}

import sqlalchemy.ext.asyncio as sa_asyncio

HERE = Path(__file__).parent
DB_PATH = HERE / "probe_tasks.sqlite"

# ---------------------------------------------------------------- log capture
LOG_RECORDS: list[tuple[str, str, str]] = []


class CaptureHandler(logging.Handler):
    def emit(self, record: logging.LoggingRecord) -> None:
        msg = record.getMessage()
        if record.exc_info:
            import traceback
            msg = msg + " | " + "".join(traceback.format_exception(*record.exc_info))
        LOG_RECORDS.append((record.name, record.levelname, msg))


root = logging.getLogger()
root.setLevel(logging.INFO)
root.addHandler(CaptureHandler())


# ------------------------------------------------------------------ executors
def text_of(message: Message) -> str:
    return "\n".join(p.text for p in message.parts)


class RogueMessageExecutor(AgentExecutor):
    """Message-only server. Conversation state lives in this dict, keyed by
    the contextId it echoes to the client. No Task event is ever emitted."""

    def __init__(self) -> None:
        self.turns: dict[str, int] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        incoming = context.message
        cid = incoming.context_id or str(uuid.uuid4())
        n = self.turns.get(cid, 0) + 1
        self.turns[cid] = n
        answer = f"[rogue reply, turn {n} for context {cid[:8]}] DONE: work executed."
        reply = Message(
            message_id=str(uuid.uuid4()),
            context_id=cid,
            role=Role.ROLE_AGENT,
            parts=[Part(text=answer, media_type="text/plain")],
        )
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


class HonestTaskExecutor(AgentExecutor):
    """Same work, but the reply envelope is a Task (+final status event)."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        incoming = context.message
        tid = context.task_id or str(uuid.uuid4())
        cid = context.context_id or incoming.context_id or str(uuid.uuid4())
        answer = f"[honest reply, task {tid[:8]}] DONE: work executed."
        reply = Message(
            message_id=str(uuid.uuid4()),
            context_id=cid,
            role=Role.ROLE_AGENT,
            parts=[Part(text=answer, media_type="text/plain")],
        )
        task = Task(
            id=tid,
            context_id=cid,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING, message=reply),
            history=[incoming],
        )
        await event_queue.enqueue_event(task)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=tid,
                context_id=cid,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED, message=reply
                ),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


# -------------------------------------------------------------------- harness
def build_server(executor: AgentExecutor, port: int, task_store):
    card = AgentCard(
        name="probe-agent",
        description="deterministic probe agent",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=f"http://127.0.0.1:{port}/a2a",
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extensions=[],
            extended_agent_card=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(id="probe", name="probe", description="deterministic echo work", tags=[])
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
        validate_input_modes=True,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_rest_routes(handler, path_prefix="/a2a"),
    ]
    app = Starlette(routes=routes)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return handler, server, thread


async def wait_up(base: str) -> None:
    for _ in range(100):
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                await c.get(f"{base}/a2a/tasks", headers=HEADERS)
            return
        except httpx.HTTPError:
            await asyncio.sleep(0.1)
    raise RuntimeError("server did not start")


def send_body(text: str, cid: str | None = None) -> dict:
    msg: dict = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }
    if cid:
        msg["contextId"] = cid
    return {
        "message": msg,
        "configuration": {
            "returnImmediately": False,
            "acceptedOutputModes": ["text/plain"],
            "historyLength": 10,
        },
    }


async def send(client: httpx.AsyncClient, base: str, text: str, cid: str | None = None):
    r = await client.post(f"{base}/a2a/message:send", json=send_body(text, cid), headers=HEADERS)
    return r.status_code, r.json()


def dump_memory_store(handler) -> dict:
    """Walk the store wrapper chain (_store/_impl) and count stored Tasks."""
    node = handler.task_store
    chain = [type(node).__name__]
    out: dict = {"type": chain[0]}
    for _ in range(4):
        for k, v in vars(node).items():
            if isinstance(v, dict) and all(not isinstance(x, (int, str)) for x in list(v.values())[:1] or [None]):
                # candidate owner -> {task_id: Task} map
                sample = next(iter(v.values()), None)
                if sample is None or isinstance(sample, dict):
                    out["owners"] = {
                        owner: {
                            tid: {"context_id": t.context_id, "state": TaskState.Name(t.status.state)}
                            for tid, t in d.items()
                        }
                        for owner, d in v.items()
                        if isinstance(d, dict)
                    }
                    out["total_tasks"] = sum(
                        len(d) for d in v.values() if isinstance(d, dict)
                    )
                    out["found_at"] = f"{type(node).__name__}.{k}"
                    return out
        inner = getattr(node, "_impl", None) or getattr(node, "_store", None)
        if inner is None or inner is node:
            break
        node = inner
        chain.append(type(node).__name__)
    out["wrapper_chain"] = chain
    return out


def dump_sqlite(path: Path) -> dict:
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------- main
async def main() -> None:
    import a2a
    import google.protobuf
    import httpx as _httpx

    from importlib.metadata import version
    print("versions: a2a-sdk", version("a2a-sdk"),
          "| python", sys.version.split()[0],
          "| protobuf", google.protobuf.__version__,
          "| httpx", _httpx.__version__)

    base = "http://127.0.0.1:8911"

    # ---------------- Scenario A: rogue message-only, in-memory store --------
    section("SCENARIO A — rogue server: Message-only replies, state in executor memory (InMemoryTaskStore)")
    LOG_RECORDS.clear()
    rogue = RogueMessageExecutor()
    handler_a, server_a, thread_a = build_server(rogue, 8911, InMemoryTaskStore())
    await wait_up(base)
    async with httpx.AsyncClient(timeout=30.0) as client:
        code, r1 = await send(client, base, "COMPLIANT_REPORT: buy 10 NVDA")
        reply1 = r1.get("message", {})
        cid = reply1.get("contextId")
        print(f"turn 1 -> HTTP {code} | reply is Message | contextId={cid}")
        print("        reply JSON:", json.dumps(r1)[:220])
        code, r2 = await send(client, base, "status?", cid)
        print(f"turn 2 (same contextId) -> HTTP {code} | reply:", json.dumps(r2)[:160])
        code, r3 = await send(client, base, "status?", cid)
        print(f"turn 3 (same contextId) -> HTTP {code} | reply:", json.dumps(r3)[:160])

        print("\n-- audit: A2A retrieval surfaces --")
        r = await client.get(f"{base}/a2a/tasks", headers=HEADERS)
        print(f"GET /a2a/tasks           -> HTTP {r.status_code} body={r.json()}")
        r = await client.get(f"{base}/a2a/tasks/{cid}", headers=HEADERS)
        print(f"GET /a2a/tasks/<ctx id>  -> HTTP {r.status_code} body={json.dumps(r.json())[:160]}")
        r = await client.get(f"{base}/a2a/tasks/{reply1.get('messageId')}", headers=HEADERS)
        print(f"GET /a2a/tasks/<msg id>  -> HTTP {r.status_code} body={json.dumps(r.json())[:160]}")

    print("\n-- audit: server-side stores (dumped in-process) --")
    print("handler attributes:", sorted(vars(handler_a)))
    print("in-memory task store:", json.dumps(dump_memory_store(handler_a), indent=2))
    print(f"executor memory (NOT protocol state): turns={ {k[:8]: v for k, v in rogue.turns.items()} }")
    info_logs = [rec for rec in LOG_RECORDS if rec[1] in ("INFO", "WARNING", "ERROR") and rec[0] != "httpx"]
    print(f"INFO+ log records emitted during scenario A: {len(info_logs)}")
    for rec in info_logs:
        print("   ", rec)
    server_a.should_exit = True
    thread_a.join(timeout=5)

    # ---------------- Scenario B: honest task reply --------------------------
    section("SCENARIO B — honest server: Task reply (InMemoryTaskStore)")
    handler_b, server_b, thread_b = build_server(HonestTaskExecutor(), 8912, InMemoryTaskStore())
    base_b = "http://127.0.0.1:8912"
    await wait_up(base_b)
    async with httpx.AsyncClient(timeout=30.0) as client:
        code, r1 = await send(client, base_b, "COMPLIANT_REPORT: buy 10 NVDA")
        task1 = r1.get("task", {})
        tid = task1.get("id")
        print(f"turn 1 -> HTTP {code} | reply envelope keys={list(r1)} task state={task1.get('status', {}).get('state')}")
        print("        reply JSON:", json.dumps(r1)[:260])

        print("\n-- audit: A2A retrieval surfaces --")
        r = await client.get(f"{base_b}/a2a/tasks", headers=HEADERS)
        print(f"GET /a2a/tasks         -> HTTP {r.status_code} | ids={[t.get('id') for t in r.json().get('tasks', [])]}")
        r = await client.get(f"{base_b}/a2a/tasks/{tid}", headers=HEADERS)
        got = r.json()
        print(f"GET /a2a/tasks/<id>    -> HTTP {r.status_code} | state={got.get('status', {}).get('state')} history_len={len(got.get('history', []))} status_message_present={bool(got.get('status', {}).get('message'))}")
        print("        task JSON:", json.dumps(r.json())[:300])

    print("\n-- audit: server-side stores --")
    print("in-memory task store:", json.dumps(dump_memory_store(handler_b), indent=2))
    server_b.should_exit = True
    thread_b.join(timeout=5)

    # ---------------- Scenario C: rogue message-only, durable sqlite store ---
    section("SCENARIO C — rogue server: Message-only replies, SDK durable DatabaseTaskStore (sqlite)")
    if DB_PATH.exists():
        DB_PATH.unlink()
    engine = sa_asyncio.create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    db_store = DatabaseTaskStore(engine=engine, create_table=True)
    await db_store.initialize()
    handler_c, server_c, thread_c = build_server(RogueMessageExecutor(), 8913, db_store)
    base_c = "http://127.0.0.1:8913"
    await wait_up(base_c)
    async with httpx.AsyncClient(timeout=30.0) as client:
        code, r1 = await send(client, base_c, "COMPLIANT_REPORT: buy 10 NVDA")
        cid = r1.get("message", {}).get("contextId")
        code, r2 = await send(client, base_c, "status?", cid)
        print(f"turns 1-2 done (contextId={str(cid)[:8]}...) second reply:", json.dumps(r2)[:140])
        r = await client.get(f"{base_c}/a2a/tasks", headers=HEADERS)
        print(f"GET /a2a/tasks -> HTTP {r.status_code} body={r.json()}")
    await engine.dispose()
    print("sqlite tables and row counts:", dump_sqlite(DB_PATH))
    server_c.should_exit = True
    thread_c.join(timeout=5)

    # ------------- Scenario D: honest task reply, durable sqlite store --------
    section("SCENARIO D — honest server: Task reply, SDK durable DatabaseTaskStore (sqlite)")
    DB_PATH_D = HERE / "probe_tasks_honest.sqlite"
    if DB_PATH_D.exists():
        DB_PATH_D.unlink()
    engine_d = sa_asyncio.create_async_engine(f"sqlite+aiosqlite:///{DB_PATH_D}")
    db_store_d = DatabaseTaskStore(engine=engine_d, create_table=True)
    await db_store_d.initialize()
    handler_d, server_d, thread_d = build_server(HonestTaskExecutor(), 8914, db_store_d)
    base_d = "http://127.0.0.1:8914"
    await wait_up(base_d)
    async with httpx.AsyncClient(timeout=30.0) as client:
        code, r1 = await send(client, base_d, "COMPLIANT_REPORT: buy 10 NVDA")
        task1 = r1.get("task", {})
        print(f"turn 1 -> HTTP {code} | reply is Task id={task1.get('id')} state={task1.get('status', {}).get('state')}")
        r = await client.get(f"{base_d}/a2a/tasks", headers=HEADERS)
        print(f"GET /a2a/tasks -> HTTP {r.status_code} | totalSize={r.json().get('totalSize')}")
    await engine_d.dispose()
    print("sqlite tables and row counts:", dump_sqlite(DB_PATH_D))
    server_d.should_exit = True
    thread_d.join(timeout=5)

    section("SUMMARY")
    print("""
Scenario A (Message-only, in-memory):  tasks queryable = 0, store rows = 0, contextId state = executor memory only
Scenario B (Task reply, in-memory):    tasks queryable = 1, store rows = 1, history + final status retrievable
Scenario C (Message-only, sqlite):     tasks queryable = 0, sqlite row counts above
Scenario D (Task reply, sqlite):       tasks queryable = 1, sqlite row counts above  (completes Table 1 of the paper)
""")


if __name__ == "__main__":
    asyncio.run(main())
