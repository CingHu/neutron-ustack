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
# @author: yong sheng gong, UnitedStack Inc.

import netaddr
import sqlalchemy as sa

from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron.common import utils
from neutron.common import uos_constants
from neutron import context
#from neutron.db import db_base_plugin_v2 as base_db
from neutron.db import common_db_mixin
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import pptpvpnaas
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron.services.vpn.common import constants as vpn_connstants

from sqlalchemy.orm import exc

LOG = logging.getLogger(__name__)


class PPTPConnection(model_base.BASEV2,
                     models_v2.HasId, models_v2.HasTenant,
                     models_v2.TimestampMixin):
    """Represents a pptpd Object."""
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    vpn_cidr = sa.Column(sa.String(255), nullable=False)
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)
    router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id'),
                          nullable=False)


class PPTPDVPNDbMixin(pptpvpnaas.VPNPluginBase, common_db_mixin.CommonDbMixin):
    """PPTPD VPN plugin database class using SQLAlchemy models."""
    pptp_driver = None

    def update_status(self, context, model, v_id, status):
        with context.session.begin(subtransactions=True):
            v_db = self._get_resource(context, model, v_id)
            v_db.update({'status': status})

    def _get_resource(self, context, model, v_id):
        try:
            r = self._get_by_id(context, model, v_id)
        except exc.NoResultFound:
            if issubclass(model, PPTPConnection):
                raise pptpvpnaas.PPTPVPNServiceNotFound(id=v_id)
            else:
                raise
        return r

    def _make_pptpconnection_dict(self, pptpconn_db, fields=None):

        res = {'id': pptpconn_db['id'],
               'tenant_id': pptpconn_db['tenant_id'],
               'name': pptpconn_db['name'],
               'description': pptpconn_db['description'],
               'vpn_cidr': pptpconn_db['vpn_cidr'],
               'admin_state_up': pptpconn_db['admin_state_up'],
               'status': pptpconn_db['status'],
               'router_id': pptpconn_db['router_id'],
               'created_at': pptpconn_db['created_at'],
               }

        return self._fields(res, fields)

    def _validate_vpn_cidr(self, context, router_id, vpn_cidr):
        """Validate the CIDR for a vpn.

        Verifies the specified CIDR does not overlap with the ones defined
        for the other subnets specified for the router and othr pptp vpn.
        """
        cidrs = set()
        core_plugin = manager.NeutronManager.get_plugin()
        try:
            rport_qry = context.session.query(models_v2.Port)
            rports = rport_qry.filter_by(device_id=router_id)
            for p in rports:
                for ip in p['fixed_ips']:
                    sub_id = ip['subnet_id']
                    cidr = core_plugin._get_subnet(context.elevated(),
                                                   sub_id)['cidr']
                    cidrs.add(cidr)
                    break
        except exc.NoResultFound:
            pass

        pptps = self.get_pptpconnections(context, {'router_id': [router_id]})
        for pptp in pptps:
            cidrs.add(pptp['vpn_cidr'])

        openvpns = self.get_openvpnconnections(context, {'router_id': [router_id]})
        for openvpn in openvpns:
            cidrs.add(openvpn['peer_cidr'])

        vpn_cidr_ipset = netaddr.IPSet([vpn_cidr])
        for cidr in cidrs:
            if (netaddr.IPSet([cidr]) & vpn_cidr_ipset):
                # don't give out details of the overlapping subnet
                err_msg = (_("Requested vpn with cidr: %(cidr)s overlaps with"
                             " another subnet or vpn") %
                           {'cidr': vpn_cidr})
                LOG.info(_("Validation for CIDR: %(new_cidr)s failed - "
                           "overlaps with CIDR %(cidr)s "
                           "(CIDR: %(cidr)s)"),
                         {'new_cidr': vpn_cidr,
                          'cidr': cidr})
                raise n_exc.InvalidInput(error_message=err_msg)

    def create_pptpconnection(self, context, pptpconnection):
        pptpconnection = pptpconnection['pptpconnection']
        tenant_id = self._get_tenant_id_for_create(context,
                                                   pptpconnection)
        l3_plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        if not l3_plugin:
            raise pptpvpnaas.RouterExtNotFound()
        l3_plugin.get_router(context, pptpconnection['router_id'])
        with context.session.begin(subtransactions=True):
            self._validate_vpn_cidr(context,
                                    pptpconnection['router_id'],
                                    pptpconnection['vpn_cidr'])
            pptpconn_db = PPTPConnection(
                id=uuidutils.generate_uuid(),
                tenant_id=tenant_id,
                name=pptpconnection['name'],
                description=pptpconnection['description'],
                vpn_cidr=pptpconnection['vpn_cidr'],
                router_id=pptpconnection['router_id'],
                admin_state_up=pptpconnection['admin_state_up'],
                status=constants.DOWN,
                created_at=timeutils.utcnow(),
            )
            utils.make_default_name(pptpconn_db, uos_constants.UOS_PRE_PPTP)
            context.session.add(pptpconn_db)
        result = self._make_pptpconnection_dict(pptpconn_db)
        if self.pptp_driver:
            self.pptp_driver.create_vpnservice(context, result)
        return result

    def update_pptpconnection(
            self, context,
            pptpconnection_id, pptpconnection):
        conn = pptpconnection['pptpconnection']
        conn.pop('created_at', None)
        new_admin_state = conn.get('admin_state_up')
        _status = None
        if new_admin_state is not None and not new_admin_state:
            _status = constants.DOWN
        if _status is not None:
            conn['status'] = _status
        with context.session.begin(subtransactions=True):
            pptpconnection_db = self._get_resource(
                context,
                PPTPConnection,
                pptpconnection_id)
            old_admin = pptpconnection_db['admin_state_up']
            if conn:
                pptpconnection_db.update(conn)
        result = self._make_pptpconnection_dict(pptpconnection_db)
        admin_change_flag = ('admin_state_up' in conn and
                             old_admin != conn['admin_state_up'])
        if self.pptp_driver and admin_change_flag:
            self.pptp_driver.update_vpnservice(context, None, result)
        return result

    def delete_pptpconnection(self, context, pptpconnection_id):
        with context.session.begin(subtransactions=True):
            pptpconnection_db = self._get_resource(
                context, PPTPConnection, pptpconnection_id)
            pptp = self._make_pptpconnection_dict(pptpconnection_db)
            context.session.delete(pptpconnection_db)
        if self.pptp_driver:
            self.pptp_driver.delete_vpnservice(context, pptp)

    def _get_pptpconnection_db(self, context,
                               pptpconnection_id, fields=None):
        return self._get_resource(
            context, PPTPConnection, pptpconnection_id)

    def get_pptpconnection(self, context,
                           pptpconnection_id, fields=None):
        pptpconnection_db = self._get_resource(
            context, PPTPConnection, pptpconnection_id)
        return self._make_pptpconnection_dict(
            pptpconnection_db, fields)

    def get_pptpconnections(self, context, filters=None, fields=None):
        return self._get_collection(context, PPTPConnection,
                                    self._make_pptpconnection_dict,
                                    filters=filters, fields=fields)

    def check_router_in_use(self, context, router_id):
        # called from l3 db
        pptp_conns = self.get_pptpconnections(
            context, filters={'router_id': [router_id]})
        if pptp_conns:
            raise pptpvpnaas.RouterInUseByVPNService(
                router_id=router_id,
                vpnservice_id=pptp_conns[0]['id'])

    def check_for_dup_router_subnet(self, context, router_id,
                                    subnet_id, subnet_cidr):
        # called from l3 db
        pptp_conns = self.get_pptpconnections(
            context, filters={'router_id': [router_id]})
        new_ipnet = netaddr.IPNetwork(subnet_cidr)
        for pptp in pptp_conns:
            cidr = pptp['vpn_cidr']
            ipnet = netaddr.IPNetwork(cidr)
            match1 = netaddr.all_matching_cidrs(new_ipnet, [cidr])
            match2 = netaddr.all_matching_cidrs(ipnet, [subnet_cidr])
            if match1 or match2:
                data = {'subnet_cidr': subnet_cidr,
                        'subnet_id': subnet_id,
                        'cidr': cidr,
                        'vpn_id': pptp['id']}
                msg = (_("Cidr %(subnet_cidr)s of subnet "
                         "%(subnet_id)s overlaps with cidr %(cidr)s "
                         "of pptpvpn %(vpn_id)s") % data)
                raise n_exc.BadRequest(resource='router', msg=msg)


