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


class BookingReviewsRequest(BaseModel):
    # one line each: a booking.com/hotel/<cc>/<slug>.html URL (preferred) or a bare hotel slug.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.booking.com/hotel/tr/old-town-point-amp-spa-antalya.html"]])
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])   # reviews/query; None/0 = all
    sort: str = Field("most_relevant", examples=["most_relevant"])  # most_relevant|newest|oldest|highest|lowest


class LinkedInPostsRequest(BaseModel):
    # one line each: a linkedin.com/company URL, a bare company slug, or a numeric company id.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["outscraper", "https://www.linkedin.com/company/outscraper/"]])
    limit: Optional[int] = Field(100, ge=0, le=1000, examples=[100])  # posts/query; None/0 = all


class LinkedInCompaniesRequest(BaseModel):
    # one line each: a linkedin.com/company URL, a bare company slug, or a numeric company id.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["outscraper", "https://www.linkedin.com/company/outscraper/"]])


class GTrendsRequest(BaseModel):
    # one line each: a query; use "term1 | term2" to compare terms.
    queries: list[str] = Field(..., min_length=1, examples=[["tesla | toyota"]])
    geo: str = Field("", examples=["US"])               # country code; "" = Worldwide
    timeframe: str = Field("Past 12 months", examples=["Past 12 months"])
    resolution: str = Field("COUNTRY", examples=["REGION"])   # COUNTRY|REGION|CITY|DMA


class AIScraperRequest(BaseModel):
    # one URL per line; Claude extracts structured data from each page.
    queries: list[str] = Field(..., min_length=1, examples=[["https://outscraper.com"]])
    prompt: str = Field("", examples=["Extract company name, pricing, and contact email"])
    schema_def: dict | None = Field(None, alias="schema",
                                    examples=[{"type": "object", "properties": {"company_name": {"type": "string"}}}])
    limit: int | None = Field(None, examples=[0])

    model_config = {"populate_by_name": True}


class AngiRequest(BaseModel):
    # one per line: an angi.com company-list or near-me search URL.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.angi.com/nearme/plumbing/?postalCode=75151"]])
    limit: int | None = Field(None, examples=[100])


class FeefoReviewsRequest(BaseModel):
    # one per line: a feefo.com/<locale>/reviews/<merchant> URL or a bare merchant identifier.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.feefo.com/en-GB/reviews/m-c-fire-protection"]])
    limit: int | None = Field(None, examples=[100])
    sort: str = Field("Newest", examples=["Newest"])   # Newest|Oldest|Most Helpful


class ThuisbezorgdReviewsRequest(BaseModel):
    # one per line: a thuisbezorgd.nl restaurant menu URL or a bare restaurant slug/ID.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.thuisbezorgd.nl/en/menu/mr-sushi-dedemsvaart"]])
    limit: int | None = Field(None, examples=[100])


class ProductHuntProfilesRequest(BaseModel):
    # one per line: a producthunt.com/@username URL, an @username, or a bare username.
    queries: list[str] = Field(..., min_length=1, examples=[["@rrhoover"]])
    limit: int | None = Field(None, examples=[100])


class KununuReviewsRequest(BaseModel):
    # one company per line: a kununu.com company URL or a company name.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.kununu.com/de/mercedes-benz-group"]])
    limit: int | None = Field(None, examples=[100])
    sort: str = Field("Newest", examples=["Newest"])   # Date|Newest|Oldest|Beste|Schlechteste


class GCareersRequest(BaseModel):
    # one line each: a careers.google.com/jobs/results/ search URL (or a bare keyword).
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://careers.google.com/jobs/results/?location=Los Angeles, CA, USA"]])
    limit: Optional[int] = Field(None, ge=0, le=1000, examples=[10])  # jobs/query; None/0 = all


class GSJobsRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["Python developer California"]])
    pages: int = Field(1, ge=1, le=20, examples=[1])           # result pages/query (~10 jobs/page)
    language: str = Field("en", examples=["en"])
    region: str = Field("us", examples=["us"])


class GShopRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["Iphone 13"]])
    limit: Optional[int] = Field(100, ge=0, le=2000, examples=[100])  # products/query; None/0 = all
    language: str = Field("en", examples=["en"])
    region: str = Field("us", examples=["us"])


