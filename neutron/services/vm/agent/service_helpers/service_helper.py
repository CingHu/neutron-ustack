# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 UnitedStack, Inc.
# All rights reserved.
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
# @author: CingHu, UnitedStack, Inc

import pickle
import eventlet
import netaddr
import os
import json
import jsonpickle
from oslo.config import cfg
from oslo import messaging
from oslo.utils import excutils

from neutron.common import constants as l3_constants
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import utils as common_utils
from neutron.openstack.common import log as logging
from neutron.openstack.common.gettextutils import _LI, _LE, _LW
from neutron import context as n_context
from neutron.plugins.common import constants as s_constants
from neutron.services.vm.agent import config as vm_config
from neutron.services.vm.common import constants as s_constants
from neutron.services.vm.agent import device_status
from neutron.services.vm.agent import driver_mgt
from neutron.services.vm.agent import svmagt_exception as svmt_exception

LOG = logging.getLogger(__name__)

N_ROUTER_PREFIX = 'vr-'


SERVICE_CLASS_INFO_MAP = {
    s_constants.VROUTER: 'RouterInfo'    
}


class RouterInfo(object):
    """Wrapper class around the (neutron) router dictionary.

    Information about the neutron router is exchanged as a python dictionary
    between plugin and config agent. RouterInfo is a wrapper around that dict,
    with attributes for common parameters. These attributes keep the state
    of the current router configuration, and are used for detecting router
    state changes when an updated router dict is received.
    """
    def __init__(self, router_id, router):
        self.device_id = router['device_dict']['id']
        self.router_id = router_id
        self.ex_gw_ports = None
        self.mgmt_ports = None
        self.ex_gw_fips = 'Invalid'
        self._snat_enabled = None
        self.internal_ports = []
        self.floating_ips = []
        self._router = None
        self.router = router
        self.routes = []

    @property
    def router(self):
        return self._router

    @property
    def id(self):
        return self.router_id

    @property
    def snat_enabled(self):
        return self._snat_enabled

    @router.setter
    def router(self, value):
        self._router = value
        if not self._router:
            return
        # enable_snat by default if it wasn't specified by plugin
        self._snat_enabled = self._router.get('enable_snat', True)

    @property
    def router_name(self):
        return N_ROUTER_PREFIX + self.router_id[:11]

