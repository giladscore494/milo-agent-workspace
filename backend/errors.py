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


#: How a failed repository write is classified, and nothing more. Static and
#: closed: the class is decided from the failure's SHAPE (transport error,
#: HTTP status, SQLSTATE), never from its text, and no database message, URL
#: or value travels with it.
#:
#: * ``transient``   -- the write may not have reached the database, or the
#:   database could not take it right now (network error, timeout, HTTP
#:   408/425/429/5xx, PostgREST connection codes, SQLSTATE class 08/53,
#:   serialization failure, deadlock, lock timeout, statement timeout, admin
#:   shutdown). Retrying an idempotent write is safe.
#: * ``rejected``    -- the database refused the content (SQLSTATE class 22 or
#:   23). Retrying cannot help and would repeat the refusal.
#: * ``unavailable`` -- anything else. Not retried: an unknown failure is never
#:   assumed to be safe to repeat.
REPOSITORY_FAILURE_CLASSES = ("transient", "rejected", "unavailable")


class RepositoryFailure(AppError):
    """A repository write that failed for a reason OTHER than a lost lease.

    The code stays ``REPOSITORY_ERROR`` and the message stays generic, so every
    existing caller that matches on either is unchanged; `failure_class` is the
    one added, allowlisted fact.
    """

    def __init__(self, failure_class: str, message: str = "guarded persistence operation failed",
                 *, timed_out: bool = False):
        if failure_class not in REPOSITORY_FAILURE_CLASSES:
            raise ValueError("repository failure class must come from the static allowlist")
        super().__init__("REPOSITORY_ERROR", message, 502)
        self.failure_class = failure_class
        #: The database cancelled the statement for a statement or lock
        #: timeout (SQLSTATE 57014 / 55P03). Always `transient`.
        self.timed_out = bool(timed_out)


class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource.upper()}_NOT_FOUND", f"{resource} not found: {identifier}", 404)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}}, headers=exc.headers)
