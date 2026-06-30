"""
Zentrale Supabase-Client-Instanz. Nutzt den service_role Key, da das Backend
RLS bewusst umgeht (siehe schema.sql Kommentar) und die einzige Zugriffsschicht
auf die Datenbank ist.
"""
import os
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
