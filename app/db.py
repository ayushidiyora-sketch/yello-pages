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
ai_results = db["ai_results"]  # one doc per AI Scraper extracted row (Claude, dynamic schema)
kununu_reviews = db["kununu_reviews"]  # one doc per scraped kununu employer review
producthunt_profiles = db["producthunt_profiles"]  # one doc per scraped Product Hunt profile
thuisbezorgd_reviews = db["thuisbezorgd_reviews"]  # one doc per scraped Thuisbezorgd restaurant review
feefo_reviews = db["feefo_reviews"]  # one doc per scraped Feefo merchant review
angi_results = db["angi_results"]  # one doc per scraped Angi company listing
gjobs = db["gjobs"]            # one doc per scraped Glassdoor job
gcompanies = db["gcompanies"]  # one doc per Glassdoor company (Company Search)
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
gsjobs_results = db["gsjobs_results"]  # one doc per job listing (Search Jobs Scraper)
gshop_results = db["gshop_results"]  # one doc per product (Search Shopping Scraper)
gsreviews_results = db["gsreviews_results"]  # one doc per product review (Shopping Reviews Scraper)
linkedin_profiles = db["linkedin_profiles"]  # one doc per LinkedIn profile (Profiles Scraper)
gflights_results = db["gflights_results"]  # one doc per flight (Google Search Flights Scraper)
gmaps_autocomplete = db["gmaps_autocomplete"]  # one doc per suggestion (Maps Autocomplete)
booking_results = db["booking_results"]  # one doc per property (Booking Search Scraper)
bestbuy_results = db["bestbuy_results"]  # one doc per product (BestBuy Products Scraper)
yelp_results = db["yelp_results"]  # one doc per business (Yelp Businesses Scraper)
yelp_reviews = db["yelp_reviews"]  # one doc per review (Yelp Reviews Scraper)
yelp_photos = db["yelp_photos"]  # one doc per photo (Yelp Photos Scraper)
yt_transcripts = db["yt_transcripts"]  # one doc per video transcript (YouTube Transcripts Scraper)
yt_search = db["yt_search"]  # one doc per video (YouTube Search Scraper)
gsearch_autocomplete = db["gsearch_autocomplete"]  # one doc per suggestion (Search Autocomplete)
gplay_results = db["gplay_results"]  # one doc per Google Play app review (Play Reviews Scraper)
gmaps_contrib_reviews = db["gmaps_contrib_reviews"]  # one doc per contributor's review (Contributor Reviews)
gmaps_photos = db["gmaps_photos"]  # one doc per place photo URL (Google Maps Photos Scraper)
gmaps_traffic = db["gmaps_traffic"]  # one doc per route sample (Google Maps Traffic Scraper)
gmaps_directory = db["gmaps_directory"]  # one doc per place (Google Maps Directory Places)
gevents_results = db["gevents_results"]  # one doc per event result (Google Search Events Scraper)
gcareers_results = db["gcareers_results"]  # one doc per Google Careers job (Google Search Careers)
gtrends_results = db["gtrends_results"]  # one doc per region/term interest value (Google Trends)
linkedin_companies_results = db["linkedin_companies_results"]  # one doc per LinkedIn company
linkedin_posts_results = db["linkedin_posts_results"]  # one doc per LinkedIn company post
booking_reviews_results = db["booking_reviews_results"]  # one doc per Booking.com hotel review
booking_prices_results = db["booking_prices_results"]  # one doc per Booking.com room price
olx_results = db["olx_results"]  # one doc per OLX listing (OLX Scraper)
apollo_results = db["apollo_results"]  # one doc per Apollo person/company (Apollo Scraper)
upwork_results = db["upwork_results"]  # one doc per Upwork job listing (Upwork Jobs Scraper)
youtube_videos_results = db["youtube_videos_results"]  # one doc per video/short (YouTube Video Scraper)
glassdoor_company_jobs_results = db["glassdoor_company_jobs_results"]  # one doc per job (Glassdoor Company Jobs)


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
    await gcompanies.create_index("job_id")
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
    await gsjobs_results.create_index("job_id")
    await gshop_results.create_index("job_id")
    await gsreviews_results.create_index("job_id")
    await linkedin_profiles.create_index("job_id")
    await gflights_results.create_index("job_id")
    await gmaps_autocomplete.create_index("job_id")
    await gsearch_autocomplete.create_index("job_id")
    await booking_results.create_index("job_id")
    await bestbuy_results.create_index("job_id")
    await yelp_results.create_index("job_id")
    await yelp_reviews.create_index("job_id")
    await yelp_photos.create_index("job_id")
    await yt_transcripts.create_index("job_id")
    await yt_search.create_index("job_id")
    await gplay_results.create_index("job_id")
    await gmaps_contrib_reviews.create_index("job_id")
    await gmaps_photos.create_index("job_id")
    await gmaps_traffic.create_index("job_id")
    await gmaps_directory.create_index("job_id")
    await gevents_results.create_index("job_id")
    await gcareers_results.create_index("job_id")
    await gtrends_results.create_index("job_id")
    await linkedin_companies_results.create_index("job_id")
    await linkedin_posts_results.create_index("job_id")
    await booking_reviews_results.create_index("job_id")
    await booking_prices_results.create_index("job_id")
    await olx_results.create_index("job_id")
    await apollo_results.create_index("job_id")
    await upwork_results.create_index("job_id")
    await youtube_videos_results.create_index("job_id")
    await glassdoor_company_jobs_results.create_index("job_id")
