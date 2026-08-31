from fastapi import HTTPException


class NotFoundError(HTTPException):
    def __init__(self, resource: str, identifier: str = ""):
        detail = f"{resource} not found"
        if identifier:
            detail += f": {identifier}"
        super().__init__(status_code=404, detail=detail)


class ValidationError(HTTPException):
    def __init__(self, message: str):
        super().__init__(status_code=422, detail=message)


class ConflictError(HTTPException):
    def __init__(self, message: str):
        super().__init__(status_code=409, detail=message)


class ForbiddenError(HTTPException):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(status_code=403, detail=message)


class BadRequestError(HTTPException):
    def __init__(self, message: str):
        super().__init__(status_code=400, detail=message)
