"""Guards for standalone supporting-document packaging.

A supporting document (payment receipt, non-substitute approval screenshot,
other user-kept evidence) must be packaged under the proof number of the
invoice it names, must never be silently dropped, and a substitute-invoice
approval screenshot (carried via the substitute unit's approval_file) must not
be double-handled here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import write_reimbursement_template as W  # noqa: E402
import package_reimbursement_files as P  # noqa: E402


UNITS = [
    {"source_document_id": "DOC-INV1", "supporting_invoice_document_id": "DOC-INV1",
     "reimbursable_amount": "100.00"},
    {"source_document_id": "DOC-INV2", "supporting_invoice_document_id": "DOC-INV2",
     "is_substitute_invoice": True, "approval_file": "/tmp/subap.png",
     "reimbursable_amount": "50.00"},
]


def extraction(docs):
    return {"documents": docs}


class CollectSupportDocuments(unittest.TestCase):
    def test_associated_document_mounts_with_its_type(self):
        mounted, orphans = W.collect_support_documents(extraction([
            {"document_id": "DOC-REC1", "document_role": "supporting_document",
             "source_file": "/tmp/receipt1.jpg", "support_type": "付款小票",
             "supports_document_id": "DOC-INV1"},
        ]), UNITS)
        self.assertEqual([m["document_id"] for m in mounted], ["DOC-REC1"])
        self.assertEqual(mounted[0]["support_type"], "付款小票")
        self.assertEqual(orphans, [])

    def test_unassociated_document_is_orphan(self):
        _, orphans = W.collect_support_documents(extraction([
            {"document_id": "DOC-ORPH", "document_role": "supporting_document",
             "source_file": "/tmp/orphan.png"},
        ]), UNITS)
        self.assertEqual([o["document_id"] for o in orphans], ["DOC-ORPH"])

    def test_link_to_missing_invoice_is_orphan(self):
        _, orphans = W.collect_support_documents(extraction([
            {"document_id": "DOC-BAD", "document_role": "supporting_document",
             "source_file": "/tmp/bad.png", "supports_document_id": "DOC-DOES-NOT-EXIST"},
        ]), UNITS)
        self.assertEqual([o["document_id"] for o in orphans], ["DOC-BAD"])

    def test_substitute_approval_is_neither_mounted_nor_orphan(self):
        mounted, orphans = W.collect_support_documents(extraction([
            {"document_id": "DOC-SUBAP", "document_role": "supporting_document",
             "source_file": "/tmp/subap.png"},
        ]), UNITS)
        self.assertEqual(mounted, [])
        self.assertEqual(orphans, [])

    def test_default_support_type_when_unset(self):
        mounted, _ = W.collect_support_documents(extraction([
            {"document_id": "DOC-X", "document_role": "supporting_document",
             "source_file": "/tmp/x.pdf", "supports_document_id": "DOC-INV1"},
        ]), UNITS)
        self.assertEqual(mounted[0]["support_type"], W.SUPPORT_DOC_DEFAULT_TYPE)

    def test_excluded_document_is_ignored(self):
        mounted, orphans = W.collect_support_documents(extraction([
            {"document_id": "DOC-DROP", "document_role": "supporting_document",
             "source_file": "/tmp/drop.png", "excluded_by_user": True},
        ]), UNITS)
        self.assertEqual((mounted, orphans), ([], []))


class AttachToProofGroups(unittest.TestCase):
    def test_attaches_to_the_group_of_its_invoice(self):
        groups = [
            {"proof_no": 1, "source_document_ids": ["DOC-INV1"]},
            {"proof_no": 2, "source_document_ids": ["DOC-INV2"]},
        ]
        mounted = [{"document_id": "DOC-REC1", "source_file": "/tmp/r.jpg",
                    "support_type": "付款小票", "supports_document_id": "DOC-INV1"}]
        W.attach_support_documents_to_groups(groups, mounted)
        self.assertEqual(groups[0]["support_documents"][0]["document_id"], "DOC-REC1")
        self.assertNotIn("support_documents", groups[1])


class PackagingNaming(unittest.TestCase):
    def test_support_file_shares_invoice_proof_number(self):
        inv = P.invoice_filename(
            {"proof_no": 6, "proof_type": "meal", "amount_total": "88.00"},
            {"source_file": "/tmp/inv.pdf"},
        )
        sup = P.support_filename(6, "付款小票", Path("/tmp/r.jpg"))
        self.assertEqual(inv.split("-")[0], sup.split("-")[0])
        self.assertTrue(sup.startswith(P.proof_no_name(6) + "-"))
        self.assertTrue(sup.endswith(".jpg"))

    def test_same_name_files_get_incrementing_suffix(self):
        used: set[str] = set()
        name = "006-付款小票.jpg"
        self.assertEqual(P.reserve_filename(name, used), "006-付款小票.jpg")
        self.assertEqual(P.reserve_filename(name, used), "006-付款小票-2.jpg")
        self.assertEqual(P.reserve_filename(name, used), "006-付款小票-3.jpg")


if __name__ == "__main__":
    unittest.main()
