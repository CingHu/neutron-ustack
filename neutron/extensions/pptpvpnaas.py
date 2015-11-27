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
# @author: Yong Sheng Gong Unitedstack Inc.

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


class RouterInUseByVPNService(qexception.InUse):
    message = _("Router %(router_id)s is used by VPNService %(vpnservice_id)s")


def _validate_pptp_cidr(data, valid_values=None):
    msg = attr._validate_subnet(data, valid_values)
    if msg:
        return msg
    try:
        net = netaddr.IPNetwork(data)
        if net.size < 4:
            msg = (_("The valid IP range defined by '%(data)s are too small") %
                   {"data": data})
        elif net.size > 256:
            msg = (_("The valid IP range defined by '%(data)s are too large") %
                   {"data": data})
    except Exception:
        msg = _("'%s' is not a valid IP subnet") % data
    return msg


RESOURCE_ATTRIBUTE_MAP = {
    'pptpconnections': {
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
        'vpn_cidr': {'allow_post': True, 'allow_put': False,
                     'validate': {'type:pptp_cidr': None},
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


class PPTPVPNServiceNotFound(qexception.NotFound):
    message = _("VPNService %(id)s could not be found")


class RouterExtNotFound(qexception.NotFound):
    message = _("Extension router is not be supported")


class Pptpvpnaas(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "pptp VPN service"

    @classmethod
    def get_alias(cls):
        return "pptp_vpnaas"

    @classmethod
    def get_description(cls):
        return "Extension for PPTP VPN service"

    @classmethod
    def get_namespace(cls):
        return "https://wiki.openstack.org/Neutron/VPNaaS/pptp"

    @classmethod
    def get_updated(cls):
        return "2014-02-23T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        attr.validators['type:pptp_cidr'] = _validate_pptp_cidr
        plural_mapping = {
            'pptpconnections': 'pptpconnection'
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
            controller = base.create_resource(
                collection_name, resource_name, plugin, params,
                allow_pagination=cfg.CONF.allow_pagination,
                allow_sorting=cfg.CONF.allow_sorting)

            resource = extensions.ResourceExtension(
                collection_name,
                controller,
                path_prefix=constants.COMMON_PREFIXES[constants.VPN],
                attr_map=params)
            resources.append(resource)
        return resources

    def update_attributes_map(self, attributes):
        super(Pptpvpnaas, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}


@six.add_metaclass(abc.ABCMeta)
class VPNPluginBase(service_base.ServicePluginBase):

    def get_plugin_name(self):
        return constants.VPN

    def get_plugin_type(self):
        return constants.VPN

    def get_plugin_description(self):
        return 'VPN service plugin'

    @abc.abstractmethod
    def get_pptpconnections(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_pptpconnection(self, context, pptpconnection_id, fields=None):
        pass

    @abc.abstractmethod
    def create_pptpconnection(self, context, pptpconnection):
        pass

    @abc.abstractmethod
    def update_pptpconnection(self, context,
                              pptpconnection_id, pptpconnection):
        pass

    @abc.abstractmethod
    def delete_pptpconnection(self, context, pptpconnection_id):
        pass
