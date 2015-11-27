#    Copyright (c) 2015 UnitedStack Inc.
#    All rights reserved.
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

import netaddr
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron.common import constants as n_constants
from neutron.db import common_db_mixin as base_db
from neutron.db import db_base_plugin_v2
from neutron.db import l3_agentschedulers_db as l3_agent_db
from neutron.db import l3_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db.tunnel import tunnel_validator
from neutron.extensions import tunnelaas
from neutron.extensions import l3
from neutron import manager
from neutron import context as n_context
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron.plugins.common import utils

LOG = logging.getLogger(__name__)


class TunnelConnection(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant,
                       models_v2.TimestampMixin):
    """Represents a Tunnel Connection Object."""
    name = sa.Column(sa.String(255))
    status = sa.Column(sa.String(16), nullable=False)
    tunnel_id = sa.Column(sa.String(36), sa.ForeignKey('tunnels.id'),
                          nullable=False)
    remote_ip = sa.Column(sa.String(255), nullable=False)
    key = sa.Column(sa.String(255))
    key_type = sa.Column(sa.Integer, nullable=False)
    checksum = sa.Column(sa.Integer, nullable=False)


class TargetNetwork(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant,
                    models_v2.TimestampMixin):
    """Represents a Target Network Object."""
    tunnel_id = sa.Column(sa.String(36), sa.ForeignKey('tunnels.id'),
                          nullable=False)
    network_cidr = sa.Column(sa.String(255))


class Tunnel(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant,
             models_v2.TimestampMixin):
    """Represents a Tunnel Object."""
    name = sa.Column(sa.String(255))
    router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id'),
                          nullable=False)
    mode = sa.Column(sa.String(16), nullable=True)
    type = sa.Column(sa.Integer, nullable=True)
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.String(255), nullable=False)
    local_subnet = sa.Column(sa.String(36), nullable=True)
    tunnel_connections = orm.relationship(
        TunnelConnection,
        backref='tunnel',
        single_parent=True,
        cascade="delete")
    target_networks = orm.relationship(
        TargetNetwork,
        backref='tunnel',
        single_parent=True,
        cascade="delete")
    router = orm.relationship(
        l3_db.Router,
        backref='tunnels')


