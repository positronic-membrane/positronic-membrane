# Operational Workflow Guide: Agentic Requirements Engineering
## Version: 1.0
## Target Environment: Antigravity IDE / Project IDX / Firebase Studio

### 1. The Natural Language Coding Pipeline
The developer operates exclusively as the Product Manager, establishing high-level intent, while the IDE’s integrated tools function as the engineering staff holding the marker. Management of the project lifecycle follows a distinct three-phase cadence:

```text
┌──────────────────────┐      ┌───────────────────────────┐      ┌────────────────────────────────┐
│   Developer Intent   │ ───> │  Direct Chat with Agent   │ ───> │  Agent Auto-Updates GEMINI.md  │
└──────────┬───────────┘      └───────────────────────────┘      └────────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Automated Git Branch & Code  │
└──────────────────────────────┘
```


#### Phase A: Ambient Brainstorming
* **Action:** The developer opens the IDE sidebar chat and details abstract feature concepts using plain English.
* **Agent Role:** The internal agent critiques the proposal against the pre-existing code infrastructure, highlighting logical conflicts or potential constraint violations based on current code states.

#### Phase B: Automated Memory Commits
* **Action:** Once conceptual logic is agreed upon, the developer commands the system to update its memory anchor rather than updating code or documentation manually.
* **Reference Prompt:** > *"Review our discussion regarding [Feature X]. Open our local file system, modify the appropriate sections of our `GEMINI.md` context anchor to map these new behavioral rules, and save the state."*

#### Phase C: Isolated Task Execution
* **Action:** With requirements anchored in the Markdown canvas, the agent transitions seamlessly from architect to developer to perform the heavy lifting.
* **Reference Prompt:**
    > *"Review our active roadmap in `GEMINI.md`. Break down the current task into an architectural design, initialize a new isolated feature Git branch, scaffold the local directory structure, and open a Pull Request for my review."*

### 2. Operational Rules for Asynchronous Agents
1.  **Read-First Directive:** Every time an asynchronous coding session initializes, the agent MUST parse the root `GEMINI.md` file before generating or modifying code modules.
2.  **The Self-Healing Loop:** If a generated script throws an environment error or fails a CI/CD compilation check, the agent must read its own local system logs, self-correct the logic gates, and re-attempt deployment natively.
3.  **Strict Content Blindness:** Agents are banned from reading raw creative text strings or writing semantic content outside of structural system files and the `GEMINI.md` memory canvas itself.
