import re
from datetime import date
from pydantic import BaseModel, ConfigDict, field_validator


class MonthQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    month: str

    @field_validator('month')
    @classmethod
    def real_month(cls, value):
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', value):
            raise ValueError('Use YYYY-MM')
        day = date.fromisoformat(value + '-01')
        # The displayed grid includes adjacent days and must be representable.
        if day.year == 9999 and day.month == 12:
            raise ValueError('Calendar grid exceeds supported date range')
        return value
