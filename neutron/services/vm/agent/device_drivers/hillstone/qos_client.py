import logging
from oslo.config import cfg

from neutron.agent.common import config
from neutron.agent.linux import ip_lib

LOG = logging.getLogger(__name__)

class QosClient():
    """Qos Client for ServiceVM
    
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

    def add_root_qdisc(self, interface_name):
        LOG.info("Add qdisc stuff for device %s", interface_name)
        if interface_name.startswith('tap'):
            LOG.error("Wrong tap device, this tap device should be start
                       with tap-xxx!!")
            return
        device_name = interface_name
        tc_cmd = ['tc', 'qdisc', 'add', 'dev', device_name, 'root',
                  'handle', '1:', 'htb', 'default', 'a']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

    def add_class(self, interface_name, rate_limit):
        LOG.info("Add class stuff for device %s and ratelimit %s",
                 interface_name, rate_limit)
        if interface_name.startswith('tap'):
            LOG.error("Wrong tap device, this tap device should be start
                       with tap-xxx!!")
            return
        rate = rate_limit
        tc_cmd = ['tc', 'class', 'add', 'dev', '%s' % interface_name,
                  'parent', '1:','classid', '1:c', 'htb', 'rate',
                  '%skbit' % rate, 'ceil', '%skbit' % rate,
                  'burst', '100kbit', 'cburst', '100kbit', 'prio', '10']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

    def add_tap_filter(self, interface_name, ip_address):
        if interface_name.startswith('tap'):
            LOG.error("Wrong tap device, this tap device should be start
                       with tap-xxx!!")
            return
        LOG.info("Add tap filter stuff for device %s and ip_address %s",
                 interface_name, ip_address)
        ip = ip_address + '/32'
        tc_cmd = ['tc', 'filter', 'add', 'dev', '%s' % interface_name,
                  'parent', '1:', 'protocol', 'ip', 'prio', '12', 'u32',
                  'match', 'ip', 'dst', '%s' % ip, 'flowid', '1:c']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

    def add_external_filter(self, ip_address):
        LOG.info("Add external filter stuff for device %s and ip_address %s",
                 self.external_device, ip_address)
        ip = ip_address + '/32'
        tc_cmd = ['tc', 'filter', 'add', 'dev', '%s' % self.external_device,
                  'parent', '1:', 'protocol', 'ip', 'prio', '12', 'u32',
                  'match', 'ip', 'src', '%s' % ip, 'flowid', '1:c']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

    def del_class(self, interface_name):
        LOG.info("Delete class for device %s", interface_name)
        if interface_name.startswith('tap'):
            LOG.error("Wrong tap device, this tap device should be start
                       with tap-xxx!!")
            return
        tc_cmd = ['tc', 'class', 'del', 'dev', '%s' % interface_name,
                  'classid', '1:c']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)

    def del_filter(self, interface_name):
        LOG.info("Delete filter for device %s", interface_name)
        if interface_name.startswith('tap'):
            LOG.error("Wrong tap device, this tap device should be start
                       with tap-xxx!!")
            return
        tc_cmd = ['tc', 'filter', 'del', 'dev', '%s' % interface_name,
                  'parent', '1:', 'protocol', 'ip', 'prio', '12']
        self.ip_wrapper.netns.execute(tc_cmd, check_exit_code=False)
