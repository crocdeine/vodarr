"""Base of every request body (ADR-0004)."""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Accepts camelCase (and snake_case) field names; unknown fields are refused."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
