import math
import re
from copy import deepcopy

from ._wire import MppCodecError, WireObject, object_value, string, timestamp

_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_ADDRESS = r"0x[0-9a-fA-F]{40}"
_MEMO = r"0x[0-9a-fA-F]{64}"
_INTEGER = r"(?:0|[1-9][0-9]*)"


def _match(value: object, pattern: str) -> str:
    result = string(value)
    if not re.fullmatch(pattern, result):
        raise MppCodecError("Invalid payment field format")
    return result


def _optional_strings(data: WireObject, fields: tuple[str, ...], pattern: str = r"[\s\S]+") -> None:
    for field in fields:
        if field in data:
            _match(data[field], pattern)


def validate_request(method: str, intent: str, value: object) -> WireObject:
    request = object_value(value)
    if method == "card" and intent == "charge":
        return _card_request(request)
    if method == "inflow" and intent in ("charge", "subscription"):
        amount = _match(request.get("amount"), r"-?[0-9]+(?:\.[0-9]+)?")
        string(request.get("currency"))
        _optional_strings(request, ("recipient",), _UUID)
        if "methodDetails" in request:
            details = object_value(request["methodDetails"])
            if "rail" in details and details["rail"] not in ("balance", "instrument"):
                raise MppCodecError("Unsupported InFlow rail")
            _optional_strings(details, ("instrumentId",), _UUID)
        if intent == "subscription":
            if amount.startswith("-") or not re.search("[1-9]", amount):
                raise MppCodecError("Subscription amount must be positive")
            unit, count = request.get("periodUnit"), request.get("periodCount")
            if unit not in ("minute", "hour", "day", "week", "month", "quarter", "year"):
                raise MppCodecError("Unsupported subscription period")
            if (
                isinstance(count, bool)
                or not isinstance(count, (int, float))
                or not 1 <= count <= 9007199254740991
                or count % 1 != 0
            ):
                raise MppCodecError("periodCount must be a positive safe integer")
            if unit == "minute" and count < 5:
                raise MppCodecError("Minute subscriptions require periodCount of at least five")
            timestamp(request.get("subscriptionExpires"))
            if "externalId" in request:
                external = string(request["externalId"])
                if not external.strip() or len(external.encode("utf-16-le")) // 2 > 128:
                    raise MppCodecError("externalId must contain 1 to 128 characters")
    elif method == "tempo" and intent == "charge":
        _match(request.get("amount"), _INTEGER)
        _optional_strings(request, ("currency", "recipient"), _ADDRESS)
        _optional_strings(request, ("description", "externalId"))
        if "methodDetails" in request:
            details = object_value(request["methodDetails"])
            if "chainId" in details:
                chain = details["chainId"]
                if (
                    isinstance(chain, bool)
                    or not isinstance(chain, (int, float))
                    or (isinstance(chain, float) and not math.isfinite(chain))
                ):
                    raise MppCodecError("chainId must be a finite number")
            if "feePayer" in details and type(details["feePayer"]) is not bool:
                raise MppCodecError("feePayer must be a boolean")
            _optional_strings(details, ("memo",), _MEMO)
            if "supportedModes" in details:
                modes = details["supportedModes"]
                if not isinstance(modes, list) or any(
                    mode not in ("pull", "push") for mode in modes
                ):
                    raise MppCodecError("Unsupported Tempo submission mode")
            if "splits" in details:
                splits = details["splits"]
                if not isinstance(splits, list):
                    raise MppCodecError("splits must be an array")
                for value in splits:
                    split = object_value(value)
                    _match(split.get("amount"), _INTEGER)
                    _match(split.get("recipient"), _ADDRESS)
                    _optional_strings(split, ("memo",), _MEMO)
    else:
        raise MppCodecError("Unsupported payment method or intent")
    return deepcopy(request)


def _card_request(request: WireObject) -> WireObject:
    amount = _match(request.get("amount"), r"[1-9][0-9]{0,7}")
    if int(amount) < 50 or request.get("currency") != "usd":
        raise MppCodecError("CARD requires USD and at least 50 cents")
    details = object_value(request.get("methodDetails"))
    networks = details.get("acceptedNetworks")
    if not isinstance(networks, list) or not networks or any(item != "visa" for item in networks):
        raise MppCodecError("CARD requires the Visa network")
    recipient, merchant = string(request.get("recipient")), string(details.get("merchantName"))
    if (
        len(recipient.encode("utf-16-le")) // 2 > 255
        or len(merchant.encode("utf-16-le")) // 2 > 255
    ):
        raise MppCodecError("CARD recipient and merchant name allow at most 255 characters")
    key = object_value(details.get("encryptionJwk"))
    if key.get("kty") != "RSA" or key.get("alg") != "RSA-OAEP-256" or key.get("use") != "enc":
        raise MppCodecError("CARD requires an RSA-OAEP-256 public encryption key")
    normalized: WireObject = {
        "acceptedNetworks": deepcopy(networks),
        "merchantName": merchant,
        "encryptionJwk": {
            "kty": "RSA",
            "alg": "RSA-OAEP-256",
            "use": "enc",
            "kid": string(key.get("kid")),
            "n": _match(key.get("n"), r"[A-Za-z0-9_-]+"),
            "e": _match(key.get("e"), r"[A-Za-z0-9_-]+"),
        },
    }
    if "billingRequired" in details:
        if type(details["billingRequired"]) is not bool:
            raise MppCodecError("billingRequired must be a boolean")
        normalized["billingRequired"] = details["billingRequired"]
    result: WireObject = {
        "amount": amount,
        "currency": "usd",
        "recipient": recipient,
        "methodDetails": normalized,
    }
    for field in ("externalId", "description"):
        if field in request:
            value = request[field]
            if not isinstance(value, str) or (
                field == "externalId" and len(value.encode("utf-16-le")) // 2 > 255
            ):
                raise MppCodecError(f"Invalid CARD {field}")
            result[field] = value
    return result


def validate_payload(method: str, value: object) -> WireObject:
    payload = object_value(value)
    if method == "card":
        encrypted = string(payload.get("encryptedPayload"))
        if len(encrypted.encode("utf-16-le")) // 2 > 16384 or payload.get("network") != "visa":
            raise MppCodecError("Invalid CARD encrypted payload or network")
        _match(payload.get("panLastFour"), r"[0-9]{4}")
        _match(payload.get("panExpirationMonth"), r"(?:0[1-9]|1[0-2])")
        _match(payload.get("panExpirationYear"), r"[0-9]{4}")
        for field in ("cardholderFullName", "paymentAccountReference"):
            if field in payload and not isinstance(payload[field], str):
                raise MppCodecError(f"Invalid CARD {field}")
        if "billingAddress" in payload:
            address = object_value(payload["billingAddress"])
            for field in ("line1", "line2", "city", "state", "zip", "countryCode"):
                if field in address and not isinstance(address[field], str):
                    raise MppCodecError(f"Invalid CARD billing {field}")
    elif method == "tempo":
        _optional_strings(payload, ("hash", "signature"), r"0x[0-9a-fA-F]+")
        _optional_strings(payload, ("transactionId",))
        kind = payload.get("type")
        if kind not in ("hash", "transaction", "proof"):
            raise MppCodecError("Unsupported Tempo credential type")
        string(payload.get("hash" if kind == "hash" else "signature"))
    elif method != "inflow":
        raise MppCodecError("Unsupported payment method")
    return deepcopy(payload)
