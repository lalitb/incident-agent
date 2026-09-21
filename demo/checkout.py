import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from opentelemetry.metrics import Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from telemetry import setup_telemetry


POOL_SIZE = int(os.getenv("POOL_SIZE", "10"))
DB_WORK_SECONDS = float(os.getenv("DB_WORK_SECONDS", "0.2"))

if POOL_SIZE < 1:
    raise ValueError("POOL_SIZE must be at least 1")

if not 0 <= DB_WORK_SECONDS <= 5:
    raise ValueError("DB_WORK_SECONDS must be between 0 and 5")

engine = create_engine(
    os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://checkout:learning-only@localhost:5432/checkout",
    ),
    pool_size=POOL_SIZE,
    max_overflow=0,
    pool_timeout=5,
    connect_args={"connect_timeout": 5},
)

tracer, meter, logger, shutdown_telemetry = setup_telemetry()

requests = meter.create_counter(
    "checkout_requests",
    description="Completed checkout attempts",
)

request_duration = meter.create_histogram(
    "checkout_duration",
    unit="s",
    description="Time spent inside the checkout handler",
)

connection_wait = meter.create_histogram(
    "checkout_connection_wait",
    unit="s",
    description="Time spent acquiring a database connection",
)

meter.create_observable_gauge(
    "checkout_pool_in_use",
    callbacks=[
        lambda options: [Observation(engine.pool.checkedout())]
    ],
    description="Database connections currently checked out",
)

meter.create_observable_gauge(
    "checkout_pool_limit",
    callbacks=[
        lambda options: [Observation(POOL_SIZE)]
    ],
    description="Maximum database connections",
)


@asynccontextmanager
async def lifespan(app):
    try:
        # Fail startup clearly if PostgreSQL is unavailable.
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        logger.info(
            "Checkout started: pool_size=%s db_work_seconds=%s",
            POOL_SIZE,
            DB_WORK_SECONDS,
        )
        yield
    finally:
        engine.dispose()
        shutdown_telemetry()


app = FastAPI(lifespan=lifespan)


@app.post("/checkout")
def checkout():
    # A synchronous endpoint runs in FastAPI's worker thread pool.
    started = time.perf_counter()
    outcome = "success"

    with tracer.start_as_current_span(
        "POST /checkout",
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": "POST",
            "http.route": "/checkout",
            "db.pool.limit": POOL_SIZE,
        },
    ) as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"

        try:
            # Includes connection creation on a cold pool.
            with tracer.start_as_current_span("db.acquire_connection"):
                waiting_since = time.perf_counter()
                try:
                    connection = engine.connect()
                finally:
                    connection_wait.record(
                        time.perf_counter() - waiting_since
                    )

            # The context manager always returns the connection.
            with connection:
                with tracer.start_as_current_span(
                    "db.checkout_query",
                    attributes={"db.system": "postgresql"},
                ):
                    connection.execute(
                        text("SELECT pg_sleep(:seconds)"),
                        {"seconds": DB_WORK_SECONDS},
                    )

            span.set_attribute("http.response.status_code", 200)

            logger.info(
                "Checkout completed trace_id=%s",
                trace_id,
            )

            return {
                "status": "ok",
                "trace_id": trace_id,
                "pool_size": POOL_SIZE,
            }

        except SQLAlchemyError as exc:
            outcome = "error"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("http.response.status_code", 503)

            logger.exception(
                "Checkout database failure trace_id=%s",
                trace_id,
            )

            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Database unavailable or pool exhausted",
                    "trace_id": trace_id,
                },
            ) from exc

        finally:
            attributes = {"outcome": outcome}
            requests.add(1, attributes)
            request_duration.record(
                time.perf_counter() - started,
                attributes,
            )