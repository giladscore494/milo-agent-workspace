"""Deterministic, read-only ingestion of the Israeli vehicle register.

Catalog PR2. This package READS `data.gov.il` and writes into the durable
relations Catalog PR1 established -- `catalog_source_snapshots`,
`catalog_raw_records`, `catalog_candidate_variants` -- through the existing
lease-guarded repository methods, and nowhere else.

What it does NOT do, by construction rather than by convention:

*   it writes no canonical row. `catalog_models` and `catalog_model_variants`
    have no repository write method and `service_role` holds SELECT only on
    both, so the canonical catalog stays empty;
*   it creates no claim, no verdict and no evidence link. PR2 provenance is
    snapshot -> raw record -> candidate; evidence mapping and verdict-backed
    promotion are PR3's;
*   it registers no Tool. `projection.py` is an internal service component, not
    a `Tool`, and the production `ToolRegistry` is untouched and still empty;
*   it makes no provider or model call anywhere, and holds no credential.

Where each rule lives: `source.py` (what may be reached, and how far),
`transport.py` (the only module that can open a socket), `client.py` (the CKAN
reader and the completeness gate), `snapshot.py` (the three digests, named
apart), `vocabulary.py` (what the register's own fields mean), `normalize.py`
(one row -> one candidate identity), `ingest.py` (the lease-guarded write
order) and `projection.py` (the bounded internal read).
"""

from .client import CapturedPage, DataGovClient, ResourceCapture, ResourceMetadata
from .ingest import GovernmentCatalogIngestor, GovernmentIngestionError, IngestionReport
from .normalize import GovernmentNormalizationError, RecordReading, read_wltp_record
from .source import (CKAN_PACKAGE_ID, GOVERNMENT_SOURCE_FAMILY, QUANTITY_RESOURCE_ID,
                     WLTP_RESOURCE_ID, GovernmentSourceError)

__all__ = ["CKAN_PACKAGE_ID", "GOVERNMENT_SOURCE_FAMILY", "QUANTITY_RESOURCE_ID",
           "WLTP_RESOURCE_ID", "CapturedPage", "DataGovClient", "GovernmentCatalogIngestor",
           "GovernmentIngestionError", "GovernmentNormalizationError", "GovernmentSourceError",
           "IngestionReport", "RecordReading", "ResourceCapture", "ResourceMetadata",
           "read_wltp_record"]