class YTSearchRequest(BaseModel):
    # each line: a YouTube search phrase (e.g. "funny cats videos")
    queries: list[str] = Field(..., min_length=1, examples=[["funny cats videos"]])
    limit: Optional[int] = Field(100, ge=0, le=2000, examples=[100])  # videos/query; None/0 = all


class EmailsContactsRequest(BaseModel):
    # each line: a domain or URL (e.g. "stripe.com" or "https://stripe.com/contact")
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com", "shopify.com"]])
    limit: Optional[int] = Field(0, ge=0, le=2000, examples=[0])  # unused (1 row/domain); kept for UI parity


class LeadsEnrichmentRequest(BaseModel):
    # each line: a company domain or URL — returns one row per contact found
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com", "shopify.com"]])
    limit: Optional[int] = Field(0, ge=0, le=50, examples=[0])  # max contacts per company; 0 = all


class EmailVerifierRequest(BaseModel):
    # each line: an email address to verify
    queries: list[str] = Field(..., min_length=1, examples=[["jane@stripe.com", "hello@x.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused; kept for UI parity


class CompanyInsightsRequest(BaseModel):
    # each line: a company domain or URL — returns firmographics
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com", "ibm.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/domain); kept for UI parity


class PhoneEnricherRequest(BaseModel):
    # each line: an international phone number (e.g. "+1 281 236 8208")
    queries: list[str] = Field(..., min_length=1, examples=[["+1 281 236 8208", "+44 20 7946 0958"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/number); kept for UI parity


class PhoneIdentityRequest(BaseModel):
    # each line: a US phone number — returns the owner name + address
    queries: list[str] = Field(..., min_length=1, examples=[["+1 281 236 8208", "1 281 236 2248"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/number); kept for UI parity


class SimilarWebRequest(BaseModel):
    # each line: a domain or URL — returns SimilarWeb traffic/rank/engagement
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com", "github.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/domain); kept for UI parity


class GeocodingRequest(BaseModel):
    # each line: a human-readable address
    queries: list[str] = Field(..., min_length=1, examples=[["321 California Ave, Palo Alto, CA 94306"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/address); kept for UI parity


class BuiltWithRequest(BaseModel):
    # each line: a domain or URL — returns the site's tech stack
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com", "shopify.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/domain); kept for UI parity


class DisposableEmailRequest(BaseModel):
    # each line: an email address — classified disposable/free/corporate
    queries: list[str] = Field(..., min_length=1, examples=[["a@mailinator.com", "b@gmail.com", "c@ibm.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/email); kept for UI parity


class WhitepagesAddressesRequest(BaseModel):
    # each line: a US address — returns location + best-effort residents
    queries: list[str] = Field(..., min_length=1, examples=[["321 California Ave, Palo Alto, CA 94306"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/address); kept for UI parity


class FastbgAddressesRequest(BaseModel):
    # each line: a US address — returns location + best-effort residents
    queries: list[str] = Field(..., min_length=1, examples=[["321 California Ave, Palo Alto, CA 94306"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/address); kept for UI parity


class ReverseGeocodingRequest(BaseModel):
    # each line: "lat,lon" coordinates
    queries: list[str] = Field(..., min_length=1, examples=[["37.427074,-122.1439166"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/coord); kept for UI parity


class DomainInfoRequest(BaseModel):
    # each line: a domain or URL — returns WHOIS/RDAP registration data
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com", "github.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/domain); kept for UI parity


class YahooSearchRequest(BaseModel):
    # each line: a search query or a yahoo.com search URL
    queries: list[str] = Field(..., min_length=1, examples=[["python web scraping"]])
    limit: Optional[int] = Field(100, ge=0, le=100, examples=[100])  # results/query; 0 = up to 100


class ZoomInfoRequest(BaseModel):
    # each line: a domain or URL — ZoomInfo company lookup (paid/blocked source)
    queries: list[str] = Field(..., min_length=1, examples=[["stripe.com"]])
    limit: Optional[int] = Field(0, ge=0, examples=[0])  # unused (1 row/domain); kept for UI parity


class ScreenshoterRequest(BaseModel):
    # each line: a URL to screenshot
    queries: list[str] = Field(..., min_length=1, examples=[["https://outscraper.com"]])
    image_format: str = Field("png", examples=["png"])   # png | jpeg | pdf (webp -> png)
    width: int = Field(1200, ge=200, le=3840, examples=[1200])
    height: int = Field(800, ge=200, le=4320, examples=[800])
    full_page: bool = Field(False, examples=[False])


class YelpBusinessRequest(BaseModel):
    # each line: a yelp.com /search URL or a "Category | Location" pair (built from cats x locations)
    queries: list[str] = Field(..., min_length=1, examples=[["Plumbing | San Francisco, CA"]])
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])  # total results; None/0 = all


class YelpReviewsRequest(BaseModel):
    # each line: a yelp.com /biz/ URL, a bare business slug, or a business id alias
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.yelp.com/biz/eggcellent-waffles-san-francisco"]])
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])  # reviews/business; None/0 = all


class YTTranscriptsRequest(BaseModel):
    # each line: a YouTube video id or URL (watch?v=, youtu.be/, /shorts/, /embed/)
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.youtube.com/watch?v=_XvHhFjrbn0", "ph5pHgklaZ0"]])


class YelpPhotosRequest(BaseModel):
    # each line: a yelp.com /biz/ URL, a bare business slug, or a business id alias
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.yelp.com/biz/eggcellent-waffles-san-francisco"]])
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])  # photos/business; None/0 = all


class BestBuyProductsRequest(BaseModel):
    # one line each: a bestbuy.com category / search / brand / product URL
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.bestbuy.com/site/searchpage.jsp?st=laptop"]])
    limit: Optional[int] = Field(100, ge=0, le=2000, examples=[100])  # products/query; None/0 = all


class BookingSearchRequest(BaseModel):
    # one line each: a booking.com searchresults URL
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.booking.com/searchresults.html?ss=Rome"]])
    limit: Optional[int] = Field(100, ge=0, le=2000, examples=[100])  # properties/query; None/0 = all


class GMapsAutocompleteRequest(BaseModel):
    # each line: a Google Maps search query (e.g. "restaurant")
    queries: list[str] = Field(..., min_length=1, examples=[["central", "restaurant", "bar"]])
    coordinates: str = Field("", examples=["@23.4933124,53.9623381,11.42z"])  # optional location bias
    language: str = Field("en", examples=["en"])
    region: str = Field("us", examples=["us"])


class GSearchAutocompleteRequest(BaseModel):
    # each line: a Google Search query (e.g. "data scraping")
    queries: list[str] = Field(..., min_length=1, examples=[["outscraper", "data scraping"]])
    language: str = Field("en", examples=["en"])
    region: str = Field("us", examples=["us"])


class GFlightsRequest(BaseModel):
    # each line: an "ORIGIN,DESTINATION" IATA pair (e.g. "EWR,LAX")
    queries: list[str] = Field(..., min_length=1, examples=[["EWR,LAX"]])
    departure_date: str = Field("", examples=["2026-07-01"])   # YYYY-MM-DD; "" = Google default
    return_date: str = Field("", examples=[""])                # "" = one-way
    limit: Optional[int] = Field(10, ge=0, le=500, examples=[10])  # flights/query; None/0 = all
    language: str = Field("en", examples=["en"])
    region: str = Field("us", examples=["us"])


class LinkedInProfilesRequest(BaseModel):
    # each line: a linkedin.com/in/<slug> URL or a bare profile slug/id
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.linkedin.com/in/williamhgates"]])


class GShopReviewsRequest(BaseModel):
    # each line: a Google Shopping product link or the long numeric product id
    queries: list[str] = Field(..., min_length=1, examples=[["7016166685587850095"]])
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])  # reviews/product; None/0 = all
    language: str = Field("en", examples=["en"])
    region: str = Field("us", examples=["us"])


class GPlayRequest(BaseModel):
    # each line: a Play Store app id (com.foo.bar) or a /store/apps/details?id=... URL
    queries: list[str] = Field(..., min_length=1, examples=[["com.skype.raider"]])
    limit: Optional[int] = Field(120, ge=0, le=10000, examples=[120])  # reviews/app; None/0 = all
    sort: str = Field("relevant", examples=["relevant"])  # relevant|newest|rating
    language: str = Field("en", examples=["en"])


class GPlayMonitorRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1, examples=[["com.spotify.music"]])
    frequency: str = Field("weekly", examples=["weekly"])  # daily|weekly|3weeks|monthly|3months
    email: str = Field("", examples=["info@sensussoft.com"])
    threshold: int = Field(3, ge=1, le=5, examples=[3])  # rating <= threshold counts as negative
    sort: str = Field("relevant", examples=["relevant"])  # relevant|newest|rating
    language: str = Field("en", examples=["en"])
    limit: Optional[int] = Field(150, ge=1, le=5000, examples=[150])  # reviews/app per cycle


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


class GlassdoorCompaniesRequest(BaseModel):
    # one line each: a company name (e.g. "google") or a Glassdoor search/explore URL
    queries: list[str] = Field(..., min_length=1, examples=[["google", "microsoft"]])
    limit: Optional[int] = Field(100, ge=0, le=1000, examples=[100])  # companies/query; None/0 = all
    domain: str = Field("glassdoor.com", examples=["glassdoor.com"])  # Glassdoor country site


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


class YouTubeVideosRequest(BaseModel):
    # one line each: a channel URL (/@handle, /channel/UC..., /@handle/videos, /@handle/shorts) or a handle.
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.youtube.com/@Google", "@Google/shorts"]])
    limit: Optional[int] = Field(None, ge=1, le=5000, examples=[100])  # videos/query; None = all
    video_type: str = Field("video", examples=["video", "short"])      # "short" = Shorts Only


class GlassdoorCompanyJobsRequest(BaseModel):
    # one line each: a Glassdoor company Jobs URL (…-Jobs-E<id>.htm) or Overview URL (…-EI_IE<id>…).
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.glassdoor.com/Jobs/USA-Jobs-Jobs-E221792.htm"]])
    limit: Optional[int] = Field(None, ge=1, le=2000, examples=[100])  # jobs/query; None = all
    sort: str = Field("relevant", examples=["relevant", "newest"])     # "newest" = Newest First


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


class UpworkRequest(BaseModel):
    # one line each: an upwork.com/nx/search/jobs/?… search URL.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.upwork.com/nx/search/jobs/?q=marketing automation"]])
    limit: Optional[int] = Field(100, ge=0, le=2000, examples=[100])   # jobs/query; None/0 = all
    sort: str = Field("recency", examples=["recency"])  # relevance | recency (Most recent)


class ApolloRequest(BaseModel):
    # one line each: an app.apollo.io/#/people?… or /#/companies?… search URL.
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://app.apollo.io/#/people?page=1&personTitles[]=project manager"]])
    cookies: str = Field("", examples=['[{"name":"session_id","value":"...","domain":".apollo.io"}]'])  # Cookie-Editor JSON
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])   # rows/query; None/0 = all


class OLXRequest(BaseModel):
    # one line each: an olx.* search URL (e.g. https://www.olx.ro/oferte/q-bmw/).
    queries: list[str] = Field(..., min_length=1, examples=[["https://www.olx.ro/oferte/q-bmw/"]])
    limit: Optional[int] = Field(100, ge=0, le=5000, examples=[100])   # listings/query; None/0 = all


class BookingPricesRequest(BaseModel):
    # one line each: a booking.com/hotel/<cc>/<slug>.html URL (or a bare hotel slug).
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.booking.com/hotel/tr/old-town-point-amp-spa-antalya.html"]])
    limit: Optional[int] = Field(None, ge=0, le=1000, examples=[50])   # rooms/hotel; None/0 = all


class BookingReviewsMonitorRequest(BaseModel):
    # one line each: a booking.com/hotel/<cc>/<slug>.html URL (or a bare hotel slug).
    queries: list[str] = Field(..., min_length=1,
                               examples=[["https://www.booking.com/hotel/tr/old-town-point-amp-spa-antalya.html"]])
    frequency: str = Field("weekly", examples=["weekly"])  # daily|weekly|3weeks|monthly|3months
    email: str = Field("", examples=["info@sensussoft.com"])  # where to send the report
    threshold: int = Field(3, ge=1, le=10, examples=[3])  # score (out of 10) <= threshold = negative
    limit: Optional[int] = Field(40, ge=1, le=1000, examples=[40])  # reviews/hotel per cycle


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
