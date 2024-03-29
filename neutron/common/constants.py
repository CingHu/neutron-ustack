# Copyright (c) 2012 OpenStack Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# TODO(salv-orlando): Verify if a single set of operational
# status constants is achievable
NET_STATUS_ACTIVE = 'ACTIVE'
NET_STATUS_BUILD = 'BUILD'
NET_STATUS_DOWN = 'DOWN'
NET_STATUS_ERROR = 'ERROR'

PORT_STATUS_ACTIVE = 'ACTIVE'
PORT_STATUS_BUILD = 'BUILD'
PORT_STATUS_DOWN = 'DOWN'
PORT_STATUS_ERROR = 'ERROR'

FLOATINGIP_STATUS_ACTIVE = 'ACTIVE'
FLOATINGIP_STATUS_DOWN = 'DOWN'
FLOATINGIP_STATUS_ERROR = 'ERROR'

DEVICE_OWNER_ROUTER_INTF = "network:router_interface"
DEVICE_OWNER_ROUTER_GW = "network:router_gateway"
DEVICE_OWNER_FLOATINGIP = "network:floatingip"
DEVICE_OWNER_DHCP = "network:dhcp"
DEVICE_OWNER_DVR_INTERFACE = "network:router_interface_distributed"
DEVICE_OWNER_AGENT_GW = "network:floatingip_agent_gateway"
DEVICE_OWNER_ROUTER_SNAT = "network:router_centralized_snat"
DEVICE_OWNER_LOADBALANCER = "neutron:LOADBALANCER"


#servicevm
SERVICEVM_OWNER_ROUTER_INTF = "servicevm:interface"
SERVICEVM_OWNER_ROUTER_GW = "servicevm:gateway"
SERVICEVM_OWNER_MGMT= "servicevm:managerment"

DEVICE_ID_RESERVED_DHCP_PORT = "reserved_dhcp_port"

DEVICE_OWNER_COMPUTE_PRE = "compute:"

FLOATINGIP_KEY = '_floatingips'
UOS_GW_FIP_KEY = '_uos_gw_floatingips'
INTERFACE_KEY = '_interfaces'
GW_INTERFACE_KEY = '_gw_interfaces'
MANAGERMENT_KEY = '_mgmt_interfaces'
GW_FIP_KEY = '_gw_floatingips'
METERING_LABEL_KEY = '_metering_labels'
FLOATINGIP_AGENT_INTF_KEY = '_floatingip_agent_interfaces'
SNAT_ROUTER_INTF_KEY = '_snat_router_interfaces'

IPv4 = 'IPv4'
IPv6 = 'IPv6'

DHCP_RESPONSE_PORT = 68

MIN_VLAN_TAG = 1
MAX_VLAN_TAG = 4094
MAX_VXLAN_VNI = 16777215
FLOODING_ENTRY = ['00:00:00:00:00:00', '0.0.0.0']

EXT_NS_COMP = '_backward_comp_e_ns'
EXT_NS = '_extension_ns'
XML_NS_V20 = 'http://openstack.org/quantum/api/v2.0'
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
XSI_ATTR = "xsi:nil"
XSI_NIL_ATTR = "xmlns:xsi"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
ATOM_XMLNS = "xmlns:atom"
ATOM_LINK_NOTATION = "{%s}link" % ATOM_NAMESPACE
TYPE_XMLNS = "xmlns:quantum"
TYPE_ATTR = "quantum:type"
VIRTUAL_ROOT_KEY = "_v_root"

TYPE_BOOL = "bool"
TYPE_INT = "int"
TYPE_LONG = "long"
TYPE_FLOAT = "float"
TYPE_LIST = "list"
TYPE_DICT = "dict"

