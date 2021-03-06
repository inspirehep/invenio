# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2014, 2015, 2016, 2018 CERN.
#
# Invenio is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

'''Utils for fetching data for ORCID library.'''

import re

import orcid
import requests

from retrying import retry, RetryError
from invenio import bibtask
from invenio.access_control_config import CFG_OAUTH2_CONFIGURATIONS
from invenio.bibauthorid_backinterface import get_orcid_id_of_author
# TO DO: change name of the function
from invenio.bibauthorid_dbinterface import (delete_token, get_all_tokens,
                                             get_doi_for_paper,
                                             get_papers_of_author,
                                             get_signatures_of_paper,
                                             trigger_aidtoken_change)
from invenio.bibauthorid_general_utils import get_doi
from invenio.bibformat import format_record as bibformat_record
from invenio.bibrecord import (record_get_field_instances,
                               record_get_field_value, record_get_field_values)
from invenio.config import CFG_ORCIDUTILS_BLACKLIST_FILE, CFG_SITE_URL
from invenio.dateutils import convert_simple_date_to_array
from invenio.errorlib import register_exception
from invenio.orcid_xml_exporter import OrcidXmlExporter
from invenio.search_engine import get_record
from invenio.textutils import (RE_ALLOWED_XML_1_0_CHARS,
                               encode_for_jinja_and_xml)
from requests.exceptions import (HTTPError, RequestException,
                                 ConnectTimeout, ReadTimeout)

try:
    import json
except ImportError:
    import simplejson as json


ORCID_ENDPOINT_PUBLIC = CFG_OAUTH2_CONFIGURATIONS['orcid']['public_url']

ORCID_SINGLE_REQUEST_WORKS = 1
MAX_COAUTHORS = 25
MAX_DESCRIPTION_LENGTH = 4500

LANGUAGE_MAP = {
    "bulgarian": "bg",
    "chinese": "zh",
    "czech": "cs",
    "dutch": "nl",
    "english": "en",
    "esperanto": "eo",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hungarian": "hu",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "latin": "la",
    "norwegian": "no",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "slovak": "sk",
    "spanish": "es",
    "swedish": "sv",
    "turkish": "tr",
    "ukrainian": "uk",
}
# ############################# PULLING #######################################


def get_dois_from_orcid_using_pid(pid):
    """Get dois in case of an unknown ORCID.

    @param scope: pid
    @type scope: int

    @return: pair - ORCID of the person indicated by pid and list of his dois
    @rtype: tuple
    """
    # author should have already an orcid if this method was triggered
    try:
        orcid_id = get_orcid_id_of_author(pid)[0][0]
    except IndexError:
        register_exception(alert_admin=True)
        orcid_id = None
    return orcid_id, get_dois_from_orcid(orcid_id)


def _get_ext_orcids_using_pid(pid):
    """Get extids in case of an unknown ORCID.

    @param scope: pid
    @type scope: int

    @return: dictionary containing all external ids.
    @rtype: tuple
    """
    # author should have already an orcid if this method was triggered
    try:
        orcid_id = get_orcid_id_of_author(pid)[0][0]
    except IndexError:
        # weird, no orcid id in the database? Let's not do anything...
        register_exception(alert_admin=True)
        orcid_id = None

    return orcid_id, _get_extids_from_orcid(orcid_id)


