# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2014 UnitedStack Inc.
#    All Rights Reserved.
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
# @author: Yong Sheng Gong, UnitedStack Inc.
'''
Created on Mar 26, 2014

@author: gongysh
'''

from sqlalchemy.orm import exc

from neutron.common import constants as l3_constants
from neutron.common import rpc as n_rpc
from neutron.common import utils as common_utils
from neutron import context as n_context
from neutron.db import db_base_plugin_v2
from neutron.db import models_v2
from neutron.extensions import uosfloatingip
from neutron.openstack.common import log as logging


LOG = logging.getLogger('uos')
DEVICE_OWNER_ROUTER_GW = l3_constants.DEVICE_OWNER_ROUTER_GW


class UosFloatingIPMixin(object):

    @common_utils.exception_logger()
    def associate_floatingip_router(self, context, floatingip_id, router_id):
        with context.session.begin(subtransactions=True):
            floatingip_db = self._get_floatingip(context,
                                                 floatingip_id)
            floating_network_id = floatingip_db['floating_network_id']
            gw_portdb = None
            try:
                port_qry = context.elevated().session.query(models_v2.Port)
                gw_portdb = port_qry.filter_by(
                    network_id=floating_network_id,
                    device_id=router_id,
                    device_owner=DEVICE_OWNER_ROUTER_GW).one()
            except exc.NoResultFound:
                raise uosfloatingip.GWPortForFloatingIPNotFound(id=router_id)
            tenant_id = floatingip_db.tenant_id
            _ctx = n_context.Context('', tenant_id)
            body = {'floatingip': {'port_id': gw_portdb['id']}}
            payload = {'id': floatingip_db.id}
            payload.update(body)
            self._notifier = n_rpc.get_notifier('network')
            _notifer = n_rpc.get_notifier('network')
            _notifer.info(_ctx, 'floatingip.update.start', payload)
            result = self.update_floatingip(context, floatingip_id, body)
            _notifer.info(_ctx, 'floatingip.update.end',
                         {'floatingip': result})
            return result

    def _uos_get_floatingips(self, context, _ips):
        port_ids = []
        port_fip_dict = {}
        for f_ip in _ips:
            if not f_ip['port_id']:
                continue
            port_ids.append(f_ip['port_id'])
            if f_ip['port_id'] in port_fip_dict:
                port_fip_dict[f_ip['port_id']].append(f_ip)
            else:
                port_fip_dict[f_ip['port_id']] = [f_ip]

        ports = self._core_plugin.get_ports(context.elevated(),
                                            filters={"id": port_ids})
        router_ids = []
        router_fip_dict = {}
        for port in ports:
            f_ips = port_fip_dict[port['id']]
            for f_ip in f_ips:
                f_ip[uosfloatingip.UOS_PORT_DEVICE_OWNER] = port['device_owner']
                f_ip[uosfloatingip.UOS_PORT_DEVICE_ID] = port['device_id']
                f_ip[uosfloatingip.UOS_PORT_DEVICE_NAME] = ''
            if (port['device_owner'] and
                port['device_owner'] == l3_constants.DEVICE_OWNER_ROUTER_GW):
                router_ids.append(port['device_id'])
                if port['device_id'] in router_fip_dict:
                    router_fip_dict[port['device_id']].append(f_ip)
                else:
                    router_fip_dict[port['device_id']] = [f_ip]

        routers = []
        if router_ids:
            routers = self.get_routers(context,
                                       filters={"id": router_ids})
        for router in routers:
            f_ips = router_fip_dict.get(router['id'])
            for f_ip in f_ips:
                f_ip[uosfloatingip.UOS_PORT_DEVICE_NAME] = router['name']
        return _ips

    def _uos_process_fip_sync(self, context, routers_dict, floating_ips):
        #NOTE(gongysh) fill in cidr suffix info
        if not floating_ips:
            return [],[]
        # Sets the fip's subnet info
        subnetid_fips_dict = {}
        for floating_ip in floating_ips:
            floating_subnet_id = floating_ip.get('floating_subnet_id')
            if not floating_subnet_id:
                continue
            f_ips = subnetid_fips_dict.get(floating_subnet_id)
            if not f_ips:
                f_ips = []
            f_ips.append(floating_ip)
            subnetid_fips_dict[floating_subnet_id] = f_ips
        filters = {'id': subnetid_fips_dict.keys()}
        fip_subnets = self._core_plugin.get_subnets(context, filters)
        for fip_subnet in fip_subnets:
            f_ips = subnetid_fips_dict[fip_subnet['id']]
            for f_ip in f_ips:
                f_ip['cidr_suffix'] = fip_subnet['cidr'].split('/', 1)[1]
                f_ip['floating_subnet_cidr'] = fip_subnet['cidr']
                f_ip['gateway_ip'] = fip_subnet['gateway_ip']
        port_ids = []
        port_fip_dict = {}
        for f_ip in floating_ips:
            if not f_ip['port_id']:
                continue
            port_ids.append(f_ip['port_id'])
            port_fip_dict[f_ip['port_id']] = f_ip
        ports = [router.get('gw_port') for router in routers_dict.values()
                 if router.get('gw_port')]
        # To set fake gw port into its fip info
        _to_delete_fip_ids = set()
        _to_delete_fips = []
        for port in ports:
            f_ip = port_fip_dict.get(port['id'])
            if not f_ip:
                continue
            port['fixed_ips'][0]['subnet_id'] = f_ip['floating_subnet_id']
            port['fixed_ips'][0]['ip_address'] = f_ip['floating_ip_address']
            port['subnet']['cidr'] = f_ip['floating_subnet_cidr']
            port['subnet']['gateway_ip'] = f_ip['gateway_ip']
            port['subnet']['id'] = f_ip['floating_subnet_id']
            _extra_subnets = []
            for ex_sub in port['extra_subnets']:
                if ex_sub['id'] != f_ip['floating_subnet_id']:
                    _extra_subnets.append(ex_sub)
                else:
                    continue
            port['extra_subnets'] = _extra_subnets
            port['_uos_fip'] = True
            _to_delete_fip_ids.add(f_ip['id'])
        # To delete the router gw's fip
        result_fips = []
        if _to_delete_fip_ids:
            for floating_ip in floating_ips:
                to_delete_flag = False
                for fip_id in _to_delete_fip_ids:
                    if fip_id == floating_ip['id']:
                        to_delete_flag = True
                        LOG.debug("_to_delete_fips append: %s", floating_ip)
                        _to_delete_fips.append(floating_ip)
                        break
                if not to_delete_flag:
                    result_fips.append(floating_ip)
        else:
            result_fips = floating_ips
        return result_fips, _to_delete_fips

    def _uos_process_router_gateways(self, context, router, f_ips):
        gateways = set()
        for f_ip in f_ips:
            gateways.add(f_ip['gateway_ip'])
        router['gateway_ips'] = list(gateways)


def __uos_extend_floatingip_dict_binding(core_plugin, res, db):
    res['floating_port_id'] = db['floating_port_id']
    res[uosfloatingip.UOS_NAME] = db['uos_name']
    res['floating_subnet_id'] = db['floating_subnet_id']
    res[uosfloatingip.UOS_REGISTERNO] = db['uos_registerno']
    res[uosfloatingip.UOS_SERVICE_PROVIDER] = db['uos_service_provider']
    if res[uosfloatingip.UOS_REGISTERNO] is None:
        res[uosfloatingip.UOS_REGISTERNO] = ''
    if res[uosfloatingip.UOS_SERVICE_PROVIDER] is None:
        res[uosfloatingip.UOS_SERVICE_PROVIDER] = ''


def _uos_extend_fip_dict():
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        "floatingips", [__uos_extend_floatingip_dict_binding])
