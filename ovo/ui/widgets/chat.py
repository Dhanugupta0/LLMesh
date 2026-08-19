"""OVO Chat widget — scrollable conversation with bubble-style messages."""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Markdown
from textual.containers import VerticalScroll, Horizontal


class MessageBubble(Static):
    """A single chat message bubble with role-based alignment."""
    pass


class OvoChat(VerticalScroll):
    """Scrollable conversation area with left/right aligned messages.

    User messages → right side
    Assistant messages → left side
    """

    def __init__(self):
        super().__init__()
        self._streaming_widget: Static | None = None
        self._streaming_text: str = ""

    def add_user_message(self, content: str):
        """Add a user-aligned message (right side)."""
        spacer = Static("", classes="msg-spacer")
        bubble = Static(content, classes="msg-bubble msg-bubble-user")
        row = Horizontal(spacer, bubble, classes="msg-row msg-row-user")
        self.mount(row)
        self.scroll_end(animate=False)

    def add_assistant_start(self):
        """Start a new assistant message (left side, for streaming)."""
        label = Static("⬡", classes="msg-avatar msg-avatar-assistant")
        widget = Static("", classes="msg-bubble msg-bubble-assistant")
        spacer = Static("", classes="msg-spacer")
        row = Horizontal(label, widget, spacer, classes="msg-row msg-row-assistant")
        self.mount(row)
        self._streaming_widget = widget
        self._streaming_text = ""
        self.scroll_end(animate=False)

    def add_streaming_token(self, token: str):
        """Append a token to the current streaming message."""
        if self._streaming_widget is not None:
            self._streaming_text += token
            self._streaming_widget.update(self._streaming_text)
            self.scroll_end(animate=False)

    def finish_streaming(self, full_content: str = ""):
        """Finish streaming — replace the Static with rendered Markdown."""
        if self._streaming_widget is not None:
            if full_content:
                parent = self._streaming_widget.parent
                self._streaming_widget.remove()
                md = Markdown(full_content, classes="msg-bubble msg-bubble-assistant")
                if parent:
                    # Insert before the spacer (last child)
                    children = list(parent.children)
                    if children:
                        parent.mount(md, before=children[-1])
                    else:
                        parent.mount(md)
                else:
                    self.mount(md)
            self._streaming_widget = None
            self._streaming_text = ""
            self.scroll_end(animate=False)

    def add_assistant_message(self, content: str):
        """Add a complete assistant message (left side)."""
        label = Static("⬡", classes="msg-avatar msg-avatar-assistant")
        bubble = Markdown(content, classes="msg-bubble msg-bubble-assistant")
        spacer = Static("", classes="msg-spacer")
        row = Horizontal(label, bubble, spacer, classes="msg-row msg-row-assistant")
        self.mount(row)
        self.scroll_end(animate=False)

    def add_status_message(self, message: str, icon: str = "●"):
        """Add a centered status indicator."""
        self.mount(Static(f" {icon} {message}", classes="msg-status"))
        self.scroll_end(animate=False)

    def add_usage_info(self, tokens: int = 0, model: str = "", latency_ms: float = 0):
        """Add token usage info after a response."""
        parts = []
        if tokens:
            parts.append(f"{tokens:,} tokens")
        if latency_ms:
            parts.append(f"{latency_ms:.0f}ms")
        if model:
            short = model.split("/", 1)[-1] if "/" in model else model
            parts.append(short)
        if parts:
            self.mount(Static(f" · {' · '.join(parts)}", classes="msg-status"))
            self.scroll_end(animate=False)

    def clear_chat(self):
        """Clear all messages."""
        for child in list(self.children):
            child.remove()
        self._streaming_widget = None
        self._streaming_text = ""