class ServiceHelper(object):

    def __init__(self, host, conf, svm_agent, plugin_rpc):
        self.conf = conf
        self.host = host
        self.svm_agent = svm_agent
        self.plugin_rpc = plugin_rpc

        self.context = n_context.get_admin_context_without_session()
        self._dev_status = device_status.DeviceStatus()
        self._drivermgr = driver_mgt.DeviceDriverManager()

        self.service_instance = {}
        self.service_type = {}

        self.router_dict = {}

        self.added_services = set()
        self.updated_services = set()
        self.removed_services = set()

        self.sync_devices = set()
        self.fullsync = True
        self._service_type_process_register()

    def _service_type_process_register(self):
        self.service_type_func = {
                  s_constants.VROUTER: 'process_router',
              }

    def dump_service_instance(self, service_instance=None):
        try:
            data_path = file(os.path.join(
                self.conf.servicevm_agent.servicevm_dir,
                "servicevm-agent.pickle"), 'w')
        except IOError as e:
            LOG.error(e)

        if service_instance:
            pickle.dump(service_instance, data_path)

    def _sync_service_instance(self):
        LOG.info(_('Now load service instance'))
        empty = (set(), set(), set())
        current_service_instance_ids = set(self._fetch_service_ids())
        try:
            data_path = file(os.path.join(
                self.conf.servicevm_agent.servicevm_dir,
                "servicevm-agent.pickle"), 'r')
            self.service_instance = pickle.load(data_path)
        except IOError as e:
            if e.errno == 2:
                os.makedirs(self.conf.servicevm_agent.servicevm_dir)
                data_path = file(os.path.join(
                        self.conf.servicevm_agent.servicevm_dir,
                        "servicevm-agent.pickle"), 'wr')
                self.service_instance = pickle.load(data_path)
        except EOFError as e:
            LOG.warn('It is no service instance')
            self.service_instance = {}
        except Exception as e:
            self.fullsync = True
            LOG.error(e)
            return empty

        LOG.info(_("Init sync completed, get %s service instance"),
                 len(self.service_instance))
        exist_service_instance_ids = set(self.service_instance.keys())

        added_services = current_service_instance_ids-exist_service_instance_ids
        updated_services = current_service_instance_ids&exist_service_instance_ids
        deleted_services = exist_service_instance_ids-current_service_instance_ids

        self.fullsync = False
        return (added_services, updated_services, deleted_services)

     ### Notifications from Plugin ####
    def create_device(self, context, device):
        pass

    def update_device(self, context, device):
        pass

    def delete_device(self, context, device):
        pass

    def create_service_instance(self, context, device, service_instance):
        self.added_services.add(service_instance['id'])

    def update_service_instance(self, context, device, service_instance):
        self.updated_services.add(service_instance['id'])
    
    def delete_service_instance(self, context,
                          device, service_instance):
        self.removed_services.add(service_instance['id'])

    ### service helper public methods ###
    def process_services(self, device_ids=None, removed_devices=None):
        try:
            LOG.debug("service processing started")
            resources = {}
            services = {}
            added_service_instances = []
            updated_service_instances = []
            removed_services = []
            all_services_flag = False
            if self.fullsync:
                LOG.info("FullSync flag is on. Starting fullsync")
                all_services_flag = True
                self.fullsync = False
                self.added_services.clear()
                self.updated_services.clear()
                self.removed_services.clear()
                self.sync_devices.clear()
                (self.added_services, self.updated_services,
                 self.removed_services) = self._sync_service_instance()
            #else:
            if True:
                if self.added_services:
                    service_instance_ids = list(self.added_services)
                    LOG.info("added service instances:%s", service_instance_ids)
                    self.added_services.clear()
                    added_service_instances = self._fetch_services(
                                            service_instance_ids=service_instance_ids)
                if self.updated_services:
                    service_instance_ids = list(self.updated_services)
                    LOG.info("Updated service instances:%s", service_instance_ids)
                    self.updated_services.clear()
                    updated_service_instances = self._fetch_services(
                                            service_instance_ids=service_instance_ids)
                if device_ids:
                    LOG.debug("Adding new devices:%s", device_ids)
                    self.sync_devices = set(device_ids) | self.sync_devices
                if self.sync_devices:
                    sync_devices_list = list(self.sync_devices)
                    LOG.info("Fetching service instances on:%s", sync_devices_list)
                    updated_service_instances.extend(self._fetch_services(
                                           device_ids=sync_devices_list))
                    self.sync_devices.clear()
                if removed_devices:
                    ids = self._get_service_instance_ids_from_removed_devices(
                                    removed_devices)
                    self.removed_services = self.removed_services | set(ids)
                if self.removed_services:
                    removed_service_instances_ids = list(self.removed_services)
                    #self.removed_services.clear()
                    LOG.info("Removed services:%s", removed_service_instances_ids)
                    for s  in removed_service_instances_ids:
                        if s in self.service_instance:
                            removed_services.append(self.service_instance[s])
                    self.removed_services.clear()

            # Sort on hosting device
            if added_service_instances:
                resources['added_services'] = added_service_instances
            if updated_service_instances:
                resources['updated_services'] = updated_service_instances
            if removed_services:
                resources['removed_services'] = removed_services

            if not resources:
                LOG.debug('It is not resource to be processed')
                return

            hosting_devices = self._sort_resources_per_hosting_device(
                                   resources)
            LOG.info("after sort resource, hosting devices: %s",
                      hosting_devices)
            pool = eventlet.GreenPool()
            for device_id, services in hosting_devices.items():
                for service_type, resources in services.items():
                    added_services = resources.get('added_services', [])
                    updated_services = resources.get('updated_services', [])
                    removed_services = resources.get('removed_services', [])
                    process_func = getattr(self, self.service_type_func[service_type])
                    pool = eventlet.GreenPool()
                    pool.spawn_n(process_func,
                                 added_services, removed_services,
                                 updated_services, device_id, 
                                 all_service_instance=all_services_flag)
                    pool.waitall()

            # save hosting device
            if added_service_instances:
                self._service_instance_added_updated(added_service_instances)
            if updated_service_instances:
                self._service_instance_added_updated(updated_service_instances)
            if removed_services:
                LOG.info('removed services %s', removed_services)
                self._service_instance_removed(removed_services)

            self.dump_service_instance(service_instance=self.service_instance)

            #huxn update, 2015.9.26
            #if removed_services:
            #    for hd_id in removed_services['hosting_data']:
            #        self._drivermgr.remove_driver_for_hosting_device(hd_id)
            LOG.debug("service processing successfully completed")
        except Exception:
            LOG.exception(_LE("Failed processing services"))
            self.fullsync = True

    # service helper internal methods
    def _fetch_services(self, service_instance_ids=None, device_ids=None,
                        all_services=False):
        """Fetch services dict from the servicd plugin.

        :param service_instance_ids: List of service_instance_ids of services to fetch
        :param device_ids: List of device_ids whose services to fetch
        :param all_services  If True fetch all the service instances for this agent.
        :return: List of service_instance dicts of format:
                 [ {service_dict1}, {service_dict2},.....]
        """
        try:
            if all_services:
                r = self.plugin_rpc.get_service_instances(self.context)
            if service_instance_ids:
                r = self.plugin_rpc.get_service_instances(self.context,
                        service_instance_ids=service_instance_ids)
            if device_ids:
                r = self.plugin_rpc.get_service_instances(self.context,
                                                   device_ids=device_ids)
            return r
        except svmt_exception.DriverException as e:
            LOG.exception(_LE("RPC Error in fetching services from plugin"))
            #self.fullsync = True

    def _fetch_service_ids(self, all_service=True, device_ids=None):
        try:
            if all_service:
                r = self.plugin_rpc.fetch_service_ids(self.context)
            if device_ids:
                r = self.plugin_rpc.fetch_service_ids(self.context,
                                                      device_ids=device_ids)
            return r
        except svmt_exception.DriverException as e:
            LOG.exception(_LE("RPC Error in fetching service ids from plugin"))
            self.fullsync = True
    
    def _get_service_instance_ids_from_removed_devices(removed_devices):
        """Extract service_instance_ids from the removed devices info dict.

        :param removed_devices: Dict of removed devices and their
               associated resources.
        Format:
                {
                  'hosting_data': {'hd_id1': {'service_instances': [id1, id2, ...]},
                                   'hd_id2': {'service_instances': [id3, id4, ...]},
                                   ...
                                  },
                  'deconfigure': True/False
                }
        :return removed_service_instance_ids: List of removed service instance ids
        """
        removed_service_instance_ids = []
        for hd_id, resources in removed_devices['hosting_data'].items():
            removed_service_instance_ids+=resources.get('service_instances', [])
        return removed_service_instance_ids

    def _sort_resources_per_hosting_device(self, resources):
        """This function will sort the resources on hosting device.

        The sorting on hosting device is done by looking up the
        `hosting_device` attribute of the resource, and its `id`.

        :param resources: a dict with key of resource name
        :return dict sorted on the hosting device of input resource. Format:
        hosting_devices = {
                            'hd_id1' : { 'vrouter':{'added_services':[services]
                                                    'removed_services':[services],
                                                    'updated_services':[services], .... }
                                        'vfirewall':{'added_services':[services]
                                                     'removed_services':[services],
                                                     'updated_services':[services], .... }
                                       }
                            'hd_id2' : { 'vrouter':{'added_services':[services]
                                                    'removed_services':[services],
                                                    'updated_services':[services], .... }
                                       }
                            .......
                            }
        """
        hosting_devices = {}
        for key in resources.keys():
            services = {}
            for s in resources.get(key) or []:
                hd_id = s['device_dict']['id']
                hosting_devices.setdefault(hd_id, {})
                subservice = {}
                service_type = s.get('service_type').get('servicetype')
                subservice.setdefault(key,[]).append(s)
                services[service_type] = subservice
            hosting_devices[hd_id] = services
        return hosting_devices

    def _check_valid_services(self, service):
        service_type = service.get('service_type').get('servicetype')
        if service_type not in s_constants.SUPPORT_SERVICE_TYPE:
            LOG.info(_LI("Service Type %(service_type)s is not support"),
                       {'service_type':service_type})
            return False

        device = service['device_dict']
        if not self._dev_status.is_device_reachable(device):
            LOG.info(_LI("Service: %(id)s is on an unreachable "
                       "hosting device. "), {'id': device['id']})
            return False

        return True

    def process_router(self, added_services=None, removed_services=None,
                          updated_services=None, device_id=None,
                          all_service_instance=False):
        try:
            if all_service_instance:
                prev_vrouter_ids = set(self.service_type.get(
                                       s_constants.VROUTER, []))
            else:
                prev_vrouter_ids = set(self.service_type.get(
                                       s_constants.VROUTER, []))

            for r in (added_services + updated_services):
                try:
