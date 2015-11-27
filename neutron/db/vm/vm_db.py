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

import copy
import uuid

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc as orm_exc

from neutron.api.v2 import attributes
from neutron.common import core as sql
from neutron.common import constants as n_constants
from neutron.common import utils
from neutron import context as t_context
from neutron.db import api as qdbapi
from neutron.db import common_db_mixin
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db import l3_db as l3
from neutron.extensions import servicevm
from neutron.extensions import l3 as l3_ext
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.openstack.common import timeutils
from neutron.plugins.common import constants
from neutron.plugins.openvswitch import ovs_db_v2
from neutron.services.vm.common import constants as s_constants
from neutron.services.vm.mgmt_drivers.rpc import svm_rpc_joint_agent_api

LOG = logging.getLogger(__name__)

_ACTIVE_UPDATE = (constants.ACTIVE, constants.PENDING_UPDATE)
_ACTIVE = constants.ACTIVE
_ACTIVE_UPDATE_ERROR_DEAD = (
    constants.PENDING_CREATE, constants.ACTIVE, constants.PENDING_UPDATE,
    constants.ERROR, constants.DEAD)
DEVICE_OWNER_ROUTER_INTF = n_constants.DEVICE_OWNER_ROUTER_INTF
DEVICE_OWNER_ROUTER_GW = n_constants.DEVICE_OWNER_ROUTER_GW
DEVICE_OWNER_FLOATINGIP = n_constants.DEVICE_OWNER_FLOATINGIP
EXTERNAL_GW_INFO = l3_ext.EXTERNAL_GW_INFO
INSTANCE_HOST_ATTR = 'OS-EXT-SRV-ATTR:host'


###########################################################################
# db tables

