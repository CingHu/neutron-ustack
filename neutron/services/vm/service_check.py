# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2014 Unitedstack Inc.
# All rights reserved.
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
# @author: cing, UnitedStack, Inc
#


import json
import netaddr
import time
import webob.exc

import functools

from neutron import manager
from neutron import quota
from neutron.common import exceptions as n_exc
from neutron.extensions import l3
from neutron.api import api_common
from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.api.v2 import resource
from neutron.common import exceptions as qexception
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import uos_utils
from neutron.common import uos_constants
from neutron.db import l3_db
from neutron.db import agents_db
from neutron.db import models_v2
from neutron.db import securitygroups_db
from neutron.extensions import l3
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron import policy
from neutron import wsgi
from neutron.services.vm.common import constants as s_constants

from neutron.extensions import dhcpagentscheduler
from neutron.extensions import l3agentscheduler

from oslo.config import cfg
from sqlalchemy.orm import exc as sa_exc

LOG = logging.getLogger(__name__)

class ServiceInstanceCheck(object):
    @property
    def _l3_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)
    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    @property
    def _service_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
                constants.SERVICEVM)

    def valid_vrouter_service(self, context, data):
        external_gw = s_constants.EXTERNAL_GATWAY_KEY
        fip = s_constants.FLOATINGIP_KEY
        if external_gw in data:
            for external in data[external_gw]:
                if 'floatingip_id' not in external:
                    msg = _("'%s' can not found floatingip_id") % external
                    raise n_exc.InvalidInput(error_message=msg)
                floatingip_id = external['floatingip_id']
            if floatingip_id:
                self._l3_plugin.get_floatingip(context, floatingip_id)

        if fip in data:
            for floatingip in data[fip]:
                if 'floatingip_id' not in floatingip or (
                  'fixed_port_id' not in floatingip):
                    msg = _("'%s' can not found floatingip_id or fixed_ip_id") % floatingip
                    raise n_exc.InvalidInput(error_message=msg)
                floatingip_id = floatingip['floatingip_id']
                fixed_port_id = floatingip['fixed_port_id']
                if floatingip_id:
                    self._l3_plugin.get_floatingip(context, floatingip_id)
                if fixed_port_id:
                    self._core_plugin.get_port(context, fixed_port_id)
