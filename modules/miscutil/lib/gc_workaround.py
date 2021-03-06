# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2017 CERN, Stanford.
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


import gc

from functools import wraps
from sys import version_info


def gcfix(f):
    """
    delegate garbage collection to the end of the decorated function for python version < 2.7
    """
    @wraps(f)
    def gcwrapper(*args, **kwargs):
        gcfixme = version_info[0] < 3 and version_info[1] < 7 \
                  and gc.isenabled()
        if gcfixme:
            gc.disable()
        retval = f(*args, **kwargs)
        if gcfixme:
            gc.enable()
        return retval
    return gcwrapper