class DeviceTemplate(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents template to create hosting device
    """
    # Descriptive name
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))

    # service type that this service vm provides.
    # At first phase, this includes only single service
    # In future, single service VM may accomodate multiple services.
    service_types = orm.relationship('ServiceType', backref='template')

    # driver to create hosting device. e.g. noop, nova, heat, etc...
    infra_driver = sa.Column(sa.String(255))

    # driver to communicate with service managment
    mgmt_driver = sa.Column(sa.String(255))

    # vendor driver for device
    device_driver = sa.Column(sa.String(255))

    # if shared is True, all user access the template
    shared = sa.Column(sa.Boolean(), nullable=False)

    created_at = sa.Column('created_at', sa.DateTime(), nullable=True)
    # (key, value) pair to spin up
    attributes = orm.relationship('DeviceTemplateAttribute',
                                  backref='template')


class ServiceType(model_base.BASEV2, models_v2.HasId):#, models_v2.HasTenant):
    """Represents service type which hosting device provides.
    Since a device may provide many services, This is one-to-many
    relationship.
    """
    template_id = sa.Column(sa.String(36), sa.ForeignKey('devicetemplates.id'),
                            nullable=False)
    servicetype = sa.Column(sa.String(255), nullable=False)


class DeviceTemplateAttribute(model_base.BASEV2, models_v2.HasId):
    """Represents attributes necessary for spinning up VM in (key, value) pair
    key value pair is adopted for being agnostic to actuall manager of VMs
    like nova, heat or others. e.g. image-id, flavor-id for Nova.
    The interpretation is up to actual driver of hosting device.
    """
    template_id = sa.Column(sa.String(36), sa.ForeignKey('devicetemplates.id'),
                            nullable=False)
    key = sa.Column(sa.String(255), nullable=False)
    #value = sa.Column(sa.String(4096), nullable=True)
    value = sa.Column(sql.JsonCom(), nullable=False)


class Device(model_base.BASEV2, models_v2.HasTenant):
    """Represents devices that hosts services.
    Here the term, 'VM', is intentionally avoided because it can be
    VM or other container.
    """
    id = sa.Column(sa.String(255),
                   primary_key=True,
                   default=uuidutils.generate_uuid)

    template_id = sa.Column(sa.String(36), sa.ForeignKey('devicetemplates.id'))
    template = orm.relationship('DeviceTemplate')

    name = sa.Column(sa.String(255), nullable=True)
    description = sa.Column(sa.String(255), nullable=True)

    # sufficient information to uniquely identify hosting device.
    # In case of service VM, it's UUID of nova VM.
    instance_id = sa.Column(sa.String(255), nullable=True)

    # For a management tool to talk to manage this hosting device.
    # opaque string.
    # e.g. (driver, mgmt_url) = (ssh, ip address), ...
    mgmt_url = sa.Column(sql.JsonCom(), nullable=True)

    # device auth info
    auth = sa.Column(sql.JsonCom(), nullable=True)

    attributes = orm.relationship("DeviceAttribute", backref="device")

    services = orm.relationship('ServiceDeviceBinding', backref='device')

    status = sa.Column(sa.String(255), nullable=False)

    created_at = sa.Column('created_at', sa.DateTime(), nullable=True)

    power_state = sa.Column('power_state', sa.String(36),
                            default=constants.DOWN, nullable=True)


class DeviceAttribute(model_base.BASEV2, models_v2.HasId):
    """Represents kwargs necessary for spinning up VM in (key, value) pair
    key value pair is adopted for being agnostic to actuall manager of VMs
    like nova, heat or others. e.g. image-id, flavor-id for Nova.
    The interpretation is up to actual driver of hosting device.
    """
    device_id = sa.Column(sa.String(255), sa.ForeignKey('devices.id'),
                          nullable=False)
    key = sa.Column(sa.String(255), nullable=False)
    # json encoded value. example
    # "nic": [{"net-id": <net-uuid>}, {"port-id": <port-uuid>}]
    #value = sa.Column(sa.String(4096), nullable=True)
    value = sa.Column(sql.JsonCom(), nullable=True)


# this table corresponds to ServiceInstance of the original spec
class ServiceInstance(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents logical service instance
    This table is only to tell what logical service instances exists.
    There will be service specific tables for each service types which holds
    actuall parameters necessary for specific service type.
    For example, tables for "Routers", "LBaaS", "FW", tables. which table
    is implicitly determined by service_type_id.
    """
    name = sa.Column(sa.String(255), nullable=True)
    service_type_id = sa.Column(sa.String(36),
                                sa.ForeignKey('servicetypes.id'))
    service_type = orm.relationship('ServiceType')
    servicetype = sa.Column(sa.String(255), nullable=False)
    # points to row in service specific table if any.
    service_table_id = sa.Column(sa.String(36), nullable=True)

    # True: This service is managed by user so that user is able to
    #       change its configurations
    # False: This service is manged by other neutron service like lbaas
    #        so that user can't change the configuration directly via
    #        servicevm API, but via API for the service.
    managed_by_user = sa.Column(sa.Boolean(), default=False)

    # mgmt driver to communicate with logical service instance in
    # hosting device.
    # e.g. noop, OpenStack MGMT, OpenStack notification, netconf, snmp,
    #      ssh, etc...
    mgmt_driver = sa.Column(sa.String(255))

    # For a management tool to talk to manage this service instance.
    # opaque string. mgmt_driver interprets it.
    mgmt_url = sa.Column(sql.JsonCom(), nullable=True)

    attributes = orm.relationship("ServiceInstanceAttribute",
                             backref="serviceinstance")
    devices = orm.relationship('ServiceDeviceBinding')

    status = sa.Column(sa.String(255), nullable=False)

    created_at = sa.Column('created_at', sa.DateTime(), nullable=True)

    # TODO(yamahata): re-think the necessity of following columns
    #                 They are all commented out for minimalism for now.
    #                 They will be added when it is found really necessary.
    #
    # multi_tenant = sa.Column(sa.Boolean())
    # state = sa.Column(sa.Enum('UP', 'DOWN',
    #                           name='service_instance_state'))
    # For a logical service instance in hosting device to recieve
    # requests from management tools.
    # opaque string. mgmt_driver interprets it.
    # e.g. the name of the interface inside the VM + protocol
    # vm_mgmt_if = sa.Column(sa.String(255), default=None, nullable=True)
    # networks =
    # obj_store =
    # cost_factor =


class ServiceInstanceAttribute(model_base.BASEV2, models_v2.HasId):
    """Represents kwargs necessary for spinning up VM in (key, value) pair
    key value pair is adopted for being agnostic to actuall manager of VMs
    like nova, heat or others. e.g. image-id, flavor-id for Nova.
    The interpretation is up to actual driver of hosting device.
    """
    service_instance_id = sa.Column(sa.String(255), 
                           sa.ForeignKey('serviceinstances.id'),
                           nullable=False)
    key = sa.Column(sa.String(255), nullable=False)
    # json encoded value. example
    # "nic": [{"net-id": <net-uuid>}, {"port-id": <port-uuid>}]
    #value = sa.Column(sa.String(4096), nullable=True)
    value = sa.Column(sql.JsonCom(), nullable=True)

class ServiceDeviceBinding(model_base.BASEV2):
    """Represents binding with Device and LogicalResource.
    Since Device can accomodate multiple services, it's many-to-one
    relationship.
    """
    service_instance_id = sa.Column(
        sa.String(36), sa.ForeignKey('serviceinstances.id'), primary_key=True)
    device_id = sa.Column(sa.String(36), sa.ForeignKey('devices.id'),
                          primary_key=True)

class DeviceAgentBinding(model_base.BASEV2):
    """Respresents binding between device and ServiceVM agents."""

    device_id = sa.Column(sa.String(36),
                          sa.ForeignKey("devices.id", ondelete='CASCADE'),
                          primary_key=True)
    servicevm_agent_id = sa.Column(sa.String(36),
                          sa.ForeignKey("agents.id", ondelete='CASCADE'),
                          primary_key=True)


###########################################################################
class ServiceResourcePluginDb(servicevm.ServiceVMPluginBase,
                              common_db_mixin.CommonDbMixin):

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    @property
    def l3_plugin(self):
        try:
            return self._l3_plugin
        except AttributeError:
            self._l3_plugin = manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)
            return self._l3_plugin

    def subnet_id_to_network_id(self, context, subnet_id):
        subnet = self._core_plugin.get_subnet(context, subnet_id)
        return subnet['network_id']

    def __init__(self):
        qdbapi.register_models()
        super(ServiceResourcePluginDb, self).__init__()

    def _get_resource(self, context, model, id):
        try:
            return self._get_by_id(context, model, id)
        except orm_exc.NoResultFound:
            if issubclass(model, DeviceTemplate):
                raise servicevm.DeviceTemplateNotFound(device_tempalte_id=id)
            elif issubclass(model, ServiceType):
                raise servicevm.ServiceTypeNotFound(service_type_id=id)
            elif issubclass(model, ServiceInstance):
                raise servicevm.ServiceInstanceNotFound(service_instance_id=id)
            elif issubclass(model, DeviceAgentBinding):
                raise servicevm.DeviceNotFound(device_id=id)
            if issubclass(model, Device):
                raise servicevm.DeviceNotFound(device_id=id)
            if issubclass(model, ServiceInstanceAttribute):
                raise servicevm.ServiceInstanceAttributeNotFound(service_instance_id=id)
            else:
                raise

    def _make_attributes_dict(self, attributes_db):
        return dict((attr.key, attr.value) for attr in attributes_db)

    def _make_service_types_list(self, service_types):
        return [{'id': service_type.id,
                 'service_type': service_type.servicetype}
                for service_type in service_types]

    def _make_template_dict(self, template, fields=None):
        res = {
            'attributes':
             self._make_attributes_dict(template['attributes']),
            'service_types':
             self._make_service_types_list(template.service_types)
        }
        key_list = ('id', 'tenant_id', 'name', 'description',
                    'shared','infra_driver', 'mgmt_driver',
                    'device_driver', 'created_at')
        res.update((key, template[key]) for key in key_list)
        return self._fields(res, fields)

    def _make_services_list(self, binding_db):
        return [binding.service_instance_id for binding in binding_db]

    def _make_dev_attrs_dict(self, dev_attrs_db):
        return dict((arg.key, arg.value) for arg in dev_attrs_db)

    def _make_device_dict(self, device_db, fields=None):
        LOG.debug(_('device_db %s'), device_db)
        LOG.debug(_('device_db attributes %s'), device_db.attributes)
        res = {
            'services':
            self._make_services_list(getattr(device_db, 'services', [])),
            'device_template':
            self._make_template_dict(device_db.template),
            'attributes': 
            self._make_dev_attrs_dict(device_db.attributes),
        }
        key_list = ('id', 'tenant_id', 'name', 'description', 'instance_id',
                    'template_id', 'status', 'mgmt_url', 'created_at',
                    'power_state', 'auth')
        res.update((key, device_db[key]) for key in key_list)
        return self._fields(res, fields)

    def _make_service_type_dict(self, service_type_db, fields=None):
        res = {}
        key_list = ('id', 'servicetype', 'template_id')
        res.update((key, service_type_db[key]) for key in key_list)
        return self._fields(res, fields)

    def _make_service_device_list(self, devices):
        return [binding.device_id for binding in devices]

    #def get_service_instance_attr(self, context, service_instance_id, fields=None):
    #    service_instance_attr__db = self._get_resource(context, ServiceInstanceAttribute,
    #                                                   service_instance_id)
    #    return self._make_service_attr_dict(service_instance_attr__db)

    def _make_service_instance_dict(self, instance_db, fields=None):
        res = {
            'attributes': 
            self._make_attributes_dict(instance_db['attributes']),
            'devices':
            self._make_service_device_list(instance_db.devices),
            'service_type':
            self._make_service_type_dict(instance_db.service_type)
        }
        key_list = ('id', 'tenant_id', 'name', 'service_type_id',
                    'service_table_id', 'mgmt_driver', 'mgmt_url',
                    'status', 'created_at')
        res.update((key, instance_db[key]) for key in key_list)
        return self._fields(res, fields)

    @staticmethod
    def _infra_driver_name(device_dict):
        return device_dict['device_template']['infra_driver']

    @staticmethod
    def _mgmt_driver_name(device_dict):
        return device_dict['device_template']['mgmt_driver']

    @staticmethod
    def _device_driver_name(device_dict):
        return device_dict['device_template']['device_driver']

    @staticmethod
    def _instance_id(device_dict):
        return device_dict['instance_id']

    ###########################################################################
    # hosting device template

    def create_device_template(self, context, device_template):
        template = device_template['device_template']
        LOG.debug(_('template %s'), template)
        tenant_id = self._get_tenant_id_for_create(context, template)
        infra_driver = template.get('infra_driver')
        mgmt_driver = template.get('mgmt_driver')
        device_driver = template.get('device_driver')
        service_types = template.get('service_types')
        shared = template.get('shared')

        if (not attributes.is_attr_set(infra_driver)):
            LOG.debug(_('hosting device driver unspecified'))
            raise servicevm.InfraDriverNotSpecified()
        if (not attributes.is_attr_set(mgmt_driver)):
            LOG.debug(_('mgmt driver unspecified'))
            raise servicevm.MGMTDriverNotSpecified()
        if (not attributes.is_attr_set(service_types)):
            LOG.debug(_('service types unspecified'))
            raise servicevm.SeviceTypesNotSpecified()

        with context.session.begin(subtransactions=True):
            template_id = str(uuid.uuid4())
            template_db = DeviceTemplate(
                id=template_id,
                tenant_id=tenant_id,
                name=template.get('name'),
                description=template.get('description'),
                infra_driver=infra_driver,
                device_driver=device_driver,
                shared=shared,
                created_at=timeutils.utcnow(),
                mgmt_driver=mgmt_driver)
            utils.make_default_name(template_db, s_constants.PRE_DEV_TEM)
            context.session.add(template_db)
            for (key, value) in template.get('attributes', {}).items():
                attribute_db = DeviceTemplateAttribute(
                    id=str(uuid.uuid4()),
                    template_id=template_id,
                    key=key,
                    value=value)
                context.session.add(attribute_db)
            for service_type in (item['service_type']
                                 for item in template['service_types']):
                service_type_db = ServiceType(
                    id=str(uuid.uuid4()),
                    template_id=template_id,
                    servicetype=service_type)
                context.session.add(service_type_db)

        LOG.debug(_('template_db %(template_db)s %(attributes)s '),
                  {'template_db': template_db,
                   'attributes': template_db.attributes})
        return self._make_template_dict(template_db)

    def update_device_template(self, context, device_template_id,
                               device_template):
        with context.session.begin(subtransactions=True):
            template_db = self._get_resource(context, DeviceTemplate,
                                             device_template_id)
            template_db.update(device_template['device_template'])
        return self._make_template_dict(template_db)

    def delete_device_template(self, context, device_template_id):
        with context.session.begin(subtransactions=True):
            # TODO(yamahata): race. prevent from newly inserting hosting device
            #                 that refers to this template
            devices_db = context.session.query(Device).filter_by(
                template_id=device_template_id).first()
            if devices_db is not None:
                raise servicevm.DeviceTemplateInUse(
                    device_template_id=device_template_id)

            context.session.query(ServiceType).filter_by(
                template_id=device_template_id).delete()
            context.session.query(DeviceTemplateAttribute).filter_by(
                template_id=device_template_id).delete()
            template_db = self._get_resource(context, DeviceTemplate,
                                             device_template_id)
            context.session.delete(template_db)

    def get_device_template(self, context, device_template_id, fields=None):
        template_db = self._get_resource(context, DeviceTemplate,
                                         device_template_id)
        return self._make_template_dict(template_db)

    def get_device_templates(self, context, filters, fields=None):
        return self._get_collection(context, DeviceTemplate,
                                    self._make_template_dict,
                                    filters=filters, fields=fields)

    # called internally, not by REST API
    # need enhancement?
    def choose_device_template(self, context, service_type,
                               required_attributes=None):
        required_attributes = required_attributes or []
        LOG.debug(_('required_attributes %s'), required_attributes)
        with context.session.begin(subtransactions=True):
            query = (
                context.session.query(DeviceTemplate).
                filter(
                    sa.exists().
                    where(sa.and_(
                        DeviceTemplate.id == ServiceType.template_id,
                        ServiceType.service_type == service_type))))
            for key in required_attributes:
                query = query.filter(
                    sa.exists().
                    where(sa.and_(
                        DeviceTemplate.id ==
                        DeviceTemplateAttribute.template_id,
                        DeviceTemplateAttribute.key == key)))
            LOG.debug(_('statements %s'), query)
            template_db = query.first()
            if template_db:
                return self._make_template_dict(template_db)

    ###########################################################################
    # hosting device

    def _device_attribute_update_or_create(
            self, context, device_id, key, value):
        arg = (self._model_query(context, DeviceAttribute).
            filter(DeviceAttribute.device_id == device_id).
            filter(DeviceAttribute.key == key).first())
        if arg:
            arg.value = value
        else:
            arg = DeviceAttribute(
                id=str(uuid.uuid4()), device_id=device_id,
                key=key, value=value)
            context.session.add(arg)

    # called internally, not by REST API
    def _create_device_pre(self, context, device):
        device = device['device']
        LOG.debug(_('device %s'), device)
        tenant_id = self._get_tenant_id_for_create(context, device)
        template_id = device['template_id']
        auth = device['auth']
        name = device.get('name')
        device_id = device.get('id') or str(uuid.uuid4())
        attributes = device.get('attributes', {})
        with context.session.begin(subtransactions=True):
            template_db = self._get_resource(context, DeviceTemplate,
                                             template_id)
            device_db = Device(id=device_id,
                               tenant_id=tenant_id,
                               name=name,
                               description=template_db.description,
                               instance_id=None,
                               template_id=template_id,
                               created_at=timeutils.utcnow(),
                               status=constants.PENDING_CREATE,
                               auth=auth,
                               power_state=constants.DOWN)
            utils.make_default_name(device_db, s_constants.PRE_DEVICE)
            context.session.add(device_db)
            for key, value in attributes.items():
                arg = DeviceAttribute(
                    id=str(uuid.uuid4()), device_id=device_id,
                    key=key, value=value)
                context.session.add(arg)

        return self._make_device_dict(device_db)

    # called internally, not by REST API
    # intsance_id = None means error on creation
    def _create_device_post(self, context, device_id, instance_id,
                            mgmt_url, device_dict):
        LOG.debug(_('device_dict %s'), device_dict)
        with context.session.begin(subtransactions=True):
            query = (self._model_query(context, Device).
                     filter(Device.id == device_id).
                     filter(Device.status == constants.PENDING_CREATE).
                     one())
            # (xining) if create instance fail, instance_id is None, It can
            # not update db
            #query.update({'instance_id': instance_id, 'mgmt_url': mgmt_url})
            if instance_id is None:
                query.update({'status': constants.ERROR})
                query.update({'mgmt_url': mgmt_url})
                query.update({'instance_id': device_dict['instance_id']})
            else:
                query.update({'instance_id': instance_id, 'mgmt_url': mgmt_url})

            for (key, value) in device_dict['attributes'].items():
                self._device_attribute_update_or_create(context, device_id,
                                                        key, value)

    def _register_agent_binding(self, context, device_id, instance):
        host = getattr(instance, INSTANCE_HOST_ATTR)
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(context,
                       n_constants.AGENT_TYPE_SERVICEVM, host)
        with context.session.begin(subtransactions=True):
            binding_db = DeviceAgentBinding(device_id=device_id,
                                        servicevm_agent_id=agent['id'])
            context.session.add(binding_db)

    def _create_device_status(self, context, device_id, new_status):
        with context.session.begin(subtransactions=True):
            (self._model_query(context, Device).
                filter(Device.id == device_id).
                filter(Device.status == constants.PENDING_CREATE).
                update({'status': new_status}))

    def _get_device_db(self, context, device_id, current_statuses, new_status):
        try:
            device_db = (
                self._model_query(context, Device).
                filter(Device.id == device_id).
                filter(Device.status.in_(current_statuses)).
                with_lockmode('update').one())
        except orm_exc.NoResultFound:
            raise servicevm.DeviceNotFound(device_id=device_id)
        if device_db.status == constants.PENDING_UPDATE:
            raise servicevm.DeviceInUse(device_id=device_id)
        device_db.update({'status': new_status})
        return device_db

    def _update_device_pre(self, context, device_id):
        with context.session.begin(subtransactions=True):
            device_db = self._get_device_db(
                context, device_id, _ACTIVE_UPDATE, constants.PENDING_UPDATE)
        return self._make_device_dict(device_db)

    def _update_device_post(self, context, device_id, new_status,
                            new_device_dict=None):
        with context.session.begin(subtransactions=True):
            (self._model_query(context, Device).
             filter(Device.id == device_id).
             filter(Device.status == constants.PENDING_UPDATE).
             update({'status': new_status}))

            dev_attrs = new_device_dict.get('attributes', {})
            (context.session.query(DeviceAttribute).
             filter(DeviceAttribute.device_id == device_id).
             filter(~DeviceAttribute.key.in_(dev_attrs.keys())).
             delete(synchronize_session='fetch'))

            
            for (key, value) in dev_attrs.items():
                self._device_attribute_update_or_create(context, device_id,
                                                        key, value)

    def update_device_name_or_desc(self, context, device_id, name=None,
                                   desc=None):
        with context.session.begin(subtransactions=True):
            (self._model_query(context, Device).
             filter(Device.id == device_id).
             one().
             update({'name': name,
                     'description': desc}))

    def _delete_device_pre(self, context, device_id):
        with context.session.begin(subtransactions=True):
            # TODO(yamahata): race. keep others from inserting new binding
            binding_db = (context.session.query(ServiceDeviceBinding).
                          filter_by(device_id=device_id).first())
            if binding_db is not None:
                raise servicevm.DeviceInUse(device_id=device_id)
            device_db = self._get_device_db(
                context, device_id, _ACTIVE_UPDATE_ERROR_DEAD,
                constants.PENDING_DELETE)

        return self._make_device_dict(device_db)

    def _delete_device_post(self, context, device_id, error):
        with context.session.begin(subtransactions=True):
            query = (
                self._model_query(context, Device).
                filter(Device.id == device_id).
                filter(Device.status == constants.PENDING_DELETE))
            if error:
                query.update({'status': constants.ERROR})
                #(self._model_query(context, Device).
                # filter(Device.id == device_id).delete())
            else:
                (self._model_query(context, DeviceAttribute).
                 filter(DeviceAttribute.device_id == device_id).delete())
                (self._model_query(context, Device).
                 filter(Device.id == device_id).delete())
                #(self._model_query(context, DeviceServiceContext).
                # filter(DeviceServiceContext.device_id == device_id).delete())
                query.delete()

    # reference implementation. needs to be overrided by subclass
    def create_device(self, context, device):
        device_dict = self._create_device_pre(context, device)
        # start actual creation of hosting device.
        # Waiting for completion of creation should be done backgroundly
        # by another thread if it takes a while.
        instance_id = str(uuid.uuid4())
        device_dict['instance_id'] = instance_id
        self._create_device_post(context, device_dict['id'], instance_id, None,
                                 device_dict)
        self._create_device_status(context, device_dict['id'],
                                   constants.ACTIVE)
        return device_dict

    # reference implementation. needs to be overrided by subclass
    def update_device(self, context, device_id, device):
        device_dict = self._update_device_pre(context, device_id)
        # start actual update of hosting device
        # waiting for completion of update should be done backgroundly
        # by another thread if it takes a while
        self._update_device_post(context, device_id, constants.ACTIVE)
        return device_dict

    # reference implementation. needs to be overrided by subclass
    def delete_device(self, context, device_id):
        self._delete_device_pre(context, device_id)
        # start actual deletion of hosting device.
        # Waiting for completion of deletion should be done backgroundly
        # by another thread if it takes a while.
        self._delete_device_post(context, device_id, False)

    def get_device(self, context, device_id, fields=None):
        device_db = self._get_resource(context, Device, device_id)
        return self._make_device_dict(device_db, fields)

    def get_devices(self, context, filters=None, fields=None):
        devices = self._get_collection(context, Device, self._make_device_dict,
                                    filters=filters, fields=fields)
        # Ugly hack to mask internaly used record
        a = [device for device in devices
                if uuidutils.is_uuid_like(device['id'])]
        return a

    def _mark_device_status(self, device_id, exclude_status, new_status):
        context = t_context.get_admin_context()
        with context.session.begin(subtransactions=True):
            try:
                device_db = (
                    self._model_query(context, Device).
                    filter(Device.id == device_id).
                    filter(~Device.status.in_(exclude_status)).
                    with_lockmode('update').one())
            except orm_exc.NoResultFound:
                LOG.warn(_('no device found %s'), device_id)
                return False

            device_db.update({'status': new_status})
        return True

    def _mark_device_error(self, device_id):
        return self._mark_device_status(
            device_id, [constants.DEAD], constants.ERROR)

    def _mark_device_dead(self, device_id):
        exclude_status = [
            constants.DOWN,
            constants.PENDING_CREATE,
            constants.PENDING_UPDATE,
            constants.PENDING_DELETE,
            constants.INACTIVE,
            constants.ERROR]
        return self._mark_device_status(
            device_id, exclude_status, constants.DEAD)

    # used by failure policy
    def rename_device_id(self, context, device_id, new_device_id):
        # ugly hack...
        context = t_context.get_admin_context()
        with context.session.begin(subtransactions=True):
            device_db = self._get_resource(context, Device, device_id)
            new_device_db = Device(
                id=new_device_id,
                tenant_id=device_db.tenant_id,
                template_id=device_db.template_id,
                name=device_db.name,
                description=device_db.description,
                instance_id=device_db.instance_id,
                created_at=timeutils.utcnow(),
                mgmt_url=device_db.mgmt_url,
                status=device_db.status)
            context.session.add(new_device_db)

            (self._model_query(context, DeviceAttribute).
             filter(DeviceAttribute.device_id == device_id).
             update({'device_id': new_device_id}))
            context.session.delete(device_db)

    ###########################################################################
    # logical service instance
    def _get_service_type(self, context, service_type_id):
        service_type_db = self._get_resource(context, ServiceType,
                                         service_type_id)
        return service_type_db['servicetype']

    # called internally, not by REST API
    def _create_service_instance(self, context, device_id,
                                 service_instance_param, managed_by_user):
        """
        :param service_instance_param: dictionary to create
            instance of ServiceInstance. The following keys are used.
            name, service_type_id, service_table_id, mgmt_driver, mgmt_url
        mgmt_driver, mgmt_url can be determined later.
        """
        name = service_instance_param['name']
        service_type_id = service_instance_param['service_type_id']
        service_table_id = service_instance_param['service_table_id']
        mgmt_driver = service_instance_param.get('mgmt_driver')
        mgmt_url = service_instance_param.get('mgmt_url')

        servicetype = self._get_service_type(context, service_type_id)

        service_instance_id = str(uuid.uuid4())
        LOG.debug('service_instance_id %s device_id %s',
                  service_instance_id, device_id)
        with context.session.begin(subtransactions=True):
            # TODO(yamahata): race. prevent modifying/deleting service_type
            # with_lockmode("update")
            device_db = self._get_resource(context, Device, device_id)
            device_dict = self._make_device_dict(device_db)
            tenant_id = self._get_tenant_id_for_create(context, device_dict)
            instance_db = ServiceInstance(
                id=service_instance_id,
                tenant_id=tenant_id,
                name=name,
                service_type_id=service_type_id,
                service_table_id=service_table_id,
                servicetype=servicetype,
                managed_by_user=managed_by_user,
                status=constants.PENDING_CREATE,
                mgmt_driver=mgmt_driver,
                created_at=timeutils.utcnow(),
                mgmt_url=mgmt_url)
            utils.make_default_name(instance_db, s_constants.PRE_SERVICE)
            context.session.add(instance_db)
            context.session.flush()
            self._add_service_instance_attr(context, service_instance_param,
                                            service_instance_id)

            binding_db = ServiceDeviceBinding(
                service_instance_id=service_instance_id, device_id=device_id)
            context.session.add(binding_db)

        return self._make_service_instance_dict(instance_db)

    def _update_attr_value(self, context, service_param, sid):
       service_instance_db = self.get_service_instance(context, sid)
       port_db_dict = {}
       no_port_db_list = []
       port_dict = {}
       no_port_list = []
       for key, value in service_instance_db['attributes'].items():
           for v in value:
               if v['floatingip_id']:
                   fip_ids = port_db_dict.get(v['fixed_port_id'], [])
                   fip_ids.append(v['floatingip_id'])
                   port_db_dict.update({v['fixed_port_id']: fip_ids})
               else:
                   no_port_db_list.append(v['fixed_port_id'])

       for key, value in service_param['attributes'].items():
           for v in value:
               if v['floatingip_id']:
                   fip_ids = port_dict.get(v['fixed_port_id'], [])
                   fip_ids.append(v['floatingip_id'])
                   port_dict.update({v['fixed_port_id']: fip_ids})
               else:
                   no_port_list.append(v['fixed_port_id'])
       for (port_id, fip_ids) in port_dict.items():
           bind_fip_ids = list(set(fip_ids) - set(port_db_dict.get(port_id, [])))
           for fip_id in bind_fip_ids:
               admin_context = t_context.get_admin_context()
               port = self._core_plugin.get_port(admin_context, port_id)
               ip_address = port['fixed_ips'][0]['ip_address']
               svm_fip_db = self.l3_plugin._get_floatingip(context, fip_id)
               svm_fip_db.update({'fixed_ip_address': ip_address,
                                  'service_instance_id': sid,
                                  'fixed_port_id': port_id})
       for (port_id, fip_ids) in port_db_dict.items():
           no_bind_fip_ids = list(set(fip_ids) - set(port_dict.get(port_id, [])))
           for fip_id in no_bind_fip_ids:
               svm_fip_db = self.l3_plugin._get_floatingip(context, fip_id)
               svm_fip_db.update({'service_instance_id': None,
                                  'fixed_port_id': None,
                                  'fixed_ip_address': None})

    def _add_attr_value(self, context, service_param, sid):
       admin_context = t_context.get_admin_context()
       with admin_context.session.begin(subtransactions=True):
           for (key, values) in \
                  service_param.get('attributes', {}).items():
               if key in [s_constants.EXTERNAL_GATWAY_KEY,
                          s_constants.FLOATINGIP_KEY]:
                   for value in values:
                       fip_id = value.get('floatingip_id', None)
                       fixed_port_id = value.get('fixed_port_id')
                       port = self._core_plugin.get_port(admin_context, fixed_port_id)
                       if fip_id:
                           ip_address = port['fixed_ips'][0]['ip_address']
                           floatingip_db = self.l3_plugin._get_floatingip(context, fip_id)
                           floatingip_db.update({'fixed_ip_address': ip_address,
                                                 'service_instance_id': sid,
                                                 'fixed_port_id': fixed_port_id})
                       if fixed_port_id:
                           svm_port_db = self._core_plugin._get_port(admin_context, fixed_port_id)
                           svm_port_db.update({'service_instance_id': sid})

    def _add_service_instance_attr(self, context, service_param, sid):
       for (key, value) in \
              service_param.get('attributes', {}).items():

           attribute_db = ServiceInstanceAttribute(
               id=str(uuid.uuid4()),
               service_instance_id=sid,
               key=key,
               value=value)
           context.session.add(attribute_db)

       self._add_attr_value(context, service_param, sid)

    # reference implementation. must be overriden by subclass
    def create_service_instance(self, context, service_instance):
        self._create_service_instance(
            context, service_instance['service_instance'], True)

    def _service_instance_attribute_update_or_create(
            self, context, service_instance_id, key, value):
        arg = (self._model_query(context, ServiceInstanceAttribute).
            filter(ServiceInstanceAttribute.service_instance_id == service_instance_id).
            filter(ServiceInstanceAttribute.key == key).first())
        if arg:
            arg.value = value
        else:
            arg = ServiceInstanceAttribute(
                id=str(uuid.uuid4()), 
                service_instance_id=service_instance_id,
                key=key, value=value)
            context.session.add(arg)

    def _update_service_instance_mgmt(self, context, service_instance_id,
                                      mgmt_driver, mgmt_url):
        with context.session.begin(subtransactions=True):
            (self._model_query(context, ServiceInstance).
             filter(ServiceInstance.id == service_instance_id).
             filter(ServiceInstance.status == constants.PENDING_CREATE).
             one().
             update({'mgmt_driver': mgmt_driver,
                     'mgmt_url': mgmt_url}))

    def _update_service_instance_check(self, context, service_instance_id,
                                       service_instance):
        service_instace = self.get_service_instance(context, service_instance_id)
        attr = copy.deepcopy(service_instace['attributes'])
        service = service_instance['service_instance']
        for key, value in service.get('attributes', {}).iteritems():
            if key in attr.keys() and attr[key] != value:
                del attr[key]
                return True
            if key in attr.keys():
                del attr[key]
        if attr:
            return True
        return False

    def _update_service_instance_pre(self, context, service_instance_id,
                                     service_instance):
        with context.session.begin(subtransactions=True):
            instance_db = (
                self._model_query(context, ServiceInstance).
                filter(ServiceInstance.id == service_instance_id).
                filter(Device.status == constants.ACTIVE).
                with_lockmode('update').one())
            instance_db.update(service_instance)
            instance_db.update({'status': constants.PENDING_UPDATE})
        return self._make_service_instance_dict(instance_db)

    def _update_service_instance_post(self, context, service_instance_id,
                                      status, new_service_instance=None):
        with context.session.begin(subtransactions=True):
            (self._model_query(context, ServiceInstance).
             filter(ServiceInstance.id == service_instance_id).
             filter(ServiceInstance.status.in_(
                 [constants.PENDING_CREATE, constants.PENDING_UPDATE])).one().
             update({'status': status}))

            if new_service_instance:
                self._update_attr_value(context, new_service_instance,
                                         service_instance_id)
                service_instance_attrs = new_service_instance.get('attributes', {})
                (context.session.query(ServiceInstanceAttribute).
                 filter(ServiceInstanceAttribute.service_instance_id == \
                        service_instance_id).
                 filter(~ServiceInstanceAttribute.key.in_(
                        service_instance_attrs.keys())).delete(
                        synchronize_session='fetch'))
                for (key, value) in service_instance_attrs.items():
                    self._service_instance_attribute_update_or_create(context, 
                                                service_instance_id, key, value)


    # reference implementation
    def update_service_instance(self, context, service_instance_id,
                                service_instance):
        service_instance_dict = self._update_service_instance_pre(
            context, service_instance_id, service_instance)
        self._update_service_instance_post(
            context, service_instance_id, service_instance, constants.ACTIVE)
        return service_instance_dict

    def _delete_service_instance_pre(self, context, service_instance_id,
                                     managed_by_user):
        with context.session.begin(subtransactions=True):
            service_instance = (
                self._model_query(context, ServiceInstance).
                filter(ServiceInstance.id == service_instance_id).
                #cinghu
                #filter(ServiceInstance.status == constants.ACTIVE).
                with_lockmode('update').one())

            if service_instance.managed_by_user != managed_by_user:
                raise servicevm.ServiceInstanceNotManagedByUser(
                    service_instance_id=service_instance_id)

            service_instance.status = constants.PENDING_DELETE

            binding_db = (
                self._model_query(context, ServiceDeviceBinding).
                filter(ServiceDeviceBinding.service_instance_id ==
                       service_instance_id).
                all())
            assert binding_db
            # check only. _post method will delete it.
            if len(binding_db) > 1:
                raise servicevm.ServiceInstanceInUse(
                    service_instance_id=service_instance_id)

    def _delete_service_instance_post(self, context, service_instance_id):
        with context.session.begin(subtransactions=True):
            binding_db = (
                self._model_query(context, ServiceDeviceBinding).
                filter(ServiceDeviceBinding.service_instance_id ==
                       service_instance_id).
                all())
            assert binding_db
            assert len(binding_db) == 1
            context.session.delete(binding_db[0])
            (self._model_query(context, ServiceInstanceAttribute).
            filter(ServiceInstanceAttribute.service_instance_id == \
                      service_instance_id).delete()) 
            (self._model_query(context, ServiceInstance).
             filter(ServiceInstance.id == service_instance_id).
             filter(ServiceInstance.status == constants.PENDING_DELETE).
             delete())
            self._update_external_resource(context, service_instance_id)

    def _update_external_resource(self, context, service_instance_id):
        port_db = (
            self._model_query(context, models_v2.Port).
            filter(models_v2.Port.service_instance_id ==
                   service_instance_id).
            all())
        for p in port_db:
            p.update({'service_instance_id':None})

        fip_db = (
            self._model_query(context, l3.FloatingIP).
            filter(l3.FloatingIP.service_instance_id ==
                   service_instance_id).
            all())
        for f in fip_db:
            f.update({'service_instance_id':None})

    def _1update_external_resource(context, service_instance_id):
        context = t_context.get_admin_context()
        filters = {'service_instance_id': service_id}
        ports = self._core_plugin.get_ports(context, filters)
        for p in ports:
            p['service_instance_id'] = None
            self._core_plugin.update_port(context, p['id'], p)

        floatingips = self.l3_plugin.get_floatingips(context, filters)
        for f in floatingips:
            f['service_instance_id'] = None
            self.l3_plugin.update_floatingips(context, f['id'], f)

    # reference implementation. needs to be overriden by subclass
    def _delete_service_instance(self, context, service_instance_id,
                                 managed_by_user):
        self._delete_service_instance_pre(context, service_instance_id,
                                          managed_by_user)
        self._delete_service_instance_post(context, service_instance_id)

    # reference implementation. needs to be overriden by subclass
    def delete_service_instance(self, context, service_instance_id):
        self._delete_service_instance(context, service_instance_id, True)

    def get_by_service_table_id(self, context, service_table_id):
        with context.session.begin(subtransactions=True):
            instance_db = (self._model_query(context, ServiceInstance).
                           filter(ServiceInstance.service_table_id ==
                                  service_table_id).one())
            device_db = (
                self._model_query(context, Device).
                filter(sa.exists().where(sa.and_(
                    ServiceDeviceBinding.device_id == Device.id,
                    ServiceDeviceBinding.service_instance_id ==
                    instance_db.id))).one())
        return (self._make_device_dict(device_db),
                self._make_service_instance_dict(instance_db))

    def get_by_service_instance_id(self, context, service_instance_id):
        with context.session.begin(subtransactions=True):
            instance_db = self._get_resource(context, ServiceInstance,
                                             service_instance_id)
            device_db = (
                self._model_query(context, Device).
                filter(sa.exists().where(sa.and_(
                    ServiceDeviceBinding.device_id == Device.id,
                    ServiceDeviceBinding.service_instance_id ==
                    instance_db.id))).one())
        return (self._make_device_dict(device_db),
                self._make_service_instance_dict(instance_db))

    def get_service_instance(self, context, service_instance_id, fields=None):
        instance_db = self._get_resource(context, ServiceInstance,
                                         service_instance_id)
        return self._make_service_instance_dict(instance_db, fields)

    def get_service_instances(self, context, filters=None, fields=None):
        return self._get_collection(
            context, ServiceInstance, self._make_service_instance_dict,
            filters=filters, fields=fields)

    def get_service_types(self, context, filters=None, fields=None):
        service_types = self._get_collection(
            context, ServiceType, self._make_service_type_dict,
            filters=filters, fields=fields)
        return service_types

    def get_service_type(self, context, service_type_id, fields=None):
        service_type_db = self._get_resource(context, ServiceType,
                                         service_type_id)
        return self._make_service_type_dict(service_type_db, fields)

    def update_device_template(self, context, device_template_id,
                               device_template):
        with context.session.begin(subtransactions=True):
            template_db = self._get_resource(context, DeviceTemplate,
                                             device_template_id)
            template_db.update(device_template['device_template'])
        return self._make_template_dict(template_db)

                
    # NOTE(changzhi)
    def attach_interface(self, context):
        pass

    def detach_interface(self, context):
        pass

