import unittest

from skills_workspace.acceptance import run
from skills_workspace.appeal_acceptance import run as run_appeals


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

    def test_appeal_offline_acceptance(self):
        result = run_appeals()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["conflict_detected"])
        self.assertTrue(result["cleared_assignee"])
        self.assertEqual([1, 2], result["material_versions"])
        self.assertTrue(result["lease_reclaimed"])
        self.assertEqual("decided", result["final_status"])
        self.assertTrue(result["independent_reviewer"])
        self.assertTrue(result["score_corrected"])
        self.assertTrue(result["public_anonymized"])


if __name__ == "__main__":
    unittest.main()
