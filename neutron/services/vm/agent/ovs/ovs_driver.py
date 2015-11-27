import hashlib
import signal
import sys
import time


import netaddr
from oslo.config import cfg
from six import moves

from neutron.agent.common import config as cconfig
from neutron.agent.linux import ip_lib
from neutron.agent.linux import ovs_lib
from neutron.agent.linux import polling
from neutron.agent.linux import utils
from neutron.common import config as common_config
from neutron.common import constants as q_const
from neutron.common import exceptions
from neutron.common import uos_constants
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import utils as q_utils
from neutron import context
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.plugins.common import constants as p_const
from neutron.plugins.openvswitch.common import config  # noqa
from neutron.plugins.openvswitch.common import constants
from neutron.services.vm.common import constants as s_constants

LOG = logging.getLogger(__name__)


ARP_TABLE = s_constants.ARP_TABLE
FIP_TABLE = s_constants.FIP_TABLE
LOCAL_TABLE = s_constants.LOCAL_TABLE

EXTERNAL_BRIDGE = s_constants.EXTERNAL_BRIDGE
TAP_INTERFACE_PREFIX = s_constants.TAP_INTERFACE_PREFIX

class DeviceListRetrievalError(exceptions.NeutronException):
    message = _("Unable to retrieve port details for devices: %(devices)s "
                "because of error: %(error)s")

