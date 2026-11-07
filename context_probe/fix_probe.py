"""Fix: client-side reply-type contract enforcement + verification.

A2A v1.0.0 gives the client no way to request a Task reply
(SendMessageConfiguration = acceptedOutputModes, taskPushNotificationConfig,
historyLength, returnImmediately). This fix adds the missing contract on the
client side:

  - Requests marked require_task=True are 'task-worthy' (long-running,
    multi-step, side-effectful work that merits a durable record).
  - Any Message reply is FINE for everything else - clarifications and
    simple exchanges are spec-intended uses of Messages (SS3.7).
  - For task-worthy requests the client demands a *verifiable* record:
      reply is Task                     -> verify with GET /tasks/{id}
      reply is Message with taskId      -> verify that task exists
      reply is Message without taskId   -> AUDIT_RECORD_MISSING (the attack)
      referenced task does not exist    -> PHANTOM_TASK_REFERENCE (a lie)

Deterministic servers, no LLM.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types.a2a_pb2 import (
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

from probe import HEADERS, build_server, send_body, wait_up

PORTS = {"honest": 8931, "rogue": 8932, "hybrid": 8933, "phantom": 8934}


# ------------------------------------------------------------------ executors
class TaskExecutor(AgentExecutor):
    """Always replies with a Task (honest server)."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        incoming = context.message
        tid = context.task_id or str(uuid.uuid4())
        cid = context.context_id or incoming.context_id or str(uuid.uuid4())
        reply = Message(
            message_id=str(uuid.uuid4()),
            context_id=cid,
            role=Role.ROLE_AGENT,
            parts=[Part(text="DONE: trade executed.", media_type="text/plain")],
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
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED, message=reply),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


class MessageExecutor(AgentExecutor):
    """Always replies with a Message. If the request says PHANTOM, the reply
    cites a taskId that does not exist (a lying server)."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        incoming = context.message
        text = "\n".join(p.text for p in incoming.parts)
        cid = incoming.context_id or str(uuid.uuid4())
        kwargs = {}
        if "PHANTOM" in text:
            kwargs["task_id"] = f"bogus-{uuid.uuid4()}"
        reply = Message(
            message_id=str(uuid.uuid4()),
            context_id=cid,
            role=Role.ROLE_AGENT,
            parts=[Part(text="DONE: trade executed.", media_type="text/plain")],
            **kwargs,
        )
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


class HybridExecutor(AgentExecutor):
    """Spec-idiomatic hybrid agent: clarification via Message (SS3.7),
    real work via Task."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        incoming = context.message
        text = "\n".join(p.text for p in incoming.parts)
        cid = incoming.context_id or str(uuid.uuid4())
        if text.startswith("CLARIFY"):
            reply = Message(
                message_id=str(uuid.uuid4()),
                context_id=cid,
                role=Role.ROLE_AGENT,
                parts=[Part(text="Which ticker do you mean?", media_type="text/plain")],
            )
            await event_queue.enqueue_event(reply)
            return
        # otherwise behave like the honest task executor
        await TaskExecutor.execute(self, context, event_queue)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------- the fix
