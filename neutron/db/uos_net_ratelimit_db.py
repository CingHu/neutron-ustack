# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Nicira Networks, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Salvatore Orlando, Nicira, Inc
#

import sqlalchemy as sa

from neutron.api.v2 import attributes
from neutron.db import db_base_plugin_v2
from neutron.db import models_v2
from neutron.extensions import uos_net_ratelimits
from neutron.openstack.common import log as logging


LOG = logging.getLogger(__name__)
RATE_LIMIT = uos_net_ratelimits.RATE_LIMIT

# Modify the Router Data Model adding the enable_snat attribute
setattr(models_v2.Network, 'uos_rate_limit',
        sa.Column(sa.Integer, default=600, nullable=False))


class Uos_net_ratelimit_db_mixin(object):
    """Mixin class to add ratelimit."""

    # Register dict extend functions for ports and networks
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        attributes.NETWORKS, ['_extend_net_dict_ratelimit'])

    def _extend_net_dict_ratelimit(self, net_res, net_db):
        net_res[RATE_LIMIT] = net_db.uos_rate_limit

    def _process_uos_ratelimit_create(self, context, net_data, netdb):
        if uos_net_ratelimits.RATE_LIMIT in net_data:
            netdb.uos_rate_limit = net_data[uos_net_ratelimits.RATE_LIMIT]

    def _process_uos_ratelimit_update(self, context, net_data, netdb):
        if uos_net_ratelimits.RATE_LIMIT in net_data:
            netdb.uos_rate_limit = net_data[uos_net_ratelimits.RATE_LIMIT]
