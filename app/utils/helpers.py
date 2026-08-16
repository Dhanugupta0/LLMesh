import secrets
import re
import json

try:
    import aiofiles
except ImportError:
    aiofiles = None

from typing import Dict, Any, Union
from datetime import datetime
from app.config.settings import settings
from app.utils.logging_config import setup_logging, get_logger

# Initialize the logging configuration
setup_logging(level="INFO")
logger = get_logger(__name__)


async def load_json_file(filename: str) -> Dict:
    """Asynchronously load a JSON file

    Args:
        filename: JSON file path

    Returns:
        Dict: the loaded JSON data
    """
    if aiofiles is None:
        raise RuntimeError("aiofiles package is required for load_json_file")
    try:
        async with aiofiles.open(filename, "r") as f:
            return json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error loading {filename}: {str(e)}")
        return {}


async def save_json_file(data: Dict, filename: str) -> None:
    """Asynchronously save a JSON file

    Args:
        data: the data to save
        filename: the file path to save to
    """
    if aiofiles is None:
        raise RuntimeError("aiofiles package is required for save_json_file")
    try:
        async with aiofiles.open(filename, "w") as f:
            await f.write(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Error saving {filename}: {str(e)}")
        raise


def generate_token(prefix: str = "xh", length: int = 20) -> str:
    """Generate a random API key

    Uses secrets.token_urlsafe to generate a URL-safe random token,
    which is more efficient than character-by-character secrets.choice
    and has better entropy density.

    Args:
        prefix: key prefix
        length: key length (in bytes, slightly longer after base64 encoding)

    Returns:
        str: the generated API key
    """
    return f"{prefix}-{secrets.token_urlsafe(length)}"


def get_current_time() -> str:
    """Get the current Beijing time

    Returns:
        str: formatted time string
    """
    return datetime.now(settings.TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def log_api_usage(api_key: str, usage_info: Dict) -> None:
    """Log API usage

    Args:
        api_key: API key
        usage_info: usage information
    """
    remaining = usage_info.get('limit', 0) - usage_info.get('usage', 0)
    logger.info(
        f"API key={api_key[-6:]} | "
        f"remaining={remaining} | "
        f"requests={usage_info.get('reqs', 0)}"
    )


def sanitize_anthropic_system_text(text: str) -> str:
    """Clean cache-busting content from Anthropic system text

    Removes content that invalidates caching:
    - x-anthropic-billing-header: ... (contains a dynamically changing cch value)
    - Normalizes excess consecutive spaces

    Args:
        text: the original system text

    Returns:
        str: the cleaned text
    """
    if not text:
        return text

    # Remove the whole x-anthropic-billing-header line (may end with a newline or be in the middle of the text)
    # Pattern: x-anthropic-billing-header: ... (up to a semicolon or newline)
    text = re.sub(
        r'x-anthropic-billing-header:\s*[^;\n]+;?\s*',
        '',
        text
    )

    # Normalize consecutive spaces to a single space (preserve newlines)
    text = re.sub(r'[ \t]+', ' ', text)

    # Remove leading/trailing spaces on each line
    lines = text.split('\n')
    lines = [line.strip() for line in lines]
    text = '\n'.join(lines)

    # Remove excess blank lines (compress more than 2 consecutive blank lines into 1)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Remove leading/trailing whitespace
    text = text.strip()

    return text


def sanitize_anthropic_request(req_data: Dict[str, Any]) -> Dict[str, Any]:
    """Clean cache-busting content from an Anthropic request

    Handles the system field (may be a string or an array of objects)

    Args:
        req_data: the original request data

    Returns:
        Dict: the cleaned request data
    """
    if not req_data:
        return req_data

    # Handle the top-level system field
    if 'system' in req_data:
        system = req_data['system']

        if isinstance(system, str):
            # system is a string, clean it directly
            req_data['system'] = sanitize_anthropic_system_text(system)

        elif isinstance(system, list):
            # system is an array of objects, clean the text field of each object
            for item in system:
                if isinstance(item, dict) and 'text' in item:
                    item['text'] = sanitize_anthropic_system_text(item['text'])

    return req_data


# Unicode ranges for CJK and fullwidth characters (used for token estimation)
_CJK_RANGES = [
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0xFF01, 0xFF60),   # Fullwidth Forms (punctuation)
    (0xFFE0, 0xFFE6),   # Fullwidth Signs
]


def estimate_tokens_fallback(text: str) -> int:
    """Fallback token estimation (used when tiktoken is unavailable)

    Estimates token count by character type, more accurate than a simple
    uniform ratio:
    - CJK/Japanese/Korean characters: about 1.5 chars/token
    - English/Latin/digits: about 4 chars/token
    - Whitespace characters are not counted

    Args:
        text: the input text

    Returns:
        the estimated token count (minimum 1)
    """
    if not text:
        return 0

    import unicodedata

    wide_chars = 0
    narrow_chars = 0

    for ch in text:
        if ch.isspace():
            continue
        cp = ord(ch)
        # Check whether it is in the CJK/fullwidth range
        is_wide = any(lo <= cp <= hi for lo, hi in _CJK_RANGES)
        if is_wide:
            wide_chars += 1
        elif unicodedata.category(ch).startswith(('L', 'N')):
            # Letter or digit (narrow character)
            narrow_chars += 1
        else:
            # Punctuation, symbols, etc. -> treat as narrow characters
            narrow_chars += 1

    # CJK characters have high token density (~1.5 chars/token), Latin characters low density (~4 chars/token)
    estimated = int(wide_chars / 1.5 + narrow_chars / 4.0)
    return max(1, estimated)