class TunnelPluginDb(tunnelaas.TunnelPluginBase, base_db.CommonDbMixin):
    """Tunnel plugin database class using SQLAlchemy models.

    This DB class will be inherited by service plugin.
    """

    def _extend_router_tunnels(self, router_res, router_db):
        router_res['tunnels'] = (TunnelPluginDb._make_tunnels_dict(
                                    router_db['tunnels']))

    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        l3.ROUTERS, [_extend_router_tunnels])

    @staticmethod
    def _make_tunnels_dict(tunnels):
        return [tunnel['id'] for tunnel in tunnels]

    def _get_validator(self):
        return tunnel_validator.TunnelReferenceValidator()

    def _make_tunnel_dict(self, tunnel, fields=None):
        res = {'id': tunnel['id'],
               'name': tunnel['name'],
               'tenant_id': tunnel['tenant_id'],
               'router_id': tunnel['router_id'],
               'mode': tunnel['mode'],
               'type': tunnel['type'],
               'local_subnet': self._make_local_subnet_dict(
                   tunnel['local_subnet']),
               'status': tunnel['status'],
               'admin_state_up': tunnel['admin_state_up'],
               'created_at': tunnel['created_at'],
               'tunnel_connections': [self._make_tunnel_conn_dict(c)
                   for c in tunnel.tunnel_connections],
               'target_networks': [self._make_target_network_dict(t)
                   for t in tunnel.target_networks]}
        return self._fields(res, fields)

    def _make_tunnel_conn_dict(self, tunnel_conn, fields=None):
        res = {'id': tunnel_conn['id'],
               'name': tunnel_conn['name'],
               'tenant_id': tunnel_conn['tenant_id'],
               'remote_ip': tunnel_conn['remote_ip'],
               'tunnel_id': tunnel_conn['tunnel_id'],
               'key': tunnel_conn['key'],
               'key_type': tunnel_conn['key_type'],
               'status': tunnel_conn['status'],
               'check_sum': tunnel_conn['checksum'],
               'created_at': tunnel_conn['created_at']}
        return self._fields(res, fields)

    def _make_target_network_dict(self, target_network, fields=None):
        res = {'id': target_network['id'],
               'tenant_id': target_network['tenant_id'],
               'tunnel_id': target_network['tunnel_id'],
               'network_cidr': target_network['network_cidr'],
               'created_at': target_network['created_at']}
        return self._fields(res, fields)

    def _make_local_subnet_dict(self, subnet_id, fields=None):
        if not subnet_id:
            return
        core_plugin = manager.NeutronManager.get_plugin()
        ctx = n_context.get_admin_context()
        subnet = core_plugin.get_subnet(ctx, subnet_id)
        res = {'id': subnet_id,
               'name': subnet.get('name'),
               'cidr': subnet.get('cidr')}
        return self._fields(res, fields)

    def _get_resource(self, context, model, v_id):
        try:
            r = self._get_by_id(context, model, v_id)
        except exc.NoResultFound:
            with excutils.save_and_reraise_exception(reraise=False) as ctx:
                if issubclass(model, TunnelConnection):
                    raise tunnelaas.TunnelConnectionNotFound()
                elif issubclass(model, Tunnel):
                    raise tunnelaas.TunnelNotFound(tunnel_id=v_id)
                elif issubclass(model, TargetNetwork):
                    raise tunnelaas.TargetNetworkNotFound(
                        target_network_id=v_id)
                ctx.reraise = True
        return r

    def _get_tunnel(self, context, tunnel_id):
        return self._get_resource(context, Tunnel, tunnel_id)

    def _get_tunnel_connection(self, context, tunnel_conn_id):
        return self._get_resource(context, TunnelConnection, tunnel_conn_id)

    def _get_target_network(self, context, target_network_id):
        return self._get_resource(context, TargetNetwork, target_network_id)

    def create_tunnel(self, context, tunnel):
        tunnel = tunnel['tunnel']
        tenant_id = self._get_tenant_id_for_create(context, tunnel)
        validator = self._get_validator()
        tunnel_id=uuidutils.generate_uuid()
        if not tunnel.get('name'):
            tunnel['name'] = 'gre-tunnel-' + tunnel_id[:11]
        with context.session.begin(subtransactions=True):
            validator.validate_tunnel(context, tunnel)
            tunnel_db = Tunnel(id=tunnel_id,
                               tenant_id=tenant_id,
                               name=tunnel['name'],
                               router_id=tunnel['router_id'],
                               mode=tunnel['mode'],
                               type=tunnel['type'],
                               local_subnet=tunnel['local_subnet'],
                               status=constants.INACTIVE,
                               admin_state_up='UP',
                               created_at=timeutils.utcnow())
            context.session.add(tunnel_db)
        return self._make_tunnel_dict(tunnel_db)

    def update_tunnel(self, context, tunnel_id, tunnel):
        tunnel = tunnel['tunnel']
        with context.session.begin(subtransactions=True):
            tunnel_db = self._get_resource(context, Tunnel, tunnel_id)
            tunnel_db.update(tunnel)
        return self._make_tunnel_dict(tunnel_db)

    def delete_tunnel(self, context, tunnel_id):
        with context.session.begin(subtransactions=True):
            if context.session.query(TunnelConnection).filter_by(
                tunnel_id=tunnel_id
            ).first():
                raise tunnelaas.TunnelInUse(tunnel_id=tunnel_id)
            tunnel_db = self._get_resource(context, Tunnel, tunnel_id)
            context.session.delete(tunnel_db)

    def get_tunnels(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        return self._get_collection(context, Tunnel,
                                    self._make_tunnel_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts, limit=limit,
                                    marker_obj=marker,
                                    page_reverse=page_reverse)

    def get_tunnel(self, context, tunnel_id, fields=None):
        tunnel_db = self._get_resource(context, Tunnel, tunnel_id)
        return self._make_tunnel_dict(tunnel_db, fields)

    def create_tunnel_connection(self, context, tunnel_connection):
        tunnel_connection = tunnel_connection['tunnel_connection']
        tenant_id = self._get_tenant_id_for_create(context, tunnel_connection)
        validator = self._get_validator()
        tunnel_conn_id=uuidutils.generate_uuid()
        if not tunnel_connection.get('name'):
            tunnel_connection['name'] = 'tun_conn_' + str(tunnel_conn_id[:8])
        with context.session.begin(subtransactions=True):
            validator.validate_tunnel_connection(context, tunnel_connection)
            tunnel_conn_db = TunnelConnection(
                id=tunnel_conn_id,
                tenant_id=tenant_id,
                name=tunnel_connection['name'],
                tunnel_id=tunnel_connection['tunnel_id'],
                remote_ip=tunnel_connection['remote_ip'],
                key=tunnel_connection.get('key'),
                key_type=tunnel_connection['key_type'],
                status=constants.INACTIVE,
                checksum=tunnel_connection['checksum'],
                created_at=timeutils.utcnow())
            context.session.add(tunnel_conn_db)
        return self._make_tunnel_conn_dict(tunnel_conn_db)

    def delete_tunnel_connection(self, context, tunnel_conn_id):
        with context.session.begin(subtransactions=True):
            tunnel_conn_db = self._get_resource(context,
                TunnelConnection, tunnel_conn_id)
            context.session.delete(tunnel_conn_db)

    def update_tunnel_conn_status(self, context, conn_id, new_status):
        with context.session.begin():
            self._update_connection_status(context, conn_id, new_status)

    def _update_connection_status(self, context, conn_id, new_status):
        try:
            conn_db = self._get_tunnel_connection(context, conn_id)
        except tunnelaas.TunnelConnectionNotFound:
            return
        conn_db.status = new_status

    def get_tunnel_connections(self, context, filters=None, fields=None,
                               sorts=None, limit=None, marker=None,
                               page_reverse=False):
        return self._get_collection(context, TunnelConnection,
                                    self._make_tunnel_conn_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts, limit=limit,
                                    marker_obj=marker,
                                    page_reverse=page_reverse)

    def get_tunnel_connection(self, context, tunnel_conn_id, fields=None):
        tunnel_conn_db = self._get_resource(
            context, TunnelConnection, tunnel_conn_id)
        return self._make_tunnel_conn_dict(tunnel_conn_db, fields)

    def create_target_network(self, context, target_network):
        target_network = target_network['target_network']
        tenant_id = self._get_tenant_id_for_create(context, target_network)
        validator = self._get_validator()
        with context.session.begin(subtransactions=True):
            validator.validate_target_network(context, target_network)
            target_network_db = TargetNetwork(id=uuidutils.generate_uuid(),
                                tenant_id=tenant_id,
                                network_cidr=target_network['network_cidr'],
                                tunnel_id=target_network['tunnel_id'],
                                created_at=timeutils.utcnow())
            context.session.add(target_network_db)
        return self._make_target_network_dict(target_network_db)

    def delete_target_network(self, context, target_network_id):
        with context.session.begin(subtransactions=True):
            target_network_db = self._get_resource(context, TargetNetwork,
                    target_network_id)
            context.session.delete(target_network_db)

    def get_target_networks(self, context, filters=None, fields=None,
                           sorts=None, limit=None, marker=None,
                           page_reverse=False):
        return self._get_collection(context, TargetNetwork,
                                    self._make_target_network_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts, limit=limit,
                                    marker_obj=marker,
                                    page_reverse=page_reverse)

    def get_target_network(self, context, target_network_id, fields=None):
        target_network_db = self._get_resource(
            context, TargetNetwork, target_network_id)
        return self._make_target_network_dict(target_network_db, fields)

    def check_router_in_use(self, context, router_id):
        tunnels = self.get_tunnels(
            context, filters={'router_id': [router_id]})
        if tunnels:
            raise tunnelaas.RouterInUseByTunnelService(
                router_id=router_id,
                tunnel_id=tunnels[0]['id'])

    def check_router_interface_in_use(self, context, router_id, interface):
        tunnels = self.get_tunnels(
            context, filters={'router_id': [router_id]})
        # NOTE(WeiW): Since onlu l2 tunnel will has local_subnet,
        # and every router will only have one l2 tunnel
        subnet_id = interface.get('subnet_id')
        if tunnels and tunnels[0]['local_subnet'] == subnet_id:
            raise tunnelaas.RouterInterfaceInUseByTunnel(
                router_id=router_id,
                tunnel_id=tunnels[0]['id'])

    def check_floatingip_in_use(self, context, fip_id):
        l3_plugin  = manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)
        fip = l3_plugin.get_floatingip(context, fip_id)
        if fip['port_id'] == None:
            return
        port_id = fip.get('port_id')
        core_plugin = manager.NeutronManager.get_plugin()
        port = core_plugin.get_port(context, port_id)
        if port['device_owner'] != 'network:router_gateway':
            return
        tunnels = self.get_tunnels(
            context, filters={'router_id': [port['device_id']]})
        if tunnels:
            raise tunnelaas.FloatingipUsedByTunnel(
                    floatingip_id=fip['id'],
                    router_id=port['device_id'],
                    tunnel_id=tunnels[0]['id'])


