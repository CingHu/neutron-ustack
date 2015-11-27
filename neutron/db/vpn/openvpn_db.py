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
# @author: hu xining, UnitedStack Inc.
import os
import netaddr
import sqlalchemy as sa
from oslo.config import cfg

from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron.common import utils
from neutron.common import core as sql
from neutron.openstack.common import jsonutils
from neutron.common import uos_constants
from neutron.common import constants as l3_constants
from neutron import context
from neutron.db import common_db_mixin
from neutron.db import model_base
from neutron.db import models_v2
from neutron.services.vpn import ca
from neutron.extensions import openvpn
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron.services.vpn.common import constants as vpn_connstants

from sqlalchemy.orm import exc

LOG = logging.getLogger(__name__)


class OpenVPNConnection(model_base.BASEV2,
                     models_v2.HasId, models_v2.HasTenant,
                     models_v2.TimestampMixin):
    """Represents a openvpn Object."""
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    peer_cidr = sa.Column(sa.String(255), nullable=False)
    port = sa.Column(sa.Integer(), nullable=False)
    protocol = sa.Column(sa.Enum("tcp", "udp",
                        name="openvpn_protocol"),
                        nullable=False)
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)
    ta_key = sa.Column(sql.JsonBlob(), nullable=False)
    zip_file = sa.Column(sql.Base64Blob(), nullable=False)
    router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id'),
                          nullable=False)

class OpenVPNDbCommon(common_db_mixin.CommonDbMixin):
    def _get_openvpn_resource(self, context, model, v_id):
        try:
            r = self._get_by_id(context, model, v_id)
        except exc.NoResultFound:
            if issubclass(model, OpenVPNConnection):
                raise openvpn.OpenVPNServiceNotFound(id=v_id)
            else:
                raise
        return r

    def _make_openvpn_ca_dict(self, openvpnconn_db, fields=None):

        res = {'id': openvpnconn_db['id'],
               'tenant_id': openvpnconn_db['tenant_id'],
               'name': openvpnconn_db['name'],
               'description': openvpnconn_db['description'],
               'peer_cidr': openvpnconn_db['peer_cidr'],
               'port': openvpnconn_db['port'],
               'protocol': openvpnconn_db['protocol'],
               'admin_state_up': openvpnconn_db['admin_state_up'],
               'status': openvpnconn_db['status'],
               'ta_key': openvpnconn_db['ta_key'],
               'zip_file': openvpnconn_db['zip_file'],
               'router_id': openvpnconn_db['router_id'],
               'created_at': openvpnconn_db['created_at'],
               }

        return self._fields(res, fields)

    def _make_openvpnconnection_dict(self, openvpnconn_db, fields=None):

        res = {'id': openvpnconn_db['id'],
               'tenant_id': openvpnconn_db['tenant_id'],
               'name': openvpnconn_db['name'],
               'description': openvpnconn_db['description'],
               'peer_cidr': openvpnconn_db['peer_cidr'],
               'port': openvpnconn_db['port'],
               'protocol': openvpnconn_db['protocol'],
               'admin_state_up': openvpnconn_db['admin_state_up'],
               'status': openvpnconn_db['status'],
               'router_id': openvpnconn_db['router_id'],
               'created_at': openvpnconn_db['created_at'],
               }

        return self._fields(res, fields)


    def _get_openvpnconnection_db(self, context,
                               openvpnconnection_id, fields=None):
        return self._get_openvpn_resource(
            context, OpenVPNConnection, openvpnconnection_id)

    def _caculate_openvpn_address(self, cidr):
        _cidr = {}
        net = netaddr.IPNetwork(cidr)
        _cidr['addr'] = str(netaddr.IPAddress(net.first))
        _cidr['netmask'] = str(netaddr.IPAddress(net.netmask))
        return _cidr

    def get_router_cidrs(self, context, router_id):
        """get all cidr of this router to push cidrs to the openvpn's client
        """
        cidrs = set()
        core_plugin = manager.NeutronManager.get_plugin()
        try:
            rport_qry = context.session.query(models_v2.Port)
            rports = rport_qry.filter_by(device_id=router_id)
            for p in rports:
                if p['device_owner'] == l3_constants.DEVICE_OWNER_ROUTER_GW:
                    continue

                for ip in p['fixed_ips']:
                    sub_id = ip['subnet_id']
                    cidr = core_plugin._get_subnet(context.elevated(),
                                                   sub_id)['cidr']
                    cidrs.add(cidr)
                    break
        except exc.NoResultFound:
            pass

        return cidrs

    def get_external(self, context, router_id, peer_cidr):
        external = {}
        external = self._get_router_info(context, router_id)
        net = self._caculate_openvpn_address(peer_cidr)
        external.update(net)
        return external

    def _get_router_info(self, context, router_id):
        l3_plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        if not l3_plugin:
            raise openvpn.RouterExtNotFound()

        core_plugin = manager.NeutronManager.get_plugin()
        if not core_plugin:
            raise openvpn.RouterExtNotFound()

        router = l3_plugin.get_router(context, router_id)

        ex_gw = {}
        ex_gw_port_id = router['gw_port_id'] if 'gw_port_id' in router else None
        ex_gw_port = core_plugin.get_port(context, ex_gw_port_id)
        ex_gw_fixed_ip = ex_gw_port['fixed_ips'][0]['ip_address'] if ex_gw_port else None

        ex_gw_ip=None
        ex_gw_floating = l3_plugin.get_floatingips(context,
               filters={'router_id':[router_id], 'fixed_ip_address':[ex_gw_fixed_ip]})
        if ex_gw_floating:
            ex_gw_ip = ex_gw_floating[0]['floating_ip_address']

        if ex_gw_ip is not None:
            ex_gw['ex_gw_ip'] = ex_gw_ip

        ex_gw['subnets'] = []
        subnets_cidr = self.get_router_cidrs(context, router_id)
        for subnet in subnets_cidr:
            net = self._caculate_openvpn_address(subnet)
            ex_gw['subnets'].append(net)

        return ex_gw

    def check_router_in_use(self, context, router_id):
        # called from l3 db
        openvpn_conns = self.get_openvpnconnections(
            context, filters={'router_id': [router_id]})
        if openvpn_conns:
            raise openvpn.RouterInUseByOpenVPNService(
                router_id=router_id,
                vpnservice_id=openvpn_conns[0]['id'])

