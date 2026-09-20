# Ollama

Run LLMs locally. `make run_ollama` starts an Ollama container on the shared network (`make stop_ollama` / `make clean` to tear down).
The `run_*` model targets (for example `make run_gpt-oss-20b`) exec into it to pull and run a model; `make gpu_stat` wraps `nvidia-smi`.
The image, container name, port, and context length are the `OLLAMA_*` variables in [Environment variables](reference/environment.md#ollama).
