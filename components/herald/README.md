# Bundled Herald Router

This is the custom Herald source used by the Ultimate Assistant Windows friend-test build. It includes the Router, provider/account adapters, scoped MCP gateway, and supporting Python package. Maintainer-only AdminLoop modules and operational configuration are excluded.

Install the complete Ultimate Assistant bundle using `apps/assistant/Install-UltimateAssistant.cmd`; the installer installs this adjacent package into the assistant environment. The app starts its Router locally. The friend connects their own provider account in the app.

CLI backends use their full native harness and tools; project-scoped Herald MCP tools supplement them. The included MIT license applies to Herald. This custom build is not a new upstream Herald registry release.
