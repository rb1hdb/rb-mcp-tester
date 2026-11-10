#!/usr/bin/env bash
cd "$(dirname "$0")"
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
# Uso: ./run_dev.sh [modelo]   (ex.: ./run_dev.sh qwen3:8b)
# Ordem: argumento > variável OLLAMA_MODEL já exportada > qwen3:14b
export OLLAMA_MODEL="${1:-${OLLAMA_MODEL:-qwen3:14b}}"
echo "[run_dev] modelo: $OLLAMA_MODEL | think=${OLLAMA_THINK:-0} | ollama: $OLLAMA_HOST"
export OLLAMA_THINK="${OLLAMA_THINK:-0}"   # 0 = sem raciocínio (padrão, rápido); 1 = com <think>
exec venv/bin/python app.py
