from copy import deepcopy
from dataclasses import dataclass, field, fields, replace

from mpp import Challenge, ChallengeEcho

from ._wire import (
    MppCodecError,
    WireObject,
    decode,
    object_value,
    render_challenge_header,
    string,
    validate_challenge,
)


@dataclass(frozen=True)
class _WireChallenge(Challenge):
    _wire: WireObject = field(default_factory=dict, repr=False, compare=False)

    def to_echo(self) -> ChallengeEcho:
        if "opaque" not in self._wire:
            return super().to_echo()
        opaque = string(self._wire["opaque"])
        if decode(opaque) != self.opaque:
            raise MppCodecError("pympp opaque data differs from its encoded value")
        # pympp otherwise re-encodes opaque. Keep the original challenge-bound bytes.
        return replace(super().to_echo(), opaque=opaque)


def to_pympp_challenge(value: WireObject) -> Challenge:
    # Parse only at this boundary; the wire object retains extensions that pympp cannot represent.
    validate_challenge(value)
    parsed = Challenge.from_www_authenticate(render_challenge_header(value))
    return _WireChallenge(
        **{item.name: getattr(parsed, item.name) for item in fields(parsed)}, _wire=deepcopy(value)
    )


def from_pympp_challenge(value: Challenge) -> WireObject:
    # request_b64 is signed input. Re-encoding the decoded request can invalidate its binding.
    result: WireObject = {
        **(deepcopy(value._wire) if isinstance(value, _WireChallenge) else {}),
        "id": value.id,
        "realm": value.realm,
        "method": value.method,
        "intent": value.intent,
        "request": value.request_b64,
    }
    for name in ("expires", "description", "digest", "header"):
        item = getattr(value, name)
        if item is not None:
            result[name] = item
    if value.opaque is not None:
        # Converted challenges retain raw opaque bytes; native pympp challenges expose its echo.
        result["opaque"] = value.to_echo().opaque
    validate_challenge(result)
    if object_value(decode(value.request_b64)) != value.request:
        raise ValueError("pympp challenge request differs from its encoded request")
    return deepcopy(result)
