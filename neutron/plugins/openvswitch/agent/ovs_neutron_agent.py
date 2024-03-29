#!/usr/bin/env python
# Copyright 2011 VMware, Inc.
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

import hashlib
import signal
import sys
import time

import eventlet
eventlet.monkey_patch()

import netaddr
from neutron.plugins.openvswitch.agent import ovs_dvr_neutron_agent
from oslo.config import cfg
from six import moves

from neutron.agent import l2population_rpc
from neutron.agent.linux import ip_lib
from neutron.agent.linux import ovs_lib
from neutron.agent.linux import polling
from neutron.agent.linux import utils
from neutron.agent import rpc as agent_rpc
from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.api.rpc.handlers import dvr_rpc
from neutron.common import config as common_config
from neutron.common import constants as q_const
from neutron.common import uos_constants
from neutron.common import exceptions
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import utils as q_utils
from neutron import context
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.plugins.common import constants as p_const
from neutron.plugins.openvswitch.common import config  # noqa
from neutron.plugins.openvswitch.common import constants


LOG = logging.getLogger(__name__)

# A placeholder for dead vlans.
DEAD_VLAN_TAG = str(q_const.MAX_VLAN_TAG + 1)


class DeviceListRetrievalError(exceptions.NeutronException):
    message = _("Unable to retrieve port details for devices: %(devices)s "
                "because of error: %(error)s")


# A class to represent a VIF (i.e., a port that has 'iface-id' and 'vif-mac'
# attributes set).
class LocalVLANMapping:
    def __init__(self, vlan, network_type, physical_network, segmentation_id,
                 vif_ports=None):
        if vif_ports is None:
            vif_ports = {}
        self.vlan = vlan
        self.network_type = network_type
        self.physical_network = physical_network
        self.segmentation_id = segmentation_id
        self.vif_ports = vif_ports
        # set of tunnel ports on which packets should be flooded
        self.tun_ofports = set()

    def __str__(self):
        return ("lv-id = %s type = %s phys-net = %s phys-id = %s" %
                (self.vlan, self.network_type, self.physical_network,
                 self.segmentation_id))


class OVSPluginApi(agent_rpc.PluginApi,
                   dvr_rpc.DVRServerRpcApiMixin,
                   sg_rpc.SecurityGroupServerRpcApiMixin):
    pass


class OVSSecurityGroupAgent(sg_rpc.SecurityGroupAgentRpcMixin):
    def __init__(self, context, plugin_rpc, root_helper):
        self.context = context
        self.plugin_rpc = plugin_rpc
        self.root_helper = root_helper
        self.init_firewall(defer_refresh_firewall=True)


