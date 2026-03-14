from dataclasses import dataclass
from typing import Awaitable, Callable

from app.models.models import Connection, Endpoint, ModelConfig


@dataclass(slots=True)
class ConnectionCrudDependencies:
    create_endpoint_from_inline_fn: Callable[..., Awaitable[Endpoint]]
    ensure_model_config_ids_exist_fn: Callable[..., Awaitable[None]]
    list_ordered_connections_fn: Callable[..., Awaitable[list[Connection]]]
    list_ordered_connections_for_models_fn: Callable[
        ..., Awaitable[dict[int, list[Connection]]]
    ]
    load_connection_or_404_fn: Callable[..., Awaitable[Connection]]
    load_model_or_404_fn: Callable[..., Awaitable[ModelConfig]]
    lock_profile_row_fn: Callable[..., Awaitable[None]]
    mark_connection_recovered_fn: Callable[..., None]
    normalize_connection_priorities_fn: Callable[[list[Connection]], None]
    serialize_custom_headers_fn: Callable[[dict[str, str] | None], str | None]
    validate_pricing_template_id_fn: Callable[..., Awaitable[int | None]]


__all__ = ["ConnectionCrudDependencies"]
