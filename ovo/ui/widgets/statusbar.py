"""OVO Status Bar widget — bottom bar with session, model, connection status."""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ovo.agent import AgentState, AgentStatus


class OvoStatusBar(Widget):
    """Bottom status bar showing live application state."""

    def compose(self) -> ComposeResult:
        with Static(id="statusbar-content"):
            yield Static("", id="sb-session", classes="status-item status-session")
            yield Static("│", classes="status-item status-sep")
            yield Static("No model", id="sb-model", classes="status-item status-model")
            yield Static("│", classes="status-item status-sep")
            yield Static("● Connected", id="sb-conn", classes="status-item status-connected")
            yield Static("│", classes="status-item status-sep")
            yield Static("", id="sb-status", classes="status-item")

    def update_state(self, state: AgentState, session_title: str = ""):
        """Update status bar from agent state."""
        # Session
        session_w = self.query_one("#sb-session", Static)
        if session_title:
            label = session_title if len(session_title) <= 25 else session_title[:22] + "…"
            session_w.update(f"📋 {label}")
        else:
            session_w.update("")

        # Model
        model_name = state.model or "No model"
        # Shorten for display
        if "/" in model_name:
            model_name = model_name.split("/", 1)[1]
        if ":free" in model_name:
            model_name = model_name.replace(":free", "")
        self.query_one("#sb-model", Static).update(model_name)

        # Connection
        conn = self.query_one("#sb-conn", Static)
        if state.connected:
            conn.update("● Connected")
            conn.set_class(True, "status-connected")
            conn.set_class(False, "status-disconnected")
        else:
            conn.update("○ Disconnected")
            conn.set_class(False, "status-connected")
            conn.set_class(True, "status-disconnected")

        # Streaming status
        status_widget = self.query_one("#sb-status", Static)
        if state.status == AgentStatus.STREAMING:
            status_widget.update("● Streaming…")
        elif state.status == AgentStatus.FAILED:
            status_widget.update("✗ Error")
        else:
            status_widget.update("")
