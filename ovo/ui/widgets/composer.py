"""OVO Composer widget — message input with send button."""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Input, Button
from textual.containers import Horizontal
from textual.message import Message


class ComposerSubmit(Message):
    """Posted when the user submits a message (Enter or Send button)."""
    def __init__(self, text: str):
        self.text = text
        super().__init__()


class OvoComposer(Widget):
    """Message input with Enter to send and a Send button."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="composer-container"):
            yield Input(
                placeholder="Type a message or /help…",
                id="composer-input",
            )
            yield Button("↑", id="composer-send", variant="primary")

    def on_mount(self):
        self.query_one("#composer-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter key in the input."""
        text = event.value.strip()
        if text:
            self.post_message(ComposerSubmit(text))
            event.input.value = ""

    def on_button_pressed(self, event: Button.Pressed):
        """Handle send button click."""
        if event.button.id == "composer-send":
            inp = self.query_one("#composer-input", Input)
            text = inp.value.strip()
            if text:
                self.post_message(ComposerSubmit(text))
                inp.value = ""
            inp.focus()

    def clear(self):
        self.query_one("#composer-input", Input).value = ""

    def set_focus(self):
        self.query_one("#composer-input", Input).focus()
