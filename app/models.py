from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ScrapeRequest(BaseModel):
    search: str = Field(..., min_length=1, examples=["Dentists"])
    location: str = Field(..., min_length=1, examples=["New York, NY"])
    region: str = Field("us", examples=["us"])
    limit: Optional[int] = Field(None, ge=1, le=100000, examples=[100])
    # View controls applied to the scraped data (YP filters/sorts client-side only).
    sort: Optional[str] = Field(None, examples=["name"])           # "" | "name" | "average_rating"
    categories: Optional[list[str]] = Field(None, examples=[["Plumbers"]])  # YP category labels


class GSearchRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["chatgpt"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[10])  # rows/query; None = all
    date_range: Optional[str] = Field("", examples=["any"])   # any|day|week|month|year
    region: str = Field("us", examples=["us"])
    language: str = Field("en", examples=["en"])
    uule: Optional[str] = Field("", examples=[""])             # Google-only geo code (ignored on DDG)


class Business(BaseModel):
    job_id: str
    name: str
    phone: Optional[str] = None
    category: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    rating: Optional[str] = None
    reviews_count: Optional[str] = None
    open_status: Optional[str] = None
    email: Optional[str] = None
    source_url: Optional[str] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
