# -*- coding: utf-8 -*-
##
## This file is part of Invenio.
## Copyright (C) 2014 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.
"""
JournalHint service to display
"Were you looking for a journal reference? Try: <link>"
when the request is a journal reference
"""
from invenio.websearch_services import SearchService
from invenio.config import (CFG_SITE_URL,
                            CFG_SITE_LANG)
from urllib import urlencode
import re
from cgi import escape

__plugin_version__ = "Search Service Plugin API 1.0"

re_keyword_at_beginning = re.compile(
    '^((f|fin|find)\s+)|([a-zA-Z0-9_-]+\:)',
    re.IGNORECASE
)


class JournalHintService(SearchService):

    def get_description(self, ln=CFG_SITE_LANG):
        "Return service description"
        return "Give hints on how to search the journal reference"

    def answer(self, req, user_info, of, cc,
               colls_to_search, p, f, search_units, ln):
        """
        Answer question given by context.

        Return (relevance, html_string) where relevance is integer
        from 0 to 100 indicating how relevant to the question the
        answer is (see C{CFG_WEBSEARCH_SERVICE_MAX_SERVICE_ANSWER_RELEVANCE}
        for details), and html_string being a formatted answer.
        """
        from invenio.refextract_api import search_from_reference

        if re_keyword_at_beginning.match(p):
            return (0, "")

        (field, pattern) = search_from_reference(p.decode('utf-8'))

        if field is not "journal":
            return (0, "")

        query = "journal:\"" + pattern + "\""
        query_link = CFG_SITE_URL + '/search?' + urlencode({'p': query})
        return (
            80,
            '<span class="journalhint">'
            'Were you looking for a journal reference? '
            'Try: <a href="{0}">{1}</a>'
            '</span>'
            .format(query_link, escape(query))
        )