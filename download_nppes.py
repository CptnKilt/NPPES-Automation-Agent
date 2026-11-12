"""Find and optionally download the monthly CMS NPPES Version 2 archive."""

import argparse
import re
import shutil
import sys
from calendar import month_name
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError


CMS_INDEX = "https://download.cms.gov/nppes/NPI_Files.html"
DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
MONTHS = {name: number for number, name in enumerate(month_name) if number}
MONTHLY_ARCHIVE = re.compile(
    r"/nppes/NPPES_Data_Dissemination_([A-Za-z]+)_([0-9]{4})_V2\.zip"
)
PROVIDER_CSV = re.compile(r"npidata_pfile_[0-9]{8}-[0-9]{8}\.csv", re.IGNORECASE)


def check_cms_url(url: str, expected_path: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "download.cms.gov" or parts.path != expected_path:
        raise ValueError("Blocked a destination outside the selected CMS listing or archive.")


class CMSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_cms_url(newurl, urlsplit(req.full_url).path)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_cms(url: str, timeout: int):
    return build_opener(CMSRedirectHandler()).open(url, timeout=timeout)


class ArchiveLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.releases: dict[tuple[int, int], str] = {}
        self.blocked_releases: set[tuple[int, int]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return

        url = urljoin(CMS_INDEX, href)
        parts = urlsplit(url)
        match = MONTHLY_ARCHIVE.fullmatch("/nppes/" + parts.path.rsplit("/", 1)[-1])
        if match is not None and match[1] in MONTHS:
            period = int(match[2]), MONTHS[match[1]]
            try:
                check_cms_url(url, match[0])
            except ValueError:
                self.blocked_releases.add(period)
            else:
                self.releases[period] = parts._replace(fragment="").geturl()


def fetch_archive_links() -> ArchiveLinks:
    with open_cms(CMS_INDEX, timeout=60) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        page = response.read().decode(encoding)
    links = ArchiveLinks()
    links.feed(page)
    links.close()
    return links


def is_valid_archive(path: Path) -> bool:
    try:
        with ZipFile(path) as archive:
            has_provider_data = any(
                PROVIDER_CSV.fullmatch(Path(member.filename).name) and member.file_size > 0
                for member in archive.infolist()
            )
            return has_provider_data and archive.testzip() is None
    except (BadZipFile, EOFError, RuntimeError, ZlibError):
        return False


def download_archive(url: str, directory: Path) -> Path:
    path = urlsplit(url).path
    if MONTHLY_ARCHIVE.fullmatch(path) is None:
        raise ValueError("Expected a CMS monthly Version 2 archive URL.")
    check_cms_url(url, path)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / Path(path).name
    if destination.is_file() and is_valid_archive(destination):
        print(f"Already downloaded and validated: {destination}")
        return destination

    print(f"Downloading {destination.name}...", flush=True)
    with NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as partial:
        partial_path = Path(partial.name)
        try:
            with open_cms(url, timeout=60) as response:
                shutil.copyfileobj(response, partial, length=1024 * 1024)
            partial.close()
            if not is_valid_archive(partial_path):
                raise ValueError("The ZIP failed its integrity check or lacks the provider CSV.")
            partial_path.replace(destination)
        finally:
            partial.close()
            partial_path.unlink(missing_ok=True)

    print(f"Downloaded and validated: {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download and validate the ZIP.")
    args = parser.parse_args()

    try:
        links = fetch_archive_links()
    except (OSError, HTTPException, ValueError, LookupError) as error:
        print(f"Could not read the CMS NPPES download index: {error}", file=sys.stderr)
        return 1

    if not links.releases:
        print("No monthly NPPES Version 2 full replacement ZIP found on the CMS index.", file=sys.stderr)
        return 1

    latest = max(links.releases)
    year, month = latest
    print(f"Latest monthly NPPES Version 2 release: {month_name[month]} {year}")
    url = links.releases[latest]
    print(url)
    if args.download:
        try:
            download_archive(url, DOWNLOAD_DIR)
        except (OSError, HTTPException, ValueError) as error:
            print(f"Could not download the monthly NPPES archive: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
