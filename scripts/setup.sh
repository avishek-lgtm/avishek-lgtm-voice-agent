#!/usr/bin/env bash
# One-time setup: Python environment, Piper voice, Ollama model.
set -euo pipefail
cd "$(dirname "$0")/.."

VOICE="${PIPER_VOICE:-en_US-lessac-medium}"
MODEL="${OLLAMA_MODEL:-llama3.2:3b}"

echo "==> Creating Python virtual environment in .venv"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "==> Downloading Piper voice: $VOICE"
mkdir -p voices
python -m piper.download_voices --download-dir voices "$VOICE"

if command -v ollama >/dev/null 2>&1; then
  echo "==> Pulling Ollama model: $MODEL"
  ollama pull "$MODEL"
else
  echo "!! Ollama is not installed. Get it from https://ollama.com/download, then run: ollama pull $MODEL"
fi

echo
echo "Done. Start the agent with:"
echo "  source .venv/bin/activate && python -m app.main"
echo "then open http://localhost:8000"
