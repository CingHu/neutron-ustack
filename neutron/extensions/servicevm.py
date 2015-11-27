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

import abc

import six

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.api.v2 import resource_helper
from neutron.common import exceptions
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services.service_base import ServicePluginBase
from neutron import manager
from neutron import quota

LOG = logging.getLogger(__name__)

class NetworkInfoNotSpecified(exceptions.InvalidInput):
    message = _('Network info should be input, such as network_id, subnet_id')

class InfraDriverNotSpecified(exceptions.InvalidInput):
    message = _('infra driver is not speicfied')


class ServiceTypesNotSpecified(exceptions.InvalidInput):
    message = _('service types are not speicfied')


class DeviceTemplateInUse(exceptions.InUse):
    message = _('device template %(device_template_id)s is still in use')


class DeviceInUse(exceptions.InUse):
    message = _('Device %(device_id)s is still in use')

class DeviceNoSubnet(exceptions.InvalidInput):
    message = _('Device have not subnet %(subnet_id)s')

class DeviceNoGateway(exceptions.InvalidInput):
    message = _('Subnet have not Gateway %(subnet_id)s')

class InvalidInfraDriver(exceptions.InvalidInput):
    message = _('invalid name for infra driver %(infra_driver)s')


class InvalidServiceType(exceptions.InvalidInput):
    message = _('invalid service type %(service_type)s')

class InvalidNetParams(exceptions.InvalidInput):
    message = _('Input params Invalied, not subnet_id and not network_id')

class DeviceCreateFailed(exceptions.NeutronException):
    message = _('creating device based on %(device_template_id)s failed')


class DeviceCreateWaitFailed(exceptions.NeutronException):
    message = _('waiting for creation of device %(device_id)s failed')


class DeviceDeleteFailed(exceptions.NeutronException):
    message = _('deleting device %(device_id)s failed')


class DeviceTemplateNotFound(exceptions.NotFound):
    message = _('device template %(device_tempalte_id)s could not be found')


class SeviceTypeNotFound(exceptions.NotFound):
    message = _('service type %(service_type_id)s could not be found')

class ServiceInstanceAttributeNotFound(exceptions.NotFound):
    message = _('service attr %(service_instance_id)s could not be found')

class MGMTSecurityGroupNotFound(exceptions.NotFound):
    message = _('mgmt port security group %(name)s could not be found')

class DeviceNotFound(exceptions.NotFound):
    message = _('device %(device_id)s could not be found')

class DeviceMGMGIpAddressNotFound(exceptions.NotFound):
    message = _('device %(device_id)s ip address  could not be found')

class ServiceInstanceNotManagedByUser(exceptions.InUse):
    message = _('service instance %(service_instance_id)s is '
                'managed by other service')

class ServiceInstanceInUse(exceptions.InUse):
    message = _('service instance %(service_instance_id)s is still in use')

class ServiceInstanceNotFound(exceptions.NotFound):
    message = _('service instance %(service_instance_id)s could not be found')

class DriverException(exceptions.NeutronException):
    """Exception created by the Driver class."""

class DriverNotExist(DriverException):
    message = _("Driver %(driver)s does not exist.")

class DriverNotSetForMissingParameter(DriverException):
    message = _("Driver cannot be set for missing parameter:%(p)s.")

class RPCNotFonundMethod(exceptions.NotFound):
    message = _('plugin %(plugin) can not find this method %(method)')

class NoAvaliableMgmtport(exceptions.NotFound):
    message = _('device is not a avaliable mgmt network %(device_id)s port')

class InvalidVPNAuth(exceptions.InvalidInput):
    message = _("password and user name must be an non-empty string "
                "with 6 to 20 ASCII characters without ' or \"")

def _validate_service_type_list(data, valid_values=None):
    if not isinstance(data, list):
        msg = _("invalid data format for service list: '%s'") % data
        LOG.debug(msg)
        return msg
    if not data:
        msg = _("empty list is not allowed for service list. '%s'") % data
        LOG.debug(msg)
        return msg
    key_specs = {
        'service_type': {
            'type:string': None,
        }
    }
    for service in data:
        msg = attr._validate_dict(service, key_specs)
        if msg:
            LOG.debug(msg)
            return msg


