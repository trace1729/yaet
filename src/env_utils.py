from __future__ import annotations

from pathlib import Path


def project_root_from_file(file_path: str) -> Path:
    return Path(file_path).resolve().parent.parent


def resolve_dotenv_path(module_file: str, env_path: Path | None = None) -> Path | None:
    if env_path is not None:
        resolved = env_path.resolve()
        return resolved if resolved.exists() else None

    candidates: list[Path] = []
    candidates.append(Path(".env"))
    candidates.append(project_root_from_file(module_file) / ".env")
    candidates.append(Path.home() / ".env")

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def load_key_from_dotenv(key_name: str, module_file: str, env_path: Path | None = None) -> str | None:
    dotenv_path = resolve_dotenv_path(module_file, env_path)
    if dotenv_path is None:
        return None

    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != key_name:
            continue
        cleaned = value.strip().strip('"').strip("'")
        if cleaned:
            return cleaned
    return None
