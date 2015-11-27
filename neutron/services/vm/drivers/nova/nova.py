# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, 2014 Intel Corporation.
# Copyright 2013, 2014 Isaku Yamahata <isaku.yamahata at intel com>
#                                     <isaku.yamahata at gmail com>
# All Rights Reserved.
#
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
# @author: Isaku Yamahata, Intel Corporation.

import time
import copy

from keystoneclient import auth as ks_auth
from keystoneclient.auth.identity import v2 as v2_auth
from keystoneclient import session as ks_session
from oslo_config import cfg

from neutron import manager
from neutron.api.v2 import attributes
from neutron.common import constants as l3_constants
from neutron import context as t_context
from neutron.extensions import portbindings
from neutron.extensions import servicevm as sv
from neutron.plugins.common import constants
from neutron.openstack.common.gettextutils import _LI, _LW
from neutron.openstack.common import log as logging
from neutron.services.vm.drivers import abstract_driver

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
NOVA_API_VERSION = "2"
SERVICEVM_NOVA_CONF_SECTION = 'servicevm_nova'

a = ks_session.Session()
a.register_conf_options(cfg.CONF, SERVICEVM_NOVA_CONF_SECTION)
ks_auth.register_conf_options(cfg.CONF, SERVICEVM_NOVA_CONF_SECTION)

OPTS = [
    cfg.StrOpt('region_name',
               help=_('Name of nova region to use. Useful if keystone manages'
                      ' more than one region.')),
]
CONF.register_opts(OPTS, group=SERVICEVM_NOVA_CONF_SECTION)

SVM_OPTS = [
    cfg.StrOpt('mgmt_network_name_id', default="manage_network",
               help=_("Manager network name or id")),
    cfg.StrOpt('mgmt_subnet_name_id', default="manage_subent",
               help=_("Manager subnet name or id")),
    cfg.StrOpt('servicevm_anti_spoofing', default=True,
               help=_("internal interface of subnet anti spoofing feature")),
    cfg.StrOpt('servicevm_mgmt_anti_spoofing', default=False,
               help=_("mgmt interface of mgmt subnet"
                      " anti spoofing feature")),
    cfg.StrOpt('mgmt_security_group', default='servicevm_mgmt_security_group',
               help=_("security group for servicevm manager port")),
    cfg.StrOpt('internal_security_group',
               default='servicevm_internal_port_security_group',
               help=_("security group for servicevm manager port")),
    cfg.StrOpt('mgmt_pps_limit', default='tcp:syn::100,udp:::100,icmp:::50',
               help=_("pps limit for servicevm manager port")),
]

cfg.CONF.register_opts(SVM_OPTS, "servicevm")

anti_spoofing = attributes.convert_to_boolean(
                     cfg.CONF.servicevm.servicevm_anti_spoofing)
mgmt_anti_spoofing = attributes.convert_to_boolean(
                     cfg.CONF.servicevm.servicevm_mgmt_anti_spoofing)
shadow_net_anti_spoofing = attributes.convert_to_boolean(True)

mgmt_subnet = cfg.CONF.servicevm.mgmt_subnet_name_id
mgmt_network = cfg.CONF.servicevm.mgmt_network_name_id
shadow_subnet = cfg.CONF.unitedstack.external_shadow_subnet
mgmt_pps_limit = cfg.CONF.servicevm.mgmt_pps_limit

MGMT = l3_constants.SERVICEVM_OWNER_MGMT
ROUTER_GW = l3_constants.SERVICEVM_OWNER_ROUTER_GW
ROUTER_INTF = l3_constants.SERVICEVM_OWNER_ROUTER_INTF

_NICS = 'nics'          # converted by novaclient => 'networks'
_NET_ID = 'net-id'      # converted by novaclient => 'uuid'
_PORT_ID = 'port-id'    # converted by novaclient => 'port'
_FILES = 'files'

PORT_ATTR_VALUES = {
    'mgmt_port':{
                    'servicevm_type': l3_constants.SERVICEVM_OWNER_MGMT,
                    'admin_state_up': True,
                    'security_groups': [],
                    portbindings.PROFILE:{'uos_pps_limits':mgmt_pps_limit}
    },
   'public_port':{
                    'servicevm_type': l3_constants.SERVICEVM_OWNER_ROUTER_GW,
                    'security_groups': [],
                    'admin_state_up':True
   },
   'inval_port':{
                    'servicevm_type': l3_constants.SERVICEVM_OWNER_ROUTER_INTF,
                    'security_groups': [],
                    'admin_state_up':True
   }
}

