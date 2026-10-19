@echo off
openclaw agent --agent main --session-id qwen3tooltest2 --model ollama/qwen3:14b -m "Call the perception_status tool and report which ollama models it lists." --json --timeout 220