def _get_extids_from_orcid(orcid_id):
    """Get all external ids from ORCID database for a given person.

    @param orcid_id: ORCID in format xxxx-xxxx-xxxx-xxxx
    @type orcid_id: str

    @return: a dictionary which contains all external identifiers for given
        person. Identifiers are stored in sets under identifiers names keys.
    @rtype: dictionary
    """
    ap = orcid.MemberAPI(CFG_OAUTH2_CONFIGURATIONS['orcid']['consumer_key'],
                         CFG_OAUTH2_CONFIGURATIONS['orcid']['consumer_secret'],
                         'sandbox' in
                         CFG_OAUTH2_CONFIGURATIONS['orcid']['member_url'])

    ext_ids_dict = {
        'doi': set(),
        'arxiv': set(),
        'isbn': set(),
        'other-id': set()
    }

    orcid_profile = None
    if orcid_id:
        try:
            orcid_profile = ap.read_record_public(orcid_id, 'activities')
        except RequestException:
            register_exception(alert_admin=True)

    if not orcid_profile:
        return ext_ids_dict

    try:
        for work in orcid_profile.get('works', {}).get('group', []):
            try:
                for identifier in work['identifiers']['identifier']:
                    identifier_type = identifier[
                        'external-identifier-type'].lower()
                    value = identifier['external-identifier-id']
                    if identifier_type in ext_ids_dict:
                        if identifier_type == "doi":
                            ext_ids_dict[identifier_type].add(get_doi(value))
                        ext_ids_dict[identifier_type].add(value)
            except (KeyError, AttributeError, TypeError):
                # No identifiers on this work.
                pass
    except (KeyError, AttributeError):
        # Very likely there are no works in this profile.
        pass

    return ext_ids_dict


def get_dois_from_orcid_api_v1(orcid_id, get_titles=False):
    """Get dois in case of a known ORCID.

    @param scope: orcid_id
    @type scope: string

    @param get_titles: whether to add titles to the results or not
    @type get_titles: boolean

    @return: dois of a person with ORCID indicated by orcid_id
    @rtype: list
    """
    ap = orcid.MemberAPI(CFG_OAUTH2_CONFIGURATIONS['orcid']['consumer_key'],
                         CFG_OAUTH2_CONFIGURATIONS['orcid']['consumer_secret'],
                         'sandbox' in
                         CFG_OAUTH2_CONFIGURATIONS['orcid']['member_url'])

    orcid_profile = None
    if orcid_id:
        try:
            orcid_profile = ap.read_record_public(orcid_id, 'activities')
        except RequestException:
            register_exception(alert_admin=True)

    dois = list()
    if orcid_profile is None or orcid_profile['works'] is None:
        return dois

    try:
        for work in orcid_profile['works']['group']:
            try:
                if work['identifiers'] and \
                   work['identifiers']['identifier'] is not None:
                    for identifier in work['identifiers']['identifier']:
                        identifier_type = \
                            identifier['external-identifier-type'].lower()
                        value = identifier['external-identifier-id']
                        if identifier_type == "doi":
                            doi = get_doi(value)
                            if doi:
                                current_dois = doi.split(",")
                                if not get_titles:
                                    dois.append(current_dois[0])
                                else:
                                    title = work['work-summary'][0]['title']
                                    if title:
                                        title = title['title']['value']
                                    dois.append((doi, title))
            except KeyError:
                # No identifiers on this work.
                pass
    except KeyError:
        register_exception(alert_admin=True)

    return dois


def retry_if_timeout_error(exception):
    """
    check whether it's a timeout
    """
    return isinstance(exception, ReadTimeout) \
        or isinstance(exception, ConnectTimeout)


