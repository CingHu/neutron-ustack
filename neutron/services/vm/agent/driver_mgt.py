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

from oslo.config import cfg
from oslo.utils import excutils

from neutron.agent.common import config
from neutron.openstack.common import lockutils
from neutron.openstack.common import timeutils
from neutron.openstack.common import importutils
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common.gettextutils import _LE
from neutron.openstack.common.gettextutils import _LI
from neutron.services.vm.agent import svmagt_exception

LOG = logging.getLogger(__name__)

# Constants for agent registration.
DRIVERS_PATH = "neutron.services.vm.agent.device_drivers."

class DeviceDriverManager(object):
    """This class acts as a manager for device drivers.

    The device driver manager  maintains the relationship between the
    different neutron logical resource (eg: routers, firewalls, vpns etc.) and
    where they are hosted. For configuring a logical resource (router) in a
    hosting device, a corresponding device driver object is used.
    Device drivers encapsulate the necessary configuration information to
    configure a logical resource (eg: routers, firewalls, vpns etc.) on a
    hosting device (eg: hillstone).

    The device driver class loads one driver object per hosting device.
    The loaded drivers are cached in memory, so when a request is made to
    get driver object for the same hosting device and resource (like router),
    the existing driver object is reused.

    This class is used by the service helper classes.
    """
    _instance = None
    _drivers = {}
    def __new__(cls):
        if not cls._instance:
            cls._instance = super(DeviceDriverManager, cls).__new__(cls)
        return cls._instance

    #def __init__(self):
    #    self._drivers = {}
    #    self._hosting_device_drivers_binding = {} 

    def get_drivers(self):
        return self._drivers

    def get_driver(self, device_id):
        try:
            return self._drivers[device_id]
        except KeyError:
            with excutils.save_and_reraise_exception(reraise=False):
                raise svmagt_exception.DriverNotFound(id=device_id)

    def set_driver(self, device_param):
        d_id = device_param['id']
        if d_id in self._drivers:
            #driver = self._hosting_device_drivers_binding[d_id]
            #self._drivers[d_id] = driver
            return
        else:
            try:
                driver_name = device_param['device_template'].get('device_driver') 
                if driver_name:
                    name_list = [DRIVERS_PATH, driver_name,'.',
                                 driver_name, '.', driver_name.capitalize()]
                    driver_class_name = "".join(name_list)
                    driver_class = importutils.import_object(driver_class_name,
                                        **device_param)
                    self._drivers[d_id] = driver_class
                    #self._hosting_device_drivers_binding[d_id] = driver_class
                    LOG.info("import drvier %(driver_class)s for device %(d_id)s", 
                            {'driver_class': driver_class, 
                             'd_id': d_id})
                else:
                    LOG.warn("Device %(device)s not specified device_driver.",
                            {'device': d_id})
            except ImportError:
                with excutils.save_and_reraise_exception(reraise=False):
                    LOG.exception(_LE("Error loading cfg agent driver %(driver)s "
                                    "for hosting device template "
                                    "%(d_id))"),
                                  {'driver': driver_class_name,
                                   'd_id': d_id})
                    raise svmagt_exception.DriverNotExist(driver=driver_class_name)
            except KeyError as e:
                with excutils.save_and_reraise_exception(reraise=False):
                    raise svmagt_exception.DriverNotSetForMissingParameter(p=e)


    def remove_driver(self, device_param):
        """Remove driver associated to a particular device."""
        device_id = device_param['id']
        if device_id in self._drivers:
            del self._drivers[device_id]

    def remove_driver_for_hosting_device(self, d_id):
        """Remove driver associated to a particular hosting device."""
        if d_id in self._hosting_device_drivers_binding:
            del self._hosting_device_drivers_binding[d_id]