PORT_UPDATE_ATTR_VALUES = {
    'mgmt_port':{
                  'binding:disable_anti_spoofing': mgmt_anti_spoofing
    },
    'public_port':{
                  'binding:disable_anti_spoofing': shadow_net_anti_spoofing 
    },
    'inval_port':{
                  'binding:disable_anti_spoofing': anti_spoofing 
    }
}

class DefaultAuthPlugin(v2_auth.Password):
    """A wrapper around standard v2 user/pass to handle bypass url.

    This is only necessary because novaclient doesn't support endpoint_override
    yet - bug #1403329.

    When this bug is fixed we can pass the endpoint_override to the client
    instead and remove this class.
    """

    def __init__(self, **kwargs):
        self._endpoint_override = kwargs.pop('endpoint_override', None)
        super(DefaultAuthPlugin, self).__init__(**kwargs)

    def get_endpoint(self, session, **kwargs):
        if self._endpoint_override:
            return self._endpoint_override

        return super(DefaultAuthPlugin, self).get_endpoint(session, **kwargs)

class DeviceNova(abstract_driver.DeviceAbstractDriver):

    """Nova driver of hosting device."""

    def __init__(self):
        super(DeviceNova, self).__init__()
        # avoid circular import
        from novaclient import client
        self._novaclient = client

    def _nova_client(self, token=None):
        auth = ks_auth.load_from_conf_options(cfg.CONF,
                                              SERVICEVM_NOVA_CONF_SECTION)
        endpoint_override = None

        if not auth:
            LOG.warning(_LW('Authenticating to nova using nova_admin_* options'
                            ' is deprecated. This should be done using'
                            ' an auth plugin, like password'))

            if cfg.CONF.nova_admin_tenant_id:
                endpoint_override = "%s/%s" % (cfg.CONF.nova_url,
                                               cfg.CONF.nova_admin_tenant_id)

            auth = DefaultAuthPlugin(
                auth_url=cfg.CONF.nova_admin_auth_url,
                username=cfg.CONF.nova_admin_username,
                password=cfg.CONF.nova_admin_password,
                tenant_id=cfg.CONF.nova_admin_tenant_id,
                tenant_name=cfg.CONF.nova_admin_tenant_name,
                endpoint_override=endpoint_override)

        session = ks_session.Session.load_from_conf_options(
            cfg.CONF, SERVICEVM_NOVA_CONF_SECTION, auth=auth)
        novaclient_cls = self._novaclient.get_client_class(NOVA_API_VERSION)
        a = novaclient_cls(session=session,
                              region_name=cfg.CONF.servicevm_nova.region_name)
        tenant_id = auth.get_project_id(session)
        return a, tenant_id

    def get_type(self):
        return 'nova'

    def get_name(self):
        return 'nova'

    def get_description(self):
        return 'Nuetron Device Nova driver'

    @staticmethod
    def _safe_pop(d, name_list):
        res = None
        for name in name_list:
            if name in d:
                res = d.pop(name)
                break
        return res

    def _get_mgmt_security_group(self, plugin, context):
        mgmt_security_group = cfg.CONF.servicevm.mgmt_security_group
        sg = plugin._core_plugin.get_security_groups(context, filters=
                                              {'name':[mgmt_security_group]})
        if not sg:
            raise  sv.MGMTSecurityGroupNotFound(name=mgmt_security_group)
        return sg[0]['id']
        
    def _get_internal_security_group(self, plugin, context):
        internal_security_group = cfg.CONF.servicevm.internal_security_group
        sg = plugin._core_plugin.get_security_groups(context, filters=
                                              {'name':[internal_security_group]})
        if not sg:
            raise  sv.MGMTSecurityGroupNotFound(name=internal_security_group)
        return sg[0]['id']

    def _create_subnet_port(self, plugin, context, tenant_id,
                            device_id, fixed_ips=None):
        # resolve subnet and create port
        LOG.info(_('fixed_ips %(fixed_ips)s)'),
                  {'fixed_ips': fixed_ips})

        subnet_id = fixed_ips.get('subnet_id', None)
        gateway_ip = fixed_ips.pop('gateway_ip', False)
        if subnet_id is None:
            raise sv.DeviceNoSubnet(subnet_id=subnet_id)

        subnet = plugin._core_plugin.get_subnet(context, subnet_id)
        if gateway_ip and subnet['gateway_ip'] is None:
            raise sv.DeviceNoGateway(subnet_id=subnet_id)

        if gateway_ip:
            fixed_ips = [{'subnet_id':subnet['id'],
                          'ip_address':subnet['gateway_ip']}]
        else:
            fixed_ips = [{'subnet_id':subnet['id']}]

        port_data = {'tenant_id': tenant_id,
                     'servicevm_device': device_id,
                     'network_id': subnet['network_id'],
                     'fixed_ips': fixed_ips}
        (p_attr, pu_attr) = self._get_port_default_attr(plugin,
                                                        subnet_id=subnet['id'])
        port_data.update(p_attr)
        
        LOG.info(_('before port data is %(port_data)s'), {'port_data':port_data})

        # See api.v2.base.prepare_request_body()
        for attr, attr_vals in attributes.RESOURCE_ATTRIBUTE_MAP[
                attributes.PORTS].iteritems():
            if not attr_vals.get('allow_post', False):
                continue
            if attr in port_data:
                continue
            port_data[attr] = attr_vals['default']

        LOG.info(_('after port_data %s'), port_data)
        port = plugin._core_plugin.create_port(context, {'port': port_data})
        #because anti spoofing must upate port, it not set when create port
        port = plugin._core_plugin.update_port(context, port['id'],
                                               {'port': pu_attr})
        LOG.info(_('port %s'), port)
        return port['id']

    def _create_network_port(self, plugin, context, tenant_id,
                             device_id, network_id=None):
        # resolve network and create port
        LOG.info(_('network_id %(network_id)s'),
                  {'network_id': network_id})
        port_update = {}
        if network_id:
            net = plugin._core_plugin.get_network(context, network_id)
            port_data = {'tenant_id': tenant_id,
                         'network_id': network_id,
                         'servicevm_device': device_id,
                         'fixed_ips': attributes.ATTR_NOT_SPECIFIED}
            (p_attr, pu_attr) = self._get_port_default_attr(plugin,
                                                            network_id=network_id)
            port_data.update(p_attr)
        
        LOG.info(_('before port data is %(port_data)s'), {'port_data':port_data})

        # See api.v2.base.prepare_request_body()
        for attr, attr_vals in attributes.RESOURCE_ATTRIBUTE_MAP[
                attributes.PORTS].iteritems():
            if not attr_vals.get('allow_post', False):
                continue
            if attr in port_data:
                continue
            port_data[attr] = attr_vals['default']

        LOG.info(_('after port_data %s'), port_data)
        port = plugin._core_plugin.create_port(context, {'port': port_data})
        #because anti spoofing must upate port, it not set when create port
        port = plugin._core_plugin.update_port(context, port['id'],
                                               {'port': pu_attr})
        LOG.info(_('port %s'), port)
        return port['id']

    def _get_port_default_attr(self, plugin, subnet_id=None, network_id=None):
        port_attr = copy.deepcopy(PORT_ATTR_VALUES)
        port_update_attr = copy.deepcopy(PORT_UPDATE_ATTR_VALUES)
        context = t_context.get_admin_context()
        if not subnet_id and not network_id:
            raise sv.InvalidNetParams()

        if subnet_id:
            subnet = plugin._core_plugin.get_subnet(context, subnet_id)
            if subnet['name'] == mgmt_subnet:
                network_id = subnet['network_id']
            elif subnet['name'] == shadow_subnet:
                return (port_attr['public_port'], port_update_attr['public_port'])
            else:
                return (port_attr['inval_port'], port_update_attr['inval_port'])
        if network_id:
            net = plugin._core_plugin.get_network(context, network_id)
            if net['name'] == mgmt_network:
                sg_id = self._get_mgmt_security_group(plugin, context)
                port_attr['mgmt_port']['security_groups'] = [sg_id]
                return (port_attr['mgmt_port'], port_update_attr['mgmt_port'])
            else:
                sg_id = self._get_internal_security_group(plugin, context)
                port_attr['inval_port']['security_groups'] = [sg_id]
                return (port_attr['inval_port'], port_update_attr['inval_port'])

    def create(self, plugin, context, device):
        nova, tenant_id = self._nova_client()
        LOG.info(_('create device data is %s'), device)
        nets= []
        # flavor and image are specially treated by novaclient
        attributes = device['device_template']['attributes'].copy()
        if 'kwargs' in attributes:
            attributes.update(device['kwargs'])

        device_attr = device['attributes'].copy()

        name = self._safe_pop(attributes, ('name', ))
        if name is None:
            # TODO(yamahata): appropreate way to generate instance name
            name = (self.__class__.__name__ + '-' + device['id'])
        image = self._safe_pop(attributes, ('image', 'imageRef'))
        flavor = self._safe_pop(attributes, ('flavor', 'flavorRef'))
        availability_zone = self._safe_pop(attributes, ('availability_zone',))
        networks = self._safe_pop(attributes, ('networks',))

        if networks:
            nets = networks
        fixed_ips = self._safe_pop(device_attr, ('fixed_ips',))
        if fixed_ips:
            nets+=fixed_ips

        files = plugin.mgmt_get_config(context, device)
        if files:
            attributes[_FILES] = files

        #add a shadow subnet for all devices
        admin_context = t_context.get_admin_context()
        subnet = plugin._core_plugin.get_subnets(admin_context,
                               filters={'name':[shadow_subnet]})
        if subnet:
            nets.append({'subnet_id': subnet[0]['id']})

        LOG.info(_('service_context: %s, nets: %s'),
                     device.get('service_context', []), nets)

        nics = []
        for sc_entry in nets:
            LOG.info(_('sc_entry: %s'), sc_entry)

            # nova API doesn't return tacker port_id.
            # so create port if necessary by hand, and use it explicitly.
            if sc_entry.get('port_id', None):
                LOG.info(_('port_id %s specified'), sc_entry['port_id'])
                port_id = sc_entry['port_id']
            elif sc_entry.get('subnet_id', None):
                LOG.info(_('subnet %s specified'), sc_entry)
                port_id = self._create_subnet_port(plugin, context, tenant_id,
                                                  device['id'], fixed_ips=sc_entry)
            elif sc_entry.get('network_id', None):
                LOG.info(_('network %s specified'), sc_entry['network_id'])
                port_id = self._create_network_port(plugin, context, tenant_id,
                                                    device['id'],
                                                    network_id=sc_entry['network_id'])
            else:
                LOG.info(_('skipping sc_entry %s'), sc_entry)
                continue

            LOG.info(_('port_id %s'), port_id)
            nics.append({_PORT_ID: port_id})

        LOG.info(_('nics %(nics)s attributes %(attributes)s'),
                  {'nics': nics, 'attributes': attributes})

        instance = nova.servers.create(name, image, flavor, tenant_id=tenant_id, 
                                       availability_zone=availability_zone,
                                       nics=nics, **attributes)
        return instance.id

    def create_wait(self, plugin, context, device_dict, device_id):
        nova, tenant_id = self._nova_client()
        instance = nova.servers.get(device_id)
        status = instance.status
        # TODO(yamahata): timeout and error
        while status == 'BUILD':
            time.sleep(5)
            instance = nova.servers.get(instance.id)
            status = instance.status
            LOG.info(_('instance:%s status: %s'), (instance.id, status))

        LOG.info(_('instance:%s status: %s'), (instance.id, status))
        if status == 'ERROR':
            #raise RuntimeError(_("creation of server %s faild") % device_id)
            LOG.error("creation of server %s faild" % device_id)
            raise sv.DeviceCreateWaitFailed(device_id=device_id)
        elif status == 'ACTIVE':
            LOG.info("creation of server %s sucessfully" % instance.id)

    def show(self, plugin, context, instance_id):
        # do nothing but checking if the instance exists at the moment
        nova, tenant_id = self._nova_client()
        try:
            instance = nova.servers.get(instance_id)
        except self._novaclient.exceptions.NotFound:
            raise RuntimeError("server %s is not Founded" %
                       instance_id)
        except Exception:
            raise RuntimeError("Server %s  get fail" % instance_id)
            
        return instance 
        

    def update(self, plugin, context, device_id, device_dict, device):
        # do nothing but checking if the instance exists at the moment
        nova, tenant_id = self._nova_client()
        nova.servers.get(device_id)

    def update_wait(self, plugin, context, device_id):
        # do nothing but checking if the instance exists at the moment
        nova, tenant_id = self._nova_client()
        nova.servers.get(device_id)

    def delete(self, plugin, context, device_id):
        nova, tenant_id = self._nova_client()
        try:
            instance = nova.servers.get(device_id)
        except self._novaclient.exceptions.NotFound:
            LOG.error("server %s is not Founded" %
                        device_id)
            return 
        instance.delete()

    def delete_wait(self, plugin, context, device_id):
        nova, tenant_id = self._nova_client()
        # TODO(yamahata): timeout and error
        while True:
            try:
                instance = nova.servers.get(device_id)
                LOG.info(_('instance status %s'), instance.status)
            except self._novaclient.exceptions.NotFound:
                break
            #hxn
            #if instance.status == 'ERROR':
            #    raise RuntimeError(_("deletion of server %s faild") %
            #                       device_id)
            time.sleep(5)

    def attach_interface(self, plugin, context, device_id, port_id):
        LOG.info(_('ataching interface %(device_id)s %(port_id)s'),
                  {'device_id': device_id, 'port_id': port_id})
        nova, tenant_id = self._nova_client()
        instance = nova.servers.get(device_id)
        instance.interface_attach(port_id, None, None)

    def dettach_interface(self, plugin, context, device_id, port_id):
        LOG.info(_('detaching interface %(device_id)s %(port_id)s'),
                  {'device_id': device_id, 'port_id': port_id})
        nova, tenant_id = self._nova_client()
        instance = nova.servers.get(device_id)
        instance.interface_detach(port_id)
