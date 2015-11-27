# Copyright (c) 2014 UnitedStack, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# @author: Wei Wang, wangwei@unitedstack.com, UnitedStack, Inc.


import contextlib

from oslo.config import cfg
from webob import exc

from neutron.api.v2 import attributes
from neutron.common import constants as l3_constants
from neutron import context
from neutron.db import db_base_plugin_v2
from neutron.db import external_net_db
from neutron.db import floatingip_ratelimits_db as fip_ratelimits_db
from neutron.extensions import floatingip_ratelimits as fip_ratelimits_ext
from neutron.extensions import l3
from neutron import manager
from neutron.plugins.common import constants as service_constants
from neutron.tests.unit import test_db_plugin
from neutron.tests.unit import test_l3_plugin


DB_PLUGIN_KLASS = ('neutron.tests.unit.test_extension_floatingip_ratelimits.'
                   'FloatingIPRatelimitsTestPlugin')


class FIPTestExtensionManager(test_l3_plugin.L3TestExtensionManager):

    def get_resources(self):
        # Add the resources to the global attribute map
        # This is done here as the setup process won't
        # initialize the main API router which extends
        # the global attribute map
        attributes.RESOURCE_ATTRIBUTE_MAP.update(
            l3.RESOURCE_ATTRIBUTE_MAP)
        attributes.RESOURCE_ATTRIBUTE_MAP.update(
            fip_ratelimits_ext.EXTENDED_ATTRIBUTES_2_0)
        return l3.L3.get_resources()


class FloatingIPRatelimitsTestCase(test_db_plugin.NeutronDbPluginV2TestCase):

    def setUp(self, plugin=None, ext_mgr=None):
        ext_mgr = ext_mgr or FIPTestExtensionManager()
        super(FloatingIPRatelimitsTestCase, self).setUp(plugin,
                                                        ext_mgr=ext_mgr)


class FloatingIPRatelimitsTestPlugin(db_base_plugin_v2.NeutronDbPluginV2,
                                fip_ratelimits_db.FloatingIPRateLimitsDbMixin,
                                external_net_db.External_net_db_mixin):

    """Test plugin that implements necessary calls on create/delete floating ip
    """

    supported_extension_aliases = ["router", "external-net",
                                   "floatingip_ratelimits"]

    __native_pagination_support = True
    __native_sorting_support = True

    def create_network(self, context, network):
        session = context.session
        with session.begin(subtransactions=True):
            net = super(FloatingIPRatelimitsTestPlugin,
                        self).create_network(context, network)
            self._process_l3_create(context, net, network['network'])
        return net

    def update_network(self, context, id, network):

        session = context.session
        with session.begin(subtransactions=True):
            net = super(FloatingIPRatelimitsTestPlugin, self).update_network(
                    context, id, network)
            self._process_l3_update(context, net, network['network'])
        return net

    def delete_network(self, context, id):
        with context.session.begin(subtransactions=True):
            self._process_l3_delete(context, id)
            super(FloatingIPRatelimitsTestPlugin, self).delete_network(
                    context, id)

    def delete_port(self, context, id, l3_port_check=True):
        plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)
        if plugin:
            if l3_port_check:
                plugin.prevent_l3_port_deletion(context, id)
            plugin.disassociate_floatingips(context, id)
        return super(FloatingIPRatelimitsTestPlugin, self).delete_port(
                context, id)


class FloatingIPRatelimitsDBTestCase(FloatingIPRatelimitsTestCase):

    def setUp(self, plugin=None, ext_mgr=None):
        plugin = plugin or DB_PLUGIN_KLASS
        ext_mgr = ext_mgr or FIPTestExtensionManager()
        super(FloatingIPRatelimitsDBTestCase, self).setUp(
                plugin=plugin, ext_mgr=ext_mgr)


