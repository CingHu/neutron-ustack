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


from neutron.api.v2 import attributes
from neutron.db import db_base_plugin_v2
from neutron.db import l3_db
from neutron.db import uos_db
from neutron.extensions import l3
from neutron.extensions import uos
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from neutron.tests.unit import test_db_plugin
from neutron.tests.unit import test_l3_plugin

_uuid = uuidutils.generate_uuid

DB_PLUGIN_KLASS = ('neutron.tests.unit.test_extension_uos_l3.UOSTestPlugin')


class UOSTestExtensionManager(test_l3_plugin.L3TestExtensionManager):

    def get_resources(self):
        # Add the resources to the global attribute map
        # This is done here as the setup process won't
        # initialize the main API router which extends
        # the global attribute map
        attributes.RESOURCE_ATTRIBUTE_MAP.update(
            l3.RESOURCE_ATTRIBUTE_MAP)
        attributes.RESOURCE_ATTRIBUTE_MAP['routers'].update(
            uos.EXTENDED_TIMESTAMP)
        return l3.L3.get_resources()


class UOSL3DbMixin(l3_db.L3_NAT_db_mixin):

    def _extend_router_dict_uos(self, res, db):
        uos_db._uos_extend_timestamp(res, db)

    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        l3.ROUTERS, ['_extend_router_dict_uos'])


class UOSL3TestCase(test_db_plugin.NeutronDbPluginV2TestCase):

    def setUp(self, plugin=None, ext_mgr=None):
        plugin = plugin or DB_PLUGIN_KLASS
        ext_mgr = ext_mgr or UOSTestExtensionManager()
        super(UOSL3TestCase, self).setUp(plugin=plugin, ext_mgr=ext_mgr)


class UOSTestPlugin(db_base_plugin_v2.NeutronDbPluginV2, UOSL3DbMixin):

    """Test plugin that implements necessary calls on create/delete floating ip
    """

    supported_extension_aliases = ["router", "uos"]

    __native_pagination_support = True
    __native_sorting_support = True


class TestUOSL3(UOSL3TestCase, test_l3_plugin.L3NatTestCaseMixin):

    def test_router_create(self):
        name = 'router1'
        tenant_id = _uuid()
        expected_value = [('name', name), ('tenant_id', tenant_id),
                          ('admin_state_up', True), ('status', 'ACTIVE'),
                          ('external_gateway_info', None)]
        _now = timeutils.utcnow()
        with self.router(name='router1', admin_state_up=True,
                         tenant_id=tenant_id) as router:
            for k, v in expected_value:
                self.assertEqual(router['router'][k], v)
            _created = timeutils.parse_strtime(router['router']['created_at'])
            delta = timeutils.delta_seconds(_now, _created)
            self.assertTrue(delta > 0)


class TestUOSL3XML(TestUOSL3):
    fmt = 'xml'