class OpenVPNDbMixin(OpenVPNDbCommon):
    """OpenVPN plugin database class using SQLAlchemy models."""
    openvpn_driver = None

    def update_status(self, context, model, v_id, status):
        with context.session.begin(subtransactions=True):
            v_db = self._get_openvpn_resource(context, model, v_id)
            v_db.update({'status': status})

    def _validate_peer_vpn_cidr(self, context, router_id, vpn_cidr):
        """Validate the CIDR for a vpn.

        Verifies the specified CIDR does not overlap with the ones defined
        for the other subnets specified for the router and other openvpn.
        """

        cidrs = set()
        all_cidrs=set()
        openvpns = self.get_openvpnconnections(context, {'router_id': [router_id]})
        for openvpn in openvpns:
            cidrs.add(openvpn['peer_cidr'])

        pptp_conns = self.get_pptpconnections(
            context, filters={'router_id': [router_id]})
        for pptp in pptp_conns:
            all_cidrs.add(pptp['vpn_cidr'])

        subnets_cidr = self.get_router_cidrs(context, router_id)
        if subnets_cidr:
            for cidr in subnets_cidr:
                all_cidrs.add(cidr)
        for cidr in cidrs:
            if cidr in all_cidrs:
                # don't give out details of the overlapping subnet
                err_msg = (_("Requested vpn with cidr: %(cidr)s overlaps with"
                             " another subnet or vpn") %
                           {'cidr': cidr})
                LOG.info(_("Validation for CIDR: %(new_cidr)s failed - "
                           "overlaps with CIDR %(cidr)s "
                           "(CIDR: %(cidr)s)"),
                         {'new_cidr': cidr,
                          'cidr': all_cidrs})

                raise n_exc.InvalidInput(error_message=err_msg)

    def get_openvpn_cons(self, openvpn_db, ca_info):
        openvpn_dbs = {}
        openvpn_dbs.update(openvpn_db)
        openvpn_dbs.update(ca_info)
        return openvpn_dbs

    def get_client_certificate(self, context, id):
        LOG.debug('get client certificate,id:%s' % id)
        name = os.path.basename(ca.get_file_name(id,server=False))+'.zip'
        openvpn_db = self._get_openvpnconnection_db(context, id)
        if 'zip_file' in openvpn_db:
            zip_contents = openvpn_db['zip_file']
            return {'contents':zip_contents,'name':name, 'file':True}
        else:
            raise openvpn.OpenVPNZipfailed(id=id)

    def create_openvpnconnection(self, context, openvpnconnection):
        openvpnconnection = openvpnconnection['openvpnconnection']
        tenant_id = self._get_tenant_id_for_create(context,
                                                   openvpnconnection)
        l3_plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        if not l3_plugin:
            raise openvpn.RouterExtNotFound()

        openvpn_conns = self.get_openvpnconnections(
            context, filters={'router_id': [openvpnconnection['router_id']]})

        if openvpn_conns:
            raise openvpn.OpenvpnInExists(router_id=openvpnconnection['router_id'])

        external = self.get_external(context, openvpnconnection['router_id'],
                                           openvpnconnection['peer_cidr'])
        openvpn_id = uuidutils.generate_uuid()
        openvpnconnection.update(external)
        openvpnconnection.update({'id':openvpn_id})

        ta_key_info = ca.OpenVPNDBDrv().generate_client_ca(openvpn_id)
        openvpn_file = ca.OpenVPNFile(openvpnconnection)
        zip_contents = openvpn_file.generate_zip_file()

        #l3_plugin.get_router(context, openvpnconnection['router_id'])
        with context.session.begin(subtransactions=True):
            self._validate_peer_vpn_cidr(context,
                                    openvpnconnection['router_id'],
                                    openvpnconnection['peer_cidr'])

            openvpnconn_db = OpenVPNConnection(
                id=openvpn_id,
                tenant_id=tenant_id,
                name=openvpnconnection['name'],
                description=openvpnconnection['description'],
                peer_cidr=openvpnconnection['peer_cidr'],
                port=openvpnconnection['port'],
                protocol=openvpnconnection['protocol'],
                router_id=openvpnconnection['router_id'],
                admin_state_up=openvpnconnection['admin_state_up'],
                status=constants.DOWN,
                created_at=timeutils.utcnow(),
                ta_key=ta_key_info['ta_key'],
                zip_file=zip_contents,
            )
            utils.make_default_name(openvpnconn_db, uos_constants.UOS_PRE_OPENVPN)
            context.session.add(openvpnconn_db)

        openvpn_cons = self._make_openvpn_ca_dict(openvpnconn_db)
        openvpn_cons.update(external)
        LOG.debug(_('openvpn service info %s in db '),
                             openvpn_cons)
        #remove all file of client
        openvpn_file.remove_all_file()

        if self.openvpn_driver:
            self.openvpn_driver.create_vpnservice(context, openvpn_cons)

        return self._make_openvpnconnection_dict(openvpnconn_db)

    def notify_router_update(self, context, routerids,update_driver=False):
        openvpn_conns = self.get_openvpnconnections(
            context, filters={'router_id': routerids})
        if not openvpn_conns:
            LOG.debug('this router is not openvpn service')
            return

        LOG.debug('update openvpn db because router updated')
        for openvpn in openvpn_conns:
            openvpns={'openvpnconnection':openvpn}
            self.update_openvpnconnection(context, openvpn['id'], openvpns,
                     force=True, update_driver=update_driver)

    def update_openvpnconnection(
            self, context,
            openvpnconnection_id, openvpnconnection,
            force=False, update_driver=False):
        conn = openvpnconnection['openvpnconnection']
        conn.pop('created_at', None)
        new_admin_state = conn.get('admin_state_up')
        if new_admin_state is not None and not new_admin_state:
            conn['status'] = constants.DOWN

        with context.session.begin(subtransactions=True):
            openvpnconnection_db = self._get_openvpn_resource(
                context,
                OpenVPNConnection,
                openvpnconnection_id)
            old_admin = openvpnconnection_db['admin_state_up']
            admin_change_flag = ('admin_state_up' in conn and
                                 old_admin != conn['admin_state_up'])

            other_change_flag = ('port' in conn and openvpnconnection_db['port'] != \
                                   conn['port'] ) or ('protocol' in conn  and \
                                 openvpnconnection_db['protocol'] != conn['protocol'])
            openvpnconnection_db.update(conn)

        openvpn_service = self._make_openvpn_ca_dict(openvpnconnection_db)
        if self.openvpn_driver and (admin_change_flag or other_change_flag or force):
            external = self.get_external(context, openvpn_service['router_id'], \
                                           openvpn_service['peer_cidr'])
            openvpn_service.update(external)

            #generate zip config file
            openvpn_file = ca.OpenVPNFile(openvpn_service)
            zip_contents = openvpn_file.generate_zip_file()
            openvpn_service['zip_file'] = zip_contents

            #update zip db
            with context.session.begin(subtransactions=True):
                openvpnconnection_db = self._get_openvpn_resource(
                    context,
                    OpenVPNConnection,
                    openvpnconnection_id)

                openvpnconnection_db.update(openvpn_service)

            #remove all file of client
            openvpn_file.remove_all_file()
            LOG.debug(_("update openvpn service, %s") % openvpn_service)
            if update_driver:
                self.openvpn_driver.update_vpnservice(context, None, openvpn_service)

        return self._make_openvpnconnection_dict(openvpnconnection_db)

    def delete_openvpnconnection(self, context, openvpnconnection_id):
        with context.session.begin(subtransactions=True):
            openvpnconnection_db = self._get_openvpn_resource(
                context, OpenVPNConnection, openvpnconnection_id)
            openvpn = self._make_openvpnconnection_dict(openvpnconnection_db)
            context.session.delete(openvpnconnection_db)
        if self.openvpn_driver:
            self.openvpn_driver.delete_vpnservice(context, openvpn)

    def _get_openvpnconnection_db(self, context,
                               openvpnconnection_id, fields=None):
        return self._get_openvpn_resource(
            context, OpenVPNConnection, openvpnconnection_id)

    def get_openvpnconnection(self, context,
                           openvpnconnection_id, fields=None):

        openvpnconnection_db = self._get_openvpn_resource(
            context, OpenVPNConnection, openvpnconnection_id)
        return self._make_openvpnconnection_dict(
            openvpnconnection_db, fields)

    def get_openvpnconnections(self, context, filters=None, fields=None):
        return self._get_collection(context, OpenVPNConnection,
                                    self._make_openvpnconnection_dict,
                                    filters=filters, fields=fields)

    def check_for_dup_router_subnet(self, context, router_id,
                                    subnet_id, subnet_cidr):
        # called from l3 db
        openvpn_conns = self.get_openvpnconnections(
            context, filters={'router_id': [router_id]})
        new_ipnet = netaddr.IPNetwork(subnet_cidr)
        for openvpn in openvpn_conns:
            cidr = openvpn['peer_cidr']
            ipnet = netaddr.IPNetwork(cidr)
            match1 = netaddr.all_matching_cidrs(new_ipnet, [cidr])
            match2 = netaddr.all_matching_cidrs(ipnet, [subnet_cidr])
            if match1 or match2:
                data = {'subnet_cidr': subnet_cidr,
                        'subnet_id': subnet_id,
                        'cidr': cidr,
                        'vpn_id': openvpn['id']}
                msg = (_("Cidr %(subnet_cidr)s of subnet "
                         "%(subnet_id)s overlaps with cidr %(cidr)s "
                         "of openvpn %(vpn_id)s") % data)
                raise n_exc.BadRequest(resource='router', msg=msg)

