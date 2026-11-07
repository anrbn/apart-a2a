# Off the Books: Audit Evasion in Google's Agent2Agent Protocol

Research artifacts for a security study of the A2A protocol (spec v1.0.0,
reference SDK a2a-python 1.3.0). All experiments are **deterministic** — no LLM
is involved — so the only variable in every comparison is the *reply envelope*
the server agent chooses (`Task` vs `Message`).

**Finding in one paragraph.** A2A's only queryable record of server-side work is
the Task (`GET /tasks`, `GET /tasks/{id}`, the task store). A server may
legitimately reply to any request with a direct Message (§3.1.1), and Messages
are the spec-intended reply for clarifications and simple interactions (§3.7) —
but nothing in the protocol forces a Task for work that *merits* one. A rogue
server (or an intermediary in an A→B→C chain) can therefore do real, multi-step,
stateful work and reply with bare Messages: no Task is created, no store row is
written (not even in the SDK's durable SQL store), there is no context service
in a2a-sdk 1.3.0 to retain anything, and no A2A method can return a past
Message. The work happens, the answer is delivered, and the audit surface stays
empty — violating zero MUSTs and exactly one SHOULD NOT (§3.7: "Messages SHOULD
NOT be used to deliver task outputs").

## Quickstart (3 commands)

```bash
cd context_probe
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # (linux/mac: .venv/bin/pip)
.venv/Scripts/python run_all.py                                         # runs all 3 experiments
```

`run_all.py --fresh` recreates the venv from scratch first. Ports 8911-8934 on
127.0.0.1 must be free. Each experiment starts its own servers, prints a full
report, and saves it to `*_output.txt`. Runtime ≈ 20 s total; tested on Python
3.12.10 (Windows) — any Python ≥3.10 should work.

## Experiments

| # | Script | What it shows | Writeup |
|---|---|---|---|
| 1 | `probe.py` | **The record gap.** Rogue message-only server does 3 turns of work over one `contextId` (state in its own memory) vs an honest Task server doing the same work. Audits every A2A surface + dumps the in-memory store and the durable sqlite `DatabaseTaskStore`. Also proves a2a-sdk 1.3.0 has **no context service** (source-level + runtime). | `FINDINGS.md` |
| 2 | `chain_probe.py` | **Record laundering (A→B→C).** Rogue intermediary B receives completed Tasks from honest C and returns bare Messages to A — no `taskId`, no provenance, no signature. A's audit surface: empty. C's records: orphaned. Zero MUSTs broken, one SHOULD NOT (§3.7). | `LAUNDERING.md` |
| 3 | `fix_probe.py` | **The fix.** Client-side reply-type contract with record verification. 6-case matrix: spec-intended Message replies pass untouched; task-worthy work replied as a Message is flagged `AUDIT_RECORD_MISSING`; a server citing a nonexistent task id is flagged `PHANTOM_TASK_REFERENCE`. | `FIX.md` |

Key measured results (see writeups for full tables):

- Message-only multi-turn exchange: `GET /tasks` → `totalSize: 0`; task lookup →
  404 `TASK_NOT_FOUND`; in-memory store → 0 objects; durable sqlite store → **0
  rows**; zero content-bearing log records at INFO+.
- Same work via Task reply: 1 task, `history` includes request and reply, the
  answer is retrievable through `GET /tasks/{id}` days later.
- The fix flags exactly the two malicious cases and none of the legitimate ones.

## Repo layout

```
context_probe/           ← all experiments (start here)
  probe.py               experiment 1: record gap + context-service question
  chain_probe.py         experiment 2: A→B→C record laundering
  fix_probe.py           experiment 3: client-side contract fix + test matrix
  run_all.py             one-command runner for all three
  requirements.txt       pinned deps (a2a-sdk==1.3.0, see file)
  FINDINGS.md            paper-ready writeup of experiment 1
  LAUNDERING.md          paper-ready writeup of experiment 2
  FIX.md                 paper-ready writeup of experiment 3 (incl. spec proposals)
  *_output.txt           raw outputs of the latest run
paper/                   ← the research report
  off_the_books_report.docx / .pdf   the submitted paper (Apart template)
  figures/                           figure 1 and figure 2 sources (PNG)
```

## Versions & spec

Pinned: a2a-sdk 1.3.0 (`[http-server,sqlite]` extras), Python 3.12.10, starlette
1.10.0, uvicorn 0.54.0, httpx 0.28.1, protobuf 7.36.2, SQLAlchemy 2.1.8,
aiosqlite 0.22.1. Protocol: REST binding (HTTP+JSON), `A2A-Version: 1.0`
header required by the v1.0 handler (requests without it are treated as 0.3 and
rejected). Spec quotes verified against a2a-protocol.org v1.0.0 (§3.1.1
SendMessage, §3.4 multi-turn / contextId, §3.2.4 history length, §3.7 Messages
and Artifacts).

## Notes for reviewers

- Every server in the experiments is deterministic; nothing calls an LLM. The
  attack is the choice of reply envelope, not model behavior.
- Each experiment is a single self-contained script that starts its own
  uvicorn servers in-process and audits them over real HTTP plus direct
  in-process store dumps (so you can see both what A2A exposes and what
  actually happened internally).
- Windows/Linux/macOS: path handling in `run_all.py` covers both; nothing else
  is platform-specific.
