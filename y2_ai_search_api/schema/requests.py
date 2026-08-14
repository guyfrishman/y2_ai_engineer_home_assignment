from pydantic import BaseModel, ConfigDict, Field


class ParseRequest(BaseModel):
    q: str = Field(min_length=1, max_length=50_000)

    model_config = ConfigDict(
        json_schema_extra={"example": {"q": "דירת 3 חדרים בירושלים עד מליון שח"}}
    )