class TestFloatingIPRatelimits(FloatingIPRatelimitsDBTestCase,
                               test_l3_plugin.L3NatTestCaseMixin):

    def _create_floatingip(self, fmt, network_id, port_id=None,
                           fixed_ip=None, set_context=False,
                           arg_list=None, **kwargs):
        data = {'floatingip': {'floating_network_id': network_id,
                               'tenant_id': self._tenant_id}}
        if port_id:
            data['floatingip']['port_id'] = port_id
            if fixed_ip:
                data['floatingip']['fixed_ip_address'] = fixed_ip
        for arg in (('rate_limit',) + (arg_list or ())):
            # kwargs must exist or function 'get' will get error
            if kwargs and kwargs.get(arg) is not None:
                data['floatingip'][arg] = kwargs[arg]
        floatingip_req = self.new_create_request('floatingips', data, fmt)

        if set_context and self._tenant_id:
            # create a specific auth context for this request
            floatingip_req.environ['neutron.context'] = context.Context(
                '', self._tenant_id)
        return floatingip_req.get_response(self.ext_api)

    def _make_floatingip(self, fmt, network_id, port_id=None, fixed_ip=None,
                         set_context=False, arg_list=None, **kwargs):
        res = self._create_floatingip(fmt, network_id, port_id, fixed_ip,
                                      set_context, arg_list, **kwargs)
        self.assertEqual(res.status_int, exc.HTTPCreated.code)
        return self.deserialize(fmt, res)

    @contextlib.contextmanager
    def floatingip_with_assoc(self, port_id=None, fixed_ip=None,
                              set_context=False, arg_list=None, **kwargs):
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            private_port = None
            if port_id:
                private_port = self._show('ports', port_id)
            with test_db_plugin.optional_ctx(private_port,
                                             self.port) as private_port:
                with self.router() as r:
                    sid = private_port['port']['fixed_ips'][0]['subnet_id']
                    private_sub = {'subnet': {'id': sid}}
                    floatingip = None

                    self._add_external_gateway_to_router(
                        r['router']['id'],
                        public_sub['subnet']['network_id'])
                    self._router_interface_action(
                        'add', r['router']['id'],
                        private_sub['subnet']['id'], None)

                    floatingip = self._make_floatingip(
                        self.fmt,
                        public_sub['subnet']['network_id'],
                        port_id=private_port['port']['id'],
                        fixed_ip=fixed_ip,
                        set_context=set_context,
                        arg_list=arg_list,
                        **kwargs)
                    yield floatingip

                    if floatingip:
                        self._delete('floatingips',
                                     floatingip['floatingip']['id'])
                    self._router_interface_action(
                        'remove', r['router']['id'],
                        private_sub['subnet']['id'], None)
                    self._remove_external_gateway_from_router(
                        r['router']['id'],
                        public_sub['subnet']['network_id'])

    def test_create_floatingip_with_assoc(
            self, expected_status=l3_constants.FLOATINGIP_STATUS_ACTIVE):
        with self.floatingip_with_assoc(
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit=2048) as fip:
            body = self._show('floatingips', fip['floatingip']['id'])
            self.assertEqual(body['floatingip']['rate_limit'],
                             fip['floatingip']['rate_limit'])
            self.assertEqual(body['floatingip']['id'],
                             fip['floatingip']['id'])
            self.assertEqual(body['floatingip']['port_id'],
                             fip['floatingip']['port_id'])
            self.assertEqual(expected_status, body['floatingip']['status'])
            self.assertIsNotNone(body['floatingip']['fixed_ip_address'])
            self.assertIsNotNone(body['floatingip']['router_id'])

    def test_create_fip_with_negative_rate_limit(self):
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            res = self._create_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'],
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit=-1)
            self.assertEqual(res.status_int, exc.HTTPBadRequest.code)

    def test_create_fip_with_not_number_rate_limit(self):
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            res = self._create_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'],
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit="test")
            self.assertEqual(res.status_int, exc.HTTPBadRequest.code)

    def test_create_fip_with_not_int_rate_limit(self):
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            res = self._create_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'],
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit="1024.5")
            self.assertEqual(res.status_int, exc.HTTPBadRequest.code)

    def test_create_fip_with_not_divisible_rate_limit(self):
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            res = self._create_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'],
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit=2000)
            self.assertEqual(res.status_int, exc.HTTPBadRequest.code)

    def test_create_fip_with_zero_rate_limit(self):
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            res = self._create_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'],
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit=0)
            self.assertEqual(res.status_int, exc.HTTPBadRequest.code)

    def test_create_fip_with_larger_than_maximum(self):
        cfg.CONF.set_override('maximum_ratelimit', 10, group='unitedstack')
        with self.subnet(cidr='11.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            res = self._create_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'],
                arg_list=(fip_ratelimits_ext.RATE_LIMIT,),
                rate_limit=1024*11)
            self.assertEqual(res.status_int, exc.HTTPBadRequest.code)


class TestFloatingIPRatelimitsXML(TestFloatingIPRatelimits):
    fmt = 'xml'