class ServiceVMPluginRpcDbMixin(object):

    def _register_service_type_sync_func(self):
        self.service_type_sync_func = {
              s_constants.VROUTER:'_get_sync_vrouter_data',
              s_constants.VFIREWALL:'_get_sync_vfirewall_data'}

    def get_devices_on_host(self, context, host):
        #hxn add,test function
        context = t_context.get_admin_context()
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_SERVICEVM, host)

        result = []

        with context.session.begin(subtransactions=True):
            device_ids = context.session.query(DeviceAgentBinding).filter_by(
                servicevm_agent_id=agent.id).all()
            ids = [q.device_id for q in device_ids]
   
            query = context.session.query(Device)
            for id in ids:
                device = context.session.query(Device).filter_by(
                    id=id)
                q = query.filter_by(id=id)
                r = self._make_device_dict(q)
                result.append(r)

        return result

    def manage_device_bindings(self, context, new_ids, agent):
        pass
 
    def register_agent_devices(self, context, resources, host):
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_SERVICEVM, host)
        if not agent.admin_state_up:
            return 

        self.manage_device_power_state(context, resources)

    def manage_device_power_state(self, context, resources):
        with context.session.begin(subtransactions=True):
            reachable_devices = resources.get('reachable', [])
            dead_devices = resources.get('dead', [])
 
            for device_id in reachable_devices:
                (self._model_query(context, Device).
                 filter(Device.id == device_id).
                 one().
                 update({'power_state':
                     constants.DEVICE_POWER_STATE['reachable']}))

            for device_id in dead_devices:
                (self._model_query(context, Device).
                 filter(Device.id == device_id).
                 one().
                 update({'power_state':
                     constants.DEVICE_POWER_STATE['dead']}))

    def get_devices_info_by_host(self, context, host):
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_SERVICEVM, host)

        with context.session.begin(subtransactions=True):
            device_db = context.session.query(DeviceAgentBinding).filter_by(
                servicevm_agent_id=agent.id).all()
            ids = [q.device_id for q in device_db]

            query = context.session.query(Device).filter(
            Device.id.in_(ids)).all()

            devices = [self._make_device_dict(d) for d in query]
        return devices
        
    def _get_sync_services(self,context, service_lists, active=None):
        return self.get_svm_gw_ports(context, service_lists, active=active)

    def _get_sync_internal_interfaces(self, context, service_lists):
        """Query router interfaces that relate to list of router_ids."""
        return self.get_svm_internal_ports(context, service_lists)

    def _get_sync_mgmt_interfaces(self, context, service_lists):
        """Query router interfaces that relate to list of router_ids."""
        return self.get_svm_mgmt_ports(context, service_lists)

    def _get_sync_floating_ips(self, context, service_lists):
        service_dicts = dict((s['id'], s) for s in service_lists)
        floating_ips = self.l3_plugin.get_floatingips(context,
                           {'service_instance_id': service_dicts.keys()})
        for floating_ip in floating_ips:
            service = service_dicts.get(floating_ip['service_instance_id'])
            if service:
                gw_fips = service['attributes'].get(s_constants.EXTERNAL_GATWAY_KEY, [])
                gw_fip_ids = [gw_fip['floatingip_id'] for gw_fip in gw_fips if gw_fip['floatingip_id']]
                common_fips = service['attributes'].get(s_constants.FLOATINGIP_KEY, [])
                com_fip_ids = [f['floatingip_id'] for f in common_fips if f['floatingip_id']]
                g_fip = []
                floatingips = []
                if floating_ip['id'] in gw_fip_ids:
                    g_fip = service.get(n_constants.GW_FIP_KEY, [])
                    g_fip.append(floating_ip)
                if floating_ip['id'] in com_fip_ids:
                    floatingips = service.get(n_constants.FLOATINGIP_KEY, [])
                    floatingips.append(floating_ip)
                if g_fip:
                    service[n_constants.GW_FIP_KEY] = g_fip
                if floatingips:
                    service[n_constants.FLOATINGIP_KEY] = floatingips
        return service_lists

    def _get_router_info_list(self, context, service_lists, active=None):
        """Query routers and their related floating_ips, interfaces."""
        with context.session.begin(subtransactions=True):
            services_gw = self._get_sync_services(context,
                                             service_lists,
                                             active=active)
            services_internal = self._get_sync_internal_interfaces(
                                       context, services_gw)
            services_mgmt = self._get_sync_mgmt_interfaces(
                                       context, services_internal)
            services_fip = self._get_sync_floating_ips(context,
                                               services_mgmt)
            return services_fip

    #hxn add
    def _update_fip_assoc(self, context, fip, floatingip_db, external_port):
        previous_router_id = floatingip_db.router_id
        port_id, internal_ip_address, router_id = (
            self._check_and_get_fip_assoc(context, fip, floatingip_db))
        floatingip_db.update({'fixed_ip_address': internal_ip_address,
                              'fixed_port_id': port_id,
                              'router_id': router_id,
                              'last_known_router_id': previous_router_id})
        if fip_rate.RATE_LIMIT in fip:
            floatingip_db[fip_rate.RATE_LIMIT] = fip[fip_rate.RATE_LIMIT]

    def get_device_services(self, context, service_ids):
        service_lists = []
        with context.session.begin(subtransactions=True):
            instance_db = (self._model_query(context, ServiceInstance).
                           filter(ServiceInstance.id.in_(service_ids))).all()
            for instance in instance_db:
                device_db = (
                    self._model_query(context, Device).
                    filter(sa.exists().where(sa.and_(
                        ServiceDeviceBinding.device_id == Device.id,
                        ServiceDeviceBinding.service_instance_id ==
                        instance.id))).one())
                service = self._make_service_instance_dict(instance)
                service['device_dict'] = self._make_device_dict(device_db)
                service_lists.append(service)
            return service_lists 

    def _get_sync_vfirewall_data(self, context,
                       svc_ids=None, active=None):
        pass

    def _get_sync_vrouter_data(self, context,
                       svc_ids=None, active=None):
        service_lists = self.get_device_services(context,
                                                 svc_ids)
        routers = self._get_router_info_list(context,
                                             service_lists,
                                             active=active)
        return routers

    def sync_service_instance_ids(self, context, host,
                                  device_ids=None):
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_SERVICEVM, host)
        if not agent.admin_state_up or not agent.reserved:
            return []
        query = context.session.query(ServiceInstance)
        query = query.join(ServiceDeviceBinding)
        query = query.join(DeviceAgentBinding,
                           DeviceAgentBinding.servicevm_agent_id==agent.id)
        if device_ids:
            if len(device_ids) == 1:
                query = query.filter(
                  ServiceDeviceBinding.device_id ==
                  device_ids[0])
            else:
                query = query.filter(
                  ServiceDeviceBinding.device_id.in_(
                  device_ids))

        svc_ids = [item['id'] for item in query]
        LOG.debug('agent get service ids %(svc_ids)s', {'svc_ids':svc_ids})
        return svc_ids


    def sync_service_instances(self, context, host,
                               service_instances_ids=None,
                               device_ids=None):
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_SERVICEVM, host)
        if not agent.admin_state_up or not agent.reserved:
            return []
        query = context.session.query(ServiceInstance)
        query = query.join(ServiceDeviceBinding)
        query = query.join(DeviceAgentBinding,
                           DeviceAgentBinding.servicevm_agent_id==agent.id)
        if service_instances_ids:
            if len(service_instances_ids) == 1:
                query = query.filter(
                  ServiceDeviceBinding.service_instance_id ==
                  service_instances_ids[0])
            else:
                query = query.filter(
                  ServiceDeviceBinding.service_instance_id.in_(
                  service_instances_ids))
        if device_ids:
            if len(device_ids) == 1:
                query = query.filter(
                  ServiceDeviceBinding.device_id ==
                  device_ids[0])
            else:
                query = query.filter(
                  ServiceDeviceBinding.device_id.in_(
                  device_ids))

        service_data = []
        svc_ids = []
        for service_type in s_constants.SURRPORT_SERVICE_TYPE:
            query = query.filter(
                     ServiceInstance.servicetype==service_type)
            svc_ids = [item['id'] for item in query]
            if not svc_ids:
                LOG.warn('service instance of service type %s is null', service_type)
                continue
            data = getattr(self, self.service_type_sync_func[service_type])(context, svc_ids)
            if data:
                service_data.extend(data)
        LOG.debug('agent get service data %(service_data)s', {'service_data':service_data})
        return service_data

    # hxn add for servicevm
    def get_sync_svm_ports(self, context, service_ids,
                           service_type, active=None):
        filters = {'service_instance_id': service_ids,
                   'servicevm_type': [service_type] }
        ports = self._core_plugin.get_ports(context, filters)
        if ports:
            self.l3_plugin._populate_subnet_for_ports(context, ports)
        return ports

    def get_sync_svm_device_ports(self, context, device_ids,
                           service_type, active=None):
        filters = {'servicevm_device': device_ids,
                   'servicevm_type': [service_type] }
        ports = self._core_plugin.get_ports(context, filters)
        if ports:
            self.l3_plugin._populate_subnet_for_ports(context, ports)
        return ports

    def _build_services_list(self, context, service_lists, gw_ports):
        for s in service_lists:
            service_id = s['id']
            # Collect gw ports only if available
            if service_id and gw_ports.get(service_id):
                s[n_constants.GW_INTERFACE_KEY] = gw_ports[service_id]
        return service_lists

    def get_svm_gw_ports(self, context, service_lists, active=None):
        service_ids = [s['id'] for s in service_lists]
        servicevm_type = n_constants.SERVICEVM_OWNER_ROUTER_GW
        gw_ports = dict((gw_port['service_instance_id'], gw_port)
                         for gw_port in
                         self.get_sync_svm_ports(context, service_ids,
                                                 servicevm_type, active=active))
        return self._build_services_list(context, service_lists, gw_ports)

    def get_svm_internal_ports(self, context, service_lists):
        # only a service instance for each service type in a device
        service_dicts = dict((s['devices'][0], s) for s in service_lists)
        servicevm_type = n_constants.SERVICEVM_OWNER_ROUTER_INTF
        interfaces = self.get_sync_svm_device_ports(context, service_dicts.keys(),
                                                    servicevm_type)
        for interface in interfaces:
            service = service_dicts.get(interface['servicevm_device'])
            if service:
                internal_interfaces = service.get(n_constants.INTERFACE_KEY, [])
                internal_interfaces.append(interface)
                service[n_constants.INTERFACE_KEY] = internal_interfaces
        return service_lists

    def get_svm_mgmt_ports(self, context, service_lists):
        # only a service instance for each service type in a device
        service_dicts = dict((s['devices'][0], s) for s in service_lists)
        servicevm_type = n_constants.SERVICEVM_OWNER_MGMT
        interfaces = self.get_sync_svm_device_ports(context, service_dicts.keys(),
                                                  servicevm_type)
        for interface in interfaces:
            service = service_dicts.get(interface['servicevm_device'])
            if service:
                internal_interfaces = service.get(n_constants.MANAGERMENT_KEY, [])
                internal_interfaces.append(interface)
                service[n_constants.MANAGERMENT_KEY] = internal_interfaces
        return service_lists

    def get_device_details(self, rpc_context, **kwargs):
        """Agent requests device details."""
        device = kwargs.get('device')
        host = kwargs.get('host')
        port = ovs_db_v2.get_port(device)
        if port:
            binding = ovs_db_v2.get_network_binding(None, port['network_id'])
            entry = {'device': device,
                     'network_id': port['network_id'],
                     'port_id': port['id'],
                     'admin_state_up': port['admin_state_up']}
                     #cinghu raise attribut error
                     #'network_type': binding.network_type,
                     #'segmentation_id': binding.segmentation_id,
                     #'physical_network': binding.physical_network}
        else:
            entry = {'device': device}
            LOG.debug(_("%s can not be found in database"), device)
        return entry

    def get_devices_details_list(self, rpc_context, devices, host):
        return [
            self.get_device_details(
                rpc_context,
                device=device,
                host=host
            )
            for device in devices
        ]

