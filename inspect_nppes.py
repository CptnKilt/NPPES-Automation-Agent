"""Check the provider CSV headers and sample records before a SQL import."""

import argparse
import csv
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from io import TextIOWrapper
from itertools import islice
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from download_nppes import PROVIDER_CSV


# Keys are Dev column names. Values are exact CMS CSV headers.
REFERENCE_FIELDS = {
    "NPI": "NPI",
    "EntityType": "Entity Type Code",
    "DeactivationDate": "NPI Deactivation Date",
}
DETAIL_FIELDS = {
    **REFERENCE_FIELDS,
    "ReplacementNPI": "Replacement NPI",
    "EIN": "Employer Identification Number (EIN)",
    "OrganizationName": "Provider Organization Name (Legal Business Name)",
    "LastName": "Provider Last Name (Legal Name)",
    "FirstName": "Provider First Name",
    "MiddleName": "Provider Middle Name",
    "NamePrefix": "Provider Name Prefix Text",
    "NameSuffix": "Provider Name Suffix Text",
    "Credential": "Provider Credential Text",
    "OtherOrganizationName": "Provider Other Organization Name",
    "OtherOrganizationNameType": "Provider Other Organization Name Type Code",
    "OtherLastName": "Provider Other Last Name",
    "OtherFirstName": "Provider Other First Name",
    "OtherMiddleName": "Provider Other Middle Name",
    "OtherNamePrefix": "Provider Other Name Prefix Text",
    "OtherNameSuffix": "Provider Other Name Suffix Text",
    "OtherCredential": "Provider Other Credential Text",
    "OtherLastNameType": "Provider Other Last Name Type Code",
    "MailingAddress1": "Provider First Line Business Mailing Address",
    "MailingAddress2": "Provider Second Line Business Mailing Address",
    "MailingCity": "Provider Business Mailing Address City Name",
    "MailingState": "Provider Business Mailing Address State Name",
    "MailingPostalCode": "Provider Business Mailing Address Postal Code",
    "MailingCountry": "Provider Business Mailing Address Country Code (If outside U.S.)",
    "MailingPhone": "Provider Business Mailing Address Telephone Number",
    "MailingFax": "Provider Business Mailing Address Fax Number",
    "PracticeAddress1": "Provider First Line Business Practice Location Address",
    "PracticeAddress2": "Provider Second Line Business Practice Location Address",
    "PracticeCity": "Provider Business Practice Location Address City Name",
    "PracticeState": "Provider Business Practice Location Address State Name",
    "PracticePostalCode": "Provider Business Practice Location Address Postal Code",
    "PracticeCountry": "Provider Business Practice Location Address Country Code (If outside U.S.)",
    "PracticePhone": "Provider Business Practice Location Address Telephone Number",
    "PracticeFax": "Provider Business Practice Location Address Fax Number",
    "EnumerationDate": "Provider Enumeration Date",
    "LastUpdateDate": "Last Update Date",
    "DeactivationReason": "NPI Deactivation Reason Code",
    "ReactivationDate": "NPI Reactivation Date",
    "Sex": "Provider Sex Code",
    "OfficialLastName": "Authorized Official Last Name",
    "OfficialFirstName": "Authorized Official First Name",
    "OfficialMiddleName": "Authorized Official Middle Name",
    "OfficialTitle": "Authorized Official Title or Position",
    "OfficialPhone": "Authorized Official Telephone Number",
    "IsSoleProprietor": "Is Sole Proprietor",
    "IsOrganizationSubpart": "Is Organization Subpart",
    "ParentOrganizationLBN": "Parent Organization LBN",
    "ParentOrganizationTIN": "Parent Organization TIN",
    "OfficialPrefix": "Authorized Official Name Prefix Text",
    "OfficialSuffix": "Authorized Official Name Suffix Text",
    "OfficialCredential": "Authorized Official Credential Text",
    "CertificationDate": "Certification Date",
}
for slot in range(1, 16):
    DETAIL_FIELDS.update({
        f"TaxonomyCode_{slot}": f"Healthcare Provider Taxonomy Code_{slot}",
        f"LicenseNumber_{slot}": f"Provider License Number_{slot}",
        f"LicenseState_{slot}": f"Provider License Number State Code_{slot}",
        f"PrimaryTaxonomy_{slot}": f"Healthcare Provider Primary Taxonomy Switch_{slot}",
    })

