# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2014 Unitedstack Inc.
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
# @author: cing, UnitedStack, Inc
#

#servicevm provide service type
VROUTER='VROUTER'
VFIREWALL='VFIREWALL'

SURRPORT_SERVICE_TYPE = [
VROUTER,
VFIREWALL,
]


EXTERNAL_GATWAY_KEY='external_gateway'
FLOATINGIP_KEY='floatingip'

SUPPORT_VROUTER_SUBSERVICE_TYPE = [
   EXTERNAL_GATWAY_KEY,
   FLOATINGIP_KEY
]

PRE_DEV_TEM='tmp-'
PRE_DEVICE='dev-'
PRE_SERVICE='svc-'


SERVICEVM_AGENT_NOTIFY='servicevm_agent_notify'

ARP_TABLE = 44
FIP_TABLE = 54
LOCAL_TABLE = 0

EXTERNAL_BRIDGE = 'br-ex'
TAP_INTERFACE_PREFIX = 'tap'

