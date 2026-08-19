"""OVO authentication — API key management."""

from ovo.config import OvoConfig, OVO_DIR


def mask_key(key: str) -> str:
    """Mask an API key for display (show first 4 + last 4)."""
    if len(key) <= 10:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def store_api_key(config: OvoConfig, key: str):
    """Store the API key in config."""
    config.api_key = key
    config.save()


def clear_credentials(config: OvoConfig):
    """Remove stored credentials (logout)."""
    config.api_key = ""
    config.save()
