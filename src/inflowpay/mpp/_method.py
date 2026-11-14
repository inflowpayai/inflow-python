from mpp.methods import Method
from pydantic import JsonValue, RootModel


class WireModel(RootModel[dict[str, JsonValue]]):
    """Preserve platform wire amounts and extension fields without token-unit conversion."""


def method_definition(name: str, intent: str) -> Method[WireModel, WireModel]:
    return Method(name=name, intent=intent, request_model=WireModel, payload_model=WireModel)