DATE_COLUMNS = (
    "EnumerationDate", "LastUpdateDate", "DeactivationDate", "ReactivationDate", "CertificationDate"
)


class ProviderRecordError(ValueError):
    def __init__(self, number: int, line: int, field: str | None, reason: str):
        self.number = number
        self.line = line
        self.field = field
        self.reason = reason
        location = f"Provider record {number:,} (CSV line {line:,})"
        super().__init__(f"{location}: {field}: {reason}" if field else f"{location}: {reason}")


@contextmanager
def open_provider_csv(path: Path):
    with ZipFile(path) as archive:
        members = [name for name in archive.namelist() if PROVIDER_CSV.fullmatch(Path(name).name)]
        if len(members) != 1:
            raise ValueError("Expected exactly one primary provider CSV in the archive.")

        with archive.open(members[0]) as source, TextIOWrapper(
            source, encoding="utf-8-sig", newline=""
        ) as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, [])
            counts = Counter(header)
            invalid_headers = [name for name in DETAIL_FIELDS.values() if counts[name] != 1]
            if invalid_headers:
                raise ValueError(f"Missing or duplicated CMS headers: {', '.join(invalid_headers)}")

            indexes = {column: header.index(name) for column, name in DETAIL_FIELDS.items()}
            yield members[0], len(header), provider_records(reader, indexes, len(header))


def provider_records(reader, indexes: dict[str, int], field_count: int):
    number = 0
    while True:
        line = reader.line_num + 1
        number += 1
        try:
            row = next(reader, None)
        except csv.Error:
            raise ProviderRecordError(number, line, None, "Malformed CSV record.") from None
        if row is None:
            return
        if len(row) != field_count:
            raise ProviderRecordError(
                number, line, None, f"Expected {field_count} fields, found {len(row)}."
            )
        npi = row[indexes["NPI"]]
        if len(npi) != 10 or not npi.isascii() or not npi.isdigit():
            raise ProviderRecordError(number, line, "NPI", "Expected ten ASCII digits.")
        entity_type = row[indexes["EntityType"]]
        if entity_type not in ("", "1", "2"):
            raise ProviderRecordError(number, line, "EntityType", "Expected 1, 2, or an empty value.")

        record = {column: row[index] or None for column, index in indexes.items()}
        record["EntityType"] = int(entity_type) if entity_type else None
        for column in DATE_COLUMNS:
            value = record[column]
            if value is not None:
                try:
                    record[column] = datetime.strptime(value, "%m/%d/%Y").date()
                except ValueError:
                    raise ProviderRecordError(number, line, column, "Expected a valid MM/DD/YYYY date.") from None
        yield record


def inspect_archive(path: Path, sample_size: int) -> None:
    with open_provider_csv(path) as (member, field_count, records):
        rows_checked = sum(1 for _ in islice(records, sample_size))

    if rows_checked == 0:
        raise ValueError("The primary provider CSV has no data records.")
    print(f"Provider CSV: {member}")
    print(f"CMS columns: {field_count}")
    print(f"Mapped source fields: {len(REFERENCE_FIELDS)} reference, {len(DETAIL_FIELDS)} details")
    print(f"Provider records checked: {rows_checked:,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Path to the downloaded CMS ZIP.")
    parser.add_argument("--sample-rows", type=int, default=10_000, help="Records to check (default: 10000).")
    args = parser.parse_args()
    if args.sample_rows < 1:
        parser.error("--sample-rows must be positive")
    try:
        inspect_archive(args.archive, args.sample_rows)
    except (OSError, BadZipFile, csv.Error, ValueError) as error:
        print(f"Could not inspect the NPPES provider CSV: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
