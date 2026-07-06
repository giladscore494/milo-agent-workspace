from postgrest import SyncFilterRequestBuilder, SyncQueryRequestBuilder

from backend.repository.postgrest_compat import install_postgrest_select_all_compatibility


def test_select_all_compatibility_is_installed_for_mutation_builders():
    install_postgrest_select_all_compatibility()

    query_builder = object.__new__(SyncQueryRequestBuilder)
    filter_builder = object.__new__(SyncFilterRequestBuilder)

    assert query_builder.select("*") is query_builder
    assert filter_builder.select("*") is filter_builder


def test_select_all_compatibility_rejects_column_projection():
    install_postgrest_select_all_compatibility()
    builder = object.__new__(SyncQueryRequestBuilder)

    try:
        builder.select("id")
    except ValueError as exc:
        assert "only supports select-all" in str(exc)
    else:
        raise AssertionError("column projection should not be silently ignored")
