# Copyright 2013, Nachi Ueno, NTT I3, Inc.
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
from oslo.config import cfg

from neutron.agent import l3_agent
from neutron.extensions import vpnaas
from neutron.openstack.common import importutils
from neutron.openstack.common import loopingcall
from neutron.openstack.common import periodic_task


vpn_agent_opts = [
    cfg.MultiStrOpt(
        'vpn_device_driver',
        default=['neutron.services.vpn.device_drivers.'
                 'ipsec.OpenSwanDriver'],
        help=_("The vpn device drivers Neutron will use")),
]
cfg.CONF.register_opts(vpn_agent_opts, 'vpnagent')


class VPNAgent(l3_agent.L3NATAgentWithStateReport):
    """VPNAgent class which can handle vpn service drivers."""
    def __init__(self, host, conf=None):
        super(VPNAgent, self).__init__(host=host, conf=conf)
        self.setup_device_drivers(host)
        self.sync_vpnservices(self.context)
        self.rpc_loop = loopingcall.FixedIntervalLoopingCall(
                         self._rpc_loop_nosync)
        self.rpc_loop.start(interval=l3_agent.RPC_LOOP_INTERVAL)

    def setup_device_drivers(self, host):
        """Setting up device drivers.

        :param host: hostname. This is needed for rpc
        Each devices will stays as processes.
        They will communicate with
        server side service plugin using rpc with
        device specific rpc topic.
        :returns: None
        """
        device_drivers = cfg.CONF.vpnagent.vpn_device_driver
        self.devices = []
        for device_driver in device_drivers:
            try:
                self.devices.append(
                    importutils.import_object(device_driver, self, host))
            except ImportError:
                raise vpnaas.DeviceDriverImportError(
                    device_driver=device_driver)

    def get_namespace(self, router_id):
        """Get namespace of router.

        :router_id: router_id
        :returns: namespace string.
            Note if the router is not exist, this function
            returns None
        """
        router_info = self.router_info.get(router_id)
        if not router_info:
            return
        return router_info.ns_name

    def add_nat_rule(self, router_id, chain, rule, top=False):
        """Add nat rule in namespace.

        :param router_id: router_id
        :param chain: a string of chain name
        :param rule: a string of rule
        :param top: if top is true, the rule
            will be placed on the top of chain
            Note if there is no rotuer, this method do nothing
        """
        router_info = self.router_info.get(router_id)
        if not router_info:
            return
        router_info.iptables_manager.ipv4['nat'].add_rule(
            chain, rule, top=top)

    def remove_nat_rule(self, router_id, chain, rule, top=False):
        """Remove nat rule in namespace.

        :param router_id: router_id
        :param chain: a string of chain name
        :param rule: a string of rule
        :param top: unused
            needed to have same argument with add_nat_rule
        """
        router_info = self.router_info.get(router_id)
        if not router_info:
            return
        router_info.iptables_manager.ipv4['nat'].remove_rule(
            chain, rule, top=top)

    def iptables_apply(self, router_id):
        """Apply IPtables.

        :param router_id: router_id
        This method do nothing if there is no router
        """
        router_info = self.router_info.get(router_id)
        if not router_info:
            return
        router_info.iptables_manager.apply()

    def _router_added(self, router_id, router):
        """Router added event.

        This method overwrites parent class method.
        :param router_id: id of added router
        :param router: dict of rotuer
        """
        super(VPNAgent, self)._router_added(router_id, router)
        for device in self.devices:
            if getattr(device, 'create_router'):
                device.create_router(router_id)

    def _router_removed(self, router_id):
        """Router removed event.

        This method overwrites parent class method.
        :param router_id: id of removed router
        """
        super(VPNAgent, self)._router_removed(router_id)
        for device in self.devices:
            if getattr(device, 'destroy_router'):
                device.destroy_router(router_id)

    def _process_routers(self, routers, all_routers=False):
        """Router sync event.

        This method overwrites parent class method.
        :param routers: list of routers
        """
        super(VPNAgent, self)._process_routers(routers, all_routers)
        for device in self.devices:
            if getattr(device, 'sync'):
                device.sync(self.context, routers)

    @periodic_task.periodic_task(run_immediately=True)
    def _sync_routers_task_nosync(self, context):
        """Router sync event.

        This method overwrites parent class method.
        :param routers: list of routers
        """
        fullsync = self.fullsync
        super(VPNAgent, self)._sync_routers_task(context)
        if not fullsync or self.fullsync:
            return
        for device in self.devices:
            if getattr(device, '_sync_vpnservices_task_nosync'):
                device._sync_vpnservices_task_nosync(self.context)

    @periodic_task.periodic_task(space=100)
    def sync_vpnservices(self, context):
        for device in self.devices:
            if getattr(device, '_sync_vpnservices'):
                device._sync_vpnservices()

    def _destroy_router_namespace(self, namespace):
        for device in self.devices:
            if getattr(device, '_destroy_router_namespace'):
                device._destroy_router_namespace(namespace)
        super(VPNAgent, self)._destroy_router_namespace(namespace)

    def _rpc_loop_nosync(self):
        if self.fullsync:
            # we need to do router sync, so don't go further
            return
        for device in self.devices:
            if getattr(device, '_rpc_loop_nosync'):
                device._rpc_loop_nosync()


def main():
    l3_agent.main(
        manager='neutron.services.vpn.agent.VPNAgent')
