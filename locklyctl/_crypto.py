"""Cipher backend: pycryptodome when it's installed (desktop), otherwise the
pure-Python fallback in _purecrypto (phones, where pycryptodome has no
wheels). LOCKLYCTL_PURE_CRYPTO=1 forces the fallback, for testing."""
from __future__ import annotations

import os

if os.environ.get("LOCKLYCTL_PURE_CRYPTO") == "1":
    from ._purecrypto import AES, DES3, RSA
    BACKEND = "pure-python"
else:
    try:
        from Crypto.Cipher import AES, DES3
        from Crypto.PublicKey import RSA
        BACKEND = "pycryptodome"
    except ImportError:
        from ._purecrypto import AES, DES3, RSA
        BACKEND = "pure-python"

__all__ = ["AES", "DES3", "RSA", "BACKEND"]