def get_dois_from_orcid_api_v2_1(orcid_id, get_titles=False, low_level=True):
    """new api v 2.1 parsing of works"""

    if not orcid_id:
        return []

    works = None

    if low_level:
        token = CFG_OAUTH2_CONFIGURATIONS['orcid'].get('search_token', '')
        if not token:
            return []
        headers = {'Content-type': 'application/vnd.orcid+json',
                   'Authorization type': 'Bearer', 'Access token': token}

        url = 'https://pub.orcid.org/v2.1/{0}/works'.format(orcid_id)

        @retry(wrap_exception=True,
               retry_on_exception=retry_if_timeout_error,
               wait_exponential_multiplier=250,
               stop_max_attempt_number=3)
        def _get_works_from_orcid(url, headers, timeout=16):
            res = requests.get(url, headers=headers, timeout=timeout)
            if not res.status_code == requests.codes.ok:
                res.raise_for_status()
            return res

        try:
            res = _get_works_from_orcid(url, headers)
        except RetryError:
            register_exception(alert_admin=True)
            return []
        except Exception:
            register_exception(alert_admin=True)
            return []

        try:
            works = res.json()
        except ValueError:
            register_exception(alert_admin=True)
            return []
    else:
        # requires orcid.py version >= '1.0.0'
        import pkg_resources
        try:
            orcversion = pkg_resources.get_distribution("orcid").version
        except Exception:
            return []

        if orcversion and orcversion.startswith(0):
            return []

        try:
            sandbox = 'sandbox' in CFG_OAUTH2_CONFIGURATIONS['orcid']['member_url']
            ap = orcid.PublicAPI(
                institution_key=CFG_OAUTH2_CONFIGURATIONS['orcid']['consumer_key'],
                institution_secret=CFG_OAUTH2_CONFIGURATIONS['orcid']['consumer_secret'],
                sandbox=sandbox,
                timeout=60)
            token = CFG_OAUTH2_CONFIGURATIONS['orcid'].get('search_token', '')
            if not token:
                token = ap.get_search_token_from_orcid()
            works = ap.read_record_public(orcid_id, 'works', token)
        except Exception:
            register_exception(alert_admin=True)

    if not works:
        return []

    dois = []

    for work in works.get('group', {}):
        if get_titles:
            wksummary = work.get('work-summary', [])
            for summary in wksummary:
                title = ''
                titles = summary.get('title', {})
                if titles is not None:
                    title = titles.get('title', {}).get('value', '')
                eids = summary.get('external-ids', {})
                if eids is None:
                    continue
                for eid in eids.get('external-id', []):
                    if eid.get('external-id-type', '') == 'doi':
                        doi = get_doi(eid.get('external-id-value', ''))
                        if not doi:
                            continue
                        dois.append((doi, title))
        else:
            for eid in work.get('external-ids', {}).get('external-id', []):
                if eid.get('external-id-type', '') == 'doi':
                    doi = get_doi(eid.get('external-id-value', ''))
                    if not doi:
                        continue
                    dois.append(doi)

    return dois


get_dois_from_orcid = get_dois_from_orcid_api_v2_1


# ############################# PUSHING #######################################


class OrcidRecordExisting(Exception):

    """Indicates that a record is present in ORCID database."""

    pass


class DoubledIds(Exception):

    """During fetching of the papers the code encountered the same id twice."""

    pass


def push_orcid_papers(pid, token):
    """Push papers authored by chosen person.

    Pushes only the papers which were absent previously in ORCID database.

    @param pid: person id of an author.
    @type pid: int
    @param token: the token received from ORCID during authentication step.
    @type token: string
    """
    return bibtask.task_low_level_submission('orcidpush', 'admin',
                                             '--author_id=' + str(pid),
                                             '--token=' + token,
                                             '-P', '1')


def _orcid_fetch_works(pid):
    """Get claimed works - only those that are not already put into ORCID.

    @param pid: person id of an author.
    @type pid: int
    @return: a pair: person doid and
        the dictionary with works for OrcidXmlExporter.
    @rtype: tuple (str, dict)
    """
    doid, existing_papers = _get_ext_orcids_using_pid(pid)
    papers_recs = [x[3] for x in get_papers_of_author(pid,
                                                      include_unclaimed=False)]

    bibtask.write_message("Papers fetched")
    # chunks the works from orcid_list
    for orcid_small_list in _get_orcid_dictionaries(papers_recs, pid,
                                                    existing_papers, doid):

        yield (doid, orcid_small_list, len(orcid_small_list))


