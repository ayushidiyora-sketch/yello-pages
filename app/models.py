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


class AmazonReviewsRequest(BaseModel):
    # one line each: a raw ASIN, a product URL (.../dp/ASIN), or a /product-reviews/ASIN URL.
    queries: list[str] = Field(default_factory=list, examples=[["B001R1RXUG"]])
    domain: str = Field("amazon.com", examples=["amazon.com"])
    limit: int = Field(20, ge=1, le=1000, examples=[20])  # max reviews per product


class EbayScrapeRequest(BaseModel):
    # one line each: an eBay item id, an item URL (.../itm/ID), a search URL, or a keyword.
    queries: list[str] = Field(default_factory=list, examples=[["golf clubs"]])
    country: str = Field("US", examples=["US"])       # ISO-2; selects the eBay marketplace
    postcode: Optional[str] = Field(None, examples=["10001"])
    limit: int = Field(20, ge=1, le=1000, examples=[20])  # max results per search query


class GSearchRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["chatgpt"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[10])  # rows/query; None = all
    date_range: Optional[str] = Field("", examples=["any"])   # any|day|week|month|year
    region: str = Field("us", examples=["us"])
    language: str = Field("en", examples=["en"])
    uule: Optional[str] = Field("", examples=[""])             # Google-only geo code (ignored on DDG)


class BBBRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["auto sellers"]])  # term or bbb.org URL
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # rows/query; None = all


class G2Request(BaseModel):
    # one line each: a g2.com product-reviews URL, a product URL, or a bare product slug.
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.g2.com/products/outscraper/reviews"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # reviews/query; None = all
    sort: str = Field("", examples=["most_recent"])  # ""|most_recent|most_helpful|highest_rated|lowest_rated


class GlassdoorJobsRequest(BaseModel):
    # one line each: a glassdoor.com job-search URL (SRCH...htm).
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.glassdoor.com/Job/los-angeles-ca-us-python-jobs-SRCH_IL.0,17_IC1146821_KO18,24.htm"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # jobs/query; None = all


class GlassdoorReviewsRequest(BaseModel):
    # one line each: a glassdoor.com company-reviews URL (Reviews/...-Reviews-E<id>.htm).
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.glassdoor.com/Reviews/Amazon-Reviews-E6036.htm"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # reviews/query; None = all
    sort: str = Field("", examples=["most_recent"])  # ""|most_recent|most_helpful


class WalmartProductsRequest(BaseModel):
    # one line each: a walmart.com product URL (/ip/<slug>/<id>).
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.walmart.com/ip/Homfa-Sofa-Bed/625493716"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # rows/query; None = all


class WalmartReviewsRequest(BaseModel):
    # one line each: a walmart.com product URL (/ip/<slug>/<id>); reviews read from /reviews/product/<id>.
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.walmart.com/ip/Blackstone-Griddle/1347629739"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # reviews/query; None = all
    sort: str = Field("", examples=["most_relevant"])  # ""|most_relevant|top_reviews|newest|oldest|high_rating|low_rating


class BBBReviewsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # a bbb.org reviews/profile URL
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[50])  # reviews/business; None = all
    sort: str = Field("recent", examples=["recent"])  # recent | highest | lowest


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
