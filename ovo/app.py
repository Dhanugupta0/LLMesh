"""OVO — Main Application.

Entry point: `ovo` command launches this.
Streaming chat with session persistence and model management.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from textual.app import App
from textual.worker import Worker

from ovo import __version__
from ovo.config import OvoConfig, OVO_DIR, SESSIONS_DIR, LOGS_DIR
from ovo.auth import store_api_key, mask_key
from ovo.agent import AgentState, AgentStatus, ChatMessage
from ovo.llmesh import ModelInfo, ModelStatus
from ovo.llmesh.client import LLMeshClient, APIError
from ovo.llmesh.streaming import StreamError
from ovo.sessions import SessionManager, Session, SessionMessage

from ovo.ui.screens.onboarding import OnboardingScreen, OnboardingComplete
from ovo.ui.screens.main import MainScreen
from ovo.ui.widgets.chat import OvoChat
from ovo.ui.widgets.composer import OvoComposer, ComposerSubmit
from ovo.ui.widgets.statusbar import OvoStatusBar
from ovo.ui.widgets.header import OvoHeader
from ovo.ui.widgets.infopanel import OvoInfoPanel


SYSTEM_PROMPT = (
    "You are OVO, a helpful AI assistant powered by LLMesh. "
    "Give clear, concise answers. Use markdown formatting when helpful."
)


class OvoApp(App):
    """OVO — Terminal AI assistant powered by LLMesh."""

    TITLE = "OVO"
    CSS_PATH = "ui/theme.tcss"

    # ── State ───────────────────────────────────────────────────

    def __init__(self):
        super().__init__()
        self.config = OvoConfig.load()
        self.state = AgentState()
        self.client: Optional[LLMeshClient] = None
        self.models: List[ModelInfo] = []
        self.session_mgr = SessionManager()
        self.current_session: Optional[Session] = None
        self._generation_worker: Optional[Worker] = None
        self._cancel_flag = False
        self._model_status_tag = ""  # Status tag for the info panel

    # ── Lifecycle ───────────────────────────────────────────────

    def on_mount(self):
        """Start with onboarding or main screen."""
        OVO_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        if self.config.is_configured():
            self.client = LLMeshClient(self.config.api_url, self.config.api_key)
            self.push_screen(MainScreen())
            self.run_worker(self._verify_and_load, thread=False)
        else:
            self.push_screen(OnboardingScreen(
                default_url=self.config.api_url,
                default_key=self.config.api_key,
            ))

    async def on_unmount(self):
        self._auto_save_session()
        if self.client:
            await self.client.close()

    # ── Onboarding ──────────────────────────────────────────────

    def on_onboarding_complete(self, event: OnboardingComplete):
        async def run_onboarding():
            await self._do_onboarding(event.api_url, event.api_key)
        self.run_worker(run_onboarding, thread=False)

    async def _do_onboarding(self, api_url: str, api_key: str):
        screen = self.screen
        if not isinstance(screen, OnboardingScreen):
            return

        screen.show_status("Connecting…")
        self.client = LLMeshClient(api_url, api_key)

        connected = await self.client.verify_connection()
        if not connected:
            screen.show_status("[red]✗ Cannot reach LLMesh endpoint[/]")
            return

        screen.show_status("✓ Endpoint reachable\nLoading models…")

        try:
            self.models = await self.client.list_models()
        except Exception as e:
            screen.show_status(f"[red]✗ Failed to load models: {e}[/]")
            return

        screen.show_status(
            f"✓ Connected\n"
            f"✓ {len(self.models)} models loaded\n\n"
            f"Welcome to OVO."
        )

        self.config.api_url = api_url
        store_api_key(self.config, api_key)
        self._select_default_model()
        self.state.connected = True

        await asyncio.sleep(1.0)

        self.pop_screen()
        self.push_screen(MainScreen())
        self._start_health_check()
        self._refresh_ui()

    # ── Verify (returning user) ─────────────────────────────────

    async def _verify_and_load(self):
        if not self.client:
            return

        connected = await self.client.verify_connection()
        self.state.connected = connected

        if connected:
            try:
                self.models = await self.client.list_models()
            except Exception:
                pass

            if self.config.current_model:
                available_names = [m.name for m in self.models if m.model_status.selectable]
                if self.config.current_model in available_names:
                    self.state.model = self.config.current_model
                    self.state.provider = self.config.current_provider
                else:
                    old = self.config.current_model
                    self._select_default_model()
                    self._chat_status(
                        f"⚠ Model '{old}' is no longer available. "
                        f"Switched to {self.state.model}."
                    )
            else:
                self._select_default_model()

            # Probe the current model to check if upstream key works
            self.run_worker(self._probe_model, thread=False)

        self._start_health_check()
        self._refresh_ui()

    def _select_default_model(self):
        """Select the first active model as default."""
        active = [m for m in self.models if m.model_status.selectable]
        if active:
            model = active[0]
            self.state.model = model.name
            self.state.provider = model.provider or ""
            if model.context_window:
                self.state.context_total = model.context_window
            self.config.current_model = self.state.model
            self.config.current_provider = self.state.provider
            self.config.save()

    # ── Model Probe (check upstream API key) ────────────────────

    async def _probe_model(self):
        """Quick probe: send a tiny request to check if the upstream key works."""
        if not self.client or not self.state.model:
            return

        try:
            result = await self.client.chat(
                messages=[{"role": "user", "content": "hi"}],
                model=self.state.model,
                max_tokens=1,
            )
            self._model_status_tag = "[#22c55e]● Key Valid[/]"
        except APIError as e:
            if e.status_code == 401 or e.status_code == 403:
                self._model_status_tag = "[#ef4444]✗ API Key Invalid[/]"
            elif e.status_code == 429:
                self._model_status_tag = "[#f59e0b]⚠ Rate Limited[/]"
            elif e.status_code == 404:
                self._model_status_tag = "[#ef4444]✗ Model Not Found[/]"
            else:
                self._model_status_tag = f"[#f59e0b]⚠ Error {e.status_code}[/]"
        except Exception:
            self._model_status_tag = "[#f59e0b]⚠ Probe Failed[/]"

        self._refresh_ui()

    # ── Health Check ────────────────────────────────────────────

    def _start_health_check(self):
        self.set_interval(30, self._check_health)

    async def _check_health(self):
        if self.client:
            connected = await self.client.health_check()
            if connected != self.state.connected:
                self.state.connected = connected
                self._refresh_ui()

    # ── Message Handling ────────────────────────────────────────

    def on_composer_submit(self, event: ComposerSubmit):
        text = event.text
        if text.startswith("/"):
            self._handle_command(text)
            return
        self._send_message(text)

    def _send_message(self, text: str):
        if not self.state.model:
            self._chat_status("No model selected. Use /models to see available models.")
            return
        if not self.state.connected:
            self._chat_status("Not connected to LLMesh.")
            return
        if self.state.status == AgentStatus.STREAMING:
            self._chat_status("Already generating. Press Ctrl+C to cancel.")
            return

        # Transition from welcome → chat if needed
        if isinstance(self.screen, MainScreen):
            self.screen.ensure_chat_mode()

        # Ensure we have a session
        if self.current_session is None:
            self.current_session = self.session_mgr.create(model=self.state.model)

        self.state.add_message("user", text)

        try:
            self.screen.query_one(OvoChat).add_user_message(text)
        except Exception:
            pass

        # Add task to info panel
        try:
            short = text[:40] + "…" if len(text) > 40 else text
            self.screen.query_one(OvoInfoPanel).add_task(short)
        except Exception:
            pass

        self._cancel_flag = False
        self.state.status = AgentStatus.STREAMING
        self._refresh_ui()
        self._generation_worker = self.run_worker(
            self._stream_response, thread=False
        )

    async def _stream_response(self):
        """Stream a response from LLMesh."""
        try:
            chat = self.screen.query_one(OvoChat)
        except Exception:
            return

        chat.add_assistant_start()

        messages = self.state.get_messages_for_api(SYSTEM_PROMPT)
        full_content = ""

        try:
            async for token in self.client.chat_stream(
                messages=messages,
                model=self.state.model,
            ):
                if self._cancel_flag:
                    chat.add_status_message("Generation cancelled", "✗")
                    break

                full_content += token
                chat.add_streaming_token(token)

            chat.finish_streaming(full_content)

            if full_content and not self._cancel_flag:
                self.state.add_message("assistant", full_content, model=self.state.model)

                est_tokens = len(full_content) // 4
                self.state.context_used += est_tokens
                chat.add_usage_info(tokens=est_tokens, model=self.state.model)

                # Mark last task as done
                try:
                    panel = self.screen.query_one(OvoInfoPanel)
                    if panel._tasks:
                        panel.mark_task_done(len(panel._tasks) - 1)
                except Exception:
                    pass

                self._auto_save_session()

        except APIError as e:
            chat.finish_streaming()
            if e.status_code in (404, 410):
                chat.add_status_message(
                    f"Model '{self.state.model}' is no longer available. Use /models to switch.", "✗"
                )
                self._model_status_tag = "[#ef4444]✗ Model Unavailable[/]"
                for m in self.models:
                    if m.name == self.state.model:
                        m.status = False
                        break
            elif e.status_code in (401, 403):
                chat.add_status_message(
                    f"API key for '{self.state.model}' is invalid or expired. Check your .env config.", "✗"
                )
                self._model_status_tag = "[#ef4444]✗ API Key Invalid[/]"
            elif e.status_code == 429:
                chat.add_status_message(
                    f"Rate limited on '{self.state.model}'. Wait a moment or switch models (/models).", "⚠"
                )
                self._model_status_tag = "[#f59e0b]⚠ Rate Limited[/]"
            else:
                chat.add_status_message(f"API error: {e.message}", "✗")
        except StreamError as e:
            chat.finish_streaming()
            chat.add_status_message(f"Stream error: {e}", "✗")
        except Exception as e:
            chat.finish_streaming()
            chat.add_status_message(f"Error: {str(e)[:100]}", "✗")

        self.state.status = AgentStatus.IDLE
        self._refresh_ui()

    # ── Session Management ──────────────────────────────────────

    def _auto_save_session(self):
        """Save the current session to disk."""
        if self.current_session is None or not self.state.messages:
            return

        self.current_session.messages = [
            SessionMessage(
                role=m.role,
                content=m.content,
                timestamp=m.timestamp.isoformat() if hasattr(m.timestamp, 'isoformat') else str(m.timestamp),
                model=m.model,
            )
            for m in self.state.messages
        ]
        self.current_session.model = self.state.model

        if self.current_session.title == "New session" and self.current_session.messages:
            self.current_session.title = self.session_mgr.auto_title(
                self.current_session.messages
            )

        self.session_mgr.save(self.current_session)

    def _restore_session(self, session: Session):
        """Restore a session: load messages into state and render in chat."""
        self._auto_save_session()
        self.state.clear_conversation()

        # Ensure chat mode
        if isinstance(self.screen, MainScreen):
            self.screen.ensure_chat_mode()

        try:
            self.screen.query_one(OvoChat).clear_chat()
        except Exception:
            pass

        self.current_session = session

        available_names = [m.name for m in self.models if m.model_status.selectable]
        if session.model and session.model in available_names:
            self.state.model = session.model
            for m in self.models:
                if m.name == session.model:
                    self.state.provider = m.provider or ""
                    break
        elif session.model:
            self._chat_status(
                f"⚠ Session model '{session.model}' is no longer available. "
                f"Using {self.state.model} instead."
            )

        for msg in session.messages:
            self.state.add_message(msg.role, msg.content, model=msg.model)

        try:
            chat = self.screen.query_one(OvoChat)
            for msg in session.messages:
                if msg.role == "user":
                    chat.add_user_message(msg.content)
                elif msg.role == "assistant":
                    chat.add_assistant_message(msg.content)
            chat.add_status_message(
                f"Session restored: {session.title} ({session.message_count} messages)", "✓"
            )
        except Exception:
            pass

        # Rebuild task list from user messages
        try:
            panel = self.screen.query_one(OvoInfoPanel)
            tasks = []
            for msg in session.messages:
                if msg.role == "user":
                    short = msg.content[:40] + "…" if len(msg.content) > 40 else msg.content
                    tasks.append((short, True))
            panel.set_tasks(tasks)
        except Exception:
            pass

        self._refresh_ui()

    # ── Command Handling ────────────────────────────────────────

    def _handle_command(self, text: str):
        # Ensure we're in chat mode for command output
        if isinstance(self.screen, MainScreen):
            self.screen.ensure_chat_mode()

        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        handlers = {
            "/help": lambda: self._cmd_help(),
            "/models": lambda: self._cmd_models(),
            "/model": lambda: self._cmd_model_select(arg),
            "/sessions": lambda: self._cmd_sessions(),
            "/session": lambda: self._cmd_session_select(arg),
            "/save": lambda: self._cmd_save(),
            "/delete": lambda: self._cmd_delete(arg),
            "/rename": lambda: self._cmd_rename(arg),
            "/new": lambda: self.action_new_session(),
            "/clear": lambda: self.action_clear_chat(),
            "/status": lambda: self._cmd_status(),
            "/exit": lambda: self.exit(),
        }

        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._chat_status(f"Unknown command: {cmd}. Type /help for commands.")

    def _cmd_help(self):
        help_text = (
            "**Commands**\n\n"
            "| Command | Description |\n"
            "|---------|-------------|\n"
            "| `/help` | Show this help |\n"
            "| `/models` | List available models |\n"
            "| `/model N` | Select model by number |\n"
            "| `/sessions` | List saved sessions |\n"
            "| `/session N` | Resume session by number |\n"
            "| `/save` | Force save current session |\n"
            "| `/rename TEXT` | Rename current session |\n"
            "| `/delete N` | Delete a saved session |\n"
            "| `/new` | New session |\n"
            "| `/clear` | Clear chat display |\n"
            "| `/status` | Connection status |\n"
            "| `/exit` | Exit OVO |\n\n"
            "**Keys**: Enter → send · Ctrl+C → cancel · Ctrl+N → new session"
        )
        try:
            self.screen.query_one(OvoChat).add_assistant_message(help_text)
        except Exception:
            pass

    def _cmd_models(self):
        if not self.models:
            self._chat_status("No models loaded. Check LLMesh connection.")
            return

        lines = ["**Available Models**\n"]
        for i, m in enumerate(self.models, 1):
            ms = m.model_status
            active = " ◀" if m.name == self.state.model else ""
            provider = m.provider or "—"
            lines.append(
                f"{i}. {ms.icon} **{m.display_name}**{active} — {provider} · {ms.label}"
            )
        lines.append(f"\n`/model N` to select (e.g. `/model 1`)")

        try:
            self.screen.query_one(OvoChat).add_assistant_message("\n".join(lines))
        except Exception:
            pass

    def _cmd_model_select(self, arg: str):
        if not arg:
            self._cmd_models()
            return

        try:
            idx = int(arg) - 1
            if 0 <= idx < len(self.models):
                model = self.models[idx]
                if not model.model_status.selectable:
                    self._chat_status(
                        f"Cannot select '{model.display_name}' — "
                        f"status: {model.model_status.label}. Choose an active model."
                    )
                    return

                self.state.model = model.name
                self.state.provider = model.provider or ""
                if model.context_window:
                    self.state.context_total = model.context_window
                self.config.current_model = self.state.model
                self.config.current_provider = self.state.provider
                self.config.save()
                self._chat_status(f"Model: {model.display_name} ({model.provider or '—'})")

                # Re-probe the new model
                self._model_status_tag = "[#71717a]● Checking…[/]"
                self._refresh_ui()
                self.run_worker(self._probe_model, thread=False)
            else:
                self._chat_status(f"Invalid number. Use /models to see the list.")
        except ValueError:
            self._chat_status("Usage: `/model N` (e.g. `/model 1`)")

    def _cmd_sessions(self):
        sessions = self.session_mgr.list_sessions(limit=10)
        if not sessions:
            self._chat_status("No saved sessions. Start chatting to create one!")
            return

        lines = ["**Saved Sessions**\n"]
        for i, s in enumerate(sessions, 1):
            current = " ◀" if self.current_session and s.id == self.current_session.id else ""
            lines.append(
                f"{i}. **{s.title}**{current} — {s.age_label} · "
                f"{s.message_count} msgs · {s.model or '—'}"
            )
        lines.append(f"\n`/session N` to resume · `/delete N` to remove")

        try:
            self.screen.query_one(OvoChat).add_assistant_message("\n".join(lines))
        except Exception:
            pass

    def _cmd_session_select(self, arg: str):
        if not arg:
            self._cmd_sessions()
            return

        sessions = self.session_mgr.list_sessions(limit=20)
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(sessions):
                session = self.session_mgr.load(sessions[idx].id)
                if session:
                    self._restore_session(session)
                else:
                    self._chat_status("Failed to load session.")
            else:
                self._chat_status("Invalid session number. Use /sessions to see the list.")
        except ValueError:
            self._chat_status("Usage: `/session N` (e.g. `/session 1`)")

    def _cmd_save(self):
        if self.current_session and self.state.messages:
            self._auto_save_session()
            self._chat_status(f"Session saved: {self.current_session.title}")
        else:
            self._chat_status("Nothing to save. Start chatting first.")

    def _cmd_delete(self, arg: str):
        if not arg:
            self._chat_status("Usage: `/delete N` (e.g. `/delete 3`)")
            return

        sessions = self.session_mgr.list_sessions(limit=20)
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(sessions):
                target = sessions[idx]
                if self.current_session and target.id == self.current_session.id:
                    self._chat_status("Cannot delete the active session. Use /new first.")
                    return
                self.session_mgr.delete(target.id)
                self._chat_status(f"Deleted session: {target.title}")
            else:
                self._chat_status("Invalid session number.")
        except ValueError:
            self._chat_status("Usage: `/delete N`")

    def _cmd_rename(self, arg: str):
        if not arg:
            self._chat_status("Usage: `/rename My Project`")
            return
        if self.current_session:
            self.current_session.title = arg.strip()
            self._auto_save_session()
            self._chat_status(f"Session renamed to: {arg.strip()}")
            self._refresh_ui()
        else:
            self._chat_status("No active session to rename.")

    def _cmd_status(self):
        status = "Connected" if self.state.connected else "Disconnected"
        session_info = self.current_session.title if self.current_session else "None"
        self._chat_status(
            f"Server: {self.config.api_url} · {status}\n"
            f"Model: {self.state.model or 'None'} · Provider: {self.state.provider or '—'}\n"
            f"Session: {session_info}"
        )

    # ── Actions ─────────────────────────────────────────────────

    def action_clear_chat(self):
        try:
            self.screen.query_one(OvoChat).clear_chat()
        except Exception:
            pass

    def action_new_session(self):
        self._auto_save_session()
        self.state.clear_conversation()
        self.current_session = None
        self._model_status_tag = ""

        if isinstance(self.screen, MainScreen):
            self.screen.ensure_chat_mode()

        try:
            self.screen.query_one(OvoChat).clear_chat()
        except Exception:
            pass

        try:
            self.screen.query_one(OvoInfoPanel).set_tasks([])
        except Exception:
            pass

        self._chat_status("New session started.")
        self._refresh_ui()

    def action_cancel_generation(self):
        if self.state.status == AgentStatus.STREAMING:
            self._cancel_flag = True
        else:
            self.exit()

    # ── UI Helpers ──────────────────────────────────────────────

    def _refresh_ui(self):
        if isinstance(self.screen, MainScreen):
            self.screen.update_state(self.state, self.current_session)

    def _chat_status(self, message: str):
        try:
            self.screen.query_one(OvoChat).add_status_message(message, "·")
        except Exception:
            pass


def main():
    """Entry point for the `ovo` command."""
    app = OvoApp()
    app.run()


if __name__ == "__main__":
    main()
