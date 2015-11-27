# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2014 UnitedStack Inc.
#    All Rights Reserved.
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

# NOTE(jianingy): have to be in asc order
FLOATINGIP_RATE_LIMITS = [1024, 2048, 3072, 4096, 5120, 6144,
                          7168, 8192, 9216, 10240, 11264, 12288,
                          13312, 14336, 15360, 16384, 17408, 18432,
                          19456, 20480, 21504, 22528, 23552, 24576,
                          25600, 26624, 27648, 28672, 29696, 30720]

# uos default name prefix
UOS_PRE_ROUTER = 'rt-'
UOS_PRE_NET = 'net-'
UOS_PRE_SUBNET = 'subnet-'
UOS_PRE_PORT = 'nic-'
UOS_PRE_FIP = 'fip-'
UOS_PRE_SG = 'sg-'
UOS_PRE_PPTP = 'pvpn-'
UOS_PRE_OPENVPN = 'openvpn-'

# uos br-ex flow table
UOS_EX_RESERVED_NET_TABLE = 10
UOS_EX_RESERVED_NET = '240.0.0.0/4'