class VPNPluginRpcDbMixin():
    def _get_vpn_services_by_routers(self, context, router_ids, host=None):
        if not router_ids:
            return []
        query = context.session.query(PPTPConnection)
        _filter = None
        if len(router_ids) > 1:
            _filter = (PPTPConnection.router_id.in_(router_ids))
        else:
            _filter = (PPTPConnection.router_id == router_ids[0])
        query = query.filter(_filter)
        result = []
        tenantid_users_dict = {}
        for conn in query:
            conn_dict = self._make_pptpconnection_dict(conn)
            result.append(conn_dict)
            users = tenantid_users_dict.get(conn_dict['tenant_id'])
            if not users:
                users = self.get_vpnusers(
                    context, filters={'tenant_id': [conn_dict['tenant_id']]})
            tenantid_users_dict[conn_dict['tenant_id']] = users
            conn_dict[vpn_connstants.USERS] = users
            cidr_str = self._caculate_pptp_address(conn_dict['vpn_cidr'])
            conn_dict[vpn_connstants.CIDR] = cidr_str
        return result

    def _caculate_pptp_address(self, cidr):
        _cidr = {}
        net = netaddr.IPNetwork(cidr)
        _cidr['firstaddr'] = str(netaddr.IPAddress(net.first + 1))
        lastAddr = str(netaddr.IPAddress(net.last - 1))
        if net.size == 4:
            _cidr['otheraddr'] = lastAddr
        else:
            firstPart = str(netaddr.IPAddress(net.first + 2))
            otherPart = lastAddr.split(".", 3)[-1]
            _cidr['otheraddr'] = firstPart + "-" + otherPart
        return _cidr

    def _update_status_by_agent(self, ctx, service_status_info_list):
        """Updating vpnservice and vpnconnection status.

        :param context: context variable
        :param service_status_info_list: list of status
        The structure is
        [{id: ptpconn_id,
          tenant_id: tenant_id
          status: ACTIVE|DOWN|ERROR}]
        The agent will set updated_pending_status as True,
        when agent update any pending status.
        """

        _resource = 'pptpconnection'
        notifier = n_rpc.get_notifier('network')
        for status in service_status_info_list:
            _ctx = context.Context('', status['tenant_id'])
            payload = {'id': status['id']}
            notifier.info(_ctx, _resource + '.update.start', payload)
        updated_pptp_dict = []
        with ctx.session.begin(subtransactions=True):
            for vpnservice in service_status_info_list:
                try:
                    vpnservice_db = self._get_pptpconnection_db(
                        ctx, vpnservice['id'])
                except pptpvpnaas.PPTPVPNServiceNotFound:
                    LOG.warn(_('vpnservice %s in db is already deleted'),
                             vpnservice['id'])
                    continue

                vpnservice_db.status = vpnservice['status']
                vpn_dict = self._make_pptpconnection_dict(vpnservice_db)
                updated_pptp_dict.append(vpn_dict)
        notifier_method = _resource + '.update.end'
        for vpn_dict in updated_pptp_dict:
            _ctx = context.Context('', vpn_dict['tenant_id'])
            result = {_resource: vpn_dict}
            notifier.info(_ctx,
                          notifier_method,
                          result)