class OpenVPNPluginRpcDbMixin(OpenVPNDbCommon):
    def _get_vpn_services_by_routers(self, context, router_ids, host=None):
        plugin = manager.NeutronManager.get_plugin()
        l3_plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        agent = plugin._get_agent_by_type_and_host(
            context, l3_constants.AGENT_TYPE_L3, host)

        if not agent.admin_state_up:
            return []
        if not router_ids:
            routers = l3_plugin.list_routers_on_l3_agent(context, agent.id)
            if routers:
                routerids = [router['id'] for router in routers['routers']]
        else:
            routerids=router_ids

        if not routerids:
            return []
        query = context.session.query(OpenVPNConnection)
        _filter = None
        if len(routerids) > 1:
            _filter = (OpenVPNConnection.router_id.in_(routerids))
        else:
            _filter = (OpenVPNConnection.router_id == routerids[0])

        query = query.filter(_filter)
        result = []
        tenantid_users_dict = {}
        for conn in query:
            conn_dict = self._make_openvpn_ca_dict(conn)
            external = self.get_external(context, conn_dict['router_id'],
                                     conn_dict['peer_cidr'])
            conn_dict.update(external)
            result.append(conn_dict)
            #conn_dict[vpn_connstants.CIDR] = cidr_str
        return result


    def _update_status_by_agent(self, ctx, service_status_info_list):
        """Updating vpnservice and vpnconnection status.

        :param context: context variable
        :param service_status_info_list: list of status
        The structure is
        [{id: openvpnconn_id,
          tenant_id: tenant_id
          status: ACTIVE|DOWN|ERROR}]
        The agent will set updated_pending_status as True,
        when agent update any pending status.
        """

        _resource = 'openvpnconnection'
        notifier = n_rpc.get_notifier('network')
        for status in service_status_info_list:
            _ctx = context.Context('', status['tenant_id'])
            payload = {'id': status['id']}
            notifier.info(_ctx, _resource + '.update.start', payload)
        updated_openvpn_dict = []
        with ctx.session.begin(subtransactions=True):
            for openvpn in service_status_info_list:
                try:
                    openvpn_db = self._get_openvpnconnection_db(
                        ctx, openvpn['id'])
                except openvpn.OpenVPNServiceNotFound:
                    LOG.warn(_('vpnservice %s in db is already deleted'),
                             vpnservice['id'])
                    continue

                openvpn_db.status = openvpn['status']
                vpn_dict = self._make_openvpnconnection_dict(openvpn_db)
                updated_openvpn_dict.append(vpn_dict)
        notifier_method = _resource + '.update.end'
        for vpn_dict in updated_openvpn_dict:
            _ctx = context.Context('', vpn_dict['tenant_id'])
            result = {_resource: vpn_dict}
            notifier.info(_ctx,
                          notifier_method,
                          result)