def _push_few_works(doid, short_list, number, pid, token):
    """Push works with fallback in case of duplicates.

    If there is a duplicate, the function will remove the problematic
    paper from the list and repush.

    @return: did the push finish successfully
    @rtype: bool
    """
    url = CFG_OAUTH2_CONFIGURATIONS['orcid']['member_url'] + \
        doid + '/orcid-works'

    xml_to_push = OrcidXmlExporter.export(short_list, 'works.xml')
    xml_to_push = RE_ALLOWED_XML_1_0_CHARS.sub('',
                                               xml_to_push).encode('utf-8')

    bibtask.write_message(
        "Pushing %s works for %s. Personid: %s" %
        (number, doid, pid))

    headers = {'Accept': 'application/vnd.orcid+xml',
               'Content-Type': 'application/vnd.orcid+xml',
               'Authorization': 'Bearer ' + token}

    response = requests.post(url, xml_to_push, headers=headers)

    code = response.status_code

    if code == 401:
        # The token has expired or the user revoke his token
        delete_token(pid)
        bibtask.write_message("Token deleted for %s" % (pid,))
        register_exception(subject="The ORCID token expired.")
        return True
    elif code != 201 and code != 200:
        try:
            response.raise_for_status()
        except HTTPError, exc:
            m = re.search(r"have the same external id \"(.*)\"", response.text)
            if m:
                bibtask.write_message("Works have the same doi: %s" %
                                      m.groups()[0])
                wrong_id = m.groups()[0]
                shorter_list = [x for x in short_list if
                                all(t[1] != wrong_id for
                                    t in x['work_external_identifiers'])]
                blacklist = {}
                try:
                    blacklist = json.loads(open(CFG_ORCIDUTILS_BLACKLIST_FILE,
                                                'r').read())
                except IOError:
                    # No such file
                    pass
                if blacklist:
                    if doid in blacklist:
                        blacklist[doid].append(wrong_id)
                    else:
                        blacklist[doid] = [wrong_id]
                    json.dump(blacklist, open(CFG_ORCIDUTILS_BLACKLIST_FILE,
                                              'w'))
                if len(shorter_list) == 0:
                    return True
                return shorter_list
            else:
                bibtask.write_message(exc)
                bibtask.write_message(response.text)
            return False
    bibtask.write_message("Pushing works succedded")
    return True


def _orcid_push_with_bibtask():
    """Push ORCID papers using bibtask scheduling.

    @return: did the task finish successfully
    @rtype: bool
    """
    pid_tokens = get_all_tokens()
    success = True

    for pid_token in pid_tokens:

        pid = pid_token[0]
        token = pid_token[1]

        if pid_token[2]:
            # pid_token[2] indicates a change in the list of claimed papers
            bibtask.write_message("Fetching works for %s" % (pid,))

            # The order of operations is important here. We need to trigger the
            # record in the database before fetching, as the claims might
            # change when the papers' data is being fetched.
            trigger_aidtoken_change(pid, 1)

            for (doid, orcid_small_list,
                 number) in _orcid_fetch_works(int(pid)):

                bibtask.write_message("Running pushing")
                result = orcid_small_list
                while isinstance(result, list):
                    result = _push_few_works(doid, result, number, pid,
                                             token)
                if not result:
                    success = False
                    break

            trigger_aidtoken_change(pid, 0)
            bibtask.write_message("Flag changed")

    return success


def _get_orcid_dictionaries(papers, personid, old_external_ids, orcid):
    """Return list of dictionaries which can be used in ORCID library.

    Yields orcid list of ORCID_SINGLE_REQUEST_WORKS works of given person.

    @param papers: list of papers' records ids.
    @type papers: list (tuple(int,))
    @param personid: personid of person who is requesting orcid dictionary of
        his works
    @type personid: int
    @param orcid: orcid of the author
    @type orcid: string
    """
    orcid_list = []

    for recid in papers:

        work_dict = {
            'work_title': {}
        }

        recstruct = get_record(recid)

        url = CFG_SITE_URL + ('/record/%d' % recid)

        try:
            external_ids = _get_external_ids(recid, url,
                                             recstruct, old_external_ids,
                                             orcid)
        except OrcidRecordExisting:
            # We will not push this record, skip it.
            continue

        # There always will be some external identifiers.
        work_dict['work_external_identifiers'] = list(external_ids)

        work_dict['work_title']['title'] = \
            encode_for_jinja_and_xml(record_get_field_value(
                recstruct, '245', '', '', 'a'))

        short_descriptions = \
            record_get_field_values(recstruct, '520', '', '', 'a')
        if short_descriptions:
            work_dict['short_description'] = encode_for_jinja_and_xml(
                short_descriptions[0])[
                    :MAX_DESCRIPTION_LENGTH
                ].rsplit(' ', 1)[0]

        journal_title = record_get_field_value(recstruct, '773', '', '', 'p')
        if journal_title:
            work_dict['journal-title'] = encode_for_jinja_and_xml(
                journal_title)

        citation = _get_citation(recid)
        if citation:
            work_dict['work_citation'] = citation

        work_dict['work_type'] = _get_work_type(recstruct)

        publication_date = _get_publication_date(recstruct)
        if publication_date:
            work_dict['publication_date'] = publication_date

        work_dict['url'] = url

        work_contributors = _get_work_contributors(recid, personid)
        if len(work_contributors) > 0:
            work_dict['work_contributors'] = work_contributors

        work_source = record_get_field_value(recstruct, '359', '', '', '9')
        if work_source:
            work_dict['work_source']['work-source'] = \
                encode_for_jinja_and_xml(work_source)

        language = record_get_field_value(recstruct, '041', '', '', 'a')
        if language:
            # If we understand the language we map it to ISO 639-2
            language = LANGUAGE_MAP.get(language.lower().strip())
            if language:
                work_dict['language_code'] = encode_for_jinja_and_xml(language)
        else:
            work_dict['language_code'] = 'en'
        work_dict['visibility'] = 'public'
        orcid_list.append(work_dict)

        bibtask.write_message("Pushing " + str(recid))

        if len(orcid_list) == ORCID_SINGLE_REQUEST_WORKS:
            bibtask.write_message("I will push " +
                                  str(ORCID_SINGLE_REQUEST_WORKS) +
                                  " records to ORCID.")
            yield orcid_list
            orcid_list = []

    if len(orcid_list) > 0:
        # empty message might be invalid
        bibtask.write_message("I will push last " + str(len(orcid_list)) +
                              " records to ORCID.")
        yield orcid_list


