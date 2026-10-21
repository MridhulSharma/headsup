"""Ed25519 signing and verification over an advisory's canonical JSON.

What is signed is `Advisory.signing_bytes()`: the whole GeoJSON Feature, keys
sorted, with `properties.signature` removed. So the signature covers the
geometry, the action, the validity window, the evidence and the issuer
together - nobody can widen a published polygon or stretch a window without
breaking it.

    signer = Signer.from_env()               # HU_SIGNING_KEY, base64 seed
    signed = signer.sign(advisory)
    verify(signed, verify_key=signer.verify_key_b64)   # True

`hu compile` with no key in the environment falls back to `Signer.dev()`,
which uses the published seed below and says so on stderr. That keeps the
command runnable on a fresh clone; it is not a secret, and an advisory signed
with it proves nothing about who issued it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.signing import SigningKey, VerifyKey

from headsup.contracts import Advisory

#: Base64 Ed25519 seed (32 bytes) the compiler signs with.
ENV_SIGNING_KEY = "HU_SIGNING_KEY"

#: Base64 Ed25519 public key a verifier checks against. Optional: a process
#: holding the signing key derives the public key from it.
ENV_VERIFY_KEY = "HU_VERIFY_KEY"

SEED_BYTES = 32
KEY_BYTES = 32
SIGNATURE_BYTES = 64

#: The development seed. Published on purpose: `hu compile` and the tests must
#: run on a fresh clone with no key material, and anything signed with a seed
#: printed in the source is self-evidently not authenticated.
DEV_SEED = b"headsup-development-signing-key!"

DEV_WARNING = (
    f"no {ENV_SIGNING_KEY} in the environment: signing with the published development key. "
    "Advisories signed this way are not authenticated."
)


class SigningError(RuntimeError):
    """A key is missing or malformed, or an advisory could not be signed."""


def _b64decode(value: str, *, what: str, expect: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SigningError(f"{what} is not valid base64: {exc}") from exc
    if expect is not None and len(raw) != expect:
        raise SigningError(f"{what} decodes to {len(raw)} bytes, expected {expect}")
    return raw


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def fingerprint(key: VerifyKey) -> str:
    """Short, stable id for a public key: the first 8 hex of its SHA-256.

    The advisory contract has no key-id field, so the fingerprint travels in
    the audit record instead. It is how a reader of the log knows which key
    was expected to verify a given advisory.
    """
    digest = hashlib.sha256(bytes(key)).hexdigest()
    return f"ed25519:{digest[:8]}"


class Signer:
    """One Ed25519 key pair, used to sign advisories."""

    def __init__(self, key: SigningKey, *, label: str = "env") -> None:
        self._key = key
        #: Where this key came from, for the audit record: `env`, `dev` or
        #: `generated`.
        self.label = label

    # --- construction -------------------------------------------------------

    @classmethod
    def generate(cls, *, label: str = "generated") -> Signer:
        """A fresh random key pair."""
        return cls(SigningKey.generate(), label=label)

    @classmethod
    def from_seed_b64(cls, seed_b64: str, *, label: str = "env") -> Signer:
        """A signer from a base64 32-byte Ed25519 seed."""
        seed = _b64decode(seed_b64, what=ENV_SIGNING_KEY, expect=SEED_BYTES)
        try:
            return cls(SigningKey(seed), label=label)
        except CryptoError as exc:  # pragma: no cover - the size is checked above
            raise SigningError(f"{ENV_SIGNING_KEY} is not a usable Ed25519 seed: {exc}") from exc

    @classmethod
    def dev(cls) -> Signer:
        """The published development key. Never for anything real."""
        return cls(SigningKey(DEV_SEED), label="dev")

    @classmethod
    def from_env(cls, *, allow_dev_key: bool = False) -> Signer:
        """The signer named by `HU_SIGNING_KEY`.

        With `allow_dev_key`, a missing key falls back to `Signer.dev()`
        instead of raising; the caller is expected to warn.
        """
        seed_b64 = os.environ.get(ENV_SIGNING_KEY, "").strip()
        if seed_b64:
            return cls.from_seed_b64(seed_b64)
        if allow_dev_key:
            return cls.dev()
        raise SigningError(
            f"{ENV_SIGNING_KEY} is not set. Generate a seed with "
            "`hu keygen`, put it in .env, and never commit it."
        )

    # --- keys ---------------------------------------------------------------

    @property
    def verify_key(self) -> VerifyKey:
        return self._key.verify_key

    @property
    def verify_key_b64(self) -> str:
        """The public key, base64. Safe to publish; readers need it."""
        return _b64encode(bytes(self._key.verify_key))

    @property
    def seed_b64(self) -> str:
        """The private seed, base64. Never log this."""
        return _b64encode(bytes(self._key))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self._key.verify_key)

    @property
    def is_dev_key(self) -> bool:
        return bytes(self._key) == DEV_SEED

    # --- signing ------------------------------------------------------------

    def sign_bytes(self, payload: bytes) -> str:
        """Base64 Ed25519 signature over arbitrary bytes."""
        return _b64encode(self._key.sign(payload).signature)

    def sign(self, advisory: Advisory) -> Advisory:
        """A copy of `advisory` carrying its signature.

        The input is not mutated: the advisory that comes back is the one to
        publish, and the one that went in stays as it was.
        """
        unsigned = advisory.model_copy(deep=True)
        unsigned.properties.signature = None
        signature = self.sign_bytes(unsigned.signing_bytes())
        signed = unsigned.model_copy(deep=True)
        signed.properties.signature = signature
        return signed

    def __repr__(self) -> str:
        return f"Signer({self.fingerprint}, label={self.label!r})"


def resolve_verify_key(verify_key: str | VerifyKey | None = None) -> VerifyKey:
    """The public key a verifier should use, in order of preference.

    An explicit key, then `HU_VERIFY_KEY`, then the public half of
    `HU_SIGNING_KEY`, then the development key. That last step is what lets
    `hu verify` check what `hu compile` just printed on a fresh clone.
    """
    if isinstance(verify_key, VerifyKey):
        return verify_key
    if verify_key:
        return VerifyKey(_b64decode(verify_key, what="verify key", expect=KEY_BYTES))
    from_env = os.environ.get(ENV_VERIFY_KEY, "").strip()
    if from_env:
        return VerifyKey(_b64decode(from_env, what=ENV_VERIFY_KEY, expect=KEY_BYTES))
    seed_b64 = os.environ.get(ENV_SIGNING_KEY, "").strip()
    if seed_b64:
        return Signer.from_seed_b64(seed_b64).verify_key
    return Signer.dev().verify_key


def is_dev_key(key: str | VerifyKey | None = None) -> bool:
    """True if `key` resolves to the published development key pair.

    `hu verify` uses this to refuse a development signature unless asked to
    accept one: an advisory signed with a seed printed in the source proves
    nothing about who issued it, and a run that quietly accepted it would be
    reporting a guarantee it does not have.
    """
    try:
        resolved = resolve_verify_key(key)
    except SigningError:
        return False
    return bytes(resolved) == bytes(Signer.dev().verify_key)


def verify(advisory: Advisory, *, verify_key: str | VerifyKey | None = None) -> bool:
    """True if this advisory carries a good signature for `verify_key`.

    False, never an exception, for every way an advisory can fail to verify:
    no signature, a signature that is not base64 or not the right length, a
    key that cannot be read, or a good signature over different bytes.
    """
    signature = advisory.properties.signature
    if not signature:
        return False
    try:
        raw = _b64decode(signature, what="signature", expect=SIGNATURE_BYTES)
        key = resolve_verify_key(verify_key)
    except SigningError:
        return False
    try:
        key.verify(advisory.signing_bytes(), raw)
    except (BadSignatureError, CryptoError, ValueError):
        return False
    return True
