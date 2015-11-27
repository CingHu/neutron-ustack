# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2014 UnitedStack Inc.
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
# @author: Yong Sheng Gong, UnitedStack, inc.

from neutron.api.v2 import attributes
from neutron.db import db_base_plugin_v2
from neutron.extensions import l3
from neutron.openstack.common import timeutils


def _uos_extend_timestamp(res, db):
    res['created_at'] = timeutils.strtime(db['created_at'])


def _uos_extend_floatingip_dict_binding(core_plugin, res, db):
    _uos_extend_timestamp(res, db)


def _uos_extend_router_dict_binding(core_plugin, res, db):
    _uos_extend_timestamp(res, db)


def _uos_extend_network_dict_binding(core_plugin, res, db):
    _uos_extend_timestamp(res, db)


def _uos_extend_port_dict_binding(core_plugin, res, db):
    _uos_extend_timestamp(res, db)


def _uos_extend_subnet_dict_binding(core_plugin, res, db):
    _uos_extend_timestamp(res, db)


def _uos_extend_sg_dict_binding(core_plugin, res, db):
    _uos_extend_timestamp(res, db)


db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
    l3.FLOATINGIPS, [_uos_extend_floatingip_dict_binding])


db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
    l3.ROUTERS, [_uos_extend_router_dict_binding])


db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
    attributes.NETWORKS, [_uos_extend_network_dict_binding])


db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
    attributes.PORTS, [_uos_extend_port_dict_binding])


db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
    attributes.SUBNETS, [_uos_extend_subnet_dict_binding])


db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
    'security_groups', [_uos_extend_sg_dict_binding])
