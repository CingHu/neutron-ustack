# Copyright (c) 2012 OpenStack Foundation.
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

import sqlalchemy as sa
from sqlalchemy import DateTime
from sqlalchemy import orm

from neutron.common import constants
from neutron.db import model_base
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils


class HasTenant(object):
    """Tenant mixin, add to subclasses that have a tenant."""

    # NOTE(jkoelker) tenant_id is just a free form string ;(
    tenant_id = sa.Column(sa.String(255))


class HasId(object):
    """id mixin, add to subclasses that have an id."""

    id = sa.Column(sa.String(36),
                   primary_key=True,
                   default=uuidutils.generate_uuid)


class TimestampMixin(object):
    created_at = sa.Column(DateTime, default=timeutils.utcnow)


class HasStatusDescription(object):
    """Status with description mixin."""

    status = sa.Column(sa.String(16), nullable=False)
    status_description = sa.Column(sa.String(255))


class IPAvailabilityRange(model_base.BASEV2):
    """Internal representation of available IPs for Neutron subnets.

    Allocation - first entry from the range will be allocated.
    If the first entry is equal to the last entry then this row
    will be deleted.
    Recycling ips involves reading the IPAllocationPool and IPAllocation tables
    and inserting ranges representing available ips.  This happens after the
    final allocation is pulled from this table and a new ip allocation is
    requested.  Any contiguous ranges of available ips will be inserted as a
    single range.
    """

    allocation_pool_id = sa.Column(sa.String(36),
                                   sa.ForeignKey('ipallocationpools.id',
                                                 ondelete="CASCADE"),
                                   nullable=False,
                                   primary_key=True)
    first_ip = sa.Column(sa.String(64), nullable=False, primary_key=True)
    last_ip = sa.Column(sa.String(64), nullable=False, primary_key=True)

    def __repr__(self):
        return "%s - %s" % (self.first_ip, self.last_ip)


class IPAllocationPool(model_base.BASEV2, HasId):
    """Representation of an allocation pool in a Neutron subnet."""

    subnet_id = sa.Column(sa.String(36), sa.ForeignKey('subnets.id',
                                                       ondelete="CASCADE"),
                          nullable=True)
    first_ip = sa.Column(sa.String(64), nullable=False)
    last_ip = sa.Column(sa.String(64), nullable=False)
    available_ranges = orm.relationship(IPAvailabilityRange,
                                        backref='ipallocationpool',
                                        lazy="joined",
                                        cascade='all, delete-orphan')

    def __repr__(self):
        return "%s - %s" % (self.first_ip, self.last_ip)


class IPAllocation(model_base.BASEV2):
    """Internal representation of allocated IP addresses in a Neutron subnet.
    """

    port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id',
                                                     ondelete="CASCADE"),
                        nullable=True)
    ip_address = sa.Column(sa.String(64), nullable=False, primary_key=True)
    subnet_id = sa.Column(sa.String(36), sa.ForeignKey('subnets.id',
                                                       ondelete="CASCADE"),
                          nullable=False, primary_key=True)
    network_id = sa.Column(sa.String(36), sa.ForeignKey("networks.id",
                                                        ondelete="CASCADE"),
                           nullable=False, primary_key=True)


class Route(object):
    """mixin of a route."""

    destination = sa.Column(sa.String(64), nullable=False, primary_key=True)
    nexthop = sa.Column(sa.String(64), nullable=False, primary_key=True)


class SubnetRoute(model_base.BASEV2, Route):

    subnet_id = sa.Column(sa.String(36),
                          sa.ForeignKey('subnets.id',
                                        ondelete="CASCADE"),
                          primary_key=True)


