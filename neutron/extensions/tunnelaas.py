# Copyright (c) 2015 UnitedStack Inc.
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
# @author: Wei Wang, UnitedStack

import abc
import netaddr

from oslo.config import cfg
import six

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.common import exceptions as qexception
from neutron.plugins.common import constants
from neutron import manager
from neutron import quota


# Tunnel Service Exceptions
class NoGatewayOrFIPFound(qexception.InvalidInput):
    message = _("Can't find gateway or fip on this router")


class DoNotNeedLocalSubnetInL3(qexception.InvalidInput):
    message = _("Please do not input local subnet when create L3 tunnel")


class DoNotNeedTargetNetworkInL2(qexception.InvalidInput):
    message = _("Please do not input target network when create L2 tunnel")


class NoCorrectInterfaceFound(qexception.InvalidInput):
    message = _("The subnet specified seems not connect to this router")


class ConnectionKeyInvalid(qexception.InvalidInput):
    message = _("%(key)s is not a valid gre tunnel key")


class NoLocalSubnetFound(qexception.Conflict):
    message = _("There is no subnet specified")


class TunnelNotFound(qexception.NotFound):
    message = _("The tunnel id %(tunnel_id)s not found")


class TunnelConnectionNotFound(qexception.NotFound):
    message = _("The tunnel connection could not found")


class TargetNetworkNotFound(qexception.NotFound):
    message = _("The target network %(target_network_id)s not found")


class DeviceDriverNotFound(qexception.NotFound):
    message = _("The device driver %(device_driver) can not found")


class RouterInUseByTunnelService(qexception.InUse):
    message = _("This router is used by tunnel, so can not delete")


class RouterInterfaceInUseByTunnel(qexception.InUse):
    message = _("This router interface is used by tunnel, so can not delete")


class FloatingipUsedByTunnel(qexception.InUse):
    message = _("This floatingip %(floatingip_id)s is used by "
                "router %(router_id)s, whcich bound to "
                "tunnel %(tunnel_id)s, so can not delete")


class TunnelInUse(qexception.InUse):
    message = _("This tunnel is still bound to a connection")


class ConflictWithOtherTunnel(qexception.Conflict):
    message = _("Layer 2 and Layer 3 tunnels can not exist in same rouer")


class ConflictWithL2Tunnel(qexception.Conflict):
    message = _("There should be only one tunnel ina  rouer")


class NetworkCIDRConflict(qexception.Conflict):
    message = _("Network CIDR overlay with each other or subnet connect to"
                " router")


class TunnelConnectionExists(qexception.Conflict):
    message = _("There is already one connection bind to this tunnel")


def _validate_gre_key(data, valid_values=None):
    # A gre key can always be transfered to an ipv4 address
    if not data:
        return
    try:
        netaddr.IPAddress(attr._validate_no_whitespace(data))
    except Exception:
        try:
            if int(data) == 2**32:  # since IPAddress doesn't recognize 2**32.
                return
            else:
                raise ConnectionKeyInvalid(key=data)
        except ValueError:
            raise ConnectionKeyInvalid(key=data)


attr.validators['type:validate_gre_key'] = _validate_gre_key
gre_supported_types = (2, 3)
tunnel_supported_modes = ('gre',)


# Attribute Map
RESOURCE_ATTRIBUTE_MAP = {
    'tunnels': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'is_visible': True, 'default': ''},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'is_visible': True},
        'router_id': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'admin_state_up': {'allow_post': True, 'allow_put': True,
            'is_visible': True, 'default':'UP',
            'validate': {'type:values': ('UP','DOWN')}},
        'type': {'allow_post': True, 'allow_put': False,
                 'is_visible': True, 'convert_to': attr.convert_to_int,
                 'validate': {'type:range': (2, 3)}},
        'mode': {'allow_post': True, 'allow_put': False,
                 'is_visible': True, 'default': 'gre',
                 'validate': {'type:values': tunnel_supported_modes}},
        'created_at': {'allow_post': False, 'allow_put': False,
                       'is_visible': True},
        'local_subnet': {'allow_post': True, 'allow_put': False,
                         'is_visible': True, 'default': ''},
        'tunnel_connections': {'allow_post': False, 'allow_put': False,
                               'is_visible': True},
        'target_networks': {'allow_post': False, 'allow_put': False,
                            'is_visible': True},
    },
    'tunnel_connections': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tunnel_id': {'allow_post': True, 'allow_put': False,
                      'is_visible': True, 'required_by_policy': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'is_visible': True, 'default': ''},
        'remote_ip': {'allow_post': True, 'allow_put': False,
                      'is_visible': True,
                      'validate': {'type:ip_address': None}},
        'key': {'allow_post': True, 'allow_put': False,
                'is_visible': True, 'default': '',
                'validate': {'type:validate_gre_key': None}},
        'key_type': {'allow_post': True, 'allow_put': False,
                     'is_visible': True, 'default': 0,
                     'convert_to': attr.convert_to_int,
                     'validate': {'type:range': (0,3)}},
        'checksum': {'allow_post': True, 'allow_put': False,
                     'is_visible': True, 'default': 0,
                     'convert_to': attr.convert_to_int,
                     'validate': {'type:range': (0,3)}},
    },
    'target_networks': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tunnel_id': {'allow_post': True, 'allow_put': False,
                      'is_visible': True, 'required_by_policy': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'is_visible': True},
        'network_cidr': {'allow_post': True, 'allow_put': False,
                         'is_visible': True,
                         'convert_to': attr.convert_to_cidr,
                         'validate': {'type:network_cidr': None}},
    }
}


