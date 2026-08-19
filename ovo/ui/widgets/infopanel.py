"""OVO Info Panel — right sidebar showing tokens, context, and task checklist."""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual.containers import Vertical, VerticalScroll

from ovo.agent import AgentState, AgentStatus


class TaskItem(Static):
    """A single task in the checklist."""
    pass


class OvoInfoPanel(Widget):
    """Right sidebar: tokens, context %, model status, task checklist.

    Takes 1/5 of the horizontal space (4:1 ratio with chat).
    """

    def __init__(self):
        super().__init__()
        self._tasks: list[tuple[str, bool]] = []  # (label, done)

    def compose(self) -> ComposeResult:
        with Vertical(id="info-panel-inner"):
            # Stats section
            yield Static("📊 Stats", id="info-header", classes="info-section-title")
            yield Static("Tokens: 0", id="info-tokens", classes="info-stat")
            yield Static("Context: 0%", id="info-context", classes="info-stat")
            yield Static("", id="info-model-status", classes="info-stat")

            # Divider
            yield Static("─" * 20, classes="info-divider")

            # Tasks section
            yield Static("📋 Tasks", id="info-tasks-header", classes="info-section-title")
            yield VerticalScroll(id="info-tasks-list")

    def update_state(self, state: AgentState, model_status_tag: str = ""):
        """Update stats from agent state."""
        try:
            # Tokens
            self.query_one("#info-tokens", Static).update(
                f"Tokens: {state.context_used:,}"
            )

            # Context percentage
            if state.context_total > 0:
                pct = min(100, int(state.context_used / state.context_total * 100))
                bar_len = 12
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                color = "#22c55e" if pct < 50 else "#f59e0b" if pct < 80 else "#ef4444"
                self.query_one("#info-context", Static).update(
                    f"Context: {pct}%\n[{color}]{bar}[/]"
                )
            else:
                self.query_one("#info-context", Static).update("Context: —")

            # Model status tag
            ms = self.query_one("#info-model-status", Static)
            if model_status_tag:
                ms.update(model_status_tag)
            elif state.status == AgentStatus.STREAMING:
                ms.update("[#00f5d4]● Streaming…[/]")
            elif state.connected:
                ms.update("[#22c55e]● Ready[/]")
            else:
                ms.update("[#ef4444]○ Disconnected[/]")
        except Exception:
            pass

    def set_tasks(self, tasks: list[tuple[str, bool]]):
        """Set the task checklist. tasks = [(label, done), ...]"""
        self._tasks = tasks
        try:
            container = self.query_one("#info-tasks-list", VerticalScroll)
            # Clear existing
            for child in list(container.children):
                child.remove()
            # Add new
            for label, done in tasks:
                icon = "[#22c55e]✓[/]" if done else "[#71717a]○[/]"
                style = "info-task-done" if done else "info-task-pending"
                container.mount(TaskItem(f"{icon} {label}", classes=f"info-task {style}"))
        except Exception:
            pass

    def add_task(self, label: str, done: bool = False):
        """Add a task to the checklist."""
        self._tasks.append((label, done))
        self.set_tasks(self._tasks)

    def mark_task_done(self, index: int):
        """Mark a task as done by index."""
        if 0 <= index < len(self._tasks):
            label, _ = self._tasks[index]
            self._tasks[index] = (label, True)
            self.set_tasks(self._tasks)
