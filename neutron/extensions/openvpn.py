# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2014 Unitedstack Inc.
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
# @author: hu xining Unitedstack Inc.

import abc
import netaddr

from oslo.config import cfg
import six

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.common import exceptions as qexception
from neutron import manager
from neutron.plugins.common import constants
from neutron import quota
from neutron.services import service_base
from neutron.services.vpn.common import constants as vpn_connstants
from neutron.openstack.common import log as logging

LOG = logging.getLogger(__name__)

OPENVPN_CONF_OPTS = [
    cfg.StrOpt('client_api_path', default='/tmp/',
                help=_('key and config of openvpn path for web api')),
]

cfg.CONF.register_opts(OPENVPN_CONF_OPTS,'openvpn')

class RouterInUseByOpenVPNService(qexception.InUse):
    message = _("Router %(router_id)s is used by VPNService %(vpnservice_id)s")

class OpenVPNZipfailed(qexception.InUse):
    message = _("openvpn %(id)s get zip file failed")

class OpenVPNServiceNotFound(qexception.NotFound):
    message = _("OpenVPNService %(id)s could not be found")

class OpenvpnInExists(qexception.InUse):
    message = _("openvpn service my be exist in router %(router_id)s")

class RouterExtNotFound(qexception.NotFound):
    message = _("Extension router is not be supported")

class FileNotFound(qexception.NotFound):
    message = _("file %(name)s could not be found")

class OpenVPNInvalidPortValue(qexception.InvalidInput):
    message = _("Invalid value for port %(port)s")

def _validate_openvpn_cidr(data, valid_values=None):
    msg = attr._validate_subnet(data, valid_values)
    if msg:
        return msg
    try:
        net = netaddr.IPNetwork(data)
        if net.size < 4:
            msg = (_("The valid IP range defined by '%(data)s are too small") %
                   {"data": data})
        elif net.size > vpn_connstants.PEER_VPN_SIZE:
            msg = (_("The valid IP range defined by '%(data)s are too large") %
                   {"data": data})
    except Exception:
        msg = _("'%s' is not a valid IP subnet") % data
    return msg

def convert_validate_port_value(port):
    if port is None:
        return port
    try:
        val = int(port)
    except (ValueError, TypeError):
        raise OpenVPNInvalidPortValue(port=port)

    if val >= 0 and val <= 65535:
        return val
    else:
        raise OpenVPNInvalidPortValue(port=port)

RESOURCE_ATTRIBUTE_MAP = {
    'openvpnconnections': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:string': None},
                 'is_visible': True, 'default': ''},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'peer_cidr': {'allow_post': True, 'allow_put': False,
                     'validate': {'type:peer_cidr': None},
                     'is_visible': True},
        'port': {'allow_post': True, 'allow_put': True,
                           'default': 1194,
                           'convert_to': convert_validate_port_value,
                           'is_visible': True},
        'protocol': {'allow_post': True, 'allow_put': True,
                           'default': 'udp',
                           'validate': {'type:values': ['udp','tcp']},
                           'is_visible': True},
        'router_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'is_visible': True,
                      'delete_notification': True},
        'admin_state_up': {'allow_post': True, 'allow_put': True,
                           'default': True,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'created_at': {'allow_post': False, 'allow_put': False,
                       'is_visible': True},
    },
}


class Openvpn(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "OpenVPN service"

    @classmethod
    def get_alias(cls):
        return "openvpn_vpnaas"

    @classmethod
    def get_description(cls):
        return "Extension for OpenVPN service"


    @classmethod
    def get_namespace(cls):
        return "https://wiki.openstack.org/Neutron/VPNaaS/OpenVPN"

    @classmethod
    def get_updated(cls):
        return "2014-02-23T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        attr.validators['type:peer_cidr'] = _validate_openvpn_cidr
        plural_mapping = {
            'openvpnconnections': 'openvpnconnection'
        }
        my_plurals = []
        for plural in RESOURCE_ATTRIBUTE_MAP:
            singular = plural_mapping.get(plural, plural[:-1])
            my_plurals.append((plural, singular))
        attr.PLURALS.update(dict(my_plurals))
        resources = []
        plugin = manager.NeutronManager.get_service_plugins()[
            constants.VPN]
        for collection_name in RESOURCE_ATTRIBUTE_MAP:
            resource_name = plural_mapping.get(
                collection_name, collection_name[:-1])
            params = RESOURCE_ATTRIBUTE_MAP[collection_name]
            collection_name = collection_name.replace('_', '-')

            quota.QUOTAS.register_resource_by_name(resource_name)
            member_actions = {
                               'get_client_certificate':'GET',
                             }
            controller = base.create_resource(
                collection_name, resource_name, plugin, params,
                member_actions=member_actions,
                allow_pagination=cfg.CONF.allow_pagination,
                allow_sorting=cfg.CONF.allow_sorting)

            ext = extensions.ResourceExtension(resource_name+'s', controller,
                        path_prefix=constants.COMMON_PREFIXES[constants.VPN],
                        member_actions=member_actions)
            resources.append(ext)
        return resources

    def update_attributes_map(self, attributes):
        super(Openvpn, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}


@six.add_metaclass(abc.ABCMeta)
class OpenVPNPluginBase(service_base.ServicePluginBase):

    def get_plugin_name(self):
        return constants.VPN

    def get_plugin_type(self):
        return constants.VPN

    def get_plugin_description(self):
        return 'OpenVPN service plugin'

    @abc.abstractmethod
    def get_client_certificate(self, context, id):
        pass

    @abc.abstractmethod
    def get_openvpnconnections(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_openvpnconnection(self, context, openvpnconnection_id, fields=None):
        pass

    @abc.abstractmethod
    def create_openvpnconnection(self, context, openvpnconnection):
        pass

    @abc.abstractmethod
    def update_openvpnconnection(self, context,
                              openvpnconnection_id, openvpnconnection):
        pass

    @abc.abstractmethod
    def delete_openvpnconnection(self, context, openvpnconnection_id):
        pass
