"""Encrypt host-provided secrets (room LLM keys) for at-rest persistence.

The keyring file lives outside version control. Losing it makes previously
stored secrets undecryptable, which the room registry treats as a room that
must be given a fresh key; secrets are never silently replaced by the
server's own credentials.
"""

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.config import PROJECT_ROOT, settings

__all__ = ["SecretDecryptionError", "decrypt_text", "encrypt_text"]


class SecretDecryptionError(ValueError):
    """Raised when stored secret material cannot be decrypted."""


def _key_path() -> Path:
    """Resolve the configured keyring path against the project root."""
    path = Path(settings.server.keyring_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_or_create_key() -> bytes:
    """Read the server keyring, generating it on first use with owner-only mode."""
    path = _key_path()
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    """Build a Fernet instance from the current keyring file."""
    try:
        return Fernet(_load_or_create_key())
    except (OSError, ValueError) as exc:
        raise SecretDecryptionError("Server keyring is unavailable or invalid.") from exc


def encrypt_text(value: str) -> str:
    """Encrypt one secret string; raises SecretDecryptionError if the keyring is unusable."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    """Decrypt one stored secret string; raises SecretDecryptionError on any failure."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise SecretDecryptionError("Stored secret cannot be decrypted.") from exc
