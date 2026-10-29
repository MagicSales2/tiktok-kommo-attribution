from __future__ import annotations

import re
import secrets
from typing import Any

# Sin caracteres ambiguos (0/O, 1/I/L) para que el cliente no se equivoque.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REF_RE = re.compile(r"\bREF-[A-Z2-9]{8}\b", re.IGNORECASE)


def generate_ref() -> str:
    return "REF-" + "".join(secrets.choice(ALPHABET) for _ in range(8))


def normalize_ref(value: str | None) -> str | None:
    if not value:
        return None
    match = REF_RE.search(str(value))
    return match.group(0).upper() if match else None


def extract_ref(*values: Any) -> str | None:
    """Busca un código REF-XXXXXXXX en textos, dicts o listas de Kommo."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            found = extract_ref(*value.values())
            if found:
                return found
            continue
        if isinstance(value, (list, tuple, set)):
            found = extract_ref(*value)
            if found:
                return found
            continue
        found = normalize_ref(str(value))
        if found:
            return found
    return None
