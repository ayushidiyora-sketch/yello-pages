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
offerup_results = db["offerup_results"]  # one doc per scraped OfferUp listing
s1688_results = db["s1688_results"]  # one doc per scraped 1688.com offer
craigslist_results = db["craigslist_results"]  # one doc per scraped Craigslist listing
allegro_results = db["allegro_results"]  # one doc per scraped Allegro product
immowelt_results = db["immowelt_results"]  # one doc per scraped Immowelt property listing
mobilede_results = db["mobilede_results"]  # one doc per scraped Mobile.de vehicle listing
willhaben_results = db["willhaben_results"]  # one doc per scraped Willhaben classified listing
feedbackcompany_reviews = db["feedbackcompany_reviews"]  # one doc per scraped Feedback Company review
feedbackcompany_companies = db["feedbackcompany_companies"]  # one doc per Feedback Company company profile
crunchbase_results = db["crunchbase_results"]  # one doc per scraped Crunchbase organization profile
crunchbase_search_results = db["crunchbase_search_results"]  # one doc per Crunchbase search match
zoominfo_results = db["zoominfo_results"]  # one doc per scraped ZoomInfo company profile
deliveroo_reviews = db["deliveroo_reviews"]  # one doc per scraped Deliveroo restaurant review
deliveroo_results = db["deliveroo_results"]  # one doc per scraped Deliveroo restaurant
ubereats_results = db["ubereats_results"]  # one doc per scraped Uber Eats restaurant
streeteasy_results = db["streeteasy_results"]  # one doc per scraped StreetEasy listing
bingmaps_results = db["bingmaps_results"]  # one doc per scraped Bing Maps business
ai_universal_results = db["ai_universal_results"]  # one doc per row (Universal AI-Powered Scraper, dynamic attrs)
email_finder_results = db["email_finder_results"]  # one doc per found email (Email Addresses Finder)
zillow_transactions_results = db["zillow_transactions_results"]  # one doc per Zillow agent transaction
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
emails_contacts = db["emails_contacts"]  # one doc per domain (Emails & Contacts Scraper)
leads_enrichment = db["leads_enrichment"]  # one doc per contact (Leads & Contacts Enrichment)
email_verifier = db["email_verifier"]  # one doc per email (Email Address Verifier)
company_insights = db["company_insights"]  # one doc per company (Company Insights)
phone_enricher = db["phone_enricher"]  # one doc per phone number (Phone Numbers Enricher)
phone_identity = db["phone_identity"]  # one doc per phone number (Phone Identity Finder)
similarweb = db["similarweb"]  # one doc per domain (SimilarWeb Scraper)
geocoding = db["geocoding"]  # one doc per address (Geocoding)
builtwith = db["builtwith"]  # one doc per domain (BuiltWith Scraper)
disposable_email = db["disposable_email"]  # one doc per email (Disposable Email Checker)
whitepages_addresses = db["whitepages_addresses"]  # one doc per address (Whitepages Addresses Scraper)
fastbackgroundcheck_addresses = db["fastbackgroundcheck_addresses"]  # one doc per address (Fastbackgroundcheck)
reverse_geocoding = db["reverse_geocoding"]  # one doc per coordinate (Reverse Geocoding)
domain_info = db["domain_info"]  # one doc per domain (Domain Information / WHOIS)
yahoo_search = db["yahoo_search"]  # one doc per result (Yahoo Search Scraper)
zoominfo = db["zoominfo"]  # one doc per domain (Zoominfo by Domains)
screenshoter = db["screenshoter"]  # one doc per URL (WebPage Screenshoter)
eventbrite = db["eventbrite"]  # one doc per event (Eventbrite Scraper)
meetup = db["meetup"]  # one doc per event (Meetup Scraper)
tiktok_videos = db["tiktok_videos"]  # one doc per video (TikTok Videos Scraper)
tiktok_hashtags = db["tiktok_hashtags"]  # one doc per hashtag (TikTok Hashtags Scraper)
tiktok_search = db["tiktok_search"]  # one doc per query (TikTok Search Scraper)
tiktok_comments = db["tiktok_comments"]  # one doc per video (TikTok Comments Scraper)
appstore_reviews = db["appstore_reviews"]  # one doc per review (AppStore Reviews Scraper)
asos_products = db["asos_products"]  # one doc per product (Asos Products Scraper)
waxie_products = db["waxie_products"]  # one doc per product (Waxie Products Scraper)
vistaprint_products = db["vistaprint_products"]  # one doc per product (Vistaprint Products Scraper)
otto_products = db["otto_products"]  # one doc per product (Otto Products Scraper)
newegg_products = db["newegg_products"]  # one doc per product (Newegg Products Scraper)
biggestbook_products = db["biggestbook_products"]  # one doc per product (BiggestBook Products Scraper)
cdw_products = db["cdw_products"]  # one doc per product (CDW Products Scraper)
decathlon_products = db["decathlon_products"]  # one doc per product (Decathlon Products Scraper)
uline_products = db["uline_products"]  # one doc per product (Uline Products Scraper)
menards_products = db["menards_products"]  # one doc per product (Menards Products Scraper)
target_products = db["target_products"]  # one doc per product (Target Products Scraper)
napaonline_products = db["napaonline_products"]  # one doc per product (NapaOnline Products Scraper)
groupon_products = db["groupon_products"]  # one doc per product (Groupon Products Scraper)
gemplers_products = db["gemplers_products"]  # one doc per product (Gemplers Products Scraper)
ferguson_products = db["ferguson_products"]  # one doc per product (Ferguson Products Scraper)
globalindustrial_products = db["globalindustrial_products"]  # one doc per product (GlobalIndustrial)
northerntool_products = db["northerntool_products"]  # one doc per product (Northerntool Products Scraper)
ipinfo = db["ipinfo"]  # one doc per IP (IPInfo Scraper)
appstore_search = db["appstore_search"]  # one doc per result (AppStore Search Scraper)
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
    await emails_contacts.create_index("job_id")
    await leads_enrichment.create_index("job_id")
    await email_verifier.create_index("job_id")
    await company_insights.create_index("job_id")
    await phone_enricher.create_index("job_id")
    await phone_identity.create_index("job_id")
    await similarweb.create_index("job_id")
    await geocoding.create_index("job_id")
    await builtwith.create_index("job_id")
    await disposable_email.create_index("job_id")
    await whitepages_addresses.create_index("job_id")
    await fastbackgroundcheck_addresses.create_index("job_id")
    await reverse_geocoding.create_index("job_id")
    await domain_info.create_index("job_id")
    await yahoo_search.create_index("job_id")
    await zoominfo.create_index("job_id")
    await screenshoter.create_index("job_id")
    await eventbrite.create_index("job_id")
    await meetup.create_index("job_id")
    await tiktok_videos.create_index("job_id")
    await tiktok_hashtags.create_index("job_id")
    await tiktok_search.create_index("job_id")
    await tiktok_comments.create_index("job_id")
    await appstore_reviews.create_index("job_id")
    await asos_products.create_index("job_id")
    await waxie_products.create_index("job_id")
    await vistaprint_products.create_index("job_id")
    await otto_products.create_index("job_id")
    await newegg_products.create_index("job_id")
    await biggestbook_products.create_index("job_id")
    await cdw_products.create_index("job_id")
    await decathlon_products.create_index("job_id")
    await uline_products.create_index("job_id")
    await menards_products.create_index("job_id")
    await target_products.create_index("job_id")
    await napaonline_products.create_index("job_id")
    await groupon_products.create_index("job_id")
    await gemplers_products.create_index("job_id")
    await ferguson_products.create_index("job_id")
    await globalindustrial_products.create_index("job_id")
    await northerntool_products.create_index("job_id")
    await ipinfo.create_index("job_id")
    await appstore_search.create_index("job_id")
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
    await craigslist_results.create_index("job_id")
    await allegro_results.create_index("job_id")
    await immowelt_results.create_index("job_id")
    await mobilede_results.create_index("job_id")
    await willhaben_results.create_index("job_id")
    await feedbackcompany_reviews.create_index("job_id")
    await feedbackcompany_companies.create_index("job_id")
    await crunchbase_results.create_index("job_id")
    await crunchbase_search_results.create_index("job_id")
    await zoominfo_results.create_index("job_id")
    await deliveroo_reviews.create_index("job_id")
    await deliveroo_results.create_index("job_id")
    await ubereats_results.create_index("job_id")
    await streeteasy_results.create_index("job_id")
    await bingmaps_results.create_index("job_id")
    await ai_universal_results.create_index("job_id")
    await email_finder_results.create_index("job_id")
    await zillow_transactions_results.create_index("job_id")