def _get_date_from_field_number(recstruct, field, subfield):
    """Get date dictionary from MARC record.

    The dictionary can have keys 'year', 'month' and 'day'

    @param recstruct: MARC record
    @param field: number of field inside MARC record. The function will extract
                  the date from the field indicated by this field
    @type field: string

    @return: dictionary
    @rtype: dict
    """
    result = {}

    publication_date = record_get_field_value(recstruct, field, '', '',
                                              subfield)
    publication_array = convert_simple_date_to_array(publication_date)
    if len(publication_array) > 0 and \
            re.match(r'[12]\d{3}$', publication_array[0]):
        result['year'] = publication_array[0]
        if len(publication_array) > 1 and \
                re.match(r'[01]\d$', publication_array[1]):
            result['month'] = publication_array[1]
            if len(publication_array) > 2 and \
                    re.match(r'[012]\d{3}$', publication_array[2]):
                result['day'] = publication_array[2]

        return result

    return None


def _get_publication_date(recstruct):
    """Get work publication date from MARC record.

    @param recstruct: MARC record

    @return: dictionary
    @rtype: dict
    """
    first_try = _get_date_from_field_number(recstruct, '269', 'c')
    if first_try:
        return first_try

    second_try = _get_date_from_field_number(recstruct, '260', 'c')
    if second_try:
        return second_try

    third_try = _get_date_from_field_number(recstruct, '502', 'd')
    if third_try:
        return third_try

    publication_year = record_get_field_value(recstruct, '773', '', '', 'y')
    if publication_year and re.match(r'[12]\d{3}$', publication_year):
        return {'year': publication_year}

    return {}


def _get_work_type(recstruct):
    """Get work type from MARC record.

    @param recstruct: MARC record

    @return: type of given work
    @rtype: str
    """
    work_type = record_get_field_values(recstruct, '980', '', '', 'a')
    if 'book' in [x.lower() for x in work_type]:
        return 'book'

    work_type_2 = record_get_field_values(recstruct, '502', '', '', 'b')
    if 'phd' in [x.lower() for x in work_type_2]:
        return 'dissertation'

    if 'conferencepaper' in [x.lower() for x in work_type]:
        return 'conference-paper'
    elif 'data' in [x.lower() for x in work_type]:
        return 'data-set'

    published_flag = 'published' in [x.lower() for x in work_type]
    if (published_flag and
            record_get_field_values(recstruct, '773', '', '', 'p')):
        return 'journal-article'

    work_type = record_get_field_instances(recstruct, '035')
    for instance in work_type:
        field_a = False
        field_9 = False
        for tup in instance[0]:
            if tup[0] == '9':
                field_9 = True
            elif tup[0] == 'a':
                field_a = True
        if field_a and field_9 and not published_flag:
            return 'working-paper'

    return 'other'


