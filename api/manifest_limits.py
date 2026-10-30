"""The manifest's explicit positive numeric resource limits."""


def manifest_rlimit(manifest: dict, key: str, default: int | None = None) -> int | None:
    """Read one cap; its consumer owns whether an omitted cap has a default."""
    block = manifest.get("rlimits") if isinstance(manifest.get("rlimits"), dict) else {}
    raw = block.get(key) if block else None
    return int(raw) if isinstance(raw, (int, float)) and raw > 0 else default
