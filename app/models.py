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


class GNewsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["Business news"]])
    limit: Optional[int] = Field(None, ge=0, le=1000, examples=[100])  # articles/query; None/0 = all
    country: str = Field("us", examples=["us"])
    date_range: Optional[str] = Field("", examples=["any"])    # any|hour|day|week|month|year
    language: str = Field("en", examples=["en"])


class TrafficPair(BaseModel):
    start: str = Field("", examples=["17.8805168994, -76.9060696994"])
    stop: str = Field("", examples=["17.8810785996, -76.9055896991"])


class GMapsTrafficRequest(BaseModel):
    pairs: list[TrafficPair] = Field(..., min_length=1)   # Start->Stop location pairs
    time_from: str = Field("", examples=["2026-06-23T14:00"])
    time_to: str = Field("", examples=["2026-06-23T14:59"])
    interval_min: int = Field(60, ge=1, le=1440, examples=[60])
    travel_mode: str = Field("best", examples=["best"])   # best|driving|transit|walking|cycling|flights


class GImagesRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["Wallpaper"]])
    limit: Optional[int] = Field(None, ge=0, le=2000, examples=[100])  # images/query; None/0 = all
    country: str = Field("us", examples=["us"])
    language: str = Field("en", examples=["en"])


class GVideosRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["Wallpaper"]])
    limit: Optional[int] = Field(None, ge=0, le=2000, examples=[100])  # videos/query; None/0 = all
    country: str = Field("us", examples=["us"])
    language: str = Field("en", examples=["en"])


class GEventsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["beer festivals minnesota"]])
    limit: Optional[int] = Field(1, ge=1, le=20, examples=[1])   # pages/query (~10 events per page)
    country: str = Field("us", examples=["us"])
    language: str = Field("en", examples=["en"])


class GTrendsRequest(BaseModel):
    # one line each: a query; use "term1 | term2" to compare terms.
    queries: list[str] = Field(..., min_length=1, examples=[["tesla | toyota"]])
    geo: str = Field("", examples=["US"])               # country code; "" = Worldwide
    timeframe: str = Field("Past 12 months", examples=["Past 12 months"])
    resolution: str = Field("COUNTRY", examples=["REGION"])   # COUNTRY|REGION|CITY|DMA


class GCareersRequest(BaseModel):
    # one line each: a careers.google.com/jobs/results/ search URL (or a bare keyword).
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://careers.google.com/jobs/results/?location=Los Angeles, CA, USA"]])
    limit: Optional[int] = Field(None, ge=0, le=1000, examples=[10])  # jobs/query; None/0 = all


class GMapsDirectoryRequest(BaseModel):
    # query: "category, city" / a Maps search URL / a place_id (ChIJ..) / a feature id (0x..:0x..)
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.google.com/maps/search/restaurants+near+New+York,+NY"]])
    limit: Optional[int] = Field(100, ge=1, le=5000, examples=[100])  # places/query; None = all
    language: str = Field("en", examples=["en"])


class GMapsContribRequest(BaseModel):
    # each line: a Google Maps contributor ID (e.g. 116992800507045820329) or a /contrib/<id> URL
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.google.com/maps/contrib/109743434949154249800/reviews"]])
    limit: Optional[int] = Field(100, ge=1, le=5000, examples=[100])  # reviews/contributor; None=all
    language: str = Field("en", examples=["en"])


class GMapsPhotosRequest(BaseModel):
    # query: "category, city, country" / a Maps URL / Google or Places ID / a feature id (0x..:0x..)
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.google.com/maps/place/?q=place_id:ChIJu7bMNFV-54gR-lrHScvPRX4"]])
    limit: Optional[int] = Field(250, ge=0, le=10000, examples=[250])  # photos/place; 0 = all
    places_limit: int = Field(1, ge=1, le=100, examples=[1])  # places per one query (premium)
    language: str = Field("en", examples=["en"])
    country: str = Field("", examples=[""])
    filtering: str = Field("any", examples=["any"])


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


class YouTubeChannelsRequest(BaseModel):
    # one line each: a channel URL (/@handle, /channel/UC..., /c/Name) or a bare handle/name.
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.youtube.com/@outscraper", "outscraper"]])


class AirbnbReviewsRequest(BaseModel):
    # one line each: an airbnb.com room URL (/rooms/<id>) or a bare listing id.
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.airbnb.com/rooms/927539322986647456"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[100])  # reviews/query; None = all
    sort: str = Field("", examples=["most_recent"])  # ""|most_recent|highest|lowest


class AirbnbSearchRequest(BaseModel):
    # one line each: an airbnb.com search URL (/s/<place>/homes) or a bare location keyword.
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.airbnb.com/s/Paris--France/homes", "Goa, India"]])
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[50])  # listings/query; None = all


class BBBReviewsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # a bbb.org reviews/profile URL
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[50])  # reviews/business; None = all
    sort: str = Field("recent", examples=["recent"])  # recent | highest | lowest


class ExpediaRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # an expedia.com Hotel-Search URL
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[10])  # hotels/URL; None = all


class ExpediaReviewsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # an expedia.com hotel URL or hotel id
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[100])  # reviews/hotel; None = all
    sort: str = Field("relevant", examples=["relevant"])  # relevant|recent|highest|lowest


class GMapsReviewsRequest(BaseModel):
    # query: "category, city, country" / a Maps URL / Google or Places ID / a feature id (0x..:0x..)
    queries: list[str] = Field(..., min_length=1, examples=[["Real estate agency, Rome, Italy"]])
    sort: str = Field("newest", examples=["newest"])  # newest|relevant|highest|lowest
    limit: Optional[int] = Field(250, ge=0, le=10000, examples=[250])  # reviews/place; 0 = unlimited
    places_limit: int = Field(1, ge=1, le=100, examples=[1])  # places per one query search
    language: str = Field("en", examples=["en"])
    country: str = Field("", examples=[""])
    reviews_query: str = Field("", examples=[""])     # filter reviews by text
    filtering: str = Field("any", examples=["any"])
    reviews_filtering: str = Field("all", examples=["all"])


class GMapsMonitorRequest(BaseModel):
    # query: "category, city, country" / a Maps URL / a Google Places ID (ChIJ..)
    queries: list[str] = Field(..., min_length=1, examples=[["McDonald's, Sydney"]])
    frequency: str = Field("weekly", examples=["weekly"])  # daily|weekly|3weeks|monthly|3months
    email: str = Field("", examples=["info@sensussoft.com"])  # where to send the report
    threshold: int = Field(3, ge=1, le=5, examples=[3])  # rating <= threshold counts as negative
    sort: str = Field("newest", examples=["newest"])  # newest|relevant|highest|lowest
    language: str = Field("en", examples=["en"])
    limit: Optional[int] = Field(100, ge=1, le=5000, examples=[100])  # reviews/place per cycle


class TrustpilotRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # trustpilot URL (category/profile) or company ID
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[50])  # companies/query; None = all


class TrustpilotSearchRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["real estate"]])  # search keyword(s)
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[100])  # companies/query; None = all


class TrustpilotReviewsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # trustpilot /review/ URL or a company domain/id
    limit: Optional[int] = Field(None, ge=1, le=5000, examples=[100])  # reviews/query; None = all
    language: str = Field("all", examples=["all"])  # all|en|es|fr|de|it|nl|pt|da|sv|no|fi|pl


class TrustpilotMonitorRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # trustpilot /review/ URL or company domain/id
    frequency: str = Field("weekly", examples=["weekly"])  # daily|weekly|3weeks|monthly|3months
    email: str = Field("", examples=["info@sensussoft.com"])  # where to send the report
    threshold: int = Field(3, ge=1, le=5, examples=[3])  # rating <= threshold counts as negative
    language: str = Field("all", examples=["all"])
    limit: Optional[int] = Field(200, ge=1, le=5000, examples=[200])  # reviews/query per cycle


class HotelsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # a hotels.com Hotel-Search URL
    limit: Optional[int] = Field(None, ge=1, le=1000, examples=[10])  # hotels/URL; None = all


class HotelsReviewsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # a hotels.com hotel URL
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[50])  # reviews/hotel; None = all
    sort: str = Field("relevant", examples=["relevant"])  # relevant|recent|highest|lowest


class GoogleMapsRequest(BaseModel):
    # categories/brands × locations -> "<category> in <location>" search queries.
    categories: list[str] = Field(..., min_length=1, examples=[["Restaurant", "Doctor"]])
    locations: list[str] = Field(default_factory=list, examples=[["Gujarat, India"]])
    limit: Optional[int] = Field(None, ge=1, le=500, examples=[60])  # places/query; None = all (~60 cap)
    region: str = Field("US", examples=["IN"])      # regionCode bias (ISO-2)
    language: str = Field("en", examples=["en"])    # languageCode
    # Advanced: Quick Filters (with_website|without_website|operational|with_phone|good_rating|
    # bad_rating), a per-query skip offset, and cross-query de-duplication.
    filters: list[str] = Field(default_factory=list, examples=[["with_website", "operational"]])
    skip: int = Field(0, ge=0, le=500)
    dedupe: bool = Field(True)


class GoogleMapsDomainsRequest(BaseModel):
    # one line each: a website domain or URL — find the Google Maps place that owns it.
    domains: list[str] = Field(..., min_length=1, examples=[["https://sensussoft.com/"]])
    limit: int = Field(1, ge=1, le=20)              # places/domain (usually 1)
    region: str = Field("US", examples=["IN"])      # regionCode bias (ISO-2)
    language: str = Field("en", examples=["en"])


class HomeDepotRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)   # a homedepot.com /b/, /p/ or /s/ URL or keyword
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[100])  # products/query; None = all


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