AGENT_TYPE_DHCP = 'DHCP agent'
AGENT_TYPE_OVS = 'Open vSwitch agent'
AGENT_TYPE_LINUXBRIDGE = 'Linux bridge agent'
AGENT_TYPE_HYPERV = 'HyperV agent'
AGENT_TYPE_NEC = 'NEC plugin agent'
AGENT_TYPE_OFA = 'OFA driver agent'
AGENT_TYPE_L3 = 'L3 agent'
AGENT_TYPE_LOADBALANCER = 'Loadbalancer agent'
AGENT_TYPE_TUNNEL = 'Tunnel agent'
AGENT_TYPE_MLNX = 'Mellanox plugin agent'
AGENT_TYPE_METERING = 'Metering agent'
AGENT_TYPE_METADATA = 'Metadata agent'
AGENT_TYPE_SDNVE = 'IBM SDN-VE agent'
AGENT_TYPE_NIC_SWITCH = 'NIC Switch agent'
AGENT_TYPE_SERVICEVM = 'ServiceVM agent'
L2_AGENT_TOPIC = 'N/A'

PAGINATION_INFINITE = 'infinite'

SORT_DIRECTION_ASC = 'asc'
SORT_DIRECTION_DESC = 'desc'

PORT_BINDING_EXT_ALIAS = 'binding'
L3_AGENT_SCHEDULER_EXT_ALIAS = 'l3_agent_scheduler'
DHCP_AGENT_SCHEDULER_EXT_ALIAS = 'dhcp_agent_scheduler'
LBAAS_AGENT_SCHEDULER_EXT_ALIAS = 'lbaas_agent_scheduler'
L3_DISTRIBUTED_EXT_ALIAS = 'dvr'

# Protocol names and numbers for Security Groups/Firewalls
PROTO_NAME_TCP = 'tcp'
PROTO_NAME_ICMP = 'icmp'
PROTO_NAME_ICMP_V6 = 'icmpv6'
PROTO_NAME_UDP = 'udp'
PROTO_NUM_TCP = 6
PROTO_NUM_ICMP = 1
PROTO_NUM_ICMP_V6 = 58
PROTO_NUM_UDP = 17

# List of ICMPv6 types that should be allowed by default:
# Multicast Listener Query (130),
# Multicast Listener Report (131),
# Multicast Listener Done (132),
# Neighbor Solicitation (135),
# Neighbor Advertisement (136)
ICMPV6_ALLOWED_TYPES = [130, 131, 132, 135, 136]
ICMPV6_TYPE_RA = 134

sg_supported_ethertypes = ['IPv4']
DHCPV6_STATEFUL = 'dhcpv6-stateful'
DHCPV6_STATELESS = 'dhcpv6-stateless'
IPV6_SLAAC = 'slaac'
IPV6_MODES = [DHCPV6_STATEFUL, DHCPV6_STATELESS, IPV6_SLAAC]

IPV6_LLA_PREFIX = 'fe80::/64'

# Linux interface max length
DEVICE_NAME_MAX_LEN = 15

# attribute name for nova boot
ATTR_NAME_IMAGE = 'image'
ATTR_NAME_FLAVOR = 'flavor'
ATTR_NAME_META = 'meta'
ATTR_NAME_FILES = "files"
ATTR_NAME_RESERVEATION_ID = 'reservation_id'
ATTR_NAME_SECURITY_GROUPS = 'security_groups'
ATTR_NAME_USER_DATA = 'user_data'
ATTR_NAME_KEY_NAME = 'key_name'
ATTR_NAME_AVAILABILITY_ZONE = 'availability_zone'
ATTR_NAME_BLOCK_DEVICE_MAPPING = 'block_device_mapping'
ATTR_NAME_BLOCK_DEVICE_MAPPING_V2 = 'block_device_mapping_v2'
ATTR_NAME_NICS = 'nics'
ATTR_NAME_NIC = 'nic'
ATTR_NAME_SCHEDULER_HINTS = 'sheculer_hints'
ATTR_NAME_CONFIG_DRIVE = 'config_drive'
ATTR_NAME_DISK_CONFIG = 'disk_config'
