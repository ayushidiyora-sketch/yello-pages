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


class AmazonScrapeRequest(BaseModel):
    # one line each: a raw ASIN ("B00K0QKBM6"), a product URL (.../dp/ASIN),
    # a search URL (.../s?k=...), or a plain search keyword.
    queries: list[str] = Field(default_factory=list, examples=[["B00K0QKBM6", "golf clubs"]])
    domain: str = Field("amazon.com", examples=["amazon.com"])
    postcode: Optional[str] = Field(None, examples=["11201"])
    language: Optional[str] = Field(None, examples=["en_US"])
    currency: Optional[str] = Field(None, examples=["USD"])
    limit: int = Field(1, ge=1, le=1000, examples=[10])  # max results per one query


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
