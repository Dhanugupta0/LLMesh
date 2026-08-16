"""Unified logging configuration module

Provides a structured, readable logging system with support for:
- Unified log format with clean color styling
- Request tracking (request_id)
- Module-level log level control
- Dual output to file and console
- Colored terminal output
- Log helper functions for simpler calls
"""
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Any
from contextvars import ContextVar
from datetime import datetime

from app.config.settings import settings

# Request context - used to pass request_id through the entire request lifecycle
_request_context: ContextVar[dict] = ContextVar("_request_context", default={})


class LogStyle:
    """Log style configuration"""

    # ANSI color codes
    COLORS = {
        # Log level colors
        "DEBUG": "\033[90m",      # dark gray
        "INFO": "\033[92m",       # bright green
        "WARNING": "\033[93m",    # bright yellow
        "ERROR": "\033[91m",      # bright red
        "CRITICAL": "\033[95m",   # bright purple
        # Element colors
        "time": "\033[90m",       # dark gray
        "module": "\033[36m",     # cyan
        "request_id": "\033[35m", # purple
        "location": "\033[33m",   # yellow
        "key": "\033[34m",        # blue
        "value": "\033[37m",      # white
        "success": "\033[92m",    # bright green
        "reset": "\033[0m",       # reset
        # Emphasis styles
        "bold": "\033[1m",
        "dim": "\033[2m",
    }

    # Log level icons (ASCII safe, good compatibility)
    ICONS = {
        "DEBUG": "··",
        "INFO": "→",
        "WARNING": "!",
        "ERROR": "×",
        "CRITICAL": "!!",
    }

    # Log level display width (for alignment)
    LEVEL_WIDTH = 7


class ColoredFormatter(logging.Formatter):
    """Clean colored log formatter"""

    def __init__(self, use_colors: bool = True, include_request_id: bool = True):
        """Initialize the formatter

        Args:
            use_colors: whether to use colored output
            include_request_id: whether to include the request ID
        """
        self.use_colors = use_colors
        self.include_request_id = include_request_id
        super().__init__()

    def _colorize(self, text: str, color_key: str) -> str:
        """Add color"""
        if not self.use_colors:
            return text
        color = LogStyle.COLORS.get(color_key, "")
        reset = LogStyle.COLORS["reset"]
        return f"{color}{text}{reset}"

    def _format_time(self, record: logging.LogRecord) -> str:
        """Format the time"""
        time_str = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        return self._colorize(time_str, "time")

    def _format_level(self, record: logging.LogRecord) -> str:
        """Format the log level"""
        level = record.levelname
        icon = LogStyle.ICONS.get(level, "·")
        # Align
        padded = f"{icon} {level}".ljust(LogStyle.LEVEL_WIDTH + 2)
        return self._colorize(padded, level)

    def _format_module(self, record: logging.LogRecord) -> str:
        """Format the module name"""
        if record.name == "root":
            return ""
        # Take the last part of the module name
        module = record.name.split(".")[-1]
        # Truncate overly long module names
        if len(module) > 12:
            module = module[:10] + ".."
        return self._colorize(f"[{module}]", "module")

    def _format_request_id(self) -> str:
        """Format the request ID"""
        if not self.include_request_id:
            return ""
        request_id = _request_context.get({}).get("request_id", "")
        if not request_id:
            return ""
        return self._colorize(f"[{request_id[:8]}]", "request_id")

    def _format_location(self, record: logging.LogRecord) -> str:
        """Format the location info (error level only)"""
        if record.levelno < logging.ERROR:
            return ""
        location = f"{record.filename}:{record.lineno}"
        return self._colorize(f"[{location}]", "location")

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record"""
        # Process parameters in the message
        message = record.getMessage()

        # Build the log line
        parts = [
            self._format_time(record),
            self._format_level(record),
        ]

        # Add the request ID
        request_id_part = self._format_request_id()
        if request_id_part:
            parts.append(request_id_part)

        # Add the module name
        module_part = self._format_module(record)
        if module_part:
            parts.append(module_part)

        # Add location info (error level)
        location_part = self._format_location(record)
        if location_part:
            parts.append(location_part)

        # Combine the prefix
        prefix = " ".join(parts)

        # Handle multi-line messages
        if "\n" in message:
            lines = message.split("\n")
            # First line displays normally
            result = f"{prefix} {lines[0]}"
            # Subsequent lines are indented for alignment
            indent = " " * (len(prefix) + 1)
            for line in lines[1:]:
                result += f"\n{indent}{line}"
            return result

        return f"{prefix} {message}"


class PlainFormatter(logging.Formatter):
    """Plain text formatter (for file output)"""

    def __init__(self, include_request_id: bool = True):
        self.include_request_id = include_request_id
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record"""
        # Timestamp
        time_str = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # Log level
        level = record.levelname.ljust(8)

        # Build the base parts
        parts = [f"{time_str} | {level}"]

        # Request ID
        if self.include_request_id:
            request_id = _request_context.get({}).get("request_id", "")
            if request_id:
                parts.append(f"| {request_id[:8]}")

        # Module name
        if record.name != "root":
            module = record.name.split(".")[-1]
            parts.append(f"| {module}")

        # Location info (error level)
        if record.levelno >= logging.ERROR:
            parts.append(f"| {record.filename}:{record.lineno}")

        # Message
        parts.append(f"| {record.getMessage()}")

        return " ".join(parts)


