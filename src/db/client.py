"""Supabase client factory.

Lazy: importing this module does not require secrets. `get_client()` raises if
SUPABASE_URL or SUPABASE_KEY is unset, so test collection works without prod creds.
"""

from functools import lru_cache

from supabase import Client, create_client

from src import config


@lru_cache(maxsize=1)
def get_client() -> Client:
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in the environment.")
    return create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
