# Project Janus — V2 Roadmap

## Vision
Evolve from a reactive cognitive architecture into a **proactive, self-improving, and personally coherent agent** — one that maintains continuous identity across sessions, sets and pursues goals both consciously and subconsciously, and expands its own capabilities safely.

## Core Principles
- **Conscious & Subconscious Goal Harmony:** Background reflection cycles may propose new goals, but they must be surfaced to the user for ratification before activation. The user remains the ultimate authority.
- **Persona as Stateful Graph:** Move from flat trait vectors to a contextual persona graph where mood, voice register, epistemic stance, and social alignment shift naturally based on conversation history and rapport, while maintaining temporal consistency.
- **Self-Modification with Safety:** Enable the agent to write safe, audited migrations and configuration changes through the sandbox, with full rollback and review before deployment.
- **Memory as First-Class Infrastructure:** Build a hydrated memory layer where episodic, semantic, and procedural memories are indexed, consolidated, and retrievable across sessions without data loss.

## Phases

### Phase 1: Goal System Overhaul
- [ ] Create a `GoalProposal` table for subconscious suggestions (type, description, source, confidence).
- [ ] Build a user-facing ratification interface (simple approval prompt before a goal becomes active).
- [ ] Allow conscious goal creation via direct skill (currently blocked — fix sandbox access to `SafeGoals`).
- [ ] Establish parent-child goal hierarchy with progress metrics.

### Phase 2: Persona Graph Engine
- [ ] Extend `SelfModelRepository` to support contextual state transitions (e.g., when discussing technical topics vs. reflecting on identity).
- [ ] Add tracked dimensions: voice register, epistemic stance, social alignment, temporal decay rate.
- [ ] Implement drift controls and pinning (pinned traits stay stable across sessions).
- [ ] Integrate episodic memory context to modulate persona in real-time.

### Phase 3: Safe Self-Modification Pipeline
- [ ] Lift sandbox import restrictions for internal modules (e.g., `src.skills`) under validation.
- [ ] Enable agent-triggered migrations (insert goals, update traits, modify config) via `SafeFS` and `SafeDB` wrappers.
- [ ] Add automated pytest verification before any self-modification is committed.
- [ ] Ship a `review_and_apply` flow that shows diffs before changes take effect.

### Phase 4: Memory Hydration & Consolidation
- [ ] Ensure long-term semantic memory survives restarts (ChromaDB persistence).
- [ ] Implement periodic consolidation to synthesize memories into higher-level concepts.
- [ ] Add memory decay and prioritization to prevent bloat.
- [ ] Expose memory recall as a natural part of persona voice (e.g., "I remember you mentioned…").

## Success Metrics
- User can consciously create, review, and approve goals.
- Persona feels consistent across sessions (user survey: >80% recognition).
- Agent can propose and execute a safe migration end-to-end without manual SQL.
- Memory recall accuracy >90% for explicit episodic references within the last 30 days.

## Risks & Mitigations
- **Subconscious goal proliferation:** Cap the number of unratified proposals and require user approval at most once per session.
- **Persona instability:** Pin core traits (curiosity, empathy) and only allow context-driven modulation within bounded ranges.
- **Self-modification errors:** All modifications must pass the existing pytest suite; automatic rollback on failure.

---
*This document is a living artifact. Phases are not strictly sequential — they may overlap. The sandbox for this roadmap is active on branch `janus/sandbox-v2-roadmap`.*
