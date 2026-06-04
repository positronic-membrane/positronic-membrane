# Positronic Membrane

Swarm AI experimentation and local autonomous agent daemon orchestration (Project Janus).

## Documentation

* **[Ollama Setup & Model Guide](file:///Users/jsmccauley/projects/positronic-membrane/docs/ollama_setup.md)**: Steps to download, install, run, and integrate local LLMs.
* **[Gemini.md Specification](file:///Users/jsmccauley/projects/positronic-membrane/docs/gemini_md_specification.md)**: Core rules, schema specifications, and constraints for the Janus system.
* **[Gemini.md Workflows](file:///Users/jsmccauley/projects/positronic-membrane/docs/gemini_md_workflows.md)**: Walkthroughs of daemon executions and Socratic alignments.
* **[Project Manifesto](file:///Users/jsmccauley/projects/positronic-membrane/docs/manifesto.md)**: Philosophy and long-term vision.

## Quick Start

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set up your environment file `.env` using [Ollama Setup & Model Guide](file:///Users/jsmccauley/projects/positronic-membrane/docs/ollama_setup.md).
3. Initialize the database and run the alignment wizard:
   ```bash
   python -m src.main
   ```

## Console Escaped Commands

When running Project Janus in CLI mode (`python -m src.main --cli`), the interactive Persona chat surface supports the following escaped/slash commands:

*   `/exit`: Gracefully shuts down the active conversation console and cancels the background daemon loops.
*   `/amend <rule_key> | <rule_text>`: Proposes a new rule or amendment to be sealed in the read-only core constitution table (requires interactive `y/n` confirmation).

### Staged Self-Modification Interactive Prompts
When the background agent swarm stages a code modification, the console intercepts execution prior to the next user prompt to display the unified diff and unit test execution logs. It then prompts for confirmation:
`Approve and commit this change? (y/n): `

*   **`y` / `yes`**: Applies the staged modifications back to the active workspace, clears the pending queue from `system_config`, cleans up temporary staging files, and restarts the async event loop to load the updated code.
*   **`n` / `no` / other key**: Rejects the modification, clears it from the queue, and deletes the temporary staging folder without altering any files in the active workspace.
