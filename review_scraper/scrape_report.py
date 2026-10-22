"""Results and page checkpoint hooks shared by the store adapters."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


PageCallback = Callable[[List[Dict[str, Any]], Dict[str, Any]], None]


@dataclass
class ScrapeResult:
    """A collection outcome, never a guarantee of complete historical coverage."""

    reviews: List[Dict[str, Any]]
    status: str
    stop_reason: str
    pages_fetched: int = 0
    checkpoint: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