TUNNELS = 'tunnels'
EXTENDED_ATTRIBUTES_2_0 = {
    'routers': {TUNNELS: {'allow_post': False,
                          'allow_put': False,
                          'is_visible': True,
                          'default': attr.ATTR_NOT_SPECIFIED}}}
tunnel_quota_opts = [
    cfg.IntOpt('quota_tunnel',
               default=10,
               help=_('Number of tunnels allowed per tenant. '
                      'A negative value means unlimited.')),
    cfg.IntOpt('quota_tunnel_connection',
               default=30,
               help=_('Number of tunnel connections allowed per tenant. '
                      'A negative value means unlimited.')),
    cfg.IntOpt('quota_target_network',
               default=60,
               help=_('Number of target networks allowed per tenant. '
                      'A negative value means unlimited.')),
]
cfg.CONF.register_opts(tunnel_quota_opts, 'QUOTAS')


class Tunnelaas(extensions.ExtensionDescriptor):
    """Security group extension."""

    @classmethod
    def get_name(cls):
        return "Neutron Tunnel as a Server"

    @classmethod
    def get_alias(cls):
        return "tunnelaas"

    @classmethod
    def get_description(cls):
        return "The tunnle as a service extension."

    @classmethod
    def get_namespace(cls):
        # todo
        return "http://docs.ustack.org/ext/tunnels/api/v2.0"

    @classmethod
    def get_updated(cls):
        return "2014-02-01T01:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        my_plurals = [(key, key[:-1]) for key in RESOURCE_ATTRIBUTE_MAP.keys()]
        attr.PLURALS.update(dict(my_plurals))
        exts = []
        plugin = manager.NeutronManager.get_service_plugins()[
            constants.TUNNEL]
        for resource_name in ['tunnel', 'tunnel_connection', 'target_network']:
            collection_name = resource_name.replace('_', '-') + "s"
            params = RESOURCE_ATTRIBUTE_MAP.get(resource_name + "s", dict())
            quota.QUOTAS.register_resource_by_name(resource_name)
            controller = base.create_resource(collection_name,
                                              resource_name,
                                              plugin, params, allow_bulk=True,
                                              allow_pagination=True,
                                              allow_sorting=True)

            ex = extensions.ResourceExtension(collection_name,
                    controller,
                    path_prefix=constants.COMMON_PREFIXES[constants.TUNNEL],
                    attr_map=params)
            exts.append(ex)
        return exts

    def get_extended_resources(self, version):
        if version == "2.0":
            return dict(EXTENDED_ATTRIBUTES_2_0.items() +
                        RESOURCE_ATTRIBUTE_MAP.items())
        else:
            return {}


@six.add_metaclass(abc.ABCMeta)
class TunnelPluginBase(object):

    def get_plugin_name(self):
        return constants.TUNNEL

    def get_plugin_type(self):
        return constants.TUNNEL

    def get_plugin_description(self):
        return 'Tunnel service plugin'

    @abc.abstractmethod
    def create_tunnel(self, context, tunnel):
        pass

    @abc.abstractmethod
    def update_tunnel(self, context, id, tunnel):
        pass

    @abc.abstractmethod
    def delete_tunnel(self, context, id):
        pass

    @abc.abstractmethod
    def get_tunnels(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        pass

    @abc.abstractmethod
    def get_tunnel(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def create_tunnel_connection(self, context, tunnel_connection):
        pass

    @abc.abstractmethod
    def delete_tunnel_connection(self, context, id):
        pass

    @abc.abstractmethod
    def get_tunnel_connections(self, context, filters=None, fields=None,
                               sorts=None, limit=None, marker=None,
                               page_reverse=False):
        pass

    @abc.abstractmethod
    def get_tunnel_connection(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def create_target_network(self, context, target_network):
        pass

    @abc.abstractmethod
    def delete_target_network(self, context, id):
        pass

    @abc.abstractmethod
    def get_target_network(self, context, filters=None, fields=None,
                           sorts=None, limit=None, marker=None,
                           page_reverse=False):
        pass

    @abc.abstractmethod
    def get_target_network(self, context, id, fields=None):
        pass
