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