class OVSDriver():
    def __init__(self, ):
        self.conf = cfg.CONF
        self.root_helper = cconfig.get_root_helper(self.conf)
        self.external_br = ovs_lib.OVSBridge(EXTERNAL_BRIDGE, self.root_helper)
        self.sync = True
        self.ovs_restarted = False
        self.ip_wrapper = ip_lib.IPWrapper(self.root_helper)

    def _setup_external_bridges(self):
        self.external_br.create()
        self.external_br.set_secure_mode()
        self.external_br.remove_all_flows()

        self.external_br.add_flow(table=0,
                        dl_type='0x0800',
                        nw_dst=uos_constants.UOS_EX_RESERVED_NET,
                        priority=1,
                        actions='resubmit(,%s)' %
                        uos_constants.UOS_EX_RESERVED_NET_TABLE)
        self.external_br.add_flow(table=0,
                        priority=0,
                        actions='normal')
        self.external_br.add_flow(table=uos_constants.UOS_EX_RESERVED_NET_TABLE,
                                priority=0,
                                actions='drop')

        self.set_arp_protect_table()
        self.set_fip_protect_table()

    def get_tap_device_name(self, interface_id):
        if not interface_id:
            LOG.warning(_("Invalid Interface ID, will lead to incorrect "
                          "tap device name"))
        tap_device_name = TAP_INTERFACE_PREFIX + interface_id[0:11]
        return tap_device_name

    def check_ovs_restart(self):
        # Check for the canary flow
        return cfg.CONF.USTACK.reset_ovs

    def daemon_loop(self):
        with polling.get_polling_manager(
            self.minimize_polling,
            self.root_helper,
            self.ovsdb_monitor_respawn_interval) as pm:
            self.rpc_loop(polling_manager=pm)

    def _agent_has_updates(self, polling_manager):
        return (polling_manager.is_polling_required or
                self.updated_ports)

    def get_tap_port_name_list(self):
        tap_names = self.external_br.get_port_name_list()
        return [name for name in tap_names \
                 if name.startswitch(TAP_INTERFACE_PREFIX)]

    def scan_ports(self, registered_ports, updated_ports=None):
        cur_ports = self.external_br.get_vif_port_set()
        self.external_br_device_count = len(cur_ports)
        port_info = {'current': cur_ports}
        if updated_ports is None:
            updated_ports = set()
        if updated_ports:
            updated_ports &= cur_ports
            if updated_ports:
                port_info['updated'] = updated_ports

        if cur_ports == registered_ports:
            # No added or removed ports to set, just return here
            return port_info

        port_info['added'] = cur_ports - registered_ports
        # Remove all the known ports not found on the external bridge
        port_info['removed'] = registered_ports - cur_ports
        return port_info

    def _port_info_has_changes(self, port_info):
        return (port_info.get('added') or
                port_info.get('removed') or
                port_info.get('updated'))


    def process_network_ports(self, port_info, ovs_restarted):
        resync_a = False
        resync_b = False
        skipped_devices = [] 
        devices_added_updated = (port_info.get('added', set()) |
                                 port_info.get('updated', set()))
        try:
            _d_details = self.plugin_rpc.get_devices_details_list(
                self.context,
                devices_added_updated)
        except Exception as e:
            raise DeviceListRetrievalError(devices=devices_added_updated,
                                           error=e)
        for details in _d_details:
            device = details['device']
            LOG.info("Processing port: %s", device)
            port = self.external_br.get_vif_port_by_id(device)
            if not port:
                # The port disappeared and cannot be processed
                LOG.info(_("Port %s was not found on the external bridge "
                           "and will therefore not be processed"), device)
                skipped_devices.append(device)
                continue

            if 'port_id' in details:
                LOG.info(_("Port %(device)s updated. Details: %(details)s"),
                         {'device': device, 'details': details})

                if details.get('admin_state_up'):
                    LOG.debug(_("Setting status for %s to UP"), device)
                else:
                    LOG.debug(_("Setting status for %s to DOWN"), device)
                LOG.info(_("Configuration for device %s completed."), device)
            else:
                LOG.warn(_("Device %s not defined on plugin"), device)
        if 'removed' in port_info:
            LOG.info('remove port: %s' % port_info['removed'])

    def rpc_loop(self, polling_manager=None):
        if not polling_manager:
            polling_manager = polling.AlwaysPoll()

        start = time.time()
        ovs_restarted = self.check_ovs_restart()
        if ovs_restarted and self.sync:
            self._setup_external_bridges()

        if self.run_daemon_loop:
            if self._agent_has_updates(polling_manager) or ovs_restarted:
                try:
                   LOG.info('ovs rpc loop running') 
                   updated_ports_copy = self.updated_ports
                   self.updated_ports = set()
                   reg_ports = (set() if ovs_restarted else self.ports)
                   port_info = self.scan_ports(reg_ports, updated_ports_copy)
                   if (self._port_info_has_changes(port_info) or ovs_restarted):
                       LOG.debug(_("Starting to process devices in:%s"), port_info)
                       self.process_network_ports(port_info, ovs_restarted)
                       self.ports = port_info['current']
                       polling_manager.polling_completed()
                   self.sync = False
                except Exception:
                    LOG.exception(_("Error while processing VIF ports"))
                    # Put the ports back in self.updated_port
                    self.updated_ports |= updated_ports_copy
                    self.sync = True

    def set_arp_protect_table(self):
        ovs_cmd = ['ovs-ofctl', 'add-flow', EXTERNAL_BRIDGE,
                   'table=%s,priority=1,arp,arp_op=2,actions=drop' % ARP_TABLE]
        self.ip_wrapper.netns.execute(ovs_cmd, check_exit_code=False)

    def set_fip_protect_table(self):
        ovs_cmd = ['ovs-ofctl', 'add-flow', EXTERNAL_BRIDGE,
                   'table=%s,priority=1,actions=drop' % FIP_TABLE]
        self.ip_wrapper.netns.execute(ovs_cmd, check_exit_code=False)

    def get_if_name_ofport(self, if_name):
        ovs_cmd = ['ovs-vsctl', 'get', 'Interface', if_name, 'ofport']
        ofport = self.ip_wrapper.netns.execute(ovs_cmd)
        LOG.info("Get if_name: %s ofport: %s", if_name, ofport)
        if ofport != -1:
            return ofport

    def get_if_name_mac(self, if_name):
        ovs_cmd = ['ovs-vsctl', 'get', 'Interface', if_name, 'mac_in_use']
        ofport = self.ip_wrapper.netns.execute(ovs_cmd)
        LOG.info("Get if_name: %s mac: %s", if_name, ofport)
        if ofport != -1:
            return ofport

    def _set_arp_protect(self, if_name, ip_address):
        ofport = self.get_if_name_ofport(if_name)
        try:
            if ofport:
                ovs_cmd = ['ovs-ofctl', 'add-flow', EXTERNAL_BRIDGE,
                           'table=%s,priority=2,arp,in_port=%s,'
                           'arp_spa=%s,arp_op=2,actions=NORMAL' % (
                           ARP_TABLE, ofport, ip_address)]
                self.ip_wrapper.netns.execute(ovs_cmd)

                ovs_cmd = ['ovs-ofctl', 'add-flow', EXTERNAL_BRIDGE,
                           'table=%s,priority=2,arp,in_port=%s,'
                           'arp_spa=%s,arp_op=2,actions=resubmit(,%s)' % (
                            LOCAL_TABLE, ofport, ip_address, ARP_TABLE)]
                self.ip_wrapper.netns.execute(ovs_cmd)
                LOG.info("Set ARP protect success for if_name: %s", if_name)
        except Exception as e:
            LOG.error("Set ARP protect fail for %s, reason: %s", (if_name, e))

    def _delete_arp_protect(self, if_name, ip_address):
        try:
            ofport = self.get_if_name_ofport(if_name)
            if ofport:
                ovs_cmd = ['ovs-ofctl', 'del-flows', EXTERNAL_BRIDGE,
                           'arp,in_port=%s,arp_spa=%s,arp_op=2' % (
                           ofport, ip_address)]
                self.ip_wrapper.netns.execute(ovs_cmd)
                LOG.info("Delete ARP protect success for if_name: %s", if_name)
        except Exception as e:
            LOG.error("Set ARP protect fail for %s, reason: %s", (if_name, e))

    def _set_fip_protect(self, if_name, ip_address):
        ofmac = self.get_if_name_mac(if_name)
        ofport = self.get_if_name_ofport(if_name)
        try:
            if ofmac and ofport:
                ovs_cmd = ['ovs-ofctl', 'add-flow', EXTERNAL_BRIDGE,
                           'table=%s,priority=2,in_port=%s,dl_src=%s,'
                           'dl_type=0x0800,nw_src=%s,actions=NORMAL' % (
                           FIP_TABLE, ofport, ofmac[1:-2], ip_address)]
                self.ip_wrapper.netns.execute(ovs_cmd)

                ovs_cmd = ['ovs-ofctl', 'add-flow', EXTERNAL_BRIDGE,
                           'table=%s,priority=2,in_port=%s,dl_src=%s,'
                           'dl_type=0x0800,nw_src=%s,actions=resubmit(,%s)' % (
                           LOCAL_TABLE, ofport, ofmac[1:-2], ip_address, FIP_TABLE)]
                self.ip_wrapper.netns.execute(ovs_cmd)
                LOG.info("Set FIP protect successful for if_name: %s", if_name)
        except Exception as e:
            LOG.error("Set FIP protect fail for %s, reason: %s", (if_name, e))

    def _delete_fip_protect(self, if_name, ip_address):
        try:
            ofport = self.get_if_name_ofport(if_name)
            ofmac = self.get_if_name_mac(if_name)
            if ofport and ofmac:
                ovs_cmd = ['ovs-ofctl', 'del-flows', EXTERNAL_BRIDGE,
                           'in_port=%s,dl_src=%s,dl_type=0x0800,nw_src=%s' % (
                            ofport, ofmac[1:-2], ip_address)]
                self.ip_wrapper.netns.execute(ovs_cmd)
                LOG.info("Delete FIP protect successful for if_name: %s", if_name)
        except Exception as e:
            LOG.error("Set FIP protect fail for %s, reason: %s", (if_name, e))

    def add_floatingip_filter(self, port_id, ip_address):
        if_name = self.get_tap_device_name(port_id)
        self._set_fip_protect(if_name, ip_address)
        self._set_arp_protect(if_name, ip_address)

    def del_floatingip_filter(self, port_id, ip_address):
        if_name = self.get_tap_device_name(port_id)
        self._delete_fip_protect(if_name, ip_address)
        self._delete_arp_protect(if_name, ip_address)