class Port(model_base.BASEV2, HasId, HasTenant, TimestampMixin):
    """Represents a port on a Neutron v2 network."""

    name = sa.Column(sa.String(255))
    network_id = sa.Column(sa.String(36), sa.ForeignKey("networks.id"),
                           nullable=False)
    fixed_ips = orm.relationship(IPAllocation, backref='ports', lazy='joined')
    mac_address = sa.Column(sa.String(32), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)
    status = sa.Column(sa.String(16), nullable=False)
    device_id = sa.Column(sa.String(255), nullable=False)
    device_owner = sa.Column(sa.String(255), nullable=False)
    disable_anti_spoofing = sa.Column(sa.Boolean(), nullable=False)
    servicevm_device = sa.Column(sa.String(255), nullable=True)
    service_instance_id = sa.Column(sa.String(255), nullable=True)
    servicevm_type = sa.Column(sa.String(255), nullable=True)

    def __init__(self, id=None, tenant_id=None, name=None, network_id=None,
                 mac_address=None, admin_state_up=None, status=None,
                 device_id=None, device_owner=None, fixed_ips=None,
                 created_at=None, disable_anti_spoofing=None,
                 servicevm_device=None, servicevm_type=None,
                 service_instance_id=None):
        self.id = id
        self.tenant_id = tenant_id
        self.name = name
        self.network_id = network_id
        self.mac_address = mac_address
        self.admin_state_up = admin_state_up
        self.device_owner = device_owner
        self.device_id = device_id
        self.created_at = created_at
        self.servicevm_device= servicevm_device
        self.service_instance_id = service_instance_id
        self.servicevm_type= servicevm_type
        # Since this is a relationship only set it if one is passed in.
        if fixed_ips:
            self.fixed_ips = fixed_ips

        # NOTE(arosen): status must be set last as an event is triggered on!
        self.status = status
        self.disable_anti_spoofing = disable_anti_spoofing


class DNSNameServer(model_base.BASEV2):
    """Internal representation of a DNS nameserver."""

    address = sa.Column(sa.String(128), nullable=False, primary_key=True)
    subnet_id = sa.Column(sa.String(36),
                          sa.ForeignKey('subnets.id',
                                        ondelete="CASCADE"),
                          primary_key=True)


class Subnet(model_base.BASEV2, HasId, HasTenant,
             TimestampMixin):
    """Represents a neutron subnet.

    When a subnet is created the first and last entries will be created. These
    are used for the IP allocation.
    """

    name = sa.Column(sa.String(255))
    network_id = sa.Column(sa.String(36), sa.ForeignKey('networks.id'))
    ip_version = sa.Column(sa.Integer, nullable=False)
    cidr = sa.Column(sa.String(64), nullable=False)
    gateway_ip = sa.Column(sa.String(64))
    allocation_pools = orm.relationship(IPAllocationPool,
                                        backref='subnet',
                                        lazy="joined",
                                        cascade='delete')
    enable_dhcp = sa.Column(sa.Boolean())
    dns_nameservers = orm.relationship(DNSNameServer,
                                       backref='subnet',
                                       cascade='all, delete, delete-orphan')
    routes = orm.relationship(SubnetRoute,
                              backref='subnet',
                              cascade='all, delete, delete-orphan')
    shared = sa.Column(sa.Boolean)
    ipv6_ra_mode = sa.Column(sa.Enum(constants.IPV6_SLAAC,
                                     constants.DHCPV6_STATEFUL,
                                     constants.DHCPV6_STATELESS,
                                     name='ipv6_ra_modes'), nullable=True)
    ipv6_address_mode = sa.Column(sa.Enum(constants.IPV6_SLAAC,
                                  constants.DHCPV6_STATEFUL,
                                  constants.DHCPV6_STATELESS,
                                  name='ipv6_address_modes'), nullable=True)


class Network(model_base.BASEV2, HasId, HasTenant, TimestampMixin):
    """Represents a v2 neutron network."""

    name = sa.Column(sa.String(255))
    ports = orm.relationship(Port, backref='networks')
    subnets = orm.relationship(Subnet, backref='networks',
                               lazy="joined")
    status = sa.Column(sa.String(16))
    admin_state_up = sa.Column(sa.Boolean)
    shared = sa.Column(sa.Boolean)
    unmanaged_network = sa.Column(sa.Boolean)
