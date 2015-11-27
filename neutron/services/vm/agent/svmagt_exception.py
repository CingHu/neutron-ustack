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


from neutron.common import exceptions

class DriverException(exceptions.NeutronException):
    """Exception created by the Driver class."""


class InitializationException(DriverException):
    """Exception when initialization of CSR1kv Routing Driver object."""
    message = (_("Critical device parameter missing. Failed initializing "))


class ConnectionException(DriverException):
    """Connection exception when connecting to hosting device."""
    message = (_("Failed connecting to device. Reason: %(reason)s. "
                 "Connection params are User:%(user)s, Host:%(host)s, "
                 "Port:%(port)s, Device timeout:%(timeout)s."))

class ConfiException(DriverException):
    """Configuration exception thrown when modifying the running config."""
    message = (_("Error executing snippet:%(snippet)s. "
                 "ErrorType:%(type)s ErrorTag:%(tag)s."))


class UnknownValueException(DriverException):
    """Exception thrown when an unknown value is received."""
    message = (_("Data in attribute: %(attribute)s does not correspond to "
                 "expected value. Value received is %(value)s. "))


class DriverNotExist(DriverException):
    message = _("Driver %(driver)s does not exist.")


class DriverNotSetForMissingParameter(DriverException):
    message = _("Driver cannot be set for missing parameter:%(p)s.")

class DriverNotFound(DriverException):
    message = _("device %(id)s Driver not found.")


class DeviceNotConnection(DriverException):
    message = _("device %(device_id)s cant not be connected")
