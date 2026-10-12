# 0057. Detached execution: one control plane, portable trial units

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Superseded by:** none

## Context and Problem Statement

Tolokaforge runs **trials**: an AI agent attempts a task inside a sandbox, and we
grade the result. Today, a single process — the **orchestrator** — does
everything for a whole batch at once: it reads the run config, decides which
trials to run, runs each agent↔user conversation, calls the LLMs, drives Docker
to execute the agent's tool calls, grades each trial, and writes the report.

That one process welds together two very different jobs:

1. **The "brain" (control):** decide what to run, hand out the work, collect the
   results, write the report.
2. **The "hands" (execution):** actually run *one* trial — stand up its sandbox,
   drive the loop, grade it.

Because the two are welded together, three things are hard:

- **Scaling means cloning the whole brain.** To use more machines you run more
  `tolokaforge worker` processes, and *every worker is a complete orchestrator*.
  That is fine on one laptop, but it is the wrong shape for a managed service,
  where one brain should hand work to many simple, interchangeable hands.
- **You cannot run just one trial.** There is no way to run a single trial
  without dragging the entire orchestrator along.
- **The runner and the grader cannot stand on their own.** They are built inside
  the orchestrator, so they cannot live on a different machine or be swapped
  independently.

## Decision Drivers

- Keep the simple single-box experience (`pip install`, one image, one command)
  working exactly as it does today.
- Make "run one trial" a thing that can happen **with or without** the
  orchestrator.
- Treat every kind of trial — our own agent loop and delegated harnesses alike —
  the same way, so the hands do not need to know what they are running.
- Let the grader be its own component: one grader, or several different ones,
  wherever they need to live.

## Decision

**Separate the brain from the hands, and make "run one trial" a self-contained,
portable unit.**

1. **A trial becomes a unit you can build and run on its own**, described
   entirely by **data** — which task, which models, which grader, plus a
   fingerprint of the task's files — together with a mounted copy of those files.
   No orchestrator required.
2. **Every trial runs through one narrow seam.** Whether it is our native agent
   loop or a delegated harness (terminal-bench, and other delegated harnesses),
   the hands only ever see "execute this one tool call" against the runner. So
   the execution side does not care what kind of trial it is.
3. **The grader is a separate, addressable component.** You can have one, or
   several different ones, and they do not have to sit next to the runner.
4. **The all-in-one mode stays the default** and behaves identically to before.
   Detaching is opt-in.

## What this lets us do

1. **Run a single trial standalone** — just the runner, no orchestrator — for a
   quick check, for embedding tolokaforge inside another tool, or for a one-off
   job.
2. **One brain, many hands.** One orchestrator can hand trials to many simple
   execution nodes on different machines, instead of running many full
   orchestrators. (The pieces are in place; sending a trial *over the network* to
   a remote machine is the next stage.)
3. **Mix job kinds in one run.** Native trials and delegated harnesses all flow
   through the same dispatch, because to the hands they all look like "one tool
   call."
4. **One grader or many.** Grade with a single grader, or route different trials
   to different graders, with no change to the runner.
5. **Scale the hands independently of the brain** — the foundation for a managed
   service where many people's jobs share one control plane and a pool of
   workers.

## Consequences

**Stays the same (important):**

- The single-box experience is untouched — same install, same command,
  byte-for-byte the same output. The old path is now just one *caller* of the new
  unit.

**New with this change:**

- You can run one trial with **no orchestrator** in the process.
- **Delegated harnesses are node-runnable** through the same unit as native
  trials.
- Because there is now exactly **one** way to run a trial, five latent bugs in
  the old single-trial path disappeared (a wrong grader address under one runtime
  mode, a dropped message-size limit, a missing Docker socket for some tasks, a
  missing snapshot setting, and a mislabeled error).

**Deliberately not done yet (staged):**

- A trial does not yet travel **over the network** to a remote machine — that is
  the next stage (a "remote" execution backend plus handing out runner
  addresses).
- There is **no job-submission service / multi-user front door** yet — that is a
  later milestone.
- **Cloud backends** (e.g. Kubernetes) are designed-for, not built.

**Trade-offs we accept:**

- A trial now travels as **serialized data plus a mounted task pack**. We attach
  a **fingerprint (hash)** of the task files, so a node that received the wrong or
  edited files **fails loudly** instead of silently grading the wrong thing.
- **One trial per process**, which keeps secret handling simple and isolated.
- In all-in-one mode the brain and hands share one process; the trust separation
  between them only matters once they are truly split, and the design keeps that
  seam intact for when they are.

## Considered Options

- **Keep "many full orchestrators" sharing a queue.** Simplest, and it already
  works for distributed batches — but it is *many brains*, not a managed service,
  and it still cannot run a single trial standalone. Rejected.
- **Rewrite the engine remote-first.** Clean end state, but it throws away the
  working, simple single-box path that everyone relies on. Rejected.
- **(Chosen) Extract a portable trial unit now, keep single-box as the default,
  and make remote execution a drop-in later.** Preserves today's experience while
  opening the path to the managed-service end state.

## References

- [ADR-0056](0056-node-runnable-trial-unit.md) — the node-runnable trial unit and
  its standalone entry (the mechanism this decision is realized by).
- [ADR-0015](0015-trial-executor-protocol.md),
  [ADR-0014](0014-trial-grader-protocol.md),
  [ADR-0022](0022-runtime-independence.md) — the execution, grading, and
  runtime-independence seams this builds on.
- [`docs/STANDALONE_RUNNER.md`](../STANDALONE_RUNNER.md),
  [`docs/DETACHED_GRADING.md`](../DETACHED_GRADING.md).