# ============================================
# Log helper functions
# ============================================

def _format_kv(key: str, value: Any) -> str:
    """Format a key-value pair"""
    if isinstance(value, float):
        value = f"{value:.2f}" if value < 1000 else f"{value:,.0f}"
    elif isinstance(value, int) and value > 1000:
        value = f"{value:,}"
    return f"{key}={value}"


def log_request(logger: logging.Logger, method: str, path: str, **kwargs):
    """Log a request

    Args:
        logger: Logger instance
        method: HTTP method
        path: request path
        **kwargs: additional info
    """
    parts = [f"{method} {path}"]
    if kwargs:
        parts.append("|")
        parts.append(" ".join(_format_kv(k, v) for k, v in kwargs.items()))
    logger.info(" ".join(parts))


def log_response(logger: logging.Logger, status: int, duration_ms: float, **kwargs):
    """Log a response

    Args:
        logger: Logger instance
        status: HTTP status code
        duration_ms: response time (milliseconds)
        **kwargs: additional info
    """
    parts = [f"← {status}"]
    parts.append(_format_kv("duration", f"{duration_ms:.1f}ms"))
    if kwargs:
        parts.append("|")
        parts.append(" ".join(_format_kv(k, v) for k, v in kwargs.items()))
    logger.info(" ".join(parts))


def log_forward(logger: logging.Logger, model: str, server: str, stream: bool = False):
    """Log a request forwarding

    Args:
        logger: Logger instance
        model: model name
        server: target server
        stream: whether it is streaming
    """
    mode = "stream" if stream else "sync"
    logger.info(f"→ {mode} | model={model} | server={server}")


def log_stream_complete(logger: logging.Logger, model: str, tokens: int = None, duration_ms: float = None):
    """Log a streaming response completion

    Args:
        logger: Logger instance
        model: model name
        tokens: token count
        duration_ms: response time
    """
    parts = [f"✓ stream | model={model}"]
    if tokens is not None:
        parts.append(f"| tokens={tokens}")
    if duration_ms is not None:
        parts.append(f"| duration={duration_ms:.0f}ms")
    logger.info(" ".join(parts))


def log_error(logger: logging.Logger, message: str, error: Exception = None, **kwargs):
    """Log an error

    Args:
        logger: Logger instance
        message: error message
        error: exception object
        **kwargs: additional context
    """
    parts = [message]
    if kwargs:
        parts.append("|")
        parts.append(" ".join(_format_kv(k, v) for k, v in kwargs.items()))
    if error:
        logger.error(" ".join(parts), exc_info=True)
    else:
        logger.error(" ".join(parts))


def log_circuit(logger: logging.Logger, event: str, server: str, **kwargs):
    """Log a circuit breaker event

    Args:
        logger: Logger instance
        event: event type (open/close/half_open)
        server: server identifier
        **kwargs: additional info
    """
    event_icons = {
        "open": "◉ OPEN",
        "close": "○ CLOSE",
        "half_open": "◐ HALF",
        "reset": "↺ RESET",
    }
    icon = event_icons.get(event, event)
    parts = [f"breaker {icon} | server={server}"]
    if kwargs:
        parts.append("|")
        parts.append(" ".join(_format_kv(k, v) for k, v in kwargs.items()))
    logger.warning(" ".join(parts))


# ============================================
# Log configuration
# ============================================

def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    include_request_id: bool = True,
) -> None:
    """Configure application logging

    Args:
        level: log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: log file path (defaults to app.log)
        include_request_id: whether to include the request ID in logs
    """
    # Clear existing handlers
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Set the log level
    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    # Log file path
    if log_file is None:
        log_file = Path(settings.BASE_DIR) / "logs" / "app.log"
    else:
        log_file = Path(log_file)

    # Ensure the log directory exists
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Whether to use colors - multiple detection methods
    # 1. NO_COLOR env var takes priority and disables
    # 2. FORCE_COLOR env var forces enabling
    # 3. Detect the terminal environment (isatty or TERM/TMUX vars)
    if os.getenv("NO_COLOR"):
        use_colors = False
    elif os.getenv("FORCE_COLOR") or os.getenv("CLICOLOR_FORCE"):
        use_colors = True
    elif sys.stdout.isatty():
        use_colors = True
    elif os.getenv("TERM") or os.getenv("TMUX"):
        # Enable colors in tmux or when TERM is set (handles pipe scenarios)
        use_colors = True
    else:
        use_colors = False

    # Create the formatters
    console_formatter = ColoredFormatter(use_colors=use_colors, include_request_id=include_request_id)
    file_formatter = PlainFormatter(include_request_id=include_request_id)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Set third-party library log levels (reduce noise)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name

    Args:
        name: logger name, usually __name__

    Returns:
        logging.Logger: the configured logger instance
    """
    return logging.getLogger(name)


def set_request_context(request_id: str, **kwargs) -> None:
    """Set the request context

    Args:
        request_id: unique request identifier
        **kwargs: other context info
    """
    _request_context.set({"request_id": request_id, **kwargs})


def get_request_context() -> dict:
    """Get the current request context

    Returns:
        dict: request context info
    """
    return _request_context.get({})


def clear_request_context() -> None:
    """Clear the request context"""
    _request_context.set({})