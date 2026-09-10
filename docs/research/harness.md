# Harness wires: prefer native over ACP adapters

Steal target: Comet/Zeron `docs/research/harness.md` (2026-07). Puppetmaster
keeps **native** worker adapters — Claude stream-json, Codex app-server /
`codex exec`, Cursor SDK, Antigravity stream-json — rather than routing every
provider through an ACP-as-universal-adapter shim.

## Why native first

- Claude Code: spawn installed `claude`, speak stream-json directly. Steerable
  runs keep stdin open (`--input-format stream-json`); interrupt is a control
  request (capabilities-gated). One-shot interrupt is SIGTERM.
- Codex: `codex app-server` JSON-RPC is the surface with token deltas, turn /
  steer / interrupt, thread resume, approvals. `codex exec --json` is CI-only
  (no deltas/steer/approvals).
- Cursor: `@cursor/sdk` one-shot + streamed subprocess heartbeats.
- Antigravity: already on stream-json stdin/stdout in-tree.

ACP adapters that wrap these CLIs can **fake Done** (quiet-settle discarding a
pending prompt future) or **hold turns open** (npx cold install stalls,
handshake without a deadline). See [acp.md](acp.md). Prefer hardening the
native adapters Puppetmaster already owns.

## Capability matrix to keep honest

Normalized events, typed tool decoding, model/effort discovery, AskUserQuestion
→ respond_input, resume, interrupt, step-boundary steering, subagent frame
filtering, error-code mapping. Map those onto PM task/artifact + the
[session command ledger](../ARCHITECTURE.md) —
not onto a second agent runtime inside Marionette / Automaton / Discord OS.

## Explicitly deferred

- ACP-as-universal-adapter day-one
- Full Loro/CRDT + ChatRoom/DeviceRoom edge stack
- UI/glass/chrome parity with Zeron

Citations stay with upstream docs (Claude headless, Codex app-server, ACP
spec). This file records the *product decision* for Puppetmaster.
