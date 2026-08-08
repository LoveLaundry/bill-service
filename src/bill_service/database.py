import motor.motor_asyncio
from .config import settings

client = motor.motor_asyncio.AsyncIOMotorClient(settings.mongo_uri)
database = client[settings.mongo_db_name]
bills_collection = database.get_collection("bills")


async def ensure_indexes():
    await bills_collection.create_index("created_at")
    await bills_collection.create_index("client_name")
    await bills_collection.create_index("quotation_id")