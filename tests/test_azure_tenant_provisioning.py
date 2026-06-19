"""Qdrant tenant-naming tests.

The SuperAdmin manual tenant-provisioning endpoint (`POST /superadmin/tenants/
provision`) was removed in the SuperAdmin console overhaul — organizations now
self-serve via Razorpay onboarding (which calls the provisioning service
directly), and the console is view + per-call-cost + plan-upgrade only. The
endpoint-driven role/provisioning tests that used to live here went with it.

What remains is the client-scoped Qdrant collection naming, which is still live
code (the provisioning service and retrieval path both rely on it).
"""
from app.services.qdrant_service import QdrantService


def test_qdrant_collection_name_is_client_scoped():
    collection_name = QdrantService.collection_name_for_tenant("Client-ABC/123")
    assert collection_name == "tenant_client-abc_123_knowledge"


def test_qdrant_cluster_ref_comes_from_settings():
    assert QdrantService.cluster_ref()
