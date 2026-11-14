from mpp import Challenge, Credential, Receipt
from mpp.runtime import ClientMethod, CredentialParams
from mpp.server import ServerMethod, Validation
from mpp.server.method import ValidateContext, VerifyContext

from inflowpay.mpp import WireObject, decode_credential, to_pympp_challenge
from inflowpay.mpp._wire import object_value


async def create_credential(method: ClientMethod, challenge: Challenge) -> Credential:
    return Credential.from_authorization(
        await method.create_credential(CredentialParams(challenge=challenge))
    )


def context(credential: Credential, request: WireObject) -> VerifyContext:
    wire = decode_credential(credential.to_authorization()[8:])
    return VerifyContext(
        credential=credential,
        request=request,
        challenge=to_pympp_challenge(object_value(wire["challenge"])),
    )


async def validate(method: ServerMethod, credential: Credential, request: WireObject) -> Validation:
    ctx = context(credential, request)
    assert method.validate is not None
    return await method.validate(
        ValidateContext(credential=credential, request=request, challenge=ctx.challenge)
    )


async def broadcast(method: ServerMethod, credential: Credential, request: WireObject) -> Receipt:
    assert method.broadcast is not None
    return await method.broadcast(context(credential, request))


async def settle(method: ServerMethod, credential: Credential, request: WireObject) -> Receipt:
    receipt, _ = await method.settle(context(credential, request))
    return receipt
