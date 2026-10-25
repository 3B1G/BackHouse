"""The owner's signing key: the one secret in the system.

owner-init generates an Ed25519 key pair. The private key is sealed with AES-GCM under a key derived (scrypt) from the
owner's passphrase; only the sealed blob, the salt and the PUBLIC key are stored. Approvals are signed with the private
key, so anyone holding just the database (and the public key in it) can check every approval, and every price change
that rests on one, without the passphrase. A process that can write the database but doesn't know the passphrase
can't produce a signature that checks out.

What it doesn't cover: someone who swaps in a whole different database with their own owner key. The seal
(verify.py) pins the owner's public key and the chain heads to catch that."""
import hashlib
import json
import os

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import append, now, transaction

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1           # ~32 MB and ~0.1 s per guess: slows offline guessing
MIN_PASSPHRASE = 10
SEAL_AAD = b"cost_price owner key v2"


class OwnerError(PermissionError):
    pass


def _kek(passphrase: str, salt: bytes, n: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, maxmem=128 * 1024 * 1024,
                          dklen=32)


class OwnerKey:
    """The unlocked private key. Lives in memory only for the command that needed it."""

    def __init__(self, private: Ed25519PrivateKey):
        self._private = private
        self.public_hex = private.public_key().public_bytes_raw().hex()

    def sign(self, payload: bytes) -> str:
        return self._private.sign(payload).hex()

    def __repr__(self):                                  # never print key material
        return f"OwnerKey(public={self.public_hex[:16]}...)"


def init_owner(con, passphrase: str) -> str:
    """One-time setup (the `owner` role). Returns the public key's fingerprint. A second owner is refused."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise OwnerError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    private = Ed25519PrivateKey.generate()
    salt, nonce = os.urandom(16), os.urandom(12)
    sealed = nonce + AESGCM(_kek(passphrase, salt, SCRYPT_N)).encrypt(nonce, private.private_bytes_raw(), SEAL_AAD)
    public_hex = private.public_key().public_bytes_raw().hex()
    with transaction(con):
        append(con, "owner", dict(public_key_hex=public_hex, sealed_key_hex=sealed.hex(), salt_hex=salt.hex(),
                                  scrypt_n=SCRYPT_N, created_at=now()))
    return fingerprint(public_hex)


def unlock(con, passphrase: str) -> OwnerKey:
    """The owner key, if the passphrase is right."""
    row = con.execute("SELECT public_key_hex, sealed_key_hex, salt_hex, scrypt_n FROM owner ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise OwnerError("no owner set up: run `cost-price owner-init`")
    sealed = bytes.fromhex(row["sealed_key_hex"])
    try:
        raw = AESGCM(_kek(passphrase, bytes.fromhex(row["salt_hex"]), row["scrypt_n"])).decrypt(sealed[:12], sealed[12:],
                                                                                                SEAL_AAD)
    except InvalidTag:
        raise OwnerError("wrong passphrase") from None
    key = OwnerKey(Ed25519PrivateKey.from_private_bytes(raw))
    if key.public_hex != row["public_key_hex"]:
        raise OwnerError("the sealed key doesn't match the stored public key (the owner row was tampered with)")
    return key


def public_key(con) -> str | None:
    row = con.execute("SELECT public_key_hex FROM owner ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def fingerprint(public_hex: str) -> str:
    """Short form to read out loud or write down: compare it with what owner-init printed."""
    h = hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:20]
    return "-".join(h[i:i + 4] for i in range(0, 20, 4))


def verify_signature(public_hex: str | None, payload: bytes, signature_hex: str) -> bool:
    if not public_hex:
        return False
    if not isinstance(public_hex, str) or not isinstance(signature_hex, str):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def approval_payload(row: dict) -> bytes:
    """What an approval signature covers: every field the owner decided on, plus the row's own position in the
    approval chain (id and prev_hash), so a copy of an old approval re-appended later doesn't verify."""
    fields = ("id", "prev_hash", "suggestion_id", "subject_sha256", "decision", "decided_by", "decided_at", "note")
    return ("cost_price approval v2\n" + canonical({k: row.get(k) for k in fields})).encode("utf-8")
