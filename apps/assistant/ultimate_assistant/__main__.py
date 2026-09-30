from __future__ import annotations

import sys

from .assistant import Assistant
from .portal import PortalSession
from .settings import settings


HELP = """Commands:
  /search <query>  Search the web with Herald's headless web-search MCP.
  /models          List models available from the Herald Router.
  /model <name>    Select and save the active model.
  /portal          Show guided academic portal commands.
  /help            Show this help.
  /quit            Exit.

Any other message chats through the assistant's scoped Herald tools and retrieves
matching local knowledge. Study practice and study planning work from the
material, deadlines, and availability you provide; no school portal is needed.
Portal commands open a temporary Edge session for human login/MFA and read-only
page discovery. Page text is sent to the selected model only after confirmation.
"""


def main() -> int:
    assistant = Assistant()
    portal = PortalSession()
    print(f"Ultimate Assistant (Herald: {settings.herald_url}; model: {assistant.model})")
    print("Type /help for commands. Tools only act within the configured assistant scope.")

    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            portal.close()
            return 0
        if not text:
            continue
        if text.casefold() in {"/quit", "/exit"}:
            portal.close()
            return 0
        if text.casefold() == "/help":
            print(HELP)
            continue
        if text.casefold() == "/models":
            try:
                for model in assistant.list_models():
                    state = " unavailable" if model.get("circuit_open") else ""
                    active = " (selected)" if model.get("id") == assistant.model else ""
                    print(f"{model.get('id')} [{model.get('backend_type', 'unknown')}]{state}{active}")
            except Exception as exc:
                print(f"Could not list Router models: {exc}", file=sys.stderr)
            continue
        if text.casefold().startswith("/model "):
            model = text[7:].strip()
            try:
                assistant.set_model(model)
                print(f"Selected {assistant.model}; saved for future launches.")
            except Exception as exc:
                print(f"Could not select model: {exc}", file=sys.stderr)
            continue
        if text.casefold() == "/portal":
            print(
                "Portal commands:\n"
                "  /portal open <url>  Open a temporary Edge session. Sign in and complete MFA yourself.\n"
                "  /portal inspect     Ask the selected model to interpret visible page text.\n"
                "  /portal follow <id> Navigate to a numbered same-origin link from the latest inspection.\n"
                "  /portal close       Close the browser and discard its session.\n"
                "Inspect requires explicit consent before page text is sent to Herald."
            )
            continue
        if text.casefold().startswith("/portal "):
            command, _, argument = text[8:].partition(" ")
            try:
                if command.casefold() == "open":
                    if not argument.strip():
                        raise ValueError("Usage: /portal open <url>")
                    title = portal.open(argument.strip())
                    print(f"Opened {title or 'portal'}. Sign in and complete MFA directly in Edge.")
                elif command.casefold() == "inspect":
                    snapshot = portal.snapshot()
                    if not portal.ai_consent:
                        print(
                            f"This sends visible page text and same-site link labels to "
                            f"{assistant.model} via Herald. It may include personal course "
                            "or grade information; form values are excluded. This page is "
                            "not added to the assistant's persistent chat-session memory."
                        )
                        if input("Send this page to the model? [y/N] ").strip().casefold() not in {"y", "yes"}:
                            print("Page not sent.")
                            continue
                        portal.ai_consent = True
                    print(f"Inspecting {snapshot['title']} — {snapshot['url']}")
                    if snapshot["truncated"]:
                        print("Note: page text was truncated to 10,000 characters.")
                    print("\nAssistant: " + assistant.interpret_portal_page(snapshot))
                elif command.casefold() == "follow":
                    if not argument.strip().isdigit():
                        raise ValueError("Usage: /portal follow <link-id>")
                    title = portal.follow_link(int(argument.strip()))
                    print(f"Opened {title or 'page'}. Inspect again to verify the result.")
                elif command.casefold() == "close":
                    portal.close()
                    print("Portal browser closed; temporary session discarded.")
                else:
                    raise ValueError("Use /portal to see the available portal commands.")
            except Exception as exc:
                print(f"Portal error: {exc}", file=sys.stderr)
            continue

        include_web = text.casefold().startswith("/search ")
        prompt = text[8:].strip() if include_web else text
        try:
            print("\nAssistant: " + assistant.respond(prompt, include_web=include_web))
        except Exception as exc:
            print(f"\nAssistant error: {exc}", file=sys.stderr)



if __name__ == "__main__":
    raise SystemExit(main())
