from pydantic import BaseModel, ConfigDict


class APIModel(BaseModel):
    """Base for all API schemas (request and response)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
