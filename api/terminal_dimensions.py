"""Terminal resize dimensions shared by transport adapters."""


def clamp_dim(value, default: int) -> int:
    """A resize frame's rows/cols as a sane int: a missing, non-numeric or
    non-positive value falls back to ``default``, a huge one caps at 1000."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if n <= 0:
        return default
    return min(n, 1000)

