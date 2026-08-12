"""Three-database connection layer for bill_service."""
from .connection_manager import (
    ROLE_MAIN,
    ROLE_SECONDARY,
    ROLE_LOCAL,
    get_client,
    get_database,
    ping,
    close_all,
)

__all__ = [
    "ROLE_MAIN",
    "ROLE_SECONDARY",
    "ROLE_LOCAL",
    "get_client",
    "get_database",
    "ping",
    "close_all",
]
