# Ollama Setup & Model Guide

This guide provides instructions for downloading, installing, and starting [Ollama](https://ollama.com) to run Large Language Models (LLMs) locally on your machine. It also highlights recommended models optimized for Project Janus's multi-agent swarm architecture.

> [!IMPORTANT]
> **Required Startup Models:** Before executing Project Janus (`python3 -m src.main`), you **MUST** download both the global text-generation model and the semantic text-embedding model. Failure to pull `nomic-embed-text` will result in database initialization and memory lookup errors at startup.
> 
> Pull both models via your command line:
> ```bash
> ollama pull qwen2.5-coder:7b
> ollama pull nomic-embed-text
> ```

---

## 1. Installation

Ollama is a lightweight, open-source framework that lets you run open-weights LLMs locally.

### macOS
You can install Ollama on macOS in one of two ways:
* **Direct Download**: Download the zip file from [ollama.com/download](https://ollama.com/download/macOS), unzip it, and drag `Ollama.app` to your `Applications` folder.
* **Homebrew**: Run the following command in your terminal:
  ```bash
  brew install ollama
  ```

### Linux
Run the official install script:
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### Windows
Download the Windows installer from [ollama.com/download](https://ollama.com/download/windows) and run the installer executable.

---

## 2. Starting the Ollama Service

Before your application can make API requests to Ollama, the Ollama background service must be running.

* **macOS (App)**: Launch `Ollama.app` from your Applications folder or via Spotlight. An Ollama icon will appear in your menu bar.
* **macOS/Linux (Service)**: If installed via Homebrew or on Linux, you can manage the service via system managers:
  ```bash
  # Homebrew (macOS)
  brew services start ollama
  ```
* **Manual Terminal Execution**: You can also launch the server directly in a terminal window:
  ```bash
  ollama serve
  ```

> [!NOTE]
> By default, Ollama binds to `127.0.0.1:11434`. Project Janus is pre-configured to look for this default port.

---

## 3. Ollama CLI Reference

Use these basic commands in your terminal to manage your local models.

| Command | Action | Example |
| :--- | :--- | :--- |
| `ollama pull <model>` | Downloads a model without starting an interactive session. | `ollama pull qwen2.5-coder:7b` |
| `ollama run <model>` | Downloads (if missing) and starts an interactive chat shell. | `ollama run qwen2.5-coder:7b` |
| `ollama list` | Lists all downloaded models currently available locally. | `ollama list` |
| `ollama rm <model>` | Deletes a downloaded model to free up disk space. | `ollama rm llama3:8b` |
| `ollama ps` | Lists models currently loaded into memory/VRAM. | `ollama ps` |

---

## 4. Recommended Models for Project Janus

Project Janus runs a multi-agent swarm containing a **Proposer**, a **Critic**, an **Explorer**, and an **Archivist**. Different tasks benefit from different model capabilities. Below is a curated selection of models appropriate for local use:

### Model Comparison Table

| Model Name | Parameters | Size (Disk) | Recommended Role | Rationale & Requirements |
| :--- | :---: | :---: | :--- | :--- |
| **`qwen2.5-coder:7b`** *(Default)* | 7.2B | ~4.7 GB | Global / General Fallback | State-of-the-art coding and logic for its size. High compliance with structured JSON outputs. Requires 8GB+ RAM/VRAM. |
| **`qwen2.5-coder:1.5b`** | 1.5B | ~980 MB | Critic / Archivist | Extremely fast with very low overhead. Excellent for simpler tasks, safety checks, or systems with limited VRAM. |
| **`llama3.1:8b`** | 8B | ~4.7 GB | Proposer / Explorer | Exceptional general instruction-following and conversation. Good fallback if Qwen is unavailable. Requires 8GB+ RAM/VRAM. |
| **`gemma2:2b`** | 2B | ~1.6 GB | Critic / Guardrails | Highly optimized Google model with strong reasoning, instruction compliance, and safety-veto capabilities. |
| **`phi3.5:3.8b`** | 3.8B | ~2.2 GB | Explorer / Archivist | Microsoft's small reasoning model. Balanced speed and intelligence, fitting well on machines with 8GB RAM. |
| **`qwen2.5-coder:14b`** | 14B | ~9.0 GB | High-Performance Global | Much stronger reasoning and complex task planning. Ideal for machines with 16GB+ RAM (Apple Silicon M-series or discrete GPUs). |
| **`nomic-embed-text`** | - | ~274 MB | Semantic Embedding | Required for Stage 3 semantic memory (ChromaDB). Must be pulled separately (`ollama pull nomic-embed-text`) before starting the daemon. |

---

## 5. Integrating with Project Janus

Project Janus uses an OpenAI-compatible API interface to communicate with Ollama. Follow these steps to configure your environment:

### Step 1: Create a `.env` File
In the root directory of the project, create or edit your `.env` file and specify the Ollama connection details.

```env
# Database location (optional)
# DB_PATH=./janus.db

# Ollama Endpoint Configuration
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama  # Ollama does not require a real API key, but a placeholder is needed

# Global Model Configuration
LLM_MODEL=qwen2.5-coder:7b
EMBEDDING_MODEL=nomic-embed-text
```

### Step 2: Configure Agent-Specific Models (Optional)
If your machine is resource-constrained or you want to route specialized tasks to specific models, you can override agent roles in the `.env` file:

```env
# Agent Role-Specific Overrides
PROPOSER_MODEL=llama3.1:8b
CRITIC_MODEL=qwen2.5-coder:1.5b      # Light and fast for auditing actions
EXPLORER_MODEL=qwen2.5-coder:7b
ARCHIVIST_MODEL=qwen2.5-coder:1.5b   # Lightweight for parsing logs and memory
```

> [!TIP]
> Running multiple different models simultaneously requires Ollama to load each model into memory, which can lead to swapping latency on systems with less than 16GB RAM. If your system is lagging, set all agents to use the same model (e.g., `qwen2.5-coder:7b`), or run lightweight models exclusively.

---

## 6. Verifying Your Setup

Once Ollama is running and you have pulled your desired model, you can run this simple Python check to verify the connection:

```python
import os
from openai import OpenAI

# Initialize the client matching project Janus configuration
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama"
)

try:
    response = client.chat.completions.create(
        model="qwen2.5-coder:7b",
        messages=[
            {"role": "user", "content": "Respond with the word 'SUCCESS' if you read this."}
        ]
    )
    print("Ollama Response:", response.choices[0].message.content.strip())
except Exception as e:
    print("Connection failed:", e)
    print("Ensure Ollama is running and you have run: ollama pull qwen2.5-coder:7b")
```