def _validate_service_context_list(data, valid_values=None):
    if not isinstance(data, list):
        msg = _("invalid data format for service context list: '%s'") % data
        LOG.debug(msg)
        return msg

    key_specs = {
        'network_id': {'type:uuid': None},
        'subnet_id': {'type:uuid': None},
        'port_id': {'type:uuid': None},
        'router_id': {'type:uuid': None},
        'role': {'type:string': None},
        'index': {'type:non_negative': None,
                  'convert_to': attr.convert_to_int},
    }
    for sc_entry in data:
        msg = attr._validate_dict_or_empty(sc_entry, key_specs=key_specs)
        if msg:
            LOG.debug(msg)
            return msg


attr.validators['type:service_type_list'] = _validate_service_type_list
attr.validators['type:service_context_list'] = _validate_service_context_list

def _validate_auth(auth, valid_values=None):
    for a in auth:
        if 'password' in a and 'username' in a:
            password = a['password']
            username = a['username']
        else:
            raise InvalidVPNAuth()

        if (attr.validators["type:not_empty_string_or_none"](password) or
                attr.validators["type:ascii_string"](password) or
                any(c in password for c in ("'", '"')) or
                not 5 < len(password) < 21) or (
                attr.validators["type:not_empty_string_or_none"](username) or
                attr.validators["type:ascii_string"](username) or
                any(c in username for c in ("'", '"')) or
                not 5 < len(username) < 21):

            raise InvalidVPNAuth()

attr.validators['type:auth'] = _validate_auth

RESOURCE_ATTRIBUTE_MAP = {

    'device_templates': {
        'id': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
            'primary_key': True,
        },
        'tenant_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'required_by_policy': True,
            'is_visible': True,
        },
        'name': {
            'allow_post': True,
            'allow_put': True,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'description': {
            'allow_post': True,
            'allow_put': True,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'shared': {
            'allow_post': True,
            'allow_put': True,
            'default': False,
            'convert_to': attr.convert_to_boolean,
            'is_visible': True, 
            'required_by_policy': True,
            'enforce_policy': True
       },
        'service_types': {
            'allow_post': True,
            'allow_put': False,
            'convert_to': attr.convert_to_list,
            'validate': {'type:service_type_list': None},
            'is_visible': True,
            'default': attr.ATTR_NOT_SPECIFIED,
        },
        'infra_driver': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': attr.ATTR_NOT_SPECIFIED,
        },
        'mgmt_driver': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': attr.ATTR_NOT_SPECIFIED,
        },
        'device_driver': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': attr.ATTR_NOT_SPECIFIED,
        },
        'created_at': {
            'allow_post': False,
            'allow_put': False,
            'is_visible': True
        },
        'attributes': {
            'allow_post': True,
            'allow_put': False,
            'convert_to': attr.convert_none_to_empty_dict,
            'validate': {'type:dict_or_nodata': None},
            'is_visible': True,
            'default': None,
        },
    },

    'devices': {
        'id': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
            'primary_key': True
        },
        'tenant_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'required_by_policy': True,
            'is_visible': True
        },
        'template_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
        },
        'name': {
            'allow_post': True,
            'allow_put': True,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'description': {
            'allow_post': True,
            'allow_put': True,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'created_at': {
            'allow_post': False,
            'allow_put': False,
            'is_visible': True
        },
        'instance_id': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
        },
        'mgmt_url': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
        },
        'auth': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:auth': None},
            'is_visible': True,
            'default': {},
        },
        'attributes': {
            'allow_post': True,
            'allow_put': True,
            'validate': {'type:dict_or_none': None},
            'is_visible': True,
            'default': {},
        },
        'services': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
        },
        'status': {
            'allow_post': False,
            'allow_put': False,
            'is_visible': True,
        },
        'power_state': {
            'allow_post': False,
            'allow_put': False,
            'is_visible': True,
        },
    },

    'service_instances': {
        'id': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
            'primary_key': True
        },
        'tenant_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'required_by_policy': True,
            'is_visible': True
        },
        'name': {
            'allow_post': True,
            'allow_put': True,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'created_at': {
            'allow_post': False,
            'allow_put': False,
            'is_visible': True
        },
        'service_type_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
        },
        'service_table_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'mgmt_driver': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'mgmt_url': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:string': None},
            'is_visible': True,
            'default': '',
        },
        'attributes': {
            'allow_post': True,
            'allow_put': True,
            'convert_to': attr.convert_none_to_empty_dict,
            'validate': {'type:dict_or_nodata': None},
            'is_visible': True,
            'default':None,
        },
        'devices': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:uuid_list': None},
            'convert_to': attr.convert_to_list,
            'is_visible': True,
        },
        'status': {
            'allow_post': False,
            'allow_put': False,
            'is_visible': True,
        },
    },
    'service_types': {
        'id': {
            'allow_post': False,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
            'primary_key': True
        },
        'template_id': {
            'allow_post': True,
            'allow_put': False,
            'validate': {'type:uuid': None},
            'is_visible': True,
        },
        'servicetype': {
            'allow_post': True,
            'allow_put': False,
            'convert_to': attr.convert_to_list,
            'validate': {'type:service_type_list': None},
            'is_visible': True,
            'default': attr.ATTR_NOT_SPECIFIED,
        },
    }
}


