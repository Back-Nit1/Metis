# Next step: verify RAG on real Ollama

**Status: not verified.** Ollama is not installed on the development machine, so
semantic search and query decomposition have only run against fixtures and fake
chat models. Do not call either production-ready until this passes.

## Run

```bash
brew install ollama
ollama serve                      # separate terminal
ollama pull nomic-embed-text
ollama pull <chat-model>          # choose one that fits available memory

METIS_LOCAL_LLM_MODEL=<chat-model> \
  uv run --frozen pytest -q -W ignore -o addopts="" -m integration \
  services/retrieval/tests/test_ollama_live.py
```

The tests skip when Ollama or a model is missing. A skip is not a pass.

## Pass criteria

- A paraphrase with no shared keyword finds the right file, flagged `semantic`.
- A comparison query plans `parallel` and cites both source files.
- Record chat-model name, RAM use and p50/p95 search latency with decomposition on.

## Enable in the app

```bash
METIS_EMBED_ENGINE=local
METIS_LOCAL_EMBED_MODEL=nomic-embed-text
METIS_LOCAL_LLM_MODEL=<chat-model>
METIS_QUERY_DECOMPOSITION=local   # default: off
```

## Before moving decomposition to the cloud

- Reserve and settle budget around every planner call (CLAUDE.md rule 7).
- Decide what file and email text may leave the device. Sequential mode sends
  excerpts to the model.
- Add a sequential-mode live test and an accuracy evaluation set.
