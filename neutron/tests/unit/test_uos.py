# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2014 UnitedStack, Inc.
# All Rights Reserved.
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

import contextlib

import mock

from neutron.api import extensions
from neutron import context
from neutron import manager
from neutron import policy
from neutron.common import exceptions
from neutron.extensions import uos
from neutron.openstack.common import policy as common_policy
from neutron.openstack.common import timeutils
from neutron.tests import base
from neutron.tests.unit.ml2 import test_ml2_plugin
from neutron.tests.unit import test_extension_security_group
from neutron.tests.unit import test_extensions
from neutron.tests.unit import test_l3_plugin
from neutron.tests.unit import test_security_groups_rpc as test_sg_rpc

NOTIFIER = 'neutron.plugins.ml2.rpc.AgentNotifierApi'


class FakeRequest(object):
    def __init__(self, fake_context=None):
        self.context = fake_context


class UosTestBase(object):
    def _test_create_time(self, func, resource, resources=None):
        if not resources:
            resources = resource + "s"
        _now = timeutils.utcnow()
        with func() as obj:
            _obj = self._show(resources, obj[resource]['id'])
            c_time = _obj[resource]['created_at']
            _c = timeutils.parse_strtime(c_time)
        delta = timeutils.delta_seconds(_now, _c)
        self.assertTrue(delta > 0)


class UosTestCase(test_l3_plugin.L3NatTestCaseMixin,
                  UosTestBase,
                  test_extension_security_group.SecurityGroupsTestCase):
    plugin_str = test_ml2_plugin.PLUGIN_NAME
    l3_plugin = ('neutron.services.l3_router.'
                 'l3_router_plugin.L3RouterPlugin')

    def setUp(self):
        manager.NeutronManager._instance = None
        extensions.PluginAwareExtensionManager._instance = None
        test_sg_rpc.set_firewall_driver(test_sg_rpc.FIREWALL_HYBRID_DRIVER)
        self.dhcp_notifier_cls_p = mock.patch(
            'neutron.api.rpc.agentnotifiers.dhcp_rpc_agent_api.'
            'DhcpAgentNotifyAPI')
        self.dhcp_notifier = mock.Mock(name='dhcp_notifier')
        self.dhcp_notifier_cls = self.dhcp_notifier_cls_p.start()
        self.dhcp_notifier_cls.return_value = self.dhcp_notifier
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        if self.l3_plugin:
            service_plugins = {'l3_plugin_name': self.l3_plugin}
        else:
            service_plugins = None
        super(UosTestCase, self).setUp(
            self.plugin_str, service_plugins=service_plugins)
        ext_mgr = extensions.PluginAwareExtensionManager.get_instance()
        self.ext_api = test_extensions.setup_extensions_middleware(ext_mgr)
        self.adminContext = context.get_admin_context()
        self.addCleanup(mock.patch.stopall)

    def test_floatingip_create_time(self):
        resource = 'floatingip'
        self._test_create_time(self.floatingip_uos, resource)

    @contextlib.contextmanager
    def floatingip_uos(self):
        with self.subnet(cidr='12.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            try:
                res = self._make_floatingip(
                    self.fmt,
                    public_sub['subnet']['network_id'])
                yield res
            finally:
                self._delete('floatingips', res['floatingip']['id'])

    def test_net_create_time(self):
        resource = 'network'
        self._test_create_time(self.network, resource)

    def test_subnet_create_time(self):
        resource = 'subnet'
        self._test_create_time(self.subnet, resource)

    def test_port_create_time(self):
        resource = 'port'
        self._test_create_time(self.port, resource)

    def test_sg_create_time(self):
        resource = 'security_group'
        self._test_create_time(self.security_group,
                               resource, resources='security-groups')

    def test_policy_403(self):
        with self.subnet(cidr='12.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            fip = self._make_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'])
            policy.reset()
            policy.init()
            rules = {
                "delete_floatingip": "role:admin_only"
            }
            common_policy.set_rules(common_policy.Rules(
                dict((k, common_policy.parse_rule(v))
                for k, v in rules.items())))
            tenant_id = fip['floatingip']['tenant_id']
            fip_id = fip['floatingip']['id']
            self.context = context.Context('fake', tenant_id, roles=['member'])
            req = self.new_delete_request('floatingips', fip_id)
            req.environ['neutron.context'] = self.context
            res = req.get_response(self._api_for_resource('floatingips'))
            self.assertEqual(403, res.status_int)
            policy.reset()
            policy.init()
            self._delete('floatingips', fip_id)

    def test_policy_404(self):
        with self.subnet(cidr='12.0.0.0/24') as public_sub:
            self._set_net_external(public_sub['subnet']['network_id'])
            fip = self._make_floatingip(
                self.fmt,
                public_sub['subnet']['network_id'])
            policy.reset()
            policy.init()
            rules = {
                "delete_floatingip": "role:admin_only"
            }
            common_policy.set_rules(common_policy.Rules(
                dict((k, common_policy.parse_rule(v))
                for k, v in rules.items())))
            fip_id = fip['floatingip']['id']
            self.context = context.Context('fake', 'fake', roles=['member'])
            req = self.new_delete_request('floatingips', fip_id)
            req.environ['neutron.context'] = self.context
            res = req.get_response(self._api_for_resource('floatingips'))
            self.assertEqual(404, res.status_int)
            policy.reset()
            policy.init()
            self._delete('floatingips', fip_id)


class UOSExtensionPolicyTestCase(base.BaseTestCase):
    def setUp(self):
        super(UOSExtensionPolicyTestCase, self).setUp()
        policy.reset()
        policy.init()
        rules = {
            "associate_floatingip_router": "not role:project_observer",
            "get_router_details": "role:admin",
            "remove_router_portforwarding": "role:member"
        }
        common_policy.set_rules(common_policy.Rules(
            dict((k, common_policy.parse_rule(v))
                 for k, v in rules.items())))
        self.context = context.Context('fake', 'fake', roles=['member'])
        self.request = FakeRequest(self.context)
        self.target = {}
        self.controller = uos.UosController()

    def test_enfoce_fail(self):
        self.assertRaises(exceptions.PolicyNotAuthorized,
                self.controller.get_router_details,
                self.request,
                '0')

    def test_enforce_success(self):
        self.assertRaisesRegexp(Exception,
                "Neutron core_plugin not configured",
                self.controller.remove_router_portforwarding,
                self.request,
                '0')

    def test_not_observer(self):
        self.assertRaisesRegexp(Exception,
                "Neutron core_plugin not configured",
                self.controller.associate_floatingip_router,
                self.request,
                '0')
