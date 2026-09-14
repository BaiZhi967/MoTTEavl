from .messages import Contract
class ContractError(Contract):
    code: str
    message: str
    pointer: str = ""