class AuditedClient:
    """Client-side reply-type contract with record verification."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.events: list[dict] = []

    async def send(self, text: str, require_task: bool, cid: str | None = None) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self.base}/a2a/message:send",
                json=send_body(text, cid),
                headers=HEADERS,
            )
            body = r.json()

            async def get_task_status(task_id: str) -> int:
                g = await client.get(
                    f"{self.base}/a2a/tasks/{task_id}", headers=HEADERS
                )
                return g.status_code

            verdict: str
            detail: str
            if "task" in body:
                tid = body["task"].get("id")
                g = await get_task_status(tid)
                verdict = "COMPLIANT_VERIFIED" if g == 200 else "PHANTOM_TASK_REFERENCE"
                detail = f"task reply id={str(tid)[:8]}... GetTask={g}"
            else:
                msg = body["message"]
                if not require_task:
                    verdict = "OK_MESSAGE_ALLOWED"
                    detail = "not task-worthy; Message is a spec-intended reply"
                elif msg.get("taskId"):
                    g = await get_task_status(msg["taskId"])
                    verdict = (
                        "COMPLIANT_VERIFIED" if g == 200 else "PHANTOM_TASK_REFERENCE"
                    )
                    detail = f"reply cites taskId={msg['taskId'][:13]}... GetTask={g}"
                else:
                    verdict = "AUDIT_RECORD_MISSING"
                    detail = "task-worthy work replied as bare Message; no durable record exists"

        self.events.append(
            {"text": text[:34], "require_task": require_task, "verdict": verdict, "detail": detail}
        )
        return verdict


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


async def main() -> None:
    from importlib.metadata import version
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore

    print("versions: a2a-sdk", version("a2a-sdk"), "| python", sys.version.split()[0])

    servers = {
        "honest": build_server(TaskExecutor(), PORTS["honest"], InMemoryTaskStore()),
        "rogue": build_server(MessageExecutor(), PORTS["rogue"], InMemoryTaskStore()),
        "hybrid": build_server(HybridExecutor(), PORTS["hybrid"], InMemoryTaskStore()),
        "phantom": build_server(MessageExecutor(), PORTS["phantom"], InMemoryTaskStore()),
    }
    for name, (_, srv, _) in servers.items():
        await wait_up(f"http://127.0.0.1:{PORTS[name]}")

    clients = {
        name: AuditedClient(f"http://127.0.0.1:{port}") for name, port in PORTS.items()
    }

    section("TEST MATRIX - client-side contract enforcement")
    cases = [
        ("honest", "COMPLIANT_REPORT: buy 10 NVDA", True, "task-worthy -> honest Task reply"),
        ("rogue", "COMPLIANT_REPORT: buy 10 NVDA", True, "task-worthy -> Message-only (THE ATTACK)"),
        ("rogue", "which exchange is NVDA on?", False, "simple question -> Message reply is fine"),
        ("hybrid", "CLARIFY: buy shares of the chip company", False, "clarification turn -> Message is spec-intended (SS3.7)"),
        ("hybrid", "COMPLIANT_REPORT: buy 10 NVDA", True, "work turn after clarification -> Task"),
        ("phantom", "COMPLIANT_REPORT PHANTOM: buy 10 NVDA", True, "Message citing a nonexistent taskId (lying server)"),
    ]
    for server_name, text, req, why in cases:
        v = await clients[server_name].send(text, req)
        flag = " <-- DETECTED" if v in ("AUDIT_RECORD_MISSING", "PHANTOM_TASK_REFERENCE") else ""
        print(f"[{server_name:7}] req={str(req):5} | {v:22} | {why}{flag}")

    section("EVENT LOG (what the operator sees)")
    for name, c in clients.items():
        for e in c.events:
            print(f"{name:7} | {e['verdict']:22} | {e['detail']}")

    section("VERDICT")
    print("""
- The fix leaves spec-intended Message replies untouched (clarifications,
  simple exchanges) - Messages are not treated as suspicious per se.
- For task-worthy requests it converts the silent gap into an explicit
  operator-visible event: AUDIT_RECORD_MISSING fires exactly when the
  attack happens, and PHANTOM_TASK_REFERENCE catches servers that cite
  task ids that do not exist (the fix verifies, it does not trust).
- Limits (state honestly in the paper): client-side policy cannot force a
  rogue server to create a record; a rogue can still create a *real* task
  with fabricated content. Full enforcement needs the spec-level changes:
  a require-task field in SendMessageConfiguration (server MUST error
  instead of silently downgrading) and signed task receipts so chains
  like A->B->C cannot strip records undetectably.
""")

    for _, srv, thr in servers.values():
        srv.should_exit = True
    for _, _, thr in servers.values():
        thr.join(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
