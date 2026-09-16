"""Deterministic, read-only reading of the Israeli vehicle register.

Catalog PR2 built the capture; Catalog PR3 connected it. This package READS
`data.gov.il` and writes into the durable relations Catalog PR1 established --
`catalog_source_snapshots`, `catalog_raw_records`, `catalog_candidate_variants`
-- through the existing lease-guarded repository methods, and nowhere else.

What it does NOT do, by construction rather than by convention:

*   it makes no provider or model call anywhere, and holds no credential;
*   it opens no socket outside `transport.py`, and no production entrypoint
    constructs one, so no release can reach the network on this path;
*   it is not itself a Tool. The `Tool` protocol lives in
    `backend/tools/government_vehicle.py`, which wraps the bounded query layer
    here; `projection.py` and `query.py` are plain service components with no
    operations mapping, no schema and no required scope;
*   it promotes nothing on its own. Evidence mapping states facts, the R4
    Verifier decides verdicts, and a canonical row needs a verified verdict per
    promoted FIELD -- see `backend/catalog/promotion.py`.

Where each rule lives: `source.py` (what may be reached, and how far),
`transport.py` (the only module that can open a socket), `client.py` (the CKAN
reader and the completeness gate), `snapshot.py` (the three digests, named
apart), `vocabulary.py` (what the register's own fields mean), `normalize.py`
(one row -> one candidate identity), `ingest.py` (the lease-guarded write
order), `projection.py` (the bounded in-Python read), `query.py` (the bounded
database-side read a whole resource needs), `evidence.py` (the ONE trusted
mapping from a tool result into R3/R4 evidence), `reconcile.py` (the
Government <-> legacy comparison) and `refresh.py` (`sync_if_changed` and the
bounded diff -- a service operation, never a schedule).
"""

from .client import CapturedPage, DataGovClient, ResourceCapture, ResourceMetadata
from .evidence import GovernmentVariantEvidenceMapper
from .ingest import GovernmentCatalogIngestor, GovernmentIngestionError, IngestionReport
from .normalize import GovernmentNormalizationError, RecordReading, read_wltp_record
from .query import GovernmentCatalogQuery
from .reconcile import (AliasRule, CatalogGap, CatalogMatch, ReconciliationReport,
                        reconcile_catalog, variants_from_candidate_rows)
from .refresh import GovernmentCatalogRefresh, RefreshOutcome, SnapshotDiff
from .source import (CKAN_PACKAGE_ID, GOVERNMENT_SOURCE_FAMILY, QUANTITY_RESOURCE_ID,
                     WLTP_RESOURCE_ID, GovernmentSourceError)

__all__ = ["CKAN_PACKAGE_ID", "GOVERNMENT_SOURCE_FAMILY", "QUANTITY_RESOURCE_ID",
           "WLTP_RESOURCE_ID", "AliasRule", "CapturedPage", "CatalogGap", "CatalogMatch",
           "DataGovClient", "GovernmentCatalogIngestor", "GovernmentCatalogQuery",
           "GovernmentCatalogRefresh", "GovernmentIngestionError",
           "GovernmentNormalizationError", "GovernmentSourceError",
           "GovernmentVariantEvidenceMapper", "IngestionReport", "ReconciliationReport",
           "RecordReading", "RefreshOutcome", "ResourceCapture", "ResourceMetadata",
           "SnapshotDiff", "read_wltp_record", "reconcile_catalog",
           "variants_from_candidate_rows"]
