import tempfile
import unittest
from unittest.mock import patch

from server_support import IdentityFixture

from chipload.artifact import BuildIdentity, compile_shop
from chipload.evidence import RevocationSnapshot
from chipload.published import ObservationSnapshot
from examples.export_project import project
from examples.heat_treatment import AT, define_shop, observations, request


class PublishedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = IdentityFixture(directory.name)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.published = self.operations.published
        self.owner = self.identity.settings.bindings[0]

    def test_public_hidden_catalog_conditions_do_not_form_a_membership_oracle(self):
        shop = define_shop()
        catalog = shop.catalogs[0]
        hidden = catalog.model_copy(
            update={
                "items": (catalog.items[0].model_copy(update={"visibility": "private"}),),
            }
        )
        shop = shop.model_copy(update={"catalogs": (hidden, *shop.catalogs[1:])})
        artifact = compile_shop(
            shop,
            BuildIdentity(
                source_sha256=project().revision,
                sdk_sha256="a" * 64,
                dependencies_sha256="b" * 64,
                runtime="test",
            ),
        )
        snapshot = ObservationSnapshot(revision=None, updated_at=AT, observations=observations())
        with patch.object(self.published, "_snapshot", return_value=(artifact, snapshot, RevocationSnapshot())):
            accepted_guess = self.published.evaluation(shop.id, request())
            rejected_guess = self.published.evaluation(
                shop.id, request(changes={"alloy": "another-alloy"})
            )
        self.assertEqual(accepted_guess, rejected_guess)
        self.assertEqual(accepted_guess["outcome"], "unknown")
        self.assertEqual(accepted_guess["reasons"][0]["code"], "unpublished-condition")

    def test_page_cursor_is_bound_to_filter_and_revision(self):
        first = self.published.page("thermal-example", "capabilities", limit=1)
        with self.assertRaises(ValueError):
            self.published.page(
                "thermal-example", "capabilities", cursor=first["next_cursor"], query="other"
            )
        catalog = self.published.catalog("thermal-example")
        with patch.object(
            self.published, "catalog", return_value={**catalog, "public_sha256": "a" * 64}
        ):
            with self.assertRaisesRegex(ValueError, "revision changed"):
                self.published.page("thermal-example", "capabilities", cursor=first["next_cursor"])

    def test_observations_reject_unknown_resources_and_duplicate_ids(self):
        snapshot = self.published.read_observations(self.owner, "thermal-example")
        for values in (
            (snapshot.observations[0].model_copy(update={"resource": "missing"}),),
            snapshot.observations * 2,
        ):
            with self.assertRaises(ValueError):
                self.published.write_observations(
                    self.owner, "thermal-example", values, expected_revision=snapshot.revision
                )
        self.assertEqual(self.published.read_observations(self.owner, "thermal-example"), snapshot)

    def test_private_variables_and_observations_are_permission_checked(self):
        from chipload.access import Forbidden, Principal
        from chipload.query import VariableQuery

        caller = Principal(subject="unbound")
        with self.assertRaises(Forbidden):
            self.published.variables(
                "thermal-example", VariableQuery(), principal=caller, private=True
            )
        with self.assertRaises(Forbidden):
            self.published.read_observations(caller, "thermal-example")


if __name__ == "__main__":
    unittest.main()
