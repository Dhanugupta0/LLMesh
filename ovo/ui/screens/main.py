"""OVO Main Screen — welcome view transitions to 4:1 chat layout."""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input
from textual.containers import Vertical, Horizontal, Center

from ovo.ui.widgets.header import OvoHeader
from ovo.ui.widgets.chat import OvoChat
from ovo.ui.widgets.composer import OvoComposer, ComposerSubmit
from ovo.ui.widgets.statusbar import OvoStatusBar
from ovo.ui.widgets.infopanel import OvoInfoPanel
from ovo.agent import AgentState


class MainScreen(Screen):
    """Main application screen with two modes:

    1. Welcome mode: centered greeting + input
    2. Chat mode: 4:1 split (chat | info panel)

    Transitions from welcome → chat on first message.
    """

    BINDINGS = [
        ("ctrl+l", "clear_chat", "Clear"),
        ("ctrl+n", "new_session", "New session"),
        ("ctrl+c", "cancel", "Cancel"),
    ]

    def __init__(self):
        super().__init__()
        self._in_welcome = True

    def compose(self) -> ComposeResult:
        yield OvoHeader()

        # Welcome view (shown initially, hidden after first message)
        with Center(id="welcome-view"):
            with Vertical(id="welcome-container"):
                yield Static("⬡", id="welcome-logo")
                yield Static("Yo, How can I help you today?", id="welcome-greeting")
                yield Static("", id="welcome-model-tag")
                yield Input(
                    placeholder="Ask me anything…",
                    id="welcome-input",
                )

        # Chat view (hidden initially, shown after first message)
        with Horizontal(id="chat-view"):
            with Vertical(id="chat-column"):
                yield OvoChat()
                yield OvoComposer()
            yield OvoInfoPanel()

        yield OvoStatusBar()

    def on_mount(self):
        """Show welcome, hide chat."""
        self.query_one("#chat-view").display = False
        self.query_one("#welcome-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter on the welcome input — transition to chat mode."""
        if event.input.id == "welcome-input":
            text = event.value.strip()
            if text:
                self._transition_to_chat()
                # Forward the message to the app as a ComposerSubmit
                self.app.on_composer_submit(ComposerSubmit(text))

    def _transition_to_chat(self):
        """Switch from welcome view to chat view."""
        if not self._in_welcome:
            return
        self._in_welcome = False
        self.query_one("#welcome-view").display = False
        self.query_one("#chat-view").display = True
        try:
            self.query_one(OvoComposer).set_focus()
        except Exception:
            pass

    def ensure_chat_mode(self):
        """Ensure we're in chat mode (called when restoring sessions etc.)."""
        self._transition_to_chat()

    def update_state(self, state: AgentState, session=None):
        """Update all widgets from agent state."""
        try:
            session_title = session.title if session else ""
            self.query_one(OvoStatusBar).update_state(state, session_title)
            self.query_one(OvoHeader).update_state(state)
            # Update info panel
            model_tag = ""
            if hasattr(self.app, '_model_status_tag'):
                model_tag = self.app._model_status_tag
            self.query_one(OvoInfoPanel).update_state(state, model_tag)
        except Exception:
            pass

        # Update welcome model tag if still in welcome mode
        if self._in_welcome and state.model:
            try:
                name = state.model
                if "/" in name:
                    name = name.split("/", 1)[1]
                if ":free" in name:
                    name = name.replace(":free", " · free")
                self.query_one("#welcome-model-tag", Static).update(
                    f"[#71717a]Model: [#00f5d4]{name}[/][/]"
                )
            except Exception:
                pass

    # ── Actions (delegated to app) ──────────────────────────────

    def action_clear_chat(self):
        self.app.action_clear_chat()

    def action_new_session(self):
        self.app.action_new_session()

    def action_cancel(self):
        self.app.action_cancel_generation()
