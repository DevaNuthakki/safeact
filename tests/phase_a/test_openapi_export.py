"""The console's committed OpenAPI schema matches the API."""

import unittest

from deploy.demo.export_openapi import TARGET, schema_text


class OpenApiExportTests(unittest.TestCase):

    def test_committed_schema_is_current(self):
        self.assertEqual(
            TARGET.read_text("utf-8"), schema_text(),
            "run: python -m deploy.demo.export_openapi",
        )

    def test_every_product_route_is_documented(self):
        import json
        paths = set(json.loads(schema_text())["paths"])
        for path in ("/auth/login", "/actions", "/actions/{operation_id}",
                     "/actions/{operation_id}/verification-requests",
                     "/proposals/{proposal_id}/decision", "/events"):
            self.assertIn(path, paths)


if __name__ == "__main__":
    unittest.main()
