from .postgrest_compat import install_postgrest_select_all_compatibility

install_postgrest_select_all_compatibility()

from .supabase import Repository, SupabaseRepository

__all__ = ["Repository", "SupabaseRepository"]