class TunnelPluginRpcDbMixin():
    """ Rpc DB Mixin which agent will call this func to control db

    This DB Mixin will inherit by service plugin
    """

    def _get_agent_hosting_tunnels(self, context, host, tunnels):
        plugin = manager.NeutronManager.get_plugin()
        agent = plugin._get_agent_by_type_and_host(
            context, n_constants.AGENT_TYPE_L3, host)
        if not agent.admin_state_up:
            return []
        result = []
        for tunnel_id in tunnels:
            try:
                tunnel = self._get_tunnel(context, tunnel_id)
            except tunnelaas.TunnelNotFound:
                LOG.warn(_("Tunnel %s not found!"), tunnel_id)
                continue
            result.append(tunnel)
        if tunnels == []:
            l3_plugin = manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)
            all_tunnels = self.get_tunnels(context, fields=['id','router_id'])
            for tunnel in all_tunnels:
                agents = l3_plugin.list_l3_agents_hosting_router(
                    context, tunnel['router_id'])['agents']
                if agents and agents[0]['host'] == host:
                    tunnel = self._get_tunnel(context, tunnel['id'])
                    result.append(tunnel)
                else:
                    LOG.warn(_("Can't get router %s 's host agent or " +
                            "not host in host %s"), tunnel['router_id'], host)
        return result

    def update_status_by_agent(self, context, service_status_info_list):
        """Updating Tunnel and TunnelConnection status.

        :param context: context variable
        :param service_status_info_list: list of status
        The structure is
        [{id: tunnel_id,
          status: ACTIVE|DOWN|ERROR,
          tunnel_connections: {
              tunnel_connection_id: {
                  status: ACTIVE|DOWN|ERROR
              }
          }]
        """
        with context.session.begin(subtransactions=True):
            for tunnel in service_status_info_list:
                try:
                    tunnel_db = self._get_tunnel(
                        context, tunnel['id'])
                except tunnelaas.TunnelNotFound:
                    LOG.warn(_('tunnel %s in db is already deleted'),
                             tunnel['id'])
                    continue

                tunnel_db.status = tunnel['status']
                conns = tunnel['tunnel_connections']
                for conn in conns:
                    try:
                        tunnel_conn_db = self._get_tunnel_connection(
                            context, conn)
                    except tunnelaas.TunnelConnectionNotFound:
                        LOG.warn(_('tunnel conn %s in db is already deleted'),
                             conn)
                        continue
                    tunnel_conn_db.status =conns[conn]
