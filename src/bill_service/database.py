import motor.motor_asyncio
from .config import settings

client = motor.motor_asyncio.AsyncIOMotorClient(settings.mongo_uri)
database = client[settings.mongo_db_name]

bills_collection = database.get_collection("bills")
gatepasses_collection = database.get_collection("gatepasses")
deliveries_collection = database.get_collection("deliveries")
payments_collection = database.get_collection("payments")
audit_collection = database.get_collection("audit_logs")


async def ensure_indexes():
    # Bills indexes
    await bills_collection.create_index("created_at")
    await bills_collection.create_index("client_name_search")
    await bills_collection.create_index("quotation_id")

    # Gatepasses indexes
    await gatepasses_collection.create_index("gate_pass_number", unique=True)
    await gatepasses_collection.create_index("client_name_search")
    await gatepasses_collection.create_index("status")
    await gatepasses_collection.create_index("created_at")

    # Deliveries indexes
    await deliveries_collection.create_index("gate_pass_id")
    await deliveries_collection.create_index("client_name_search")
    await deliveries_collection.create_index("created_at")

    # Payments indexes
    await payments_collection.create_index("bill_id")
    await payments_collection.create_index("client_name_search")
    await payments_collection.create_index("created_at")

    # Audit logs indexes
    await audit_collection.create_index("timestamp")
    await audit_collection.create_index("user_id")
    await audit_collection.create_index("entity_id")