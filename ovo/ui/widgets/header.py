"""OVO Header widget — top bar with title, model, and connection status."""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ovo.agent import AgentState


class OvoHeader(Widget):
    """Top bar: OVO title + model + connection indicator."""

    DEFAULT_CSS = ""

    def __init__(self, connected: bool = False):
        super().__init__()
        self._connected = connected

    def compose(self) -> ComposeResult:
        with Static(id="header-bar"):
            yield Static("⬡ OVO", id="header-title")
            yield Static("", id="header-model")
            yield Static("", id="header-spacer")
            yield Static("", id="header-connection")

    def update_state(self, state: AgentState):
        """Update header from agent state."""
        # Model
        model = self.query_one("#header-model", Static)
        if state.model:
            name = state.model
            if "/" in name:
                name = name.split("/", 1)[1]
            if ":free" in name:
                name = name.replace(":free", " ·free")
            model.update(f"  [{name}]")
        else:
            model.update("")

        # Connection
        self.set_connected(state.connected)

    def set_connected(self, connected: bool):
        self._connected = connected
        conn = self.query_one("#header-connection", Static)
        if connected:
            conn.update("● Connected")
            conn.set_class(True, "status-connected")
            conn.set_class(False, "status-disconnected")
        else:
            conn.update("○ Disconnected")
            conn.set_class(False, "status-connected")
            conn.set_class(True, "status-disconnected")