class OVSNeutronAgent(n_rpc.RpcCallback,
                      sg_rpc.SecurityGroupAgentRpcCallbackMixin,
                      l2population_rpc.L2populationRpcCallBackTunnelMixin,
                      dvr_rpc.DVRAgentRpcCallbackMixin):
    '''Implements OVS-based tunneling, VLANs and flat networks.

    Two local bridges are created: an integration bridge (defaults to
    'br-int') and a tunneling bridge (defaults to 'br-tun'). An
    additional bridge is created for each physical network interface
    used for VLANs and/or flat networks.

    All VM VIFs are plugged into the integration bridge. VM VIFs on a
    given virtual network share a common "local" VLAN (i.e. not
    propagated externally). The VLAN id of this local VLAN is mapped
    to the physical networking details realizing that virtual network.

    For virtual networks realized as GRE tunnels, a Logical Switch
    (LS) identifier is used to differentiate tenant traffic on
    inter-HV tunnels. A mesh of tunnels is created to other
    Hypervisors in the cloud. These tunnels originate and terminate on
    the tunneling bridge of each hypervisor. Port patching is done to
    connect local VLANs on the integration bridge to inter-hypervisor
    tunnels on the tunnel bridge.

    For each virtual network realized as a VLAN or flat network, a
    veth or a pair of patch ports is used to connect the local VLAN on
    the integration bridge with the physical network bridge, with flow
    rules adding, modifying, or stripping VLAN tags as necessary.
    '''

    # history
    #   1.0 Initial version
    #   1.1 Support Security Group RPC
    #   1.2 Support DVR (Distributed Virtual Router) RPC
    RPC_API_VERSION = '1.2'

    def __init__(self, integ_br, tun_br, local_ip,
                 bridge_mappings, root_helper,
                 polling_interval, tunnel_types=None,
                 veth_mtu=None, l2_population=False,
                 enable_distributed_routing=False,
                 minimize_polling=False,
                 ovsdb_monitor_respawn_interval=(
                     constants.DEFAULT_OVSDBMON_RESPAWN),
                 arp_responder=False,
                 use_veth_interconnection=False):
        '''Constructor.

        :param integ_br: name of the integration bridge.
        :param tun_br: name of the tunnel bridge.
        :param local_ip: local IP address of this hypervisor.
        :param bridge_mappings: mappings from physical network name to bridge.
        :param root_helper: utility to use when running shell cmds.
        :param polling_interval: interval (secs) to poll DB.
        :param tunnel_types: A list of tunnel types to enable support for in
               the agent. If set, will automatically set enable_tunneling to
               True.
        :param veth_mtu: MTU size for veth interfaces.
        :param l2_population: Optional, whether L2 population is turned on
        :param minimize_polling: Optional, whether to minimize polling by
               monitoring ovsdb for interface changes.
        :param ovsdb_monitor_respawn_interval: Optional, when using polling
               minimization, the number of seconds to wait before respawning
               the ovsdb monitor.
        :param arp_responder: Optional, enable local ARP responder if it is
               supported.
        :param use_veth_interconnection: use veths instead of patch ports to
               interconnect the integration bridge to physical bridges.
        '''
        super(OVSNeutronAgent, self).__init__()
        self.use_veth_interconnection = use_veth_interconnection
        self.veth_mtu = veth_mtu
        self.root_helper = root_helper
        self.available_local_vlans = set(moves.xrange(q_const.MIN_VLAN_TAG,
                                                      q_const.MAX_VLAN_TAG))
        self.tunnel_types = tunnel_types or []
        self.l2_pop = l2_population
        # TODO(ethuleau): Change ARP responder so it's not dependent on the
        #                 ML2 l2 population mechanism driver.
        self.enable_distributed_routing = enable_distributed_routing
        self.arp_responder_enabled = arp_responder and self.l2_pop
        self.agent_state = {
            'binary': 'neutron-openvswitch-agent',
            'host': cfg.CONF.host,
            'topic': q_const.L2_AGENT_TOPIC,
            'configurations': {'bridge_mappings': bridge_mappings,
                               'tunnel_types': self.tunnel_types,
                               'tunneling_ip': local_ip,
                               'l2_population': self.l2_pop,
                               'arp_responder_enabled':
                               self.arp_responder_enabled,
                               'enable_distributed_routing':
                               self.enable_distributed_routing},
            'agent_type': q_const.AGENT_TYPE_OVS,
            'start_flag': True}

        # Keep track of int_br's device count for use by _report_state()
        self.int_br_device_count = 0

        self.int_br = ovs_lib.OVSBridge(integ_br, self.root_helper)
        self.setup_integration_br()
        # Stores port update notifications for processing in main rpc loop
        self.updated_ports = set()
        self.setup_rpc()
        self.bridge_mappings = bridge_mappings
        self.setup_physical_bridges(self.bridge_mappings)
        self.local_vlan_map = {}
        self.tun_br_ofports = {p_const.TYPE_GRE: {},
                               p_const.TYPE_VXLAN: {}}

        self.polling_interval = polling_interval
        self.minimize_polling = minimize_polling
        self.ovsdb_monitor_respawn_interval = ovsdb_monitor_respawn_interval

        if tunnel_types:
            self.enable_tunneling = True
        else:
            self.enable_tunneling = False
        self.local_ip = local_ip
        self.tunnel_count = 0
        self.vxlan_udp_port = cfg.CONF.AGENT.vxlan_udp_port
        self.dont_fragment = cfg.CONF.AGENT.dont_fragment
        self.tun_br = None
        self.patch_int_ofport = constants.OFPORT_INVALID
        self.patch_tun_ofport = constants.OFPORT_INVALID
        if self.enable_tunneling:
            # The patch_int_ofport and patch_tun_ofport are updated
            # here inside the call to setup_tunnel_br
            self.setup_tunnel_br(tun_br)

        self.dvr_agent = ovs_dvr_neutron_agent.OVSDVRNeutronAgent(
            self.context,
            self.plugin_rpc,
            self.int_br,
            self.tun_br,
            self.patch_int_ofport,
            self.patch_tun_ofport,
            cfg.CONF.host,
            self.enable_tunneling,
            self.enable_distributed_routing)

        self.dvr_agent.setup_dvr_flows_on_integ_tun_br()

        # Collect additional bridges to monitor
        self.ancillary_brs = self.setup_ancillary_bridges(integ_br, tun_br)

        # Security group agent support
        self.sg_agent = OVSSecurityGroupAgent(self.context,
                                              self.plugin_rpc,
                                              root_helper)
        # Initialize iteration counter
        self.iter_num = 0
        self.run_daemon_loop = True
        self.portinfo_dict = dict()

    def _report_state(self):
        # How many devices are likely used by a VM
        self.agent_state.get('configurations')['devices'] = (
            self.int_br_device_count)
        try:
            self.state_rpc.report_state(self.context,
                                        self.agent_state)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception(_("Failed reporting state!"))

    def setup_rpc(self):
        self.agent_id = 'ovs-agent-%s' % cfg.CONF.host
        self.topic = topics.AGENT
        self.plugin_rpc = OVSPluginApi(topics.PLUGIN)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.PLUGIN)

        # RPC network init
        self.context = context.get_admin_context_without_session()
        # Handle updates from service
        self.endpoints = [self]
        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.NETWORK, topics.DELETE],
                     [constants.TUNNEL, topics.UPDATE],
                     [topics.SECURITY_GROUP, topics.UPDATE],
                     [topics.DVR, topics.UPDATE]]
        if self.l2_pop:
            consumers.append([topics.L2POPULATION,
                              topics.UPDATE, cfg.CONF.host])
        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers)
        report_interval = cfg.CONF.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=report_interval)

    def get_net_uuid(self, vif_id):
        for network_id, vlan_mapping in self.local_vlan_map.iteritems():
            if vif_id in vlan_mapping.vif_ports:
                return network_id

    def network_delete(self, context, **kwargs):
        LOG.debug(_("network_delete received"))
        network_id = kwargs.get('network_id')
        LOG.debug(_("Delete %s"), network_id)
        # The network may not be defined on this agent
        lvm = self.local_vlan_map.get(network_id)
        if lvm:
            self.reclaim_local_vlan(network_id)
        else:
            LOG.debug(_("Network %s not used on agent."), network_id)

    def port_update(self, context, **kwargs):
        port = kwargs.get('port')
        # Put the port identifier in the updated_ports set.
        # Even if full port details might be provided to this call,
        # they are not used since there is no guarantee the notifications
        # are processed in the same order as the relevant API requests
        self.updated_ports.add(port['id'])
        LOG.debug(_("port_update message processed for port %s"), port['id'])

    def tunnel_update(self, context, **kwargs):
        LOG.debug(_("tunnel_update received"))
        if not self.enable_tunneling:
            return
        tunnel_ip = kwargs.get('tunnel_ip')
        tunnel_id = kwargs.get('tunnel_id', self.get_ip_in_hex(tunnel_ip))
        if not tunnel_id:
            return
        tunnel_type = kwargs.get('tunnel_type')
        if not tunnel_type:
            LOG.error(_("No tunnel_type specified, cannot create tunnels"))
            return
        if tunnel_type not in self.tunnel_types:
            LOG.error(_("tunnel_type %s not supported by agent"), tunnel_type)
            return
        if tunnel_ip == self.local_ip:
            return
        tun_name = '%s-%s' % (tunnel_type, tunnel_id)
        if not self.l2_pop:
            self._setup_tunnel_port(self.tun_br, tun_name, tunnel_ip,
                                    tunnel_type)

    def fdb_add(self, context, fdb_entries):
        LOG.debug("fdb_add received")
        for lvm, agent_ports in self.get_agent_ports(fdb_entries,
                                                     self.local_vlan_map):
            agent_ports.pop(self.local_ip, None)
            if len(agent_ports):
                if not self.enable_distributed_routing:
                    with self.tun_br.deferred() as deferred_br:
                        self.fdb_add_tun(context, deferred_br, lvm,
                                         agent_ports, self.tun_br_ofports)
                else:
                    self.fdb_add_tun(context, self.tun_br, lvm,
                                     agent_ports, self.tun_br_ofports)

    def fdb_remove(self, context, fdb_entries):
        LOG.debug("fdb_remove received")
        for lvm, agent_ports in self.get_agent_ports(fdb_entries,
                                                     self.local_vlan_map):
            agent_ports.pop(self.local_ip, None)
            if len(agent_ports):
                if not self.enable_distributed_routing:
                    with self.tun_br.deferred() as deferred_br:
                        self.fdb_remove_tun(context, deferred_br, lvm,
                                            agent_ports, self.tun_br_ofports)
                else:
                    self.fdb_remove_tun(context, self.tun_br, lvm,
                                        agent_ports, self.tun_br_ofports)

    def add_fdb_flow(self, br, port_info, remote_ip, lvm, ofport):
        if port_info == q_const.FLOODING_ENTRY:
            lvm.tun_ofports.add(ofport)
            ofports = ','.join(lvm.tun_ofports)
            br.mod_flow(table=constants.FLOOD_TO_TUN,
                        dl_vlan=lvm.vlan,
                        actions="strip_vlan,set_tunnel:%s,output:%s" %
                        (lvm.segmentation_id, ofports))
        else:
            self.setup_entry_for_arp_reply(br, 'add', lvm.vlan, port_info[0],
                                           port_info[1])
            br.add_flow(table=constants.UCAST_TO_TUN,
                        priority=2,
                        dl_vlan=lvm.vlan,
                        dl_dst=port_info[0],
                        actions="strip_vlan,set_tunnel:%s,output:%s" %
                        (lvm.segmentation_id, ofport))

    def del_fdb_flow(self, br, port_info, remote_ip, lvm, ofport):
        if port_info == q_const.FLOODING_ENTRY:
            lvm.tun_ofports.remove(ofport)
            if len(lvm.tun_ofports) > 0:
                ofports = ','.join(lvm.tun_ofports)
                br.mod_flow(table=constants.FLOOD_TO_TUN,
                            dl_vlan=lvm.vlan,
                            actions="strip_vlan,set_tunnel:%s,output:%s" %
                            (lvm.segmentation_id, ofports))
            else:
                # This local vlan doesn't require any more tunnelling
                br.delete_flows(table=constants.FLOOD_TO_TUN, dl_vlan=lvm.vlan)
        else:
            self.setup_entry_for_arp_reply(br, 'remove', lvm.vlan,
                                           port_info[0], port_info[1])
            br.delete_flows(table=constants.UCAST_TO_TUN,
                            dl_vlan=lvm.vlan,
                            dl_dst=port_info[0])

    def _fdb_chg_ip(self, context, fdb_entries):
        LOG.debug("update chg_ip received")
        with self.tun_br.deferred() as deferred_br:
            self.fdb_chg_ip_tun(context, deferred_br, fdb_entries,
                                self.local_ip, self.local_vlan_map)

    def setup_entry_for_arp_reply(self, br, action, local_vid, mac_address,
                                  ip_address):
        '''Set the ARP respond entry.

        When the l2 population mechanism driver and OVS supports to edit ARP
        fields, a table (ARP_RESPONDER) to resolve ARP locally is added to the
        tunnel bridge.
        '''
        if not self.arp_responder_enabled:
            return

        mac = netaddr.EUI(mac_address, dialect=netaddr.mac_unix)
        ip = netaddr.IPAddress(ip_address)

        if action == 'add':
            actions = constants.ARP_RESPONDER_ACTIONS % {'mac': mac, 'ip': ip}
            br.add_flow(table=constants.ARP_RESPONDER,
                        priority=1,
                        proto='arp',
                        dl_vlan=local_vid,
                        nw_dst='%s' % ip,
                        actions=actions)
        elif action == 'remove':
            br.delete_flows(table=constants.ARP_RESPONDER,
                            proto='arp',
                            dl_vlan=local_vid,
                            nw_dst='%s' % ip)
        else:
            LOG.warning(_('Action %s not supported'), action)

    def provision_local_vlan(self, net_uuid, network_type, physical_network,
                             segmentation_id):
        '''Provisions a local VLAN.

        :param net_uuid: the uuid of the network associated with this vlan.
        :param network_type: the network type ('gre', 'vxlan', 'vlan', 'flat',
                                               'local')
        :param physical_network: the physical network for 'vlan' or 'flat'
        :param segmentation_id: the VID for 'vlan' or tunnel ID for 'tunnel'
        '''

        # On a restart or crash of OVS, the network associated with this VLAN
        # will already be assigned, so check for that here before assigning a
        # new one.
        lvm = self.local_vlan_map.get(net_uuid)
        if lvm:
            lvid = lvm.vlan
        else:
            if not self.available_local_vlans:
                LOG.error(_("No local VLAN available for net-id=%s"), net_uuid)
                return
            lvid = self.available_local_vlans.pop()
            self.local_vlan_map[net_uuid] = LocalVLANMapping(lvid,
                                                             network_type,
                                                             physical_network,
                                                             segmentation_id)

        LOG.info(_("Assigning %(vlan_id)s as local vlan for "
                   "net-id=%(net_uuid)s"),
                 {'vlan_id': lvid, 'net_uuid': net_uuid})

        if network_type in constants.TUNNEL_NETWORK_TYPES:
            if self.enable_tunneling:
                # outbound broadcast/multicast
                ofports = ','.join(self.tun_br_ofports[network_type].values())
                if ofports:
                    self.tun_br.mod_flow(table=constants.FLOOD_TO_TUN,
                                         dl_vlan=lvid,
                                         actions="strip_vlan,"
                                         "set_tunnel:%s,output:%s" %
                                         (segmentation_id, ofports))
                # inbound from tunnels: set lvid in the right table
                # and resubmit to Table LEARN_FROM_TUN for mac learning
                if self.enable_distributed_routing:
                    self.dvr_agent.process_tunneled_network(
                        network_type, lvid, segmentation_id)
                else:
                    self.tun_br.add_flow(
                        table=constants.TUN_TABLE[network_type],
                        priority=1,
                        tun_id=segmentation_id,
                        actions="mod_vlan_vid:%s,"
                        "resubmit(,%s)" %
                        (lvid, constants.LEARN_FROM_TUN))

            else:
                LOG.error(_("Cannot provision %(network_type)s network for "
                          "net-id=%(net_uuid)s - tunneling disabled"),
                          {'network_type': network_type,
                           'net_uuid': net_uuid})
        elif network_type == p_const.TYPE_FLAT:
            if physical_network in self.phys_brs:
                # outbound
                br = self.phys_brs[physical_network]
                br.add_flow(priority=4,
                            in_port=self.phys_ofports[physical_network],
                            dl_vlan=lvid,
                            actions="strip_vlan,normal")
                # inbound
                self.int_br.add_flow(
                    priority=3,
                    in_port=self.int_ofports[physical_network],
                    dl_vlan=0xffff,
                    actions="mod_vlan_vid:%s,normal" % lvid)
            else:
                LOG.error(_("Cannot provision flat network for "
                            "net-id=%(net_uuid)s - no bridge for "
                            "physical_network %(physical_network)s"),
                          {'net_uuid': net_uuid,
                           'physical_network': physical_network})
        elif network_type == p_const.TYPE_VLAN:
            if physical_network in self.phys_brs:
                # outbound
                br = self.phys_brs[physical_network]
                br.add_flow(priority=4,
                            in_port=self.phys_ofports[physical_network],
                            dl_vlan=lvid,
                            actions="mod_vlan_vid:%s,normal" % segmentation_id)
                # inbound
                self.int_br.add_flow(priority=3,
                                     in_port=self.
                                     int_ofports[physical_network],
                                     dl_vlan=segmentation_id,
                                     actions="mod_vlan_vid:%s,normal" % lvid)
            else:
                LOG.error(_("Cannot provision VLAN network for "
                            "net-id=%(net_uuid)s - no bridge for "
                            "physical_network %(physical_network)s"),
                          {'net_uuid': net_uuid,
                           'physical_network': physical_network})
        elif network_type == p_const.TYPE_LOCAL:
            # no flows needed for local networks
            pass
        else:
            LOG.error(_("Cannot provision unknown network type "
                        "%(network_type)s for net-id=%(net_uuid)s"),
                      {'network_type': network_type,
                       'net_uuid': net_uuid})

    def reclaim_local_vlan(self, net_uuid):
        '''Reclaim a local VLAN.

        :param net_uuid: the network uuid associated with this vlan.
        :param lvm: a LocalVLANMapping object that tracks (vlan, lsw_id,
            vif_ids) mapping.
        '''
        lvm = self.local_vlan_map.pop(net_uuid, None)
        if lvm is None:
            LOG.debug(_("Network %s not used on agent."), net_uuid)
            return

        LOG.info(_("Reclaiming vlan = %(vlan_id)s from net-id = %(net_uuid)s"),
                 {'vlan_id': lvm.vlan,
                  'net_uuid': net_uuid})

        if lvm.network_type in constants.TUNNEL_NETWORK_TYPES:
            if self.enable_tunneling:
                self.tun_br.delete_flows(
                    table=constants.TUN_TABLE[lvm.network_type],
                    tun_id=lvm.segmentation_id)
                self.tun_br.delete_flows(dl_vlan=lvm.vlan)
                if self.l2_pop:
                    # Try to remove tunnel ports if not used by other networks
                    for ofport in lvm.tun_ofports:
                        self.cleanup_tunnel_port(self.tun_br, ofport,
                                                 lvm.network_type)
        elif lvm.network_type == p_const.TYPE_FLAT:
            if lvm.physical_network in self.phys_brs:
                # outbound
                br = self.phys_brs[lvm.physical_network]
                br.delete_flows(in_port=self.phys_ofports[lvm.
                                                          physical_network],
                                dl_vlan=lvm.vlan)
                # inbound
                br = self.int_br
                br.delete_flows(in_port=self.int_ofports[lvm.physical_network],
                                dl_vlan=0xffff)
        elif lvm.network_type == p_const.TYPE_VLAN:
            if lvm.physical_network in self.phys_brs:
                # outbound
                br = self.phys_brs[lvm.physical_network]
                br.delete_flows(in_port=self.phys_ofports[lvm.
                                                          physical_network],
                                dl_vlan=lvm.vlan)
                # inbound
                br = self.int_br
                br.delete_flows(in_port=self.int_ofports[lvm.physical_network],
                                dl_vlan=lvm.segmentation_id)
        elif lvm.network_type == p_const.TYPE_LOCAL:
            # no flows needed for local networks
            pass
        else:
            LOG.error(_("Cannot reclaim unknown network type "
                        "%(network_type)s for net-id=%(net_uuid)s"),
                      {'network_type': lvm.network_type,
                       'net_uuid': net_uuid})

        self.available_local_vlans.add(lvm.vlan)

    def add_dev_tc(self, device_name, profile):
        if not device_name.startswith("qvo"):
            return
        device_name = device_name.replace("qvo", "qvb")
        try:
            utils.execute(["tc", "qdisc", "delete", "dev", device_name,
                           "root"], root_helper="sudo",
                          log_fail_as_error=False)
        except RuntimeError:
            pass
        try:
            utils.execute(["tc", "qdisc", "replace", "dev", device_name,
                           "root", "handle", "1:0", "htb", "default", "ff"],
                          root_helper="sudo")
            # "1": "rate"
            uos_marks = profile.get('uos_marks', {})
            for mark, rate_limit in uos_marks.iteritems():
                burst = str(int(rate_limit) / 100)
                utils.execute(["tc", "class", "replace", "dev", device_name,
                               "parent", "1:", "classid", "1:%x" % int(mark),
                               "htb", "rate", "%skbit" % rate_limit,
                               "ceil", "%skbit"% rate_limit,
                               "burst", "%sk" % burst, "cburst", "%sk" % burst,
                               "prio", "10"], root_helper="sudo")
                utils.execute(["tc", "qdisc", "replace", "dev", device_name,
                               "parent", "1:%x" % int(mark), "handle",
                               mark, "sfq"],
                              root_helper="sudo")
                utils.execute(["tc", "filter", "add", "dev", device_name,
                               "parent", "1:0", "prio", "10", "handle", mark,
                               "fw", "flowid", "1:%x" % int(mark)],
                               root_helper="sudo")
        except RuntimeError:
            pass

    def port_bound(self, port, net_uuid,
                   network_type, physical_network,
                   segmentation_id, fixed_ips, device_owner,
                   ovs_restarted):
        '''Bind port to net_uuid/lsw_id and install flow for inbound traffic
        to vm.

        :param port: a ovslib.VifPort object.
        :param net_uuid: the net_uuid this port is to be associated with.
        :param network_type: the network type ('gre', 'vlan', 'flat', 'local')
        :param physical_network: the physical network for 'vlan' or 'flat'
        :param segmentation_id: the VID for 'vlan' or tunnel ID for 'tunnel'
        :param fixed_ips: the ip addresses assigned to this port
        :param device_owner: the string indicative of owner of this port
        :param ovs_restarted: indicates if this is called for an OVS restart.
        '''
        if net_uuid not in self.local_vlan_map or ovs_restarted:
            self.provision_local_vlan(net_uuid, network_type,
                                      physical_network, segmentation_id)
        lvm = self.local_vlan_map[net_uuid]
        lvm.vif_ports[port.vif_id] = port

        self.dvr_agent.bind_port_to_dvr(port, network_type, fixed_ips,
                                        device_owner,
                                        local_vlan_id=lvm.vlan)

        # Do not bind a port if it's already bound
        cur_tag = self.int_br.db_get_val("Port", port.port_name, "tag")
        if cur_tag != str(lvm.vlan):
            self.int_br.set_db_attribute("Port", port.port_name, "tag",
                                         str(lvm.vlan))
            # Here we info log
            LOG.info(_("Port bound tag changed,pid: %(pid)s network type "
                          "%(network_type)s for net-id=%(net_uuid)s"
                          " cur tag is %(cur_tag)s, dl_vlan is %(dl_vlan)s"),
                      {'pid': port.vif_id,
                       'network_type': lvm.network_type,
                       'net_uuid': net_uuid,
                       'cur_tag': cur_tag,
                       'dl_vlan': lvm.vlan})

            if port.ofport != -1:
                self.int_br.delete_flows(in_port=port.ofport)
                #we should replace flows in phys br and int br
                #if it is in vlan/flat network
                try:
                    delete_vlan = int(cur_tag)
                    # if qvo exists cur_tag should be []
                    # but if some error happened in nova
                    # this maybe None, so except TypeError
                    # and ValueError
                except (TypeError, ValueError):
                    LOG.info("cur tag is %s, not need change flows", cur_tag)
                    return

                if cur_tag == DEAD_VLAN_TAG:
                    LOG.debug("cur tag is DEAD_VLAN_TAG")
                    return

                if cfg.CONF.USTACK.reset_ovs or ovs_restarted:
                    LOG.info("port bound port %s vlan changed", port.vif_id)
                    return

                if physical_network:
                    LOG.error("port %s is physical network and port vlan changed", port.vif_id)
                    br = self.phys_brs[physical_network]
                    if (br and (network_type == p_const.TYPE_VLAN)):
                        LOG.info("port is vlan physical_network")
                        br.delete_flows(
                            in_port=self.phys_ofports[physical_network],
                            dl_vlan=delete_vlan)
                        br.add_flow(priority=4,
                            in_port=self.phys_ofports[physical_network],
                            dl_vlan=lvm.vlan,
                            actions="mod_vlan_vid:%s,normal" % segmentation_id)

                        self.int_br.mod_flow(
                                     in_port=self.int_ofports[physical_network],
                                     dl_vlan=segmentation_id,
                                     actions="mod_vlan_vid:%s,normal" % lvm.vlan)

                    if (br and (network_type == p_const.TYPE_FLAT)):
                        br.delete_flows(
                            in_port=self.phys_ofports[physical_network],
                            dl_vlan=delete_vlan)
                        br.add_flow(priority=4,
                            in_port=self.phys_ofports[physical_network],
                            dl_vlan=lvm.vlan,
                            actions="strip_vlan,normal")

                        self.int_br.mod_flow(
                            in_port=self.int_ofports[physical_network],
                            dl_vlan=0xffff,
                            actions="mod_vlan_vid:%s,normal" % lvm.vlan)
                else:
                    LOG.error("port %s is tunnel network and port vlan changed", port.vif_id)
                    if network_type in constants.TUNNEL_NETWORK_TYPES:
                        self.tun_br.delete_flows(dl_vlan=delete_vlan)

                # Should we delete the match vlan action tunnel
                # in table FLOOD_TO_TUN and ARP_RESPONDER like bellow?
                #if network_type in constants.TUNNEL_NETWORK_TYPES:
                #    if self.enable_tunneling:
                #        # we should delete flows in phys br and tun br
                #        # here is tun br
                #        self.tun_br.delete_flows(table=constants.FLOOD_TO_TUN,
                #                                 dl_vlan=cur_tag)
                #    if not self.arp_responder_enabled:
                #        # we should delete flows in phys br and tun br
                #        # here is tun br
                #        #also delete all arp flows
                #       self.tun_br.delete_flows(table=constants.ARP_RESPONDER,
                #                proto='arp',
                #                dl_vlan=local_vid)
                # Should we also make dvr agent notified or clear the flows in
                # DVR_TO_SRC_MAC of dvr mode.DVR_PROCESS

    def port_unbound(self, vif_id, net_uuid=None):
        '''Unbind port.

        Removes corresponding local vlan mapping object if this is its last
        VIF.

        :param vif_id: the id of the vif
        :param net_uuid: the net_uuid this port is associated with.
        '''
        if net_uuid is None:
            net_uuid = self.get_net_uuid(vif_id)

        if not self.local_vlan_map.get(net_uuid):
            LOG.info(_('port_unbound(): net_uuid %s not in local_vlan_map'),
                     net_uuid)
            return

        lvm = self.local_vlan_map[net_uuid]

        if vif_id in lvm.vif_ports:
            vif_port = lvm.vif_ports[vif_id]
            self.dvr_agent.unbind_port_from_dvr(vif_port,
                                                local_vlan_id=lvm.vlan)
        lvm.vif_ports.pop(vif_id, None)

        if not lvm.vif_ports:
            self.reclaim_local_vlan(net_uuid)

    def port_dead(self, port):
        '''Once a port has no binding, put it on the "dead vlan".

        :param port: a ovs_lib.VifPort object.
        '''
        # Don't kill a port if it's already dead
        cur_tag = self.int_br.db_get_val("Port", port.port_name, "tag")
        if cur_tag != DEAD_VLAN_TAG:
            self.int_br.set_db_attribute("Port", port.port_name, "tag",
                                         DEAD_VLAN_TAG)
            self.int_br.add_flow(priority=2, in_port=port.ofport,
                                 actions="drop")

    def setup_integration_br(self, ovs_restarted = False):
        '''Setup the integration bridge.

        Create patch ports and remove all existing flows.

        :param bridge_name: the name of the integration bridge.
        :returns: the integration bridge
        '''
        # Ensure the integration bridge is created.
        # ovs_lib.OVSBridge.create() will run
        #   ovs-vsctl -- --may-exist add-br BRIDGE_NAME
        # which does nothing if bridge already exists.
        self.int_br.create()
        self.int_br.set_secure_mode()

        # Only if when the ovs_restarted or need reset
        # should we clear all flows and rebuild patch port
        if cfg.CONF.USTACK.reset_ovs or ovs_restarted:
            self.int_br.delete_port(cfg.CONF.OVS.int_peer_patch_port)
            self.int_br.remove_all_flows()

        # switch all traffic using L2 learning
        self.int_br.add_flow(priority=1, actions="normal")
        # Add a canary flow to int_br to track OVS restarts
        self.int_br.add_flow(table=constants.CANARY_TABLE, priority=0,
                             actions="drop")

    def setup_ancillary_bridges(self, integ_br, tun_br):
        '''Setup ancillary bridges - for example br-ex.'''
        ovs_bridges = set(ovs_lib.get_bridges(self.root_helper))
        # Remove all known bridges
        ovs_bridges.remove(integ_br)
        if self.enable_tunneling:
            ovs_bridges.remove(tun_br)
        br_names = [self.phys_brs[physical_network].br_name for
                    physical_network in self.phys_brs]
        ovs_bridges.difference_update(br_names)
        # Filter list of bridges to those that have external
        # bridge-id's configured
        br_names = []
        for bridge in ovs_bridges:
            id = ovs_lib.get_bridge_external_bridge_id(self.root_helper,
                                                       bridge)
            if id != bridge:
                br_names.append(bridge)
        ovs_bridges.difference_update(br_names)
        ancillary_bridges = []
        for bridge in ovs_bridges:
            br = ovs_lib.OVSBridge(bridge, self.root_helper)
            if br.br_name == 'br-ex':
                br.add_flow(table=0,
                            dl_type='0x0800',
                            nw_dst=uos_constants.UOS_EX_RESERVED_NET,
                            priority=1,
                            actions='resubmit(,%s)' %
                                uos_constants.UOS_EX_RESERVED_NET_TABLE)
                br.add_flow(table=0,
                            priority=0,
                            actions='NORMAL')
                br.add_flow(table=uos_constants.UOS_EX_RESERVED_NET_TABLE,
                            priority=0,
                            actions='drop')
            LOG.info(_('Adding %s to list of bridges.'), bridge)
            ancillary_bridges.append(br)
        return ancillary_bridges

    def setup_tunnel_br(self, tun_br_name=None, ovs_restarted = False):
        '''Setup the tunnel bridge.

        Creates tunnel bridge, and links it to the integration bridge
        using a patch port.

        :param tun_br_name: the name of the tunnel bridge.
        '''
        if not self.tun_br:
            self.tun_br = ovs_lib.OVSBridge(tun_br_name, self.root_helper)

        if cfg.CONF.USTACK.reset_ovs or ovs_restarted:
            self.tun_br.reset_bridge()
            self.tun_br.remove_all_flows()
        else:
            self.tun_br.create()
        self.patch_tun_ofport = self.int_br.add_patch_port(
            cfg.CONF.OVS.int_peer_patch_port, cfg.CONF.OVS.tun_peer_patch_port)
        self.patch_int_ofport = self.tun_br.add_patch_port(
            cfg.CONF.OVS.tun_peer_patch_port, cfg.CONF.OVS.int_peer_patch_port)
        if int(self.patch_tun_ofport) < 0 or int(self.patch_int_ofport) < 0:
            LOG.error(_("Failed to create OVS patch port. Cannot have "
                        "tunneling enabled on this agent, since this version "
                        "of OVS does not support tunnels or patch ports. "
                        "Agent terminated!"))
            exit(1)

        # Table 0 (default) will sort incoming traffic depending on in_port
        self.tun_br.add_flow(priority=1,
                             in_port=self.patch_int_ofport,
                             actions="resubmit(,%s)" %
                             constants.PATCH_LV_TO_TUN)
        self.tun_br.add_flow(priority=0, actions="drop")
        if self.arp_responder_enabled:
            # ARP broadcast-ed request go to the local ARP_RESPONDER table to
            # be locally resolved
            self.tun_br.add_flow(table=constants.PATCH_LV_TO_TUN,
                                 priority=1,
                                 proto='arp',
                                 dl_dst="ff:ff:ff:ff:ff:ff",
                                 actions=("resubmit(,%s)" %
                                          constants.ARP_RESPONDER))
        # PATCH_LV_TO_TUN table will handle packets coming from patch_int
        # unicasts go to table UCAST_TO_TUN where remote addresses are learnt
        self.tun_br.add_flow(table=constants.PATCH_LV_TO_TUN,
                             priority=0,
                             dl_dst="00:00:00:00:00:00/01:00:00:00:00:00",
                             actions="resubmit(,%s)" % constants.UCAST_TO_TUN)
        # Broadcasts/multicasts go to table FLOOD_TO_TUN that handles flooding
        self.tun_br.add_flow(table=constants.PATCH_LV_TO_TUN,
                             priority=0,
                             dl_dst="01:00:00:00:00:00/01:00:00:00:00:00",
                             actions="resubmit(,%s)" % constants.FLOOD_TO_TUN)
        # Tables [tunnel_type]_TUN_TO_LV will set lvid depending on tun_id
        # for each tunnel type, and resubmit to table LEARN_FROM_TUN where
        # remote mac addresses will be learnt
        for tunnel_type in constants.TUNNEL_NETWORK_TYPES:
            self.tun_br.add_flow(table=constants.TUN_TABLE[tunnel_type],
                                 priority=0,
                                 actions="drop")
        # LEARN_FROM_TUN table will have a single flow using a learn action to
        # dynamically set-up flows in UCAST_TO_TUN corresponding to remote mac
        # addresses (assumes that lvid has already been set by a previous flow)
        learned_flow = ("table=%s,"
                        "priority=1,"
                        "hard_timeout=300,"
                        "NXM_OF_VLAN_TCI[0..11],"
                        "NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],"
                        "load:0->NXM_OF_VLAN_TCI[],"
                        "load:NXM_NX_TUN_ID[]->NXM_NX_TUN_ID[],"
                        "output:NXM_OF_IN_PORT[]" %
                        constants.UCAST_TO_TUN)
        # Once remote mac addresses are learnt, output packet to patch_int
        self.tun_br.add_flow(table=constants.LEARN_FROM_TUN,
                             priority=1,
                             actions="learn(%s),output:%s" %
                             (learned_flow, self.patch_int_ofport))
        # Egress unicast will be handled in table UCAST_TO_TUN, where remote
        # mac addresses will be learned. For now, just add a default flow that
        # will resubmit unknown unicasts to table FLOOD_TO_TUN to treat them
        # as broadcasts/multicasts
        self.tun_br.add_flow(table=constants.UCAST_TO_TUN,
                             priority=0,
                             actions="resubmit(,%s)" %
                             constants.FLOOD_TO_TUN)
        if self.arp_responder_enabled:
            # If none of the ARP entries correspond to the requested IP, the
            # broadcast-ed packet is resubmitted to the flooding table
            self.tun_br.add_flow(table=constants.ARP_RESPONDER,
                                 priority=0,
                                 actions="resubmit(,%s)" %
                                 constants.FLOOD_TO_TUN)
        # FLOOD_TO_TUN will handle flooding in tunnels based on lvid,
        # for now, add a default drop action
        self.tun_br.add_flow(table=constants.FLOOD_TO_TUN,
                             priority=0,
                             actions="drop")

    def get_peer_name(self, prefix, name):
        """Construct a peer name based on the prefix and name.

        The peer name can not exceed the maximum length allowed for a linux
        device. Longer names are hashed to help ensure uniqueness.
        """
        if len(prefix + name) <= q_const.DEVICE_NAME_MAX_LEN:
            return prefix + name
        # We can't just truncate because bridges may be distinguished
        # by an ident at the end. A hash over the name should be unique.
        # Leave part of the bridge name on for easier identification
        hashlen = 6
        namelen = q_const.DEVICE_NAME_MAX_LEN - len(prefix) - hashlen
        new_name = ('%(prefix)s%(truncated)s%(hash)s' %
                    {'prefix': prefix, 'truncated': name[0:namelen],
                     'hash': hashlib.sha1(name).hexdigest()[0:hashlen]})
        LOG.warning(_("Creating an interface named %(name)s exceeds the "
                      "%(limit)d character limitation. It was shortened to "
                      "%(new_name)s to fit."),
                    {'name': name, 'limit': q_const.DEVICE_NAME_MAX_LEN,
                     'new_name': new_name})
        return new_name

    def setup_physical_bridges(self, bridge_mappings, ovs_restarted = False):
        '''Setup the physical network bridges.

        Creates physical network bridges and links them to the
        integration bridge using veths.

        :param bridge_mappings: map physical network names to bridge names.
        '''
        self.phys_brs = {}
        self.int_ofports = {}
        self.phys_ofports = {}
        ip_wrapper = ip_lib.IPWrapper(self.root_helper)
        ovs_bridges = ovs_lib.get_bridges(self.root_helper)
        for physical_network, bridge in bridge_mappings.iteritems():
            LOG.info(_("Mapping physical network %(physical_network)s to "
                       "bridge %(bridge)s"),
                     {'physical_network': physical_network,
                      'bridge': bridge})
            # setup physical bridge
            if bridge not in ovs_bridges:
                LOG.error(_("Bridge %(bridge)s for physical network "
                            "%(physical_network)s does not exist. Agent "
                            "terminated!"),
                          {'physical_network': physical_network,
                           'bridge': bridge})
                sys.exit(1)
            br = ovs_lib.OVSBridge(bridge, self.root_helper)

            if cfg.CONF.USTACK.reset_ovs or ovs_restarted:
                br.remove_all_flows()
            br.add_flow(priority=1, actions="normal")
            self.phys_brs[physical_network] = br

            # interconnect physical and integration bridges using veth/patchs
            int_if_name = self.get_peer_name(constants.PEER_INTEGRATION_PREFIX,
                                             bridge)
            phys_if_name = self.get_peer_name(constants.PEER_PHYSICAL_PREFIX,
                                              bridge)
            if cfg.CONF.USTACK.reset_ovs or ovs_restarted:
                self.int_br.delete_port(int_if_name)
                br.delete_port(phys_if_name)
            if self.use_veth_interconnection:
                if ip_lib.device_exists(int_if_name, self.root_helper):
                    if cfg.CONF.USTACK.reset_ovs or ovs_restarted:
                        ip_lib.IPDevice(int_if_name,
                                        self.root_helper).link.delete()
                    # Give udev a chance to process its rules here, to avoid
                    # race conditions between commands launched by udev rules
                    # and the subsequent call to ip_wrapper.add_veth
                    utils.execute(['/sbin/udevadm', 'settle', '--timeout=10'])
                int_veth, phys_veth = ip_wrapper.add_veth(int_if_name,
                                                          phys_if_name)
                int_ofport = self.int_br.add_port(int_veth)
                phys_ofport = br.add_port(phys_veth)
            else:
                # Create patch ports without associating them in order to block
                # untranslated traffic before association
                int_ofport = self.int_br.add_patch_port(
                    int_if_name, constants.NONEXISTENT_PEER)
                phys_ofport = br.add_patch_port(
                    phys_if_name, constants.NONEXISTENT_PEER)

            self.int_ofports[physical_network] = int_ofport
            self.phys_ofports[physical_network] = phys_ofport

            # block all untranslated traffic between bridges
            self.int_br.add_flow(priority=2, in_port=int_ofport,
                                 actions="drop")
            br.add_flow(priority=2, in_port=phys_ofport, actions="drop")

            if self.use_veth_interconnection:
                # enable veth to pass traffic
                int_veth.link.set_up()
                phys_veth.link.set_up()
                if self.veth_mtu:
                    # set up mtu size for veth interfaces
                    int_veth.link.set_mtu(self.veth_mtu)
                    phys_veth.link.set_mtu(self.veth_mtu)
            else:
                # associate patch ports to pass traffic
                self.int_br.set_db_attribute('Interface', int_if_name,
                                             'options:peer', phys_if_name)
                br.set_db_attribute('Interface', phys_if_name,
                                    'options:peer', int_if_name)

    def scan_ports(self, registered_ports, updated_ports=None):
        cur_ports = self.int_br.get_vif_port_set()
        self.int_br_device_count = len(cur_ports)
        port_info = {'current': cur_ports}
        if updated_ports is None:
            updated_ports = set()
        updated_ports.update(self.check_changed_vlans(registered_ports))
        if updated_ports:
            # Some updated ports might have been removed in the
            # meanwhile, and therefore should not be processed.
            # In this case the updated port won't be found among
            # current ports.
            updated_ports &= cur_ports
            if updated_ports:
                port_info['updated'] = updated_ports

        # FIXME(salv-orlando): It's not really necessary to return early
        # if nothing has changed.
        if cur_ports == registered_ports:
            # No added or removed ports to set, just return here
            return port_info

        port_info['added'] = cur_ports - registered_ports
        # Remove all the known ports not found on the integration bridge
        port_info['removed'] = registered_ports - cur_ports
        return port_info

    def check_changed_vlans(self, registered_ports):
        """Return ports which have lost their vlan tag.

        The returned value is a set of port ids of the ports concerned by a
        vlan tag loss.
        """
        port_tags = self.int_br.get_port_tag_dict()
        changed_ports = set()
        for lvm in self.local_vlan_map.values():
            for port in registered_ports:
                if (
                    port in lvm.vif_ports
                    and lvm.vif_ports[port].port_name in port_tags
                    and port_tags[lvm.vif_ports[port].port_name] != lvm.vlan
                ):
                    LOG.info(
                        _("Port '%(port_name)s' has lost "
                            "its vlan tag '%(vlan_tag)d'!"),
                        {'port_name': lvm.vif_ports[port].port_name,
                         'vlan_tag': lvm.vlan}
                    )
                    changed_ports.add(port)
        return changed_ports

    def update_ancillary_ports(self, registered_ports):
        ports = set()
        for bridge in self.ancillary_brs:
            ports |= bridge.get_vif_port_set()

        if ports == registered_ports:
            return
        added = ports - registered_ports
        removed = registered_ports - ports
        return {'current': ports,
                'added': added,
                'removed': removed}

    def treat_vif_port(self, vif_port, port_id, network_id, network_type,
                       physical_network, segmentation_id, admin_state_up,
                       fixed_ips, device_owner, ovs_restarted, profile):
        # When this function is called for a port, the port should have
        # an OVS ofport configured, as only these ports were considered
        # for being treated. If that does not happen, it is a potential
        # error condition of which operators should be aware
        if not vif_port.ofport:
            LOG.warn(_("VIF port: %s has no ofport configured, and might not "
                       "be able to transmit"), vif_port.vif_id)
        if vif_port:
            if admin_state_up:
                self.port_bound(vif_port, network_id, network_type,
                                physical_network, segmentation_id,
                                fixed_ips, device_owner, ovs_restarted)
                self.add_dev_tc(vif_port.port_name, profile)
            else:
                self.port_dead(vif_port)
        else:
            LOG.debug(_("No VIF port for port %s defined on agent."), port_id)

    def _setup_tunnel_port(self, br, port_name, remote_ip, tunnel_type):
        ofport = br.add_tunnel_port(port_name,
                                    remote_ip,
                                    self.local_ip,
                                    tunnel_type,
                                    self.vxlan_udp_port,
                                    self.dont_fragment)
        ofport_int = -1
        try:
            ofport_int = int(ofport)
        except (TypeError, ValueError):
            LOG.exception(_("ofport should have a value that can be "
                            "interpreted as an integer"))
        if ofport_int < 0:
            LOG.error(_("Failed to set-up %(type)s tunnel port to %(ip)s"),
                      {'type': tunnel_type, 'ip': remote_ip})
            return 0

        self.tun_br_ofports[tunnel_type][remote_ip] = ofport
        # Add flow in default table to resubmit to the right
        # tunnelling table (lvid will be set in the latter)
        br.add_flow(priority=1,
                    in_port=ofport,
                    actions="resubmit(,%s)" %
                    constants.TUN_TABLE[tunnel_type])

        ofports = ','.join(self.tun_br_ofports[tunnel_type].values())
        if ofports and not self.l2_pop:
            # Update flooding flows to include the new tunnel
            for network_id, vlan_mapping in self.local_vlan_map.iteritems():
                if vlan_mapping.network_type == tunnel_type:
                    br.mod_flow(table=constants.FLOOD_TO_TUN,
                                dl_vlan=vlan_mapping.vlan,
                                actions="strip_vlan,set_tunnel:%s,output:%s" %
                                (vlan_mapping.segmentation_id, ofports))
        return ofport

    def setup_tunnel_port(self, br, remote_ip, network_type):
        remote_ip_hex = self.get_ip_in_hex(remote_ip)
        if not remote_ip_hex:
            return 0
        port_name = '%s-%s' % (network_type, remote_ip_hex)
        ofport = self._setup_tunnel_port(br,
                                         port_name,
                                         remote_ip,
                                         network_type)
        return ofport

    def cleanup_tunnel_port(self, br, tun_ofport, tunnel_type):
        # Check if this tunnel port is still used
        for lvm in self.local_vlan_map.values():
            if tun_ofport in lvm.tun_ofports:
                break
        # If not, remove it
        else:
            for remote_ip, ofport in self.tun_br_ofports[tunnel_type].items():
                if ofport == tun_ofport:
                    port_name = '%s-%s' % (tunnel_type,
                                           self.get_ip_in_hex(remote_ip))
                    br.delete_port(port_name)
                    br.delete_flows(in_port=ofport)
                    self.tun_br_ofports[tunnel_type].pop(remote_ip, None)

    def treat_devices_added_or_updated(self, devices, ovs_restarted,
                                       d_details=None):
        skipped_devices = []
        try:
            _d_details = d_details or self.plugin_rpc.get_devices_details_list(
                self.context,
                devices,
                self.agent_id,
                cfg.CONF.host)

            # For the ports can be found in server db, we rebuild the
            # vlan mapping if it is the first time called in loop
            # and ovs_restarted not happened
            # to avoid change the flows for it.
            self.rebuild_local_vlan_mapping(_d_details, ovs_restarted)
        except Exception as e:
            raise DeviceListRetrievalError(devices=devices, error=e)
        for details in _d_details:
            device = details['device']
            LOG.debug("Processing port: %s", device)
            port = self.int_br.get_vif_port_by_id(device)
            if not port:
                # The port disappeared and cannot be processed
                LOG.info(_("Port %s was not found on the integration bridge "
                           "and will therefore not be processed"), device)
                skipped_devices.append(device)
                continue

            if 'port_id' in details:
                LOG.info(_("Port %(device)s updated. Details: %(details)s"),
                         {'device': device, 'details': details})
                self.treat_vif_port(port, details['port_id'],
                                    details['network_id'],
                                    details['network_type'],
                                    details['physical_network'],
                                    details['segmentation_id'],
                                    details['admin_state_up'],
                                    details['fixed_ips'],
                                    details['device_owner'],
                                    ovs_restarted,
                                    details['profile'])
                # update plugin about port status
                # FIXME(salv-orlando): Failures while updating device status
                # must be handled appropriately. Otherwise this might prevent
                # neutron server from sending network-vif-* events to the nova
                # API server, thus possibly preventing instance spawn.
                if details.get('admin_state_up'):
                    LOG.debug(_("Setting status for %s to UP"), device)
                    self.plugin_rpc.update_device_up(
                        self.context, device, self.agent_id, cfg.CONF.host)
                else:
                    LOG.debug(_("Setting status for %s to DOWN"), device)
                    self.plugin_rpc.update_device_down(
                        self.context, device, self.agent_id, cfg.CONF.host)
                LOG.info(_("Configuration for device %s completed."), device)
            else:
                LOG.warn(_("Device %s not defined on plugin"), device)
                if (port and port.ofport != -1):
                    self.port_dead(port)
        return skipped_devices

    def treat_ancillary_devices_added(self, devices):
        try:
            devices_details_list = self.plugin_rpc.get_devices_details_list(
                self.context,
                devices,
                self.agent_id,
                cfg.CONF.host)
        except Exception as e:
            raise DeviceListRetrievalError(devices=devices, error=e)

        for details in devices_details_list:
            device = details['device']
            LOG.info(_("Ancillary Port %s added"), device)

            # update plugin about port status
            self.plugin_rpc.update_device_up(self.context,
                                             device,
                                             self.agent_id,
                                             cfg.CONF.host)

    def treat_devices_skipped(self, devices):
        LOG.info("Device skipping number : %s", len(devices))
        self.sg_agent.remove_devices_filter(devices)
        for device in devices:
            try:
                LOG.info("Port %s was not found on the"
                         " integration bridge and"
                         " update_device_down called", device)
                self.plugin_rpc.update_device_down(self.context,
                    device, self.agent_id,
                    cfg.CONF.host)
            except Exception as e:
                LOG.info(_("port treat skipped failed for %(device)s: %(e)s"),
                          {'device': device, 'e': e})
                continue

    def treat_devices_removed(self, devices):
        resync = False
        self.sg_agent.remove_devices_filter(devices)
        for device in devices:
            LOG.info(_("Attachment %s removed"), device)
            try:
                self.plugin_rpc.update_device_down(self.context,
                                                   device,
                                                   self.agent_id,
                                                   cfg.CONF.host)
            except Exception as e:
                LOG.debug(_("port_removed failed for %(device)s: %(e)s"),
                          {'device': device, 'e': e})
                resync = True
                continue
            self.port_unbound(device)
        return resync

    def treat_ancillary_devices_removed(self, devices):
        resync = False
        for device in devices:
            LOG.info(_("Attachment %s removed"), device)
            try:
                details = self.plugin_rpc.update_device_down(self.context,
                                                             device,
                                                             self.agent_id,
                                                             cfg.CONF.host)
            except Exception as e:
                LOG.debug(_("port_removed failed for %(device)s: %(e)s"),
                          {'device': device, 'e': e})
                resync = True
                continue
            if details['exists']:
                LOG.info(_("Port %s updated."), device)
                # Nothing to do regarding local networking
            else:
                LOG.debug(_("Device %s not defined on plugin"), device)
        return resync

    def process_network_ports(self, port_info, ovs_restarted):
        resync_a = False
        resync_b = False
        # VIF wiring needs to be performed always for 'new' devices.
        # For updated ports, re-wiring is not needed in most cases, but needs
        # to be performed anyway when the admin state of a device is changed.
        # A device might be both in the 'added' and 'updated'
        # list at the same time; avoid processing it twice.
        devices_added_updated = (port_info.get('added', set()) |
                                 port_info.get('updated', set()))
        # TODO(salv-orlando): consider a solution for ensuring notifications
        # are processed exactly in the same order in which they were
        # received. This is tricky because there are two notification
        # sources: the neutron server, and the ovs db monitor process
        # If there is an exception while processing security groups ports
        # will not be wired anyway, and a resync will be triggered
        # TODO(salv-orlando): Optimize avoiding applying filters unnecessarily
        # (eg: when there are no IP address changes)
        try:
            _d_details = self.plugin_rpc.get_devices_details_list(
                self.context,
                devices_added_updated,
                self.agent_id,
                cfg.CONF.host)
        except Exception as e:
            raise DeviceListRetrievalError(devices=devices_added_updated,
                                           error=e)
        device_port_dict = {}
        for d_detail in _d_details:
            device_port_dict[d_detail['device']] = d_detail

        self.sg_agent.setup_port_filters(port_info.get('added', set()),
                                         port_info.get('updated', set()),
                                         device_port_dict)

        if devices_added_updated:
            start = time.time()
            try:
                skipped_devices = self.treat_devices_added_or_updated(
                    devices_added_updated, ovs_restarted)
                LOG.debug(_("process_network_ports - iteration:%(iter_num)d -"
                            "treat_devices_added_or_updated completed. "
                            "Skipped %(num_skipped)d devices of "
                            "%(num_current)d devices currently available. "
                            "Time elapsed: %(elapsed).3f"),
                          {'iter_num': self.iter_num,
                           'num_skipped': len(skipped_devices),
                           'num_current': len(port_info['current']),
                           'elapsed': time.time() - start})
                # Update the list of current ports storing only those which
                # have been actually processed.
                port_info['current'] = (port_info['current'] -
                                        set(skipped_devices))

                # Delete the security group rules for skipped devices
                # And update status to down for it
                self.treat_devices_skipped(skipped_devices)

            except DeviceListRetrievalError:
                # Need to resync as there was an error with server
                # communication.
                LOG.exception(_("process_network_ports - iteration:%d - "
                                "failure while retrieving port details "
                                "from server"), self.iter_num)
                resync_a = True
        if 'removed' in port_info:
            start = time.time()
            resync_b = self.treat_devices_removed(port_info['removed'])
            LOG.debug(_("process_network_ports - iteration:%(iter_num)d -"
                        "treat_devices_removed completed in %(elapsed).3f"),
                      {'iter_num': self.iter_num,
                       'elapsed': time.time() - start})
        # If one of the above operations fails => resync with plugin
        return (resync_a | resync_b)

    def rebuild_local_vlan_mapping(self, details, ovs_restarted):

        # If we restart ovs-db,we must re-provision the local vlan mapping
        # If we only restart the neutron-openvswitch-agent we not need re-provision
        # the local vlan mapping but rebuild vlan for the exist ports
        # when it is the first time after __init__
        # So that the neutron-openvswitch-agent restart will not be
        # awared by the end-user

        if (self.iter_num != 0 or ovs_restarted
            or cfg.CONF.USTACK.reset_ovs):
            LOG.debug("Processing rebuild_local_vlan_mapping will not be applied.")
            return

        for detail in details:
            device = detail['device']
            LOG.debug("Processing rebuild_local_vlan_mapping for port: %s", device)
            port = self.int_br.get_vif_port_by_id(device)
            if not port:
                # The port disappeared and cannot be processed
                LOG.info(_("Port %s was not found on the integration bridge "
                           "and will therefore not be processed by"
                           " rebuild_local_vlan_mapping"), device)
                continue
            if 'network_id' not in detail:
                # The port network_id not correct and cannot be processed
                LOG.error(_("network_id not configured for port %s "
                           "and will therefore not be processed by"
                           " rebuild_local_vlan_mapping"), device)
                continue
            network_id = detail['network_id']
            if not network_id:
               # The port network_id not correct and cannot be processed
                LOG.error(_("network_id not configured for port %s "
                           "and will therefore not be processed by"
                           " rebuild_local_vlan_mapping"), device)
                continue
            cur_tag = self.int_br.db_get_val("Port", port.port_name, "tag")
            if cur_tag == DEAD_VLAN_TAG:
                LOG.info(_("cur_tag is DEAD_VLAN_TAG for port %s "
                           "on the integration bridge "
                           "and will therefore not be processed"
                           " by rebuild_local_vlan_mapping"), device)
                continue
            try:
                cur_vlan = int(cur_tag)
            except ValueError:
                LOG.info("cur tag is %s, not need rebuild_local_vlan_mapping", cur_tag)
                continue
            self.local_vlan_map[network_id] = LocalVLANMapping(int(cur_tag),
                                                             detail['network_type'],
                                                             detail['physical_network'],
                                                             detail['segmentation_id'])
            LOG.info(_("cur_tag is %(vlan)s for port %(device)s"
                        " of sg_id %(segmentation_id)s"
                           " on the integration bridge "
                           " by rebuild_local_vlan_mapping"),
                        {'vlan':cur_tag,'device':device,
                         'segmentation_id' : detail['segmentation_id']})

            LOG.info(_("rebuild_local_vlan_mapping info network %(net_id)s,sg_id %(sg)s,vlan %(vlan)s"),
                     {'net_id':network_id,'sg':detail['segmentation_id'],'vlan':cur_tag})

            if int(cur_tag) in self.available_local_vlans:
                self.available_local_vlans.remove(int(cur_tag))

    def process_ancillary_network_ports(self, port_info):
        resync_a = False
        resync_b = False
        if 'added' in port_info:
            start = time.time()
            try:
                self.treat_ancillary_devices_added(port_info['added'])
                LOG.debug(_("process_ancillary_network_ports - iteration: "
                            "%(iter_num)d - treat_ancillary_devices_added "
                            "completed in %(elapsed).3f"),
                        {'iter_num': self.iter_num,
                        'elapsed': time.time() - start})
            except DeviceListRetrievalError:
                # Need to resync as there was an error with server
                # communication.
                LOG.exception(_("process_ancillary_network_ports - "
                                "iteration:%d - failure while retrieving "
                                "port details from server"), self.iter_num)
                resync_a = True
        if 'removed' in port_info:
            start = time.time()
            resync_b = self.treat_ancillary_devices_removed(
                port_info['removed'])
            LOG.debug(_("process_ancillary_network_ports - iteration: "
                        "%(iter_num)d - treat_ancillary_devices_removed "
                        "completed in %(elapsed).3f"),
                      {'iter_num': self.iter_num,
                       'elapsed': time.time() - start})

        # If one of the above operations fails => resync with plugin
        return (resync_a | resync_b)

    def get_ip_in_hex(self, ip_address):
        try:
            return '%08x' % netaddr.IPAddress(ip_address, version=4)
        except Exception:
            LOG.warn(_("Unable to create tunnel port. Invalid remote IP: %s"),
                     ip_address)
            return

    def tunnel_sync(self):
        try:
            for tunnel_type in self.tunnel_types:
                details = self.plugin_rpc.tunnel_sync(self.context,
                                                      self.local_ip,
                                                      tunnel_type)
                if not self.l2_pop:
                    tunnels = details['tunnels']
                    for tunnel in tunnels:
                        if self.local_ip != tunnel['ip_address']:
                            tunnel_id = tunnel.get('id')
                            # Unlike the OVS plugin, ML2 doesn't return an id
                            # key. So use ip_address to form port name instead.
                            # Port name must be <=15 chars, so use shorter hex.
                            remote_ip = tunnel['ip_address']
                            remote_ip_hex = self.get_ip_in_hex(remote_ip)
                            if not tunnel_id and not remote_ip_hex:
                                continue
                            tun_name = '%s-%s' % (tunnel_type,
                                                  tunnel_id or remote_ip_hex)
                            self._setup_tunnel_port(self.tun_br,
                                                    tun_name,
                                                    tunnel['ip_address'],
                                                    tunnel_type)
        except Exception as e:
            LOG.debug(_("Unable to sync tunnel IP %(local_ip)s: %(e)s"),
                      {'local_ip': self.local_ip, 'e': e})
            return True
        return False

    def _agent_has_updates(self, polling_manager):
        return (polling_manager.is_polling_required or
                self.updated_ports or
                self.sg_agent.firewall_refresh_needed())

    def _port_info_has_changes(self, port_info):
        return (port_info.get('added') or
                port_info.get('removed') or
                port_info.get('updated'))

    def check_ovs_restart(self):
        # Check for the canary flow
        canary_flow = self.int_br.dump_flows_for_table(constants.CANARY_TABLE)
        return not canary_flow

    def rpc_loop(self, polling_manager=None):
        if not polling_manager:
            polling_manager = polling.AlwaysPoll()

        sync = True
        ports = set()
        updated_ports_copy = set()
        ancillary_ports = set()
        tunnel_sync = True
        ovs_restarted = False
        while self.run_daemon_loop:
            start = time.time()
            port_stats = {'regular': {'added': 0,
                                      'updated': 0,
                                      'removed': 0},
                          'ancillary': {'added': 0,
                                        'removed': 0}}
            LOG.debug(_("Agent rpc_loop - iteration:%d started"),
                      self.iter_num)
            if sync:
                LOG.info(_("Agent out of sync with plugin!"))
                ports.clear()
                ancillary_ports.clear()
                sync = False
                polling_manager.force_polling()
            ovs_restarted = self.check_ovs_restart()
            if ovs_restarted:
                self.setup_integration_br(ovs_restarted)
                self.setup_physical_bridges(self.bridge_mappings, ovs_restarted)
                if self.enable_tunneling:
                    self.setup_tunnel_br(ovs_restarted)
                    tunnel_sync = True
                self.dvr_agent.reset_ovs_parameters(self.int_br,
                                                    self.tun_br,
                                                    self.patch_int_ofport,
                                                    self.patch_tun_ofport)
                self.dvr_agent.setup_dvr_flows_on_integ_tun_br(ovs_restarted)
            # Notify the plugin of tunnel IP
            if self.enable_tunneling and tunnel_sync:
                LOG.info(_("Agent tunnel out of sync with plugin!"))
                try:
                    tunnel_sync = self.tunnel_sync()
                except Exception:
                    LOG.exception(_("Error while synchronizing tunnels"))
                    tunnel_sync = True
            if self._agent_has_updates(polling_manager) or ovs_restarted:
                try:
                    LOG.debug(_("Agent rpc_loop - iteration:%(iter_num)d - "
                                "starting polling. Elapsed:%(elapsed).3f"),
                              {'iter_num': self.iter_num,
                               'elapsed': time.time() - start})
                    # Save updated ports dict to perform rollback in
                    # case resync would be needed, and then clear
                    # self.updated_ports. As the greenthread should not yield
                    # between these two statements, this will be thread-safe
                    updated_ports_copy = self.updated_ports
                    self.updated_ports = set()
                    reg_ports = (set() if ovs_restarted else ports)
                    port_info = self.scan_ports(reg_ports, updated_ports_copy)
                    LOG.debug(_("Agent rpc_loop - iteration:%(iter_num)d - "
                                "port information retrieved. "
                                "Elapsed:%(elapsed).3f"),
                              {'iter_num': self.iter_num,
                               'elapsed': time.time() - start})
                    # Secure and wire/unwire VIFs and update their status
                    # on Neutron server
                    if (self._port_info_has_changes(port_info) or
                        self.sg_agent.firewall_refresh_needed() or
                        ovs_restarted):
                        LOG.debug(_("Starting to process devices in:%s"),
                                  port_info)
                        # If treat devices fails - must resync with plugin
                        sync = self.process_network_ports(port_info,
                                                          ovs_restarted)
                        LOG.debug(_("Agent rpc_loop - iteration:%(iter_num)d -"
                                    "ports processed. Elapsed:%(elapsed).3f"),
                                  {'iter_num': self.iter_num,
                                   'elapsed': time.time() - start})
                        port_stats['regular']['added'] = (
                            len(port_info.get('added', [])))
                        port_stats['regular']['updated'] = (
                            len(port_info.get('updated', [])))
                        port_stats['regular']['removed'] = (
                            len(port_info.get('removed', [])))
                    ports = port_info['current']
                    # Treat ancillary devices if they exist
                    if self.ancillary_brs:
                        port_info = self.update_ancillary_ports(
                            ancillary_ports)
                        LOG.debug(_("Agent rpc_loop - iteration:%(iter_num)d -"
                                    "ancillary port info retrieved. "
                                    "Elapsed:%(elapsed).3f"),
                                  {'iter_num': self.iter_num,
                                   'elapsed': time.time() - start})

                        if port_info:
                            rc = self.process_ancillary_network_ports(
                                port_info)
                            LOG.debug(_("Agent rpc_loop - iteration:"
                                        "%(iter_num)d - ancillary ports "
                                        "processed. Elapsed:%(elapsed).3f"),
                                      {'iter_num': self.iter_num,
                                       'elapsed': time.time() - start})
                            ancillary_ports = port_info['current']
                            port_stats['ancillary']['added'] = (
                                len(port_info.get('added', [])))
                            port_stats['ancillary']['removed'] = (
                                len(port_info.get('removed', [])))
                            sync = sync | rc

                    polling_manager.polling_completed()
                except Exception:
                    LOG.exception(_("Error while processing VIF ports"))
                    # Put the ports back in self.updated_port
                    self.updated_ports |= updated_ports_copy
                    sync = True

            # sleep till end of polling interval
            elapsed = (time.time() - start)
            LOG.debug(_("Agent rpc_loop - iteration:%(iter_num)d "
                        "completed. Processed ports statistics: "
                        "%(port_stats)s. Elapsed:%(elapsed).3f"),
                      {'iter_num': self.iter_num,
                       'port_stats': port_stats,
                       'elapsed': elapsed})
            if (elapsed < self.polling_interval):
                time.sleep(self.polling_interval - elapsed)
            else:
                LOG.debug(_("Loop iteration exceeded interval "
                            "(%(polling_interval)s vs. %(elapsed)s)!"),
                          {'polling_interval': self.polling_interval,
                           'elapsed': elapsed})
            self.iter_num = self.iter_num + 1

    def daemon_loop(self):
        with polling.get_polling_manager(
            self.minimize_polling,
            self.root_helper,
            self.ovsdb_monitor_respawn_interval) as pm:

            self.rpc_loop(polling_manager=pm)

    def _handle_sigterm(self, signum, frame):
        LOG.debug("Agent caught SIGTERM, quitting daemon loop.")
        self.run_daemon_loop = False


