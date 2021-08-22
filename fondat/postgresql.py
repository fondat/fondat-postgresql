"""Module to manage resource items in a PostgreSQL database."""

import asyncio
import asyncpg
import contextvars
import dataclasses
import fondat.codec
import fondat.error
import fondat.sql
import functools
import json
import logging
import typing
import uuid

from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from fondat.data import datacls
from fondat.sql import Statement
from fondat.types import is_subclass
from fondat.validation import validate, validate_arguments
from typing import Annotated, Any, Literal, Optional, Union
from uuid import UUID


_logger = logging.getLogger(__name__)

NoneType = type(None)


class PostgreSQLCodec(fondat.codec.Codec[fondat.codec.F, Any]):
    """Base class for PostgreSQL codecs."""


codec_providers = []


@functools.cache
def get_codec(python_type) -> PostgreSQLCodec:
    """Return a codec compatible with the specified Python type."""

    if typing.get_origin(python_type) is typing.Annotated:
        python_type = typing.get_args(python_type)[0]  # strip annotation

    for provider in codec_providers:
        if (codec := provider(python_type)) is not None:
            return codec

    raise TypeError(f"failed to provide PostgreSQL codec for {python_type}")


def _codec_provider(wrapped=None):
    if wrapped is None:
        return functools.partial(_codec_provider)
    codec_providers.append(wrapped)
    return wrapped


def _pass_codec(python_type, sql_type):
    class PassCodec(PostgreSQLCodec[python_type]):
        def __init__(self):
            self.python_type = python_type
            self.sql_type = sql_type

        @validate_arguments
        def encode(self, value: python_type) -> python_type:
            return value

        @validate_arguments
        def decode(self, value: python_type) -> python_type:
            return value

    return PassCodec()


_pass_codecs = []


def _add_pass_codec(python_type, sql_type):
    _pass_codecs.append(_pass_codec(python_type, sql_type))


# order is significant
_add_pass_codec(str, "text")
_add_pass_codec(bool, "boolean")
_add_pass_codec(int, "bigint")
_add_pass_codec(float, "double precision")
_add_pass_codec(bytes, "bytea")
_add_pass_codec(bytearray, "bytea")
_add_pass_codec(UUID, "uuid")
_add_pass_codec(Decimal, "numeric")
_add_pass_codec(datetime, "timestamp with time zone")
_add_pass_codec(date, "date")


@_codec_provider
def pass_provider(python_type):
    for codec in _pass_codecs:
        if is_subclass(python_type, codec.python_type):
            return codec


@_codec_provider
def _iterable_codec_provider(python_type):

    origin = typing.get_origin(python_type)
    if not origin or not is_subclass(origin, Iterable):
        return

    args = typing.get_args(python_type)
    if not args or len(args) > 1:
        return

    codec = get_codec(args[0])

    class IterableCodec(PostgreSQLCodec[python_type]):

        sql_type = f"{codec.sql_type}[]"

        @validate_arguments
        def encode(self, value: python_type) -> Any:
            return [codec.encode(v) for v in value]

        @validate_arguments
        def decode(self, value: Any) -> python_type:
            return python_type(codec.decode(v) for v in value)

    return IterableCodec()


@_codec_provider
def _union_codec_provider(python_type):
    """
    Provides a codec that encodes/decodes a Union or Optional value to/from a
    compatible PostgreSQL value. For Optional value, will use codec for its
    type, otherwise it encodes/decodes as jsonb.
    """

    origin = typing.get_origin(python_type)
    if origin is not Union:
        return

    args = typing.get_args(python_type)
    is_nullable = NoneType in args
    args = [a for a in args if a is not NoneType]
    codec = (
        get_codec(args[0]) if len(args) == 1 else _jsonb_codec_provider(python_type)
    )  # Optional[T]

    class UnionCodec(PostgreSQLCodec[python_type]):

        sql_type = codec.sql_type

        @validate_arguments
        def encode(self, value: python_type) -> Any:
            if value is None:
                return None
            return codec.encode(value)

        @validate_arguments
        def decode(self, value: Any) -> python_type:
            if value is None and is_nullable:
                return None
            return codec.decode(value)

    return UnionCodec()


@_codec_provider
def _literal_codec_provider(python_type):
    """
    Provides a codec that encodes/decodes a Literal value to/from a compatible
    PostgreSQL value. If all literal values share the same type, then a codec
    for that type will be used, otherwise it encodes/decodes as jsonb.
    """

    origin = typing.get_origin(python_type)
    if origin is not Literal:
        return

    return get_codec(Union[tuple(type(arg) for arg in typing.get_args(python_type))])


