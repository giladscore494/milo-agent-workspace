from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers: dict[str, str] | None = None
        super().__init__(message)


#: The repository codes that mean "this worker no longer holds the run".
#:
#: Two spellings for one condition, and both must escape wherever a caller
#: absorbs failures: the Supabase repository classifies a stale-lease RPC
#: failure as `RUN_LEASE_LOST`, and the in-memory repository raises
#: `RUN_TRANSITION_CONFLICT` from the same check. A stale worker is an
#: INFRASTRUCTURE outcome that has to reach the worker's own lease handling;
#: laundering it into a decision about evidence would make a lost lease look
#: like an answer about the data.
#:
#: Defined HERE, in the leaf module, because more than one subsystem has to
#: recognise it: `backend/catalog/promotion.py` re-exports it under its own
#: name, and the V1 evidence authority re-raises on it.
LEASE_FAILURE_CODES = frozenset({"RUN_LEASE_LOST", "RUN_TRANSITION_CONFLICT"})


class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource.upper()}_NOT_FOUND", f"{resource} not found: {identifier}", 404)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}}, headers=exc.headers)