def create_agent_config_map(config):
    """Create a map of agent config parameters.

    :param config: an instance of cfg.CONF
    :returns: a map of agent configuration parameters
    """
    try:
        bridge_mappings = q_utils.parse_mappings(config.OVS.bridge_mappings)
    except ValueError as e:
        raise ValueError(_("Parsing bridge_mappings failed: %s.") % e)

    kwargs = dict(
        integ_br=config.OVS.integration_bridge,
        tun_br=config.OVS.tunnel_bridge,
        local_ip=config.OVS.local_ip,
        bridge_mappings=bridge_mappings,
        root_helper=config.AGENT.root_helper,
        polling_interval=config.AGENT.polling_interval,
        minimize_polling=config.AGENT.minimize_polling,
        tunnel_types=config.AGENT.tunnel_types,
        veth_mtu=config.AGENT.veth_mtu,
        enable_distributed_routing=config.AGENT.enable_distributed_routing,
        l2_population=config.AGENT.l2_population,
        arp_responder=config.AGENT.arp_responder,
        use_veth_interconnection=config.OVS.use_veth_interconnection,
    )

    # If enable_tunneling is TRUE, set tunnel_type to default to GRE
    if config.OVS.enable_tunneling and not kwargs['tunnel_types']:
        kwargs['tunnel_types'] = [p_const.TYPE_GRE]

    # Verify the tunnel_types specified are valid
    for tun in kwargs['tunnel_types']:
        if tun not in constants.TUNNEL_NETWORK_TYPES:
            msg = _('Invalid tunnel type specified: %s'), tun
            raise ValueError(msg)
        if not kwargs['local_ip']:
            msg = _('Tunneling cannot be enabled without a valid local_ip.')
            raise ValueError(msg)

    return kwargs


def main():
    cfg.CONF.register_opts(ip_lib.OPTS)
    common_config.init(sys.argv[1:])
    common_config.setup_logging(cfg.CONF)
    q_utils.log_opt_values(LOG)

    try:
        agent_config = create_agent_config_map(cfg.CONF)
    except ValueError as e:
        LOG.error(_('%s Agent terminated!'), e)
        sys.exit(1)

    is_xen_compute_host = 'rootwrap-xen-dom0' in agent_config['root_helper']
    if is_xen_compute_host:
        # Force ip_lib to always use the root helper to ensure that ip
        # commands target xen dom0 rather than domU.
        cfg.CONF.set_default('ip_lib_force_root', True)

    agent = OVSNeutronAgent(**agent_config)
    signal.signal(signal.SIGTERM, agent._handle_sigterm)

    # Start everything.
    LOG.info(_("Agent initialized successfully, now running... "))
    agent.daemon_loop()


if __name__ == "__main__":
    main()