def _get_citation(recid):
    """Get citation in BibTeX format.

    Strips down html tags

    @param recid: the id of record
    @type recid: int

    @return: citation in BibTex format
    @rtype: string
    """
    tex_str = bibformat_record(recid, 'hx')
    bibtex_content = encode_for_jinja_and_xml(tex_str[tex_str.find('@'):
                                                      tex_str.rfind('}')+1])

    return ('bibtex', bibtex_content)


def _get_external_ids(recid, url, recstruct, old_external_ids, orcid):
    """Get external identifiers used by ORCID.

    Fetches DOI, ISBN, ARXIVID and INSPIREID identifiers.

    @param recid: the id of record
    @type recid: int
    @param url: URL of given paper. It will be used as a spare external id in
            case nothing else is achievable.
    @type url: string
    @param recstruct: the MARC record_get_field_values
    @param old_external_ids: external_ids which are already inside
            ORCID database
    @type old_external_ids: dict
    @param orcid: orcid of the currently processed author
    @type orcid: string

    @return: external ids in form of pairs: name if id, value.
    @rtype: list
    """
    def _stub_method():
        raise DoubledIds()

    blacklist = {}
    try:
        blacklist = json.loads(open(CFG_ORCIDUTILS_BLACKLIST_FILE, 'r').read())
    except IOError:
        # No such file
        pass

    external_ids = set()
    doi = get_doi_for_paper(recid, recstruct)
    # There are two different fields in MARC records responsiple for ISBN id.
    isbn = record_get_field_value(recstruct, '020', '', '', 'a')
    record_ext_ids = record_get_field_instances(recstruct, '037')
    if doi:
        for single_doi in set(doi):
            if single_doi in old_external_ids['doi'] or \
                    single_doi in blacklist.get(orcid, []):
                raise OrcidRecordExisting
            encoded = encode_for_jinja_and_xml(single_doi)
            external_ids.add(('doi', encoded))
    if isbn:
        if isbn in old_external_ids['isbn']:
            raise OrcidRecordExisting
        external_ids.add(('isbn', encode_for_jinja_and_xml(isbn)))

    for rec in record_ext_ids:
        arxiv = False
        the_id = None
        for field in rec[0]:
            if field[0] == '9' and field[1].lower() == 'arxiv':
                arxiv = True
            elif field[0] == 'a':
                the_id = field[1]
        if arxiv:
            if the_id in old_external_ids['arxiv']:
                raise OrcidRecordExisting
            encoded = encode_for_jinja_and_xml(the_id)
            if encoded in [x for (y, x) in external_ids if y == 'arxiv']:
                try:
                    _stub_method()
                except DoubledIds:
                    register_exception(subject="The paper %s contains the"
                                       " same arXiv id %s twice."
                                       % (recid, encoded),
                                       alert_admin=True)
            else:
                external_ids.add(('arxiv', encoded))

    if url in old_external_ids['other-id']:
        raise OrcidRecordExisting
    if len(external_ids) == 0:
        external_ids.add(('other-id', url))
    return external_ids


def _get_work_contributors(recid, personid):
    """Get contributors data used by ORCID.

    @param recid: the id of record
    @type recid: int
    @param personid: the id of author
    @type personid: int

    @return: contributors records with fields required by ORCID database.
    @rtype: list
    """
    work_contributors = []
    signatures = get_signatures_of_paper(recid)

    if len(signatures) <= MAX_COAUTHORS:
        # The ORCID servers can't process INSPIRE collaborations' papers

        for sig in signatures:

            if sig[0] == personid:
                # The author himself is not a contributor for his work
                continue

            contributor_dict = {
                'name': encode_for_jinja_and_xml(sig[4]),
                'attributes': {'role': 'author'}
            }

            work_contributors.append(contributor_dict)

    return work_contributors


def main():
    """Daemon responsible for pushing papers to ORCID."""
    bibtask.task_init(
        authorization_action="orcidpush",
        task_run_fnc=_orcid_push_with_bibtask
    )