#                    if not r['admin_state_up']:
#                        continue
                    hd = r['device_dict']
                    if r['id'] not in self.router_dict:
                        self._router_added(r['id'], r)
                    ri = self.router_dict[r['id']]
                    ri.router = r
                    self._process_router(ri)
                except KeyError as e:
                    LOG.exception(_LE("Key Error, missing key: %s"), e)
                    self.updated_services.add(r['id'])
                    continue
                except svmt_exception.DriverException as e:
                    LOG.exception(_LE("Driver Exception on router:%(id)s. "
                                    "Error is %(e)s"), {'id': r['id'], 'e': e})
                    self.updated_services.add(r['id'])
                    continue

            if removed_services:
                for router in removed_services:
                    self._router_removed(router['id'])
        except Exception:
            LOG.exception(_LE("Exception in processing routers on device:%s"),
                          device_id)
            self.sync_devices.add(device_id)

    def _set_value(self, dict, key, value):
        if key not in dict:
            dict[key] = value

    def _service_instance_added_updated(self, service_instances):
        for s in service_instances:
            if s['id'] in self.service_instance:
                del self.service_instance[s['id']]
            self.service_instance[s['id']] = s

    def _service_instance_removed(self, service_instances):
        for s in service_instances:
            if s['id']in self.service_instance:
                del self.service_instance[s['id']]

    def _router_added(self, router_id, router):
        """Operations when a router is added.

        Create a new RouterInfo object for this router and add it to the
        service helpers router_info dictionary.  Then `router_added()` is
        called on the device driver.

        :param router_id: id of the router
        :param router: router dict
        :return: None
        """
        try:
            ri = RouterInfo(router_id, router)
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.router_added(ri)
            self.service_type.setdefault(s_constants.VROUTER,[]).append(router_id)
            self.router_dict[router_id] = ri
        except AttributeError as e:
            LOG.error(e)
            raise svmt_exception.DeviceNotConnection(device_id=ri.device_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _router_removed(self, router_id, deconfigure=True):
        """Operations when a router is removed.

        Get the RouterInfo object corresponding to the router in the service
        helpers's router_info dict. If deconfigure is set to True,
        remove this router's configuration from the hosting device.
        :param router_id: id of the router
        :param deconfigure: if True, the router's configuration is deleted from
        the hosting device.
        :return: None
        """
        if router_id in self.router_dict:
            ri = self.router_dict[router_id]
            if ri is None:
                LOG.warning(_LW("Info for router %s was not found. "
                           "Skipping router removal"), router_id)
                return
        else:
            LOG.warning(_LW("Info for router %s was not found. "
                           "Skipping router removal"), router_id)
            return

        ri.router[l3_constants.INTERFACE_KEY] = []
        ri.router[l3_constants.FLOATINGIP_KEY] = []
        ri.router[l3_constants.GW_FIP_KEY] = []

        try:
            if deconfigure:
                self._process_router(ri)
                driver = self._drivermgr.get_driver(ri.device_id)
                driver.router_removed(ri)
                self._drivermgr.remove_driver(ri.router)
            #del self.service_instance[router_id]
            del self.router_dict[router_id]
            self.service_type[s_constants.VROUTER].remove(router_id)
            self.removed_services.discard(router_id)
        except svmt_exception.DriverException:
            LOG.warning(_LW("Router remove for service_id : %s was incomplete. "
                       "Adding the router to removed_services list"), router_id)
            self.removed_services.add(router_id)
            # remove this router from updated_routers if it is there. It might
            # end up there too if exception was thrown earlier inside
            # `_process_router()`
            self.updated_services.discard(router_id)

        #ri.router[l3_constants.INTERFACE_KEY] = []
        #ri.router[l3_constants.FLOATINGIP_KEY] = []
        #ri.router[l3_constants.MANAGERMENT_KEY] = []
        #ri.router[l3_constants.GW_INTERFACE_KEY] = []
        #ri.router[l3_constants.GW_FIP_KEY] = []

    def _process_router(self, ri):
        """Process a router, apply latest configuration and update router_info.

        Get the router dict from  RouterInfo and proceed to detect changes
        from the last known state. When new ports or deleted ports are
        detected, `internal_network_added()` or `internal_networks_removed()`
        are called accordingly. Similarly changes in ex_gw_ports causes
         `external_gateway_added()` or `external_gateway_removed()` calls.
        Next, floating_ips and routes are processed. Also, latest state is
        stored in ri.internal_ports and ri.ex_gw_ports for future comparisons.

        :param ri : RouterInfo object of the router being processed.
        :return:None
        :raises:
            networking_cisco.plugins.cisco.cfg_agent.cfg_exceptions.DriverException
        if the configuration operation fails.
        """
        try:
            ex_gw_ports = ri.router.get(l3_constants.GW_INTERFACE_KEY, [])
            internal_ports = ri.router.get(l3_constants.INTERFACE_KEY, [])
            mgmt_ports = ri.router.get(l3_constants.MANAGERMENT_KEY, [])
            ex_gw_fips = ri.router.get(l3_constants.GW_FIP_KEY, [])
            existing_port_ids = set([p['id'] for p in ri.internal_ports])
            current_port_ids = set([p['id'] for p in internal_ports
                                    if p['admin_state_up']])
            new_ports = [p for p in internal_ports
                         if p['id'] in (current_port_ids - existing_port_ids)]
            old_ports = [p for p in ri.internal_ports
                         if p['id'] not in current_port_ids]

            for p in new_ports:
                self._set_subnet_info(p)
                self._internal_network_added(ri, p, ex_gw_ports,
                                             ex_gw_fips)
                ri.internal_ports.append(p)

            for p in old_ports:
                self._internal_network_removed(ri, p, ri.ex_gw_ports,
                                               ri.ex_gw_fips)
                ri.internal_ports.remove(p)

            if ex_gw_ports and ex_gw_fips and ex_gw_fips != ri.ex_gw_fips:
                #TODO select the first ex gw port
                self._set_subnet_info(ex_gw_ports)
                self._external_gateway_added(ri, ex_gw_fips, ex_gw_ports)
            elif ex_gw_ports and not ex_gw_fips and ex_gw_fips != ri.ex_gw_fips:
                self._external_gateway_removed(ri, ri.ex_gw_fips, ex_gw_ports)

            if ex_gw_ports:
                self._process_router_floating_ips(ri, ex_gw_ports)

            ri.ex_gw_ports = ex_gw_ports
            ri.mgmt_ports = mgmt_ports
            ri.ex_gw_fips = ex_gw_fips
            #hxn
            #self._routes_updated(ri)
        except svmt_exception.DriverException as e:
            with excutils.save_and_reraise_exception():
                self.updated_services.update(ri.router_id)
                LOG.error(e)

    def _process_router_floating_ips(self, ri, ex_gw_ports):
        """Process a router's floating ips.

        Compare current floatingips (in ri.floating_ips) with the router's
        updated floating ips (in ri.router.floating_ips) and detect
        flaoting_ips which were added or removed. Notify driver of
        the change via `floating_ip_added()` or `floating_ip_removed()`.

        :param ri:  RouterInfo object of the router being processed.
        :param ex_gw_ports: Port dict of the external gateway port.
        :return: None
        :raises: networking_cisco.plugins.cisco.cfg_agent.cfg_exceptions.
        DriverException
        if the configuration operation fails.
        """
        floating_ips = ri.router.get(l3_constants.FLOATINGIP_KEY, [])
        existing_floating_ip_ids = set(
            [fip['id'] for fip in ri.floating_ips])
        cur_floating_ip_ids = set([fip['id'] for fip in floating_ips])

        id_to_fip_map = {}
        for fip in floating_ips:
            if fip['port_id']:
                # store to see if floatingip was remapped
                id_to_fip_map[fip['id']] = fip
                if fip['id'] not in existing_floating_ip_ids:
                    ri.floating_ips.append(fip)
                    self._floating_ip_added(ri, ex_gw_ports,
                                            fip['floating_ip_address'],
                                            fip['rate_limit'],
                                            fip['fixed_ip_address'])

        floating_ip_ids_to_remove = (existing_floating_ip_ids -
                                     cur_floating_ip_ids)
        for fip in ri.floating_ips:
            if fip['id'] in floating_ip_ids_to_remove:
                ri.floating_ips.remove(fip)
                self._floating_ip_removed(ri, ri.ex_gw_ports,
                                          fip['floating_ip_address'],
                                          fip['fixed_ip_address'])
            else:
                # handle remapping of a floating IP
                new_fip = id_to_fip_map[fip['id']]
                new_fixed_ip = new_fip['fixed_ip_address']
                new_rate_limit = new_fip['rate_limit']
                existing_fixed_ip = fip['fixed_ip_address']
                existing_rate_limit = fip['rate_limit']
                if (new_fixed_ip and existing_fixed_ip and
                        new_fixed_ip != existing_fixed_ip) or (
                        new_rate_limit != existing_rate_limit):
                    floating_ip = fip['floating_ip_address']
                    self._floating_ip_removed(ri, ri.ex_gw_ports,
                                              floating_ip,
                                              existing_fixed_ip)
                    self._floating_ip_added(ri, ri.ex_gw_ports, floating_ip,
                                            rate_limit,new_fixed_ip)
                    ri.floating_ips.remove(fip)
                    ri.floating_ips.append(new_fip)

    def _internal_network_added(self, ri, port, ex_gw_ports,
                               ex_gw_fips):
        try:
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.internal_network_added(ri, port)
            if ri.snat_enabled and ex_gw_ports:
                driver.enable_internal_network_NAT(ri, port, ex_gw_ports,
                                                  ex_gw_fips)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _internal_network_removed(self, ri, port, ex_gw_ports,
                                  ex_gw_fips):
        try:
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.internal_network_removed(ri, port)
            if ri.snat_enabled and ex_gw_ports:
                driver.disable_internal_network_NAT(ri, port, ex_gw_ports,
                                                    ex_gw_fips)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _external_gateway_added(self, ri, ex_gw_fips, ex_gw_ports):
        try:
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.external_gateway_added(ri, ex_gw_fips, ex_gw_ports)
            if ri.snat_enabled and ri.internal_ports:
                for port in ri.internal_ports:
                    driver.enable_internal_network_NAT(ri, port, ex_gw_ports,
                                                       ex_gw_fips)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _external_gateway_removed(self, ri, ex_gw_fips, ex_gw_ports):
        try:
            driver = self._drivermgr.get_driver(ri.device_id)
            if ri.snat_enabled and ri.internal_ports:
                for port in ri.internal_ports:
                    driver.disable_internal_network_NAT(ri, port, ex_gw_ports,
                                                       ex_gw_fips)
            driver.external_gateway_removed(ri, ex_gw_fips, ex_gw_ports)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _floating_ip_added(self, ri, ex_gw_ports, floating_ip,
                           rate_limit, fixed_ip):
        try:
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.floating_ip_added(ri, ex_gw_ports, floating_ip,
                                     rate_limit, fixed_ip)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _floating_ip_removed(self, ri, ex_gw_ports,
                             floating_ip, fixed_ip ):
        try:
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.floating_ip_removed(ri, ex_gw_ports,
                                       floating_ip, fixed_ip)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("driver config fail ")

    def _sync_device(self):
        try:
            for driver in self._drivermgr.get_drivers().values():
                driver.sync_device()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("sync device error")

    def _routes_updated(self, ri):
        """Update the state of routes in the router.

        Compares the current routes with the (configured) existing routes
        and detect what was removed or added. Then configure the
        logical router in the hosting device accordingly.
        :param ri: RouterInfo corresponding to the router.
        :return: None
        :raises:
            networking_cisco.plugins.cisco.cfg_agent.cfg_exceptions.DriverException
        if the configuration operation fails.
        """
        new_routes = ri.router['routes']
        old_routes = ri.routes
        adds, removes = common_utils.diff_list_of_dict(old_routes,
                                                       new_routes)
        for route in adds:
            LOG.debug("Added route entry is '%s'", route)
            # remove replaced route from deleted route
            for del_route in removes:
                if route['destination'] == del_route['destination']:
                    removes.remove(del_route)
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.routes_updated(ri, 'replace', route)

        for route in removes:
            LOG.debug("Removed route entry is '%s'", route)
            driver = self._drivermgr.get_driver(ri.device_id)
            driver.routes_updated(ri, 'delete', route)
        ri.routes = new_routes

    @staticmethod
    def _set_subnet_info(port):
        ips = port['fixed_ips']
        if not ips:
            raise Exception(_("Router port %s has no IP address") % port['id'])
        if len(ips) > 1:
            LOG.error(_LE("Ignoring multiple IPs on router port %s"),
                      port['id'])
        prefixlen = netaddr.IPNetwork(port['subnet']['cidr']).prefixlen
        port['ip_cidr'] = "%s/%s" % (ips[0]['ip_address'], prefixlen)
