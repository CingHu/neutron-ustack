import logging
import random
from oslo.config import cfg

from neutron.agent.common import config
from neutron.agent.linux import ip_lib
from neutron.agent.linux import ovs_lib
from neutron.services.vm.common import constants as s_constants

LOG = logging.getLogger(__name__)

TAP_INTERFACE_PREFIX = s_constants.TAP_INTERFACE_PREFIX
EXTERNAL_BRIDGE = s_constants.EXTERNAL_BRIDGE

class QosDriver():
    """Qos Driver for ServiceVM
    
       1. add qdisc
       2. add class   
       3. add tap filter
       4. add eth filter
    """
    def __init__(self):
        self.conf = cfg.CONF
        self.root_helper = config.get_root_helper(self.conf)
        self.external_device = self.conf.servicevm_agent.external_device
        self.ip_wrapper = ip_lib.IPWrapper(self.root_helper)
        self.external_br = ovs_lib.OVSBridge(EXTERNAL_BRIDGE, self.root_helper)
        self.clear_floatingip_qos()
        self.available_classes = set(xrange(11, 65530))
        self.available_prio = set(xrange(11, 65530))
        self.tc_fip_mapping = {}

    def get_tap_device_name(self, interface_id):
        if not interface_id:
            LOG.warning(_("Invalid Interface ID, will lead to incorrect "
                          "tap device name"))
        tap_device_name = TAP_INTERFACE_PREFIX + interface_id[0:11]
        return tap_device_name


    def _add_root_qdisc(self, interface_name):
        LOG.info("Add qdisc stuff for device %s", interface_name)
        if not interface_name.startswith('tap'):
            LOG.error("Wrong tap device %(interface_name)s,"
                      " this tap device should be start with tap-xxx!!",
                      {'interface_name': interface_name})
            return
        device_name = interface_name
        tc_cmd = ['tc', 'qdisc', 'add', 'dev', device_name, 'root',
                  'handle', '1:', 'htb', 'default', 'a']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

    def _add_class_filter_for_device(self, interface_name,
                                           rate_limit, ip_address):
        LOG.info("Add class and filter for internal and external device %s",
                interface_name)

        if not interface_name.startswith('tap'):
            LOG.error("Wrong tap device %(interface_name)s,"
                      " this tap device should be start with tap-xxx!!",
                      {'interface_name': interface_name})
            return
        rate = rate_limit
        classid = self.available_classes.pop()
        prio = self.available_prio.pop()

        try:
            # Add ingress rules
            tc_cmd = ['tc', 'class', 'add', 'dev', '%s' % interface_name,
                      'parent', '1:','classid', '1:%x' % classid, 'htb', 'rate',
                      '%skbit' % rate, 'ceil', '%skbit' % rate,
                      'burst', '100kbit', 'cburst', '100kbit', 'prio', '10']
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

            ip = ip_address + '/32'
            tc_cmd = ['tc', 'filter', 'add', 'dev', '%s' % interface_name,
                      'parent', '1:', 'protocol', 'ip', 'prio',
                      '%s' % prio, 'u32',
                      'match', 'ip', 'dst', '%s' % ip, 'flowid', '1:%x' % classid]
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

            LOG.info("Add external class and filter for device %s"
                     "and ip_address %s", self.external_device, ip_address)

            # Add engress rules
            tc_cmd = ['tc', 'class', 'add', 'dev', '%s' % self.external_device,
                      'parent', '1:','classid', '1:%x' % classid, 'htb', 'rate',
                      '%skbit' % rate, 'ceil', '%skbit' % rate,
                      'burst', '100kbit', 'cburst', '100kbit', 'prio', '10']
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

            ip = ip_address + '/32'
            tc_cmd = ['tc', 'filter', 'add', 'dev', '%s' % self.external_device,
                      'parent', '1:', 'protocol', 'ip', 'prio',
                      '%s' % prio, 'u32',
                      'match', 'ip', 'src', '%s' % ip, 'flowid', '1:%x' % classid]
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)
    
            # Record classid and prio for fip
            self.tc_fip_mapping[ip_address] = {}
            self.tc_fip_mapping[ip_address]['classid'] = classid
            self.tc_fip_mapping[ip_address]['prio'] = prio
        except Exception as e:
            LOG.error("Error occurs when add tc for device %s"
                      "Details: %s", interface_name, e)

    def _del_class_filter_for_device(self, interface_name, ip_address):
        LOG.info("Delete class and filter for device %s", interface_name)
        if not interface_name.startswith('tap'):
            LOG.error("Wrong tap device %(interface_name)s,"
                      " this tap device should be start with tap-xxx!!",
                      {'interface_name': interface_name})
            return

        try:
            # Delete internal class for device
            tc_cmd = ['tc', 'class', 'del', 'dev', '%s' % interface_name,
                      'classid', '1:%x' % self.tc_fip_mapping[ip_address]['classid']]
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

            # Delete internal filter for device
            tc_cmd = ['tc', 'filter', 'del', 'dev', '%s' % interface_name,
                      'parent', '1:', 'protocol', 'ip', 'prio',
                      '%s' % self.tc_fip_mapping[ip_address]['prio']]
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

            # Delete external class for device
            tc_cmd = ['tc', 'class', 'del', 'dev', '%s' % self.external_device,
                      'classid', '1:%x' % self.tc_fip_mapping[ip_address]['classid']]
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

            # Delete external filter for device
            tc_cmd = ['tc', 'filter', 'del', 'dev', '%s' % self.external_device,
                      'parent', '1:', 'protocol', 'ip', 'prio',
                      '%s' % self.tc_fip_mapping[ip_address]['prio']]
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)
        except Exception as e:
            LOG.error("Error occurs when delete tc for device %s"
                      "Details: %s", interface_name, e)

    def add_floatingip_qos(self, ip_address, rate_limit, if_id):
        if_name = self.get_tap_device_name(if_id)
        self._add_root_qdisc(if_name)
        self._add_class_filter_for_device(if_name, rate_limit, ip_address)

    def del_floatingip_qos(self, ip_address, if_id):
        if_name = self.get_tap_device_name(if_id)
        self._del_class_filter_for_device(if_name, ip_address)

    def _get_tap_port_name_list(self):
        tap_names = self.external_br.get_port_name_list()
        return [name for name in tap_names \
                 if name.startswith(TAP_INTERFACE_PREFIX)]

    def clear_floatingip_qos(self):
        # Delete root qdisc
        tap_names = self._get_tap_port_name_list()
        tap_names.append(self.external_device)
        for if_name in tap_names:
            tc_cmd = ['tc', 'qdisc', 'del', 'dev', if_name, 'root']
            self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)
