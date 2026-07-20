# Product Vision

## Problem

Application repositories need a durable controller that can schedule Factory work without confusing transient process state, mutable cache files, or prose with accepted Git and queue evidence. Independent phase-specific lifecycle implementations have caused stale-session resumes, duplicate-work risk, and incorrect historical conclusions.

## Users and outcomes

The owner can run one controller across isolated application repositories and receive deterministic scheduling, exact recovery, and review-ready evidence. Application writers retain repository-local authority; the Conveyor never becomes a cross-repository production-code writer.

## Principles

- Append immutable phase evidence before projecting current state.
- Bind every write to one repository, transaction, workflow, lease, branch, and starting commit.
- Prefer exact Git, queue, report, and runtime identity over exit codes or prose.
- Keep default-branch merge, push, release, and deployment explicitly human-controlled.

## Non-goals

- Implementing application product features directly.
- Automatically merging a milestone into its default branch.
- Publishing, deploying, tagging, releasing, or notarizing.
- Treating a cache, timestamp, PID death, or agent claim as sufficient completion evidence.