@_codec_provider
def _jsonb_codec_provider(python_type):
    """
    Provides a codec that encodes/decodes a value to/from a PostgreSQL jsonb
    value. It unconditionally returns the codec, regardless of Python type.
    It must be the last provider in the list to serve as a catch-all.
    """

    json_codec = fondat.codec.get_codec(fondat.codec.JSON, python_type)

    class JSONBCodec(PostgreSQLCodec[python_type]):

        sql_type = "jsonb"

        @validate_arguments
        def encode(self, value: python_type) -> str:
            return json.dumps(json_codec.encode(value))

        @validate_arguments
        def decode(self, value: str) -> python_type:
            return json_codec.decode(json.loads(value))

    return JSONBCodec()


class _Results(AsyncIterator[Any]):
    def __init__(self, statement, results):
        self.statement = statement
        self.results = results
        self.codecs = {
            k: get_codec(t)
            for k, t in typing.get_type_hints(statement.result, include_extras=True).items()
        }

    def __aiter__(self):
        return self

    async def __anext__(self):
        row = await self.results.__anext__()
        return self.statement.result(**{k: self.codecs[k].decode(row[k]) for k in self.codecs})


@datacls
class Config:
    dsn: Annotated[Optional[str], "connection arguments in libpg connection URI format"]
    host: Annotated[Optional[str], "database host address"]
    port: Annotated[Optional[int], "port number to connect to"]
    user: Annotated[Optional[str], "the name of the database role used for authentication"]
    password: Annotated[Optional[str], "password to be used for authentication"]
    passfile: Annotated[Optional[str], "the name of the file used to store passwords"]
    database: Annotated[Optional[str], "the name of the database to connect to"]
    timeout: Annotated[Optional[float], "connection timeout in seconds"]
    ssl: Optional[Literal["disable", "prefer", "require", "verify-ca", "verify-full"]]


@asynccontextmanager
async def _async_null_context():
    yield


class Database(fondat.sql.Database):
    """
    Manages access to a PostgreSQL database.

    Supplied configuration can be a Config dataclass instance, or a function or coroutine
    function that returns a Config dataclass instance.
    """

    def __init__(
        self,
        config: Union[Config, Callable[[], Config], Callable[[], Coroutine[Any, Any, Config]]],
    ):
        super().__init__()
        self.config = config
        self._conn = contextvars.ContextVar("fondat_postgresql_conn", default=None)
        self._txn = contextvars.ContextVar("fondat_postgresql_conn", default=None)

    async def _config(self):
        config = None
        if isinstance(self.config, Config):
            config = self.config
        elif callable(self.config):
            config = self.config()
            if asyncio.iscoroutine(config):
                config = await config
        with fondat.error.replace(Exception, RuntimeError):
            validate(config, Config, "config")
        return config

    @asynccontextmanager
    async def connection(self):
        if self._conn.get(None):  # connection already established
            yield
            return
        config = await self._config()
        kwargs = {k: v for k, v in dataclasses.asdict(config).items() if v is not None}
        _logger.debug(f"open connection ({kwargs})")
        connection = await asyncpg.connect(**kwargs)
        token = self._conn.set(connection)
        try:
            yield
        finally:
            _logger.debug("close connection")
            self._conn.reset(token)
            try:
                await connection.close()
            except Exception as e:
                _logger.error(exc_info=e)

    @asynccontextmanager
    async def transaction(self):
        txid = uuid.uuid4().hex
        _logger.debug("transaction begin %s", txid)
        token = self._txn.set(txid)
        async with self.connection():
            connection = self._conn.get()
            transaction = connection.transaction()
            await transaction.start()

            async def commit():
                _logger.debug("transaction commit %s", txid)
                await transaction.commit()

            async def rollback():
                _logger.debug("transaction rollback %s", txid)
                await transaction.rollback()

            try:
                yield
            except GeneratorExit:  # explicit cleanup of asynchronous generator
                await commit()
            except Exception:
                await rollback()
                raise
            else:
                await commit()
            finally:
                self._txn.reset(token)

    async def execute(self, statement: Statement) -> Optional[AsyncIterator[Any]]:
        if not self._txn.get():
            raise RuntimeError("transaction context required to execute statement")
        text = []
        args = []
        for fragment in statement:
            if isinstance(fragment, str):
                text.append(fragment)
            else:
                args.append(get_codec(fragment.python_type).encode(fragment.value))
                text.append(f"${len(args)}")
        text = "".join(text)
        conn = self._conn.get()
        _logger.debug("%s args=%s", text, args)
        if statement.result is None:
            await conn.execute(text, *args)
        else:  # expecting a result
            return _Results(statement, conn.cursor(text, *args).__aiter__())

    def get_codec(self, python_type: Any) -> PostgreSQLCodec:
        return get_codec(python_type)
