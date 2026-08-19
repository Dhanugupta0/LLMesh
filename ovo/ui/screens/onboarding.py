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
                yield Static("Connect your LLMesh account", classes="onboarding-label")
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
                    placeholder="Enter your LLMesh API key",
                    id="onboarding-key",
                    password=True,
                    classes="onboarding-input",
                )
                yield Static(
                    "Tip: Set GROQ_API_KEY, NVIDIA_NIM_API_KEY, or\n"
                    "OPENROUTER_API_KEY in .env for upstream providers.",
                    classes="onboarding-label",
                )
                yield Static("")
                yield Button("Connect", id="connect-btn", variant="primary")
                yield Static("", id="onboarding-status")

    def on_mount(self):
        # Focus the URL input if no key, otherwise the key input
        if self._default_key:
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
