"""OVO Onboarding Screen — first-run setup."""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Input, Button
from textual.containers import Vertical
from textual.message import Message


class OnboardingComplete(Message):
    """Posted when onboarding is done."""
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key
        super().__init__()


class OnboardingScreen(Screen):
    """First-run screen: connect to LLMesh."""

    BINDINGS = [("escape", "quit", "Exit")]

    def __init__(self, default_url: str = "http://localhost:8087", default_key: str = ""):
        super().__init__()
        self._default_url = default_url
        self._default_key = default_key

    def compose(self) -> ComposeResult:
        with Vertical(id="onboarding"):
            with Vertical(id="onboarding-container"):
                yield Static("⬡ OVO", id="onboarding-title")
                yield Static("Powered by LLMesh", id="onboarding-subtitle")
                yield Static("")
                yield Static(
                    "Add your LLMesh API key to get started.\n"
                    "Get yours from the LLMesh web dashboard.",
                    classes="onboarding-label",
                )
                yield Static("")
                yield Static("LLMesh Endpoint:", classes="onboarding-label")
                yield Input(
                    value=self._default_url,
                    placeholder="http://localhost:8087",
                    id="onboarding-url",
                    classes="onboarding-input",
                )
                yield Static("API Key:", classes="onboarding-label")
                yield Input(
                    value=self._default_key,
                    placeholder="Paste your LLMesh API key here",
                    id="onboarding-key",
                    password=True,
                    classes="onboarding-input",
                )
                yield Static("")
                yield Button("Add API Key & Connect", id="connect-btn", variant="primary")
                yield Static("", id="onboarding-status")

    def on_mount(self):
        # Focus the key input if we already have a URL
        if self._default_url and not self._default_key:
            self.query_one("#onboarding-key", Input).focus()
        else:
            self.query_one("#onboarding-url", Input).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "connect-btn":
            self._try_connect()

    def on_key(self, event):
        if event.key == "enter":
            self._try_connect()

    def _try_connect(self):
        """Attempt to connect to LLMesh."""
        api_url = self.query_one("#onboarding-url", Input).value.strip()
        api_key = self.query_one("#onboarding-key", Input).value.strip()
        status = self.query_one("#onboarding-status", Static)

        if not api_url:
            status.update("[red]Please enter a LLMesh endpoint[/]")
            return

        if not api_key:
            status.update("[red]Please enter your API key[/]")
            return

        status.update("Connecting…")

        # Post the connect event — the app will handle async verification
        self.post_message(OnboardingComplete(api_url, api_key))

    def show_status(self, text: str):
        """Update status text from the app."""
        try:
            self.query_one("#onboarding-status", Static).update(text)
        except Exception:
            pass

    def action_quit(self):
        self.app.exit()
