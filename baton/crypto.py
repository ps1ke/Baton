"""Keys and signatures: Ed25519 for every event and every peer connection.

Uses the `cryptography` package when it is installed (fast), and otherwise a pure-Python implementation of RFC 8032
(slow but correct; checked against the RFC's test vectors), so baton keeps working with the standard library alone.
Verification results are cached by (event id, signature), since an event's id is the hash of its content.

A private key never leaves the machine: it lives in the workspace's `keys/` folder (mode 0600), one file per actor.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

try:   # the fast path
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization
    FAST = True
except ImportError:   # pragma: no cover - exercised where cryptography is missing
    FAST = False

# -- RFC 8032 reference implementation (section 6) ------------------------------------------------------------------
_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zinv = _inv(P[2])
    x, y = P[0] * zinv % _p, P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _modq(b):
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _q


def _pure_public(secret: bytes) -> bytes:
    return _compress(_mul(_expand(secret)[0], _G))


def _pure_sign(secret: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = _modq(prefix + msg)
    Rs = _compress(_mul(r, _G))
    s = (r + _modq(Rs + A + msg) * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def _pure_verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if len(public) != 32 or len(sig) != 64:
        return False
    A, R = _decompress(public), _decompress(sig[:32])
    if not A or not R:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _q:
        return False
    return _equal(_mul(s, _G), _add(R, _mul(_modq(sig[:32] + public + msg), A)))


# -- the interface ---------------------------------------------------------------------------------------------------
def new_secret() -> bytes:
    return secrets.token_bytes(32)


def public_key(secret: bytes) -> str:
    if FAST:
        k = Ed25519PrivateKey.from_private_bytes(secret).public_key()
        return k.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return _pure_public(secret).hex()


def sign(secret: bytes, msg: bytes) -> str:
    if FAST:
        return Ed25519PrivateKey.from_private_bytes(secret).sign(msg).hex()
    return _pure_sign(secret, msg).hex()


_VERIFIED: set = set()


def verify(public_hex: str, msg: bytes, sig_hex: str) -> bool:
    try:
        public, sig = bytes.fromhex(public_hex), bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        return False
    if len(public) != 32 or len(sig) != 64:
        return False
    key = hashlib.sha256(public + sig + msg).digest()
    if key in _VERIFIED:
        return True
    if FAST:
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(sig, msg)
            ok = True
        except (InvalidSignature, ValueError):
            ok = False
    else:
        ok = _pure_verify(public, msg, sig)
    if ok:
        _VERIFIED.add(key)
    return ok


def fingerprint(public_hex: str) -> str:
    """A short, readable form of a key for people to compare: 4 groups of 4 hex digits of its SHA-256."""
    h = hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


class Keyring:
    """This machine's private keys, one per actor name, in <workspace>/keys (created 0700, files 0600)."""

    def __init__(self, folder: Path):
        self.dir = Path(folder)

    def _file(self, actor: str) -> Path:
        safe = hashlib.sha256(actor.encode()).hexdigest()[:24]
        return self.dir / f"{safe}.key"

    def has(self, actor: str) -> bool:
        return self._file(actor).exists()

    def secret(self, actor: str, create: bool = True) -> bytes:
        f = self._file(actor)
        if f.exists():
            data = f.read_text().split()
            return bytes.fromhex(data[1])
        if not create:
            raise KeyError(f"this machine holds no key for {actor}")
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        secret = new_secret()
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(f"{actor} {secret.hex()}\n")
        return secret

    def public(self, actor: str, create: bool = True) -> str:
        return public_key(self.secret(actor, create))

    def sign(self, actor: str, msg: bytes) -> tuple[str, str]:
        """(public key hex, signature hex)."""
        s = self.secret(actor)
        return public_key(s), sign(s, msg)


def default_keyring() -> Keyring:
    from .workspace import default_dir
    return Keyring(default_dir() / "keys")
