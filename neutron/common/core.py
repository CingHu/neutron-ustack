# Copyright 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""SQL backends for the various services.

Before using this module, call initialize(). This has to be done before
CONF() because it sets up configuration options.

"""
import json

import sqlalchemy as sql
from sqlalchemy import types as sql_types

from neutron.openstack.common import jsonutils



# Special Fields
class JsonBlob(sql_types.TypeDecorator):

    impl = sql.BLOB

    def process_bind_param(self, value, dialect):
        return value.encode("base64")

    def process_result_value(self, value, dialect):
        return value.decode("base64")

# Special Fields
class JsonCom(sql_types.TypeDecorator):

    impl = sql.BLOB

    def process_bind_param(self, value, dialect):
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value:
            return json.loads(value)

# Special Fields
class Base64Blob(sql_types.TypeDecorator):

    impl = sql.BLOB

    def process_bind_param(self, value, dialect):
        return value.encode("base64")

    def process_result_value(self, value, dialect):
        #return value.decode("base64")
        return value


