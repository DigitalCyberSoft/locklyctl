"""The pure-Python cipher fallback (used by the phone apps) must match
pycryptodome byte for byte."""
import base64
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locklyctl import _purecrypto as pure
from locklyctl import protocol as P

Crypto = pytest.importorskip("Crypto.Cipher")
from Crypto.Cipher import AES, DES3          # noqa: E402
from Crypto.PublicKey import RSA             # noqa: E402

rng = random.Random(1234)


def _bytes(n):
    return bytes(rng.randrange(256) for _ in range(n))


def test_aes_known_answer():
    # FIPS-197 appendix C.1
    key = bytes(range(16))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = pure.AES.new(key, pure.AES.MODE_ECB).encrypt(pt)
    assert ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert pure.AES.new(key, pure.AES.MODE_ECB).decrypt(ct) == pt


def test_aes_matches_pycryptodome():
    for size in (16, 24, 32):
        for _ in range(20):
            key, data = _bytes(size), _bytes(16 * rng.randrange(1, 5))
            ref = AES.new(key, AES.MODE_ECB).encrypt(data)
            assert pure.AES.new(key, pure.AES.MODE_ECB).encrypt(data) == ref
            assert pure.AES.new(key, pure.AES.MODE_ECB).decrypt(ref) == data


def test_des3_matches_pycryptodome():
    for size in (16, 24):
        for _ in range(20):
            key = DES3.adjust_key_parity(_bytes(size))
            data = _bytes(8 * rng.randrange(1, 6))
            ref = DES3.new(key, DES3.MODE_ECB).encrypt(data)
            assert pure.DES3.new(key, pure.DES3.MODE_ECB).encrypt(data) == ref
            assert pure.DES3.new(key, pure.DES3.MODE_ECB).decrypt(ref) == data


def test_ecb_alignment():
    with pytest.raises(ValueError):
        pure.AES.new(bytes(16), pure.AES.MODE_ECB).encrypt(b"short")


def test_rsa_keys_match():
    for b64 in (P._PUB_KEY_DER_B64, P._PRIV_KEY_DER_B64):
        der = base64.b64decode(b64)
        ref, ours = RSA.import_key(der), pure.RSA.import_key(der)
        assert (ours.n, ours.e) == (ref.n, ref.e)
        if ref.has_private():
            assert ours.d == ref.d
    gen = RSA.generate(1024)
    for der in (gen.export_key("DER"), gen.export_key("DER", pkcs=8), gen.public_key().export_key("DER")):
        k = pure.RSA.import_key(der)
        assert (k.n, k.e) == (gen.n, gen.e)


def test_protocol_frames_under_fallback(monkeypatch):
    """A whole frame built with the fallback equals the pycryptodome frame."""
    lock = ("12345678", "aa0011223344556677889900", "123456")
    nonce = "00112233445566778899AABBCCDDEEFF"
    ref = P.build_unlock_cmd(*lock, nonce)
    monkeypatch.setattr(P, "AES", pure.AES)
    assert P.build_unlock_cmd(*lock, nonce) == ref
    key = DES3.adjust_key_parity(_bytes(24))
    monkeypatch.setattr(P, "DES3", pure.DES3)
    assert P.des3_decrypt(key, P.des3_encrypt(key, '{"a": "ü"}')) == '{"a": "ü"}'.encode()
