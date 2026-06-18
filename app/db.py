from motor.motor_asyncio import AsyncIOMotorClient
from .config import settings

client = AsyncIOMotorClient(settings.MONGO_URI)
db = client[settings.MONGO_DB]

jobs = db["jobs"]              # one doc per scrape run
businesses = db["businesses"]  # one doc per scraped listing (Yellow Pages)
products = db["products"]      # one doc per scraped Amazon product
gresults = db["gresults"]      # one doc per Google/DDG search result row
bbbresults = db["bbbresults"]  # one doc per BBB business result row


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
    await gresults.create_index("job_id")
    await bbbresults.create_index("job_id")