class Servicevm(extensions.ExtensionDescriptor):
    @classmethod
    def get_name(cls):
        return 'Service VM'

    @classmethod
    def get_alias(cls):
        return 'servicevm'

    @classmethod
    def get_description(cls):
        return "Extension for ServiceVM service"

    @classmethod
    def get_namespace(cls):
        return 'http://wiki.openstack.org/Tacker/ServiceVM'

    @classmethod
    def get_updated(cls):
        return "2013-11-19T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        my_plurals = [(key, key[:-1]) for key in RESOURCE_ATTRIBUTE_MAP.keys()]
        attr.PLURALS.update(dict(my_plurals))
        exts = []
        plugin = manager.NeutronManager.get_service_plugins()[
            constants.SERVICEVM]
        for resource_name in ['device', 'device_template', 'service_instance', 'service_type']:
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
                    path_prefix=constants.COMMON_PREFIXES[constants.SERVICEVM],
                    attr_map=params)
            exts.append(ex)
        return exts

    @classmethod
    def get_plugin_interface(cls):
        return ServiceVMPluginBase

    def update_attributes_map(self, attributes):
        super(Servicevm, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        version_map = {'1.0': RESOURCE_ATTRIBUTE_MAP}
        return version_map.get(version, {})


@six.add_metaclass(abc.ABCMeta)
class ServiceVMPluginBase(ServicePluginBase):

    def get_plugin_name(self):
        return constants.SERVICEVM

    def get_plugin_type(self):
        return constants.SERVICEVM

    def get_plugin_description(self):
        return 'Service VM plugin'

    @abc.abstractmethod
    def create_device_template(self, context, device_template):
        pass

    @abc.abstractmethod
    def delete_device_template(self, context, device_template_id):
        pass

    @abc.abstractmethod
    def get_device_template(self, context, device_template_id, fields=None):
        pass

    @abc.abstractmethod
    def get_device_templates(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_devices(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_device(self, context, device_id, fields=None):
        pass

    @abc.abstractmethod
    def create_device(self, context, device):
        pass

    @abc.abstractmethod
    def update_device(
            self, context, device_id, device):
        pass

    @abc.abstractmethod
    def delete_device(self, context, device_id):
        pass

    @abc.abstractmethod
    def attach_interface(self, context, id, port_id):
        pass

    @abc.abstractmethod
    def detach_interface(self, contexct, id, port_id):
        pass

    @abc.abstractmethod
    def get_service_instances(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_service_instance(self, context, service_instance_id, fields=None):
        pass

    @abc.abstractmethod
    def update_service_instance(self, context, service_instance_id,
                                service_instance):
        pass

    @abc.abstractmethod
    def get_service_types(self, context, filters=None, fields=None):
        pass
