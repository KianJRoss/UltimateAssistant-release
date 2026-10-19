@echo off
openclaw agent --agent main -m "Call the perception describe_screen tool and tell me exactly what is on my screen right now. If it reports blank, say so." --json --timeout 200
