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
# @author: Zhi Chang, UnitedStack, Inc
"""
Shell based configuration snippets in Hillstone's VFW
"""

VFWUSERNAME = 'hillstone'
VFWPASSWD = 'hillstone'
#=====================================================#
# Set IP address for a interface
# $(config) interface ethernet0/1
# $(config) zone trust
# $(config-if-eth0/1) ip address 1.2.3.4 255.255.255.0
#=====================================================#
CONFIG_IP_ADDRESS = """\
    'config',\
    'interface %s',\
    'zone trust',\
    'ip address %s %s',\
"""
