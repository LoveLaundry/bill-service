"""The live revocation check must find accounts in user-service's database."""
from bill_service import auth_helper
from bill_service import config


class _Collection:
    def __init__(self, db_name: str):
        self.db_name = db_name

    def find_one(self, query, projection):
        if self.db_name == "users_db" and query.get("auth_id") == "only-in-users-db":
            return {"auth_id": "only-in-users-db", "status": "ACTIVE"}
        return None


class _Database:
    def __init__(self, name: str):
        self.name = name

    def __getitem__(self, _collection):
        return _Collection(self.name)


class _Client:
    def __getitem__(self, db_name):
        return _Database(db_name)


class _Settings:
    def resolve_main_uri(self):
        return "mongodb://localhost"

    def resolve_main_db(self):
        return "laundry_management"


def test_fresh_user_falls_back_to_users_db(monkeypatch):
    """A user created by user-service must not be reported as deleted."""
    monkeypatch.setattr(auth_helper, "_users_client", _Client())
    monkeypatch.setattr(auth_helper, "USER_SERVICE_DB_NAME", "laundry_management")
    monkeypatch.setattr(config, "settings", _Settings())

    user = auth_helper._fresh_user("only-in-users-db")

    assert user["auth_id"] == "only-in-users-db"
