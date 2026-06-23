from motor.motor_asyncio import AsyncIOMotorClient
from .config import settings

client = AsyncIOMotorClient(settings.MONGO_URI)
db = client[settings.MONGO_DB]

jobs = db["jobs"]              # one doc per scrape run
businesses = db["businesses"]  # one doc per scraped listing (Yellow Pages)
products = db["products"]      # one doc per scraped Amazon product
reviews = db["reviews"]        # one doc per scraped Amazon review
ebay_products = db["ebay_products"]  # one doc per scraped eBay product
gresults = db["gresults"]      # one doc per Google/DDG search result row
bbbresults = db["bbbresults"]  # one doc per BBB business result row
g2reviews = db["g2reviews"]    # one doc per scraped G2 product review
gjobs = db["gjobs"]            # one doc per scraped Glassdoor job
gdreviews = db["gdreviews"]    # one doc per scraped Glassdoor company review
walmart_products = db["walmart_products"]  # one doc per scraped Walmart product
walmart_reviews = db["walmart_reviews"]    # one doc per scraped Walmart product review
youtube_channels = db["youtube_channels"]  # one doc per scraped YouTube channel
airbnb_reviews = db["airbnb_reviews"]      # one doc per scraped Airbnb listing review
bbbreviews = db["bbbreviews"]  # one doc per BBB customer review
expedia_results = db["expedia_results"]  # one doc per Expedia hotel result
trustpilot_results = db["trustpilot_results"]  # one doc per Trustpilot company
trustpilot_search_results = db["trustpilot_search_results"]  # one doc per Trustpilot search hit
trustpilot_reviews = db["trustpilot_reviews"]  # one doc per Trustpilot customer review
monitors = db["monitors"]  # one doc per Trustpilot Reviews Monitoring config (recurring)
hotels_results = db["hotels_results"]  # one doc per hotels.com hotel result
hotels_reviews = db["hotels_reviews"]  # one doc per hotels.com guest review
homedepot_results = db["homedepot_results"]  # one doc per Home Depot product
expedia_reviews = db["expedia_reviews"]  # one doc per expedia.com guest review
airbnb_search_results = db["airbnb_search_results"]  # one doc per airbnb.com search listing
gmaps_results = db["gmaps_results"]  # one doc per Google Maps place (Places API)
gmaps_domain_results = db["gmaps_domain_results"]  # one doc per place found by domain
gmaps_reviews = db["gmaps_reviews"]  # one doc per Google Maps place review
gnews_results = db["gnews_results"]  # one doc per Google News article (Search News Scraper)
gimages_results = db["gimages_results"]  # one doc per image result (Search Images Scraper)
gvideos_results = db["gvideos_results"]  # one doc per video result (Search Videos Scraper)
gmaps_contrib_reviews = db["gmaps_contrib_reviews"]  # one doc per contributor's review (Contributor Reviews)
gmaps_photos = db["gmaps_photos"]  # one doc per place photo URL (Google Maps Photos Scraper)
gmaps_traffic = db["gmaps_traffic"]  # one doc per route sample (Google Maps Traffic Scraper)
gmaps_directory = db["gmaps_directory"]  # one doc per place (Google Maps Directory Places)


async def ensure_indexes():
    await businesses.create_index("job_id")
    # avoid duplicate same listing within a single job
    await businesses.create_index(
        [("job_id", 1), ("name", 1), ("phone", 1)], unique=True
    )
    await jobs.create_index("job_id", unique=True)
    # Amazon products: fetch-by-job + de-dupe the same ASIN within one job
    await products.create_index("job_id")
    await products.create_index([("job_id", 1), ("asin", 1)])
    # Amazon reviews: fetch-by-job + de-dupe the same review within one job
    await reviews.create_index("job_id")
    await reviews.create_index([("job_id", 1), ("review_id", 1)])
    # eBay products: fetch-by-job + de-dupe the same item within one job
    await ebay_products.create_index("job_id")
    await ebay_products.create_index([("job_id", 1), ("item_id", 1)])
    await gresults.create_index("job_id")
    await bbbresults.create_index("job_id")
    await g2reviews.create_index("job_id")
    await gjobs.create_index("job_id")
    await gdreviews.create_index("job_id")
    await walmart_products.create_index("job_id")
    await walmart_reviews.create_index("job_id")
    await youtube_channels.create_index("job_id")
    await airbnb_reviews.create_index("job_id")
    await bbbreviews.create_index("job_id")
    await expedia_results.create_index("job_id")
    await trustpilot_results.create_index("job_id")
    await trustpilot_search_results.create_index("job_id")
    await trustpilot_reviews.create_index("job_id")
    await monitors.create_index("monitor_id", unique=True)
    await hotels_results.create_index("job_id")
    await hotels_reviews.create_index("job_id")
    await homedepot_results.create_index("job_id")
    await expedia_reviews.create_index("job_id")
    await airbnb_search_results.create_index("job_id")
    await gmaps_results.create_index("job_id")
    await gmaps_domain_results.create_index("job_id")
    await gmaps_reviews.create_index("job_id")
    await gnews_results.create_index("job_id")
    await gimages_results.create_index("job_id")
    await gvideos_results.create_index("job_id")
    await gmaps_contrib_reviews.create_index("job_id")
    await gmaps_photos.create_index("job_id")
    await gmaps_traffic.create_index("job_id")
    await gmaps_directory.create_index("job_id")
