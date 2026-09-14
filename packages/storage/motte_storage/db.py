"""Database boundary reserved for the PostgreSQL implementation."""
def create_engine(*args, **kwargs):
    raise NotImplementedError("database engine is not configured in the foundation slice")
