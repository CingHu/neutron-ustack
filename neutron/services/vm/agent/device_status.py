# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
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

import datetime

from oslo.config import cfg
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils

from neutron.agent.linux import utils as linux_utils
from neutron.services.vm.agent import config as vm_config


LOG = logging.getLogger(__name__)


BOOT_TIME_INTERVAL = 120


def _is_pingable(ip):
    """Checks whether an IP address is reachable by pinging.

    Use linux utils to execute the ping (ICMP ECHO) command.
    Sends 5 packets with an interval of 0.2 seconds and timeout of 1
    seconds. Runtime error implies unreachability else IP is pingable.
    :param ip: IP to check
    :return: bool - True or False depending on pingability.
    """
    if not ip:
       LOG.warning("inputing ip adress is None")
       return False

    #ip = '10.0.88.138'
    ping_cmd = ['ping',
                '-c', '5',
                '-W', '1',
                '-i', '0.2',
                ip]
    try:
        linux_utils.execute(ping_cmd, check_exit_code=True)
        return True
    except RuntimeError:
        LOG.warning("Cannot ping ip address: %s", ip)
        return False


class DeviceStatus(object):
    """Device status and backlog processing."""

    _instance = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super(DeviceStatus, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.backlog_devices = {}
                
    def get_backlogged_devices(self):
        return self.backlog_devices.keys()

    def get_backlogged_devices_info(self):
        wait_time = datetime.timedelta(
            seconds=cfg.CONF.servicevm_agent.device_dead_timeout)
        resp = []
        for hd_id in self.backlog_devices:
            hd = self.backlog_devices[hd_id]
            created_time = hd['created_at']
            #TODO boottime is the restfull api service avaliable time, so
            # we need get this time by the period task
            boottime = datetime.timedelta(seconds=BOOT_TIME_INTERVAL)
            backlogged_at = hd['backlog_insertion_ts']
            booted_at = created_time + boottime
            dead_at = backlogged_at + wait_time
            resp.append({'host id': hd['id'],
                         'created at': str(created_time),
                         'backlogged at': str(backlogged_at),
                         'estimate booted at': str(booted_at),
                         'considered dead at': str(dead_at)})
        return resp

    def is_device_reachable(self, device):
        """Check the device which hosts this resource is reachable.

        If the resource is not reachable, it is added to the backlog.

        :param device : dict of the device
        :return True if device is reachable, else None
        """
        hd = device
        hd_id = device['id']
        mgmt_url = device.get('mgmt_url', None)
        if mgmt_url:
            hd_mgmt_ip = mgmt_url.get('ip_address', None)
            device['created_at'] = datetime.datetime.strptime(
                device['created_at'], '%Y-%m-%dT%H:%M:%S.000000')

            if hd_id not in self.backlog_devices.keys():
                if _is_pingable(hd_mgmt_ip):
                    LOG.debug("Hosting device: %(hd_id)s@%(ip)s is reachable.",
                              {'hd_id': hd_id, 'ip': hd_mgmt_ip})
                    return True
                LOG.warn("Hosting device: %(hd_id)s@%(ip)s is NOT reachable.",
                          {'hd_id': hd_id, 'ip': hd_mgmt_ip})
                #hxn add
                hd['backlog_insertion_ts'] = max(
                    timeutils.utcnow(),
                    hd['created_at'] +
                    datetime.timedelta(seconds=BOOT_TIME_INTERVAL))
                self.backlog_devices[hd_id] = hd
                LOG.debug("Hosting device: %(hd_id)s @ %(ip)s is now added "
                          "to backlog", {'hd_id': hd_id, 'ip': hd_mgmt_ip})
        else:
            LOG.debug("Hosting device: %(hd_id)s can not added "
                      "to backlog, because of no mgmt_ip", {'hd_id': hd_id})

    def add_backlog_device(self, devices):
        for d in devices:
            if d['id'] not in self.backlog_devices:
                self.backlog_devices[d['id']] = d

    def remove_backlog_device(self, devices):
        for d in devices:
            if d['id'] in self.backlog_devices:
                self.backlog_devices.pop(d['id'])

    def check_backlogged_devices(self):
        """"Checks the status of backlogged devices.

        Skips newly spun up instances during their booting time as specified
        in the boot time parameter.

        :return A dict of the format:
        {'reachable': [<hd_id>,..], 'dead': [<hd_id>,..]}
        """
        response_dict = {'reachable': [], 'dead': []}
        LOG.debug("Current Backlogged devices: %s",
                  self.backlog_devices.keys())
        for hd_id in self.backlog_devices.keys():
            hd = self.backlog_devices[hd_id]
            if hd.get('mgmt_url'):
                if not timeutils.is_older_than(hd['created_at'],
                                               BOOT_TIME_INTERVAL):
                    LOG.debug("Hosting device: %(hd_id)s @ %(ip)s hasn't "
                                 "passed minimum boot time. Skipping it. ",
                             {'hd_id': hd_id, 'ip': hd['mgmt_url'].get('ip_address', None)})
                    continue
                LOG.debug("Checking device: %(hd_id)s @ %(ip)s for "
                           "reachability.", {'hd_id': hd_id,
                                              'ip': hd['mgmt_url'].get('ip_address', None)})
                if _is_pingable(hd['mgmt_url'].get('ip_address', None)):
                    response_dict['reachable'].append(hd_id)
                    LOG.debug("Hosting device: %(hd_id)s @ %(ip)s is now "
                               "reachable. Adding it to response",
                             {'hd_id': hd_id, 'ip': hd['mgmt_url'].get('ip_address', None)})
                else:
                    LOG.debug("Hosting device: %(hd_id)s @ %(ip)s still not "
                               "reachable ", {'hd_id': hd_id,
                                               'ip': hd['mgmt_url'].get('ip_address', None)})
                    if hd.get('backlog_insertion_ts'):
                        if timeutils.is_older_than(
                                hd['backlog_insertion_ts'],
                                cfg.CONF.servicevm_agent.device_dead_timeout):
                            LOG.debug("Hosting device: %(hd_id)s @ %(ip)s hasn't "
                                      "been reachable for the last %(time)d seconds. "
                                      "Marking it dead.",
                                      {'hd_id': hd_id,
                                       'ip': hd['mgmt_url'].get('ip_address', None),
                                       'time': cfg.CONF.servicevm_agent.
                                      device_dead_timeout})
                            response_dict['dead'].append(hd_id)
                    else:
                        response_dict['dead'].append(hd_id)
            else:
                response_dict['dead'].append(hd_id)
        return response_dict
