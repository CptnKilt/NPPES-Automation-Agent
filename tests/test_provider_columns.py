"""Regression checks for the complete provider CSV mapping."""

import csv
import io
import unittest

from inspect_nppes import ADDITIONAL_DETAIL_COLUMNS, DETAIL_FIELDS, ProviderRecordError, provider_records


class ProviderColumnsTests(unittest.TestCase):
    def test_all_repeated_fields_are_mapped(self):
        self.assertEqual(len(DETAIL_FIELDS), 330)
        self.assertEqual(len(set(DETAIL_FIELDS.values())), 330)
        self.assertEqual(len(ADDITIONAL_DETAIL_COLUMNS), 215)
        for slot in range(1, 51):
            self.assertEqual(
                DETAIL_FIELDS[f"OtherProviderIdentifierIssuer_{slot}"],
                f"Other Provider Identifier Issuer_{slot}",
            )
        for slot in range(1, 16):
            self.assertEqual(
                DETAIL_FIELDS[f"TaxonomyGroup_{slot}"], f"Healthcare Provider Taxonomy Group_{slot}"
            )

    def records(self, changes):
        values = dict.fromkeys(DETAIL_FIELDS, "")
        values.update(NPI="1234567890", EntityType="1", **changes)
        output = io.StringIO(newline="")
        csv.writer(output).writerow(values.values())
        output.seek(0)
        indexes = {name: position for position, name in enumerate(DETAIL_FIELDS)}
        return provider_records(csv.reader(output, strict=True), indexes, len(indexes))

    def test_codes_and_leading_zeroes_are_preserved(self):
        record = next(self.records({
            "OtherProviderIdentifier_1": "0000123",
            "OtherProviderIdentifierType_1": "01",
            "OtherProviderIdentifierIssuer_50": "Issuer",
            "TaxonomyGroup_15": "Group",
        }))
        self.assertEqual(record["OtherProviderIdentifier_1"], "0000123")
        self.assertEqual(record["OtherProviderIdentifierType_1"], "01")
        self.assertEqual(record["OtherProviderIdentifierIssuer_50"], "Issuer")
        self.assertEqual(record["TaxonomyGroup_15"], "Group")
        self.assertIsNone(record["OtherProviderIdentifier_50"])

    def test_utf16_column_limits_generate_record_error(self):
        name = "OtherProviderIdentifierType_1"
        self.assertEqual(next(self.records({name: "01"}))[name], "01")
        with self.assertRaises(ProviderRecordError) as caught:
            next(self.records({name: "123"}))
        self.assertEqual(caught.exception.field, name)
        self.assertEqual(caught.exception.number, 1)
        # One non-BMP character uses two UTF-16 units.
        self.assertEqual(next(self.records({name: chr(0x1F600)}))[name], chr(0x1F600))
        with self.assertRaises(ProviderRecordError):
            next(self.records({name: chr(0x1F600) + "x"}))


if __name__ == "__main__":
    unittest.main()
