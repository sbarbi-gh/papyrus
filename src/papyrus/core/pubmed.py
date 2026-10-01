import json
import os
import re as _re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

RATE_NO_KEY = 3
RATE_WITH_KEY = 10
DEFAULT_RETMAX = 1000


def _api_key() -> str | None:
    return os.environ.get("NCBI_API_KEY")


def _rates() -> tuple[float, int]:
    key = _api_key()
    if key:
        return 1.0 / RATE_WITH_KEY, RATE_WITH_KEY
    return 1.0 / RATE_NO_KEY, RATE_NO_KEY


class _RateLimiter:
    def __init__(self):
        self._last_call = 0.0

    def wait(self):
        # Resolve the interval on every call: .env is loaded after imports
        # (cli.py _load_dotenv_for_kb runs in main()), so an interval captured
        # at import time always sees the no-key rate (issue #3-F).
        interval, _ = _rates()
        now = time.monotonic()
        since = now - self._last_call
        if since < interval:
            time.sleep(interval - since)
        self._last_call = time.monotonic()


_limiter = _RateLimiter()


def _get(path: str, params: dict[str, str], max_retries: int = 3) -> httpx.Response:
    key = _api_key()
    if key:
        params["api_key"] = key
    url = f"{NCBI_BASE}/{path}"
    last_exc = None
    for attempt in range(max_retries):
        _limiter.wait()
        try:
            resp = httpx.get(url, params=params, timeout=30.0)
            resp.raise_for_status()
            return resp
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


def _safe_json(text: str) -> dict | list:
    text = _re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)
    return json.loads(text)


def esearch(term: str, retmax: int = DEFAULT_RETMAX) -> list[str]:
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(retmax),
        "usehistory": "n",
        "sort": "relevance",
    }
    resp = _get("esearch.fcgi", params)
    data = _safe_json(resp.text)
    return data.get("esearchresult", {}).get("idlist", [])


def efetch(pmids: list[str]) -> list[dict[str, Any]]:
    if not pmids:
        return []
    id_str = ",".join(pmids)
    params = {
        "db": "pubmed",
        "id": id_str,
        "rettype": "abstract",
        "retmode": "xml",
    }
    resp = _get("efetch.fcgi", params)
    return _parse_efetch_xml(resp.text)


def elink_neighbors(pmid: str) -> list[str]:
    params = {
        "dbfrom": "pubmed",
        "db": "pubmed",
        "cmd": "neighbor",
        "id": pmid,
        "retmode": "json",
    }
    resp = _get("elink.fcgi", params)
    data = _safe_json(resp.text)
    linksets = data.get("linksets", [])
    if not linksets:
        return []
    neighbor_ids = linksets[0].get("linksetdbs", [{}])[0].get("links", [])
    return neighbor_ids


def _parse_efetch_xml(xml_str: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_str)
    articles = []
    for pa in root.findall(".//PubmedArticle"):
        article = _parse_pubmed_article(pa)
        if article:
            articles.append(article)
    return articles


def _parse_pubmed_article(pa: ET.Element) -> dict[str, Any] | None:
    med = pa.find(".//MedlineCitation")
    if med is None:
        return None

    pmid_el = med.find("PMID")
    if pmid_el is None or pmid_el.text is None:
        return None
    pmid = pmid_el.text.strip()

    article_el = med.find("Article")
    if article_el is None:
        return None

    title_el = article_el.find("ArticleTitle")
    title = title_el.text.strip() if title_el is not None and title_el.text else ""

    abstract_parts = []
    abstract_el = article_el.find("Abstract")
    if abstract_el is not None:
        for at in abstract_el.findall("AbstractText"):
            label = at.get("Label", "")
            text = " ".join(at.itertext()).strip()
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
    abstract = "\n".join(abstract_parts)

    authors = []
    author_list = article_el.find("AuthorList")
    if author_list is not None:
        for auth in author_list.findall("Author"):
            last = auth.find("LastName")
            fore = auth.find("ForeName")
            given = fore.text.strip() if fore is not None and fore.text else ""
            family = last.text.strip() if last is not None and last.text else ""
            if family or given:
                authors.append({"family": family, "given": given})

    journal_el = article_el.find("Journal")
    journal_title = ""
    journal_abbrev = ""
    issn_list: list[str] = []
    volume = ""
    issue = ""
    year = None
    if journal_el is not None:
        jt = journal_el.find("Title")
        if jt is not None and jt.text:
            journal_title = jt.text.strip()
        ja = journal_el.find("ISOAbbreviation")
        if ja is not None and ja.text:
            journal_abbrev = ja.text.strip()
        for issn_el in journal_el.findall("ISSN"):
            if issn_el.text and issn_el.text.strip():
                issn_list.append(issn_el.text.strip())
        ji = journal_el.find("JournalIssue")
        if ji is not None:
            vol_el = ji.find("Volume")
            if vol_el is not None and vol_el.text:
                volume = vol_el.text.strip()
            iss_el = ji.find("Issue")
            if iss_el is not None and iss_el.text:
                issue = iss_el.text.strip()
            pd = ji.find("PubDate")
            if pd is not None:
                yr = pd.find("Year")
                if yr is not None and yr.text:
                    year = yr.text.strip()

    # Pagination: prefer MedlinePgn (e.g. "10770-5"), else StartPage-EndPage.
    pages = ""
    pag_el = article_el.find("Pagination")
    if pag_el is not None:
        mpg = pag_el.find("MedlinePgn")
        if mpg is not None and mpg.text and mpg.text.strip():
            pages = mpg.text.strip()
        else:
            sp = pag_el.find("StartPage")
            ep = pag_el.find("EndPage")
            start = sp.text.strip() if sp is not None and sp.text else ""
            end = ep.text.strip() if ep is not None and ep.text else ""
            if start and end:
                pages = f"{start}-{end}"
            elif start:
                pages = start

    doi = ""
    pubmed_data = pa.find(".//PubmedData")
    if pubmed_data is not None:
        aid_list = pubmed_data.find("ArticleIdList")
        if aid_list is not None:
            for aid in aid_list.findall("ArticleId"):
                if aid.get("IdType") == "doi" and aid.text:
                    doi = aid.text.strip()

    references = []
    ref_list = pubmed_data.find("ReferenceList") if pubmed_data is not None else None
    if ref_list is not None:
        for ref in ref_list.findall("Reference"):
            ref_aid_list = ref.find("ArticleIdList")
            if ref_aid_list is not None:
                for raid in ref_aid_list.findall("ArticleId"):
                    if raid.get("IdType") == "pubmed" and raid.text:
                        references.append(raid.text.strip())

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "journal": journal_title,
        "journal_abbrev": journal_abbrev,
        "issn": issn_list,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "year": year,
        "doi": doi,
        "references": references,
    }
