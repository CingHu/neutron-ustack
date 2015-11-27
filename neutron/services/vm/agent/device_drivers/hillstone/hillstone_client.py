import struct
import socket
import eventlet
import logging
import requests
import netaddr
from oslo.utils import excutils
from urllib import quote as q
from neutron.common import exceptions as qexception
from neutron.openstack.common import jsonutils

LOG = logging.getLogger('hillstone_client')

USERNAME = 'hillstone'
PASSWORD = 'hillstone'
PORT = 80
VRID = "1"
IFVSYSID = "0"
LANG = "zh_CN"

TIMEOUT = 20.0
MAX_TRIES = 5

COOKIE_CONTENT = '''token={token}; platform={platform}; hw_platform={hw_platform};
                    host_name={host_name}; company={company}; oemid={oemid}; 
                    vsysid={vsysid}; vsysName={vsysName}; role={role};
                    license={license}; httpProtocol={httpProtocol};
                    soft_version={soft_version}; sw_version={sw_version};
                    username={username}; overseaLicense={overseaLicense};
                    HS.frame.lang=zh_CN'''

URL_BASE = 'http://%(host)s/rest/%(resource)s'
HILLSTONE_LOGIN_PATH = 'login'
RESOURCE_URL_BASE = 'http://%(host)s/rest/%(resource)s'
RESOURCE_URL= 'http://%(host)s/%(path)s'
RESOURCE_TARGET_URL_BASE = 'http://%(host)s/rest/%(resource)s?target=%(target)s'
HEADER_CONTENT_TYPE_JSON = {'content-type': 'application/json'}
CONFIG_INTERFACE_URL = 'http://%(host)s/rest/Interface_WebUi?idfield=name'
REBOOT_URL = 'http://%(host)s/rest/execution?moduleName=admind&operation=reboot'
GET_INTERFACE_URL = 'rest/if_interface?isDynamic=1'
GET_ROUTE_URL = 'rest/vr_vrouter?target=ribv4&isDynamic=1&query={"fields":[],"conditions":[{"field":"vr_name","value": "%s"},{"field":"ribv4.route_type","value":"1"}]}'
PASSWORD_ENCRPT_URL='admind_encrypt_password?isDynamic=1'
PASSWORD_URL='aaa_administrator_edit?isPartial=1&idfield=name,vsys_id'
COMMON_USER_URL='aaa_administrator'

# Params for create dnat/snat rules to Hillstone

# if RULE_ID is "0", the rule's id in hillstone
# will be generated. i.e. 0, 1, 2, 3..

RULE_ID = 0
ENABLE = 1
GROUP_ID = "0"
SERVICE = "Any"
DESCRIPTION = ""
POS_FLAG = "0"
IF_LOG = False
FROM_IS_IP = "1"
SNAT_TO_IS_IP = "0"
DNAT_TO_IS_IP = "1"
TO = "Any"
TRANS_TO_IS_IP = "1"
FLAG = "4"

PREFERENCE = "1"
WEIGHT = "1"
ROUTE_TYPE = "1"
NEXTHOP_TYPE = '5'
IF_GATE = '0'

#Console(1), Telnet(2), SSH(4), HTTP(8), HTTPS(16), 0x1f:all
OPERATER_LOGIN_TYPE='0x0c'


#restful api resource
SNAT_RESOURCE = 'Snat'
DNAT_RESOURCE = 'Dnat'
POLICY_RESOURCE = 'policy_rule?idfield=id&isDynamic=1'
CONFIG_INTERFACE_RESOURCE = 'config_interface'
VROUTER_RESOURCE = 'vr_vrouter'
INTERFACE_RESOURCE = 'if_interface'

#resource target
RIBV4_TARGET = 'ribv4'

#resource name
VROUTER = 'vr_vrouter'
SNAT_RULE = 'snat_rule'
DNAT_RULE = 'dnat_rule'
VROUTER_KEY = 'vr_name'
RULE_ID = 'rule_id'

#Constants
VROUTER_NAME = 'trust-vr'

SURPPORT_MULTI_VROUTER = False

class InterfaceNotFound(qexception.NotFound):
    message = _("interface name of  %(mac)s could not be found")

class ResponseError(qexception.NeutronException):
    message = _("http response value is null or config fail")

class NoAuthenticate(qexception.NeutronException):
    message = _("hillstone is not authenticate")

class HillStoneRestClient(object):

    _instance = None
    def __new__(cls, device_ip):
        if not cls._instance:
            cls._instance = super(HillStoneRestClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, device_ip):
        self.userName = USERNAME
        self.password = PASSWORD
        self.port = PORT
        self.ifVsysId = IFVSYSID
        self.vrId = VRID
        self.lang = LANG
        self.timeout = TIMEOUT
        self.max_tries = MAX_TRIES
        self.host = ':'.join([device_ip, str(self.port)])
        self.device_ip = device_ip
        self.auth = {
            'userName': self.userName,
            'password': self.password,
            'ifVsysId': self.ifVsysId,
            'vrId': self.vrId,
            'lang': self.lang
        }
        self.token = None
        self.status = requests.codes.OK
        self.session = requests.Session()
        self.pool = eventlet.GreenPool(3)

    def authenticate(self):
        url = URL_BASE % {'host': self.host, 'resource': HILLSTONE_LOGIN_PATH}
        headers = {'Content-Length': '0',
                   'Accept': HEADER_CONTENT_TYPE_JSON}
        headers.update(HEADER_CONTENT_TYPE_JSON)
        self.token = None
        try:
            response = self._request("POST", url, headers=headers, data=jsonutils.dumps(self.auth))
        except Exception as e:
           with excutils.save_and_reraise_exception():
               LOG.error(e)

        if response is not None and response.get('success', False):
            self.token = response.get('result').get('token')
            self.result = response.get('result')

            self.cookie = COOKIE_CONTENT.format(
             token       =q(self.token),
             platform    =q(self.result.get('platform')),
             hw_platform =q(self.result.get('hw_platform')),
             host_name   =q(self.result.get('host_name')),
             company     =q(self.result.get('company')),
             oemid       =q(self.result.get('oemId')),
             vsysid      =q(self.result.get('vsysId')),
             vsysName    =q(self.result.get('vsysName')),
             role        =q(self.result.get('role')),
             license     =q(self.result.get('license')),
             httpProtocol=q(self.result.get('httpProtocol')),
             soft_version=q(self.result['sysInfo'].get('soft_version')),
             sw_version  =q(self.result['sysInfo'].get('sw_version')),
             username    ='hillstone',
             overseaLicense=q(self.result.get('overseaLicense')),
            )
            return True
        return False

    def _do_request(self, method, resource, target=None,
                    payload=None, more_headers=None,
                    params=None, full_url=False):
        """Perform a REST request to a Hillstone resource.

        If this is the first time interacting with the Hillstone, a token will
        be obtained. If the request fails, due to an expired token, the
        token will be obtained and the request will be retried once more.
        """

        if self.token is None:
            if not self.authenticate():
                raise NoAuthenticate()
        if full_url:
            url = (RESOURCE_URL % {'host': self.host, 'path': resource})
        elif resource == CONFIG_INTERFACE_RESOURCE:
            url = (CONFIG_INTERFACE_URL % {'host': self.device_ip})
        elif not target:
            url = (RESOURCE_URL_BASE % {'host': self.host, 'resource': resource})
        else:
            url = (RESOURCE_TARGET_URL_BASE % {'host': self.host, 'resource': resource, 'target': target})

        LOG.info('%s  Request URL is: %s' % (method, url))

        headers = {'Content-Type': HEADER_CONTENT_TYPE_JSON,
                   'Cookie':self.cookie}

        if more_headers:
            headers.update(more_headers)
        if payload:
            payload = jsonutils.dumps(payload)

        try:
            response = self._request(method, url, data=payload,
                                     headers=headers)
        except Exception as e:
            response = self._request(method, url, data=payload,
                                     headers=headers)
        if not response or not response['success']:
           with excutils.save_and_reraise_exception():
               LOG.error("config hillstone error, method:%s, url:%s,"
                         " payload:%s, headers: %s" % (
                          method, url, payload, headers))
               LOG.error("return value :%s" % response)

        return response

    def get_request(self, resource, target=None, params=None, full_url=False):
        """Perform a REST GET requests for a Hillstone resource."""
        return self._do_request('GET', resource, target, params=params, full_url=full_url)

    def post_request(self, resource, target=None, payload=None):
        """Perform a POST request to a Hillstone resource."""
        return self._do_request('POST', resource, target, payload=payload,
                                more_headers=HEADER_CONTENT_TYPE_JSON)

    def put_request(self, resource, target=None, payload=None):
        """Perform a PUT request to a Hillstone resource."""
        return self._do_request('PUT', resource, target, payload=payload,
                                more_headers=HEADER_CONTENT_TYPE_JSON)

    def delete_request(self, resource, target=None, payload=None):
        """Perform a DELETE request on a Hillstone resource."""
        return self._do_request('DELETE', resource, target, payload=payload,
                                more_headers=HEADER_CONTENT_TYPE_JSON)

    def _request(self, method, url, **kwargs):
        try:
            response = self.pool.spawn(self.session.request,
                    method,
                    url=url,
                    verify=False,
                    timeout=self.timeout,
                    **kwargs).wait()
            return jsonutils.loads(response.content)
        except Exception as e:
           with excutils.save_and_reraise_exception():
               LOG.error(e)
               LOG.error("ConnectionError occurs")

    def _get_data(self, resource, params=None, full_url=False):
        try:
            data = self.get_request(resource, params=params, full_url=full_url)
            LOG.info(data)
            result = data['result']
        except TypeError as e:
            LOG.error('http response data:  %s' % data)
            raise ResponseError()
        # for hillstone, if len(result) == 1, value is dict type, else list type
        if isinstance(result, dict):
            return [result]
        return result

    def get_vrouter_info(self):
        return self._get_data(VROUTER_RESOURCE)

    def get_user_info(self):
        return self._get_data(COMMON_USER_URL)

    def _get_interface_name(self, mac):
        mac1=mac.split(':')
        mac2=".".join([mac1[0]+mac1[1], mac1[2]+mac1[3], mac1[4]+mac1[5]])
        interfaces = self._get_interface_info()
        interface_dict = dict((i['mac'], i['name']) for i in interfaces)
        if mac2 not in interface_dict.keys():
            raise InterfaceNotFound(mac=mac)
        return interface_dict[mac2]

    def _get_interface_info(self):
        """{u'vlanif': u'NULL', u'bandwidth': u'1000000000', u'shutdown': u'0', u'port_isolation': u'0', u'speed': u'auto', u'arpi_rate_limit': u'0', u'keepalive': u'0', u'arpl': u'0', u'arpi': u'0', u'monitor': u'0', u'zone': u'trust', u'duplex': u'auto', u'arp_dis_dynamic_entry': u'0', u'lastchange': u'0', u'sfp_to_copper': u'0', u'default_mac': u'fa16.3e2f.987b', u'redundantif': u'NULL', u'pppoe_setroute': u'0', u'flag': u'637534244', u'dns_proxy': u'0', u'mirror_enable': u'0', u'export_to_vsys': u'root', u'aggregateif': u'NULL', u'name': u'ethernet0/0', u'setroute': u'0', u'vsys': u'root', u'mtu': u'1500', u'up_bandwidth': u'1000000000', u'arpi_trust': u'0', u'arptimeout': u'1200', u'keepalive_ip': u'0', u'autharp': u'0', u'ifid': u'8', u'sfp_to_copper_present': u'0', u'subtype': u'0', u'up_traffic': u'0', u'ipaddr': u'167794865', u'overlap': u'0', u'state': u'7', u'dhcp': u'dhcp', u'backup_revert': u'0', u'tunnel_type': u'0', u'primaryif': u'NULL', u'mac': u'fa16.3e2f.987b', u'ipaddr_mask': u'4294967040', u'macl': u'0', u'down_traffic': u'0', u'access_ip': u'0', u'alias': u'ethernet0/0', u'manage_ip': u'0', u'bgroupif': u'NULL'}
        """
        return self._get_data(GET_INTERFACE_URL, full_url=True)

    def _get_route_info(self, router_name):
        _URL = GET_ROUTE_URL % router_name
        return self._get_data(_URL, full_url=True)

    def get_dnat_info(self):
        """Return value like this:

           {u'total': u'1', u'result': [{u'enable': u'1', u'from': u'Any', u'from_is_ip': u'0', u'trans_to_is_ip': u'1', u'to': u'1.1.1.1/24', u'trans_to': u'2.2.2.2/24', u'group_id': u'0', u'rule_id': u'3', u'to_is_ip': u'1'}, {u'enable': u'1', u'from': u'Any', u'service': u'HTTP', u'from_is_ip': u'0', u'trans_to_is_ip': u'1', u'to': u'2.2.2.2/24', u'trans_to': u'2.2.2.2/24', u'group_id': u'0', u'rule_id': u'1', u'to_is_ip': u'1'}], u'success': True}
        """
        return self._get_data(DNAT_RESOURCE)

    def get_snat_info(self):
        """Return value like this:

           {u'total': u'1', u'result': [{u'enable': u'1', u'from': u'192.168.10.0/24', u'service': u'Any', u'from_is_ip': u'1', u'trans_to_is_ip': u'1', u'to': u'192.168.20.0/24', u'flag': u'1', u'pos_flag': u'0', u'trans_to': u'10.10.10.10/24', u'group_id': u'0', u'rule_id': u'1', u'to_is_ip': u'1'}, {u'enable': u'1', u'from': u'192.168.10.0/24', u'service': u'Any', u'from_is_ip': u'1', u'trans_to_is_ip': u'1', u'to': u'192.168.20.0/24', u'flag': u'1', u'pos_flag': u'0', u'trans_to': u'11.11.11.11/24', u'group_id': u'0', u'rule_id': u'3', u'to_is_ip': u'1'}], u'success': True}
        """
        return self._get_data(SNAT_RESOURCE)

    def get_policy_info(self):
        return self._get_data(POLICY_RESOURCE)

    def add_dnat_policy_rule(self, policy_data):
        """
        """
        payload=[]
        policy_rules = self.get_policy_info()
        rule_data = dict(('_'.join([p['dst_subnet']['ip'], p['dst_subnet']['netmask']]),
                         p['id']) for p in policy_rules if 'dst_subnet' in p)
        for policy in policy_data:
            ip = netaddr.IPAddress(policy['ip'])
            netmask = policy['netmask']
            p = '_'.join([str(int(ip)), str(netmask)])
            if p in rule_data.keys():
                LOG.warn('dnat policy rule %s is existed, so ignore it' % p)
                return
            rule= {
                  "id": -1,
                  "name": {
                      "name": ""
                  },
                  "src_zone": {
                      "name": "Any"
                  },
                  "src_addr": {
                      "type": "0",
                      "member": "Any"
                  },
                  "src_subnet": [],
                  "src_range": [],
                  "src_host": [],
                  "dst_zone": {
                      "name": "Any"
                  },
                  "dst_addr": [],
                  "dst_subnet": {
                      "ip": int(ip),
                      "netmask": int(netmask)
                  },
                  "dst_range": [],
                  "dst_host": [],
                  "service": {
                      "member": "Any"
                  },
                  "application": [],
                  "schedname": [
                      {}
                  ],
                  "action": "2",
                  "vpnname": "",
                  "log_start": "0",
                  "log_end": "0",
                  "log_deny": "0"
                 }
            payload.append(rule)    
        LOG.info("Create dnat policy rule, payload is %s", payload)
        self.post_request(POLICY_RESOURCE, payload=payload)

    def add_snat_policy_rule(self, policy_data):
        """
        """
        payload=[]
        policy_rules = self.get_policy_info()
        rule_data = dict(('_'.join([p['src_subnet']['ip'], p['src_subnet']['netmask']]),
                         p['id']) for p in policy_rules if 'src_subnet' in p)

        for policy in policy_data:
            ip = netaddr.IPAddress(policy['ip'])
            netmask = policy['netmask']
            p = '_'.join([str(int(ip)), str(netmask)])
            if p in rule_data.keys():
                LOG.warn('snat policy rule %s is existed, so ignore it' % p)
                return
            rule = {
                       "id": -1,
                       "name": {
                           "name": ""
                       },
                       "src_zone": {
                           "name": "Any"
                       },
                       "src_addr": [],
                       "src_subnet": {
                          "ip": int(ip),
                          "netmask": int(netmask)
                       },
                       "src_range": [],
                       "src_host": [],
                       "dst_zone": {
                           "name": "Any"
                       },
                       "dst_addr": {
                           "type": "0",
                           "member": "Any"
                       },
                       "dst_subnet": [],
                       "dst_range": [],
                       "dst_host": [],
                       "service": {
                           "member": "Any"
                       },
                       "application": [],
                       "schedname": [
                           {}
                       ],
                       "action": "2",
                       "vpnname": "",
                       "log_start": "0",
                       "log_end": "0",
                       "log_deny": "0"
                   }
            payload.append(rule)    
        LOG.info("Create snat policy rule, payload is %s", payload)
        self.post_request(POLICY_RESOURCE, payload=payload)

    def del_snat_policy_rule(self, policy_data):
        """
        """
        payload= {'keys':[]}
        policy_rules = self.get_policy_info()
        rule_data = dict(('_'.join([p['src_subnet']['ip'], p['src_subnet']['netmask']]),
                         p['id']) for p in policy_rules if 'src_subnet' in p)

        for policy in policy_data:
            ip = netaddr.IPAddress(policy['ip'])
            netmask = policy['netmask']
            p = '_'.join([str(int(ip)), str(netmask)])
            if p in rule_data.keys():
                payload['keys'].append({'id':rule_data[p]})
        LOG.info("Delete snat policy rule, payload is %s", payload)
        self.delete_request(POLICY_RESOURCE, payload=payload)

    def del_dnat_policy_rule(self, policy_data):
        """
        """
        payload= {'keys':[]}
        policy_rules = self.get_policy_info()
        rule_data = dict(('_'.join([p['dst_subnet']['ip'], p['dst_subnet']['netmask']]),
                         p['id']) for p in policy_rules if 'dst_subnet' in p)
        for policy in policy_data:
            ip = netaddr.IPAddress(policy['ip'])
            netmask = policy['netmask']
            p = '_'.join([str(int(ip)), str(netmask)])
            if p in rule_data.keys():
                payload['keys'].append({'id':rule_data[p]})
        LOG.info("Delete dnat policy rule, payload is %s", payload)
        self.delete_request(POLICY_RESOURCE, payload=payload)
    
    def add_dnat_rule(self, dnat_data):
        payload = [{VROUTER_KEY: VROUTER_NAME,
                    DNAT_RULE: []}] 
        dnat_rules = self.get_dnat_info()
        rule_data = dict((d['trans_to'], d['rule_id']) for d in dnat_rules if 'trans_to' in d)
        for d in dnat_data:
            dnat = d['trans_to']
            if dnat in rule_data.keys():
                LOG.warn('dnat rule %s is existed, so ignore it' % dnat)
                return
            rule = {
                 "rule_id": RULE_ID,
                 "group_id": GROUP_ID,
                 "description": DESCRIPTION,
                 "to_is_ip": DNAT_TO_IS_IP,
                 "to": d['to'],
                 "trans_to_is_ip": TRANS_TO_IS_IP,
                 "trans_to": d['trans_to'],
                 "from": SERVICE,
                 "from_is_ip": "0",
                 "enable": ENABLE
            }
            payload[0][DNAT_RULE].append(rule)
            if SURPPORT_MULTI_VROUTER:
                payload[0][VROUTER_KEY] = d['router_name']

        LOG.info("Create dnat rule, payload is %s", payload)
        self.post_request(DNAT_RESOURCE, target=DNAT_RULE, payload=payload)

    def add_snat_rule(self, snat_data):
        """

        """
        payload = [{VROUTER_KEY: VROUTER_NAME,
                    SNAT_RULE: []}] 
        snat_rules = self.get_snat_info()
        rule_data = dict((s['from'], s['rule_id']) for s in snat_rules if 'from' in s)
        for s in snat_data:
            snat = s['from']
            if snat in rule_data.keys():
                LOG.warn('snat rule %s is existed, so ignore it' % snat)
                return
            rule = {
                "rule_id": RULE_ID,
                "enable": ENABLE,
                "group_id": GROUP_ID,
                "service": SERVICE,
                "description": DESCRIPTION,
                "pos_flag": POS_FLAG,
                "log": IF_LOG,
                "from_is_ip": FROM_IS_IP,
                "from": s['from'],
                "to_is_ip": SNAT_TO_IS_IP,
                "to": TO,
                "trans_to_is_ip": TRANS_TO_IS_IP,
                "trans_to": s['trans_to'],
                "flag": FLAG
            }
            payload[0][SNAT_RULE].append(rule)
            if SURPPORT_MULTI_VROUTER:
                payload[0][VROUTER_KEY] = s['router_name']

        LOG.info("Create Snat Rule complete, payload is %s", payload)
        self.post_request(SNAT_RESOURCE, target=SNAT_RULE, payload=payload)

    def delete_dnat_rule(self, dnats):
        """
[{u'enable': u'1', u'from': u'Any', u'log': u'0', u'service': u'HTTP', u'from_is_ip': u'0', u'trans_to_is_ip': u'1', u'to': u'172.29.2.102/32', u'trans_to': u'172.30.248.56/32', u'group_id': u'0', u'rule_id': u'1', u'to_is_ip': u'1'}, {u'slb_server_pool_name': u'test', u'enable': u'1', u'from': u'Any', u'log': u'0', u'service': u'HTTP', u'from_is_ip': u'0', u'trans_to_is_ip': u'0', u'to': u'172.29.2.110/32', u'flag': u'17', u'group_id': u'0', u'rule_id': u'2', u'to_is_ip': u'1'}, {u'enable': u'1', u'from': u'Any', u'log': u'0', u'service': u'Any', u'from_is_ip': u'0', u'trans_to_is_ip': u'1', u'to': u'172.29.2.137/32', u'trans_to': u'192.168.100.121/32', u'group_id': u'0', u'rule_id': u'3', u'to_is_ip': u'1'}, {u'enable': u'1', u'from': u'Any', u'from_is_ip': u'0', u'trans_to_is_ip': u'1', u'to': u'172.29.2.207/32', u'trans_to': u'192.168.100.121/32', u'group_id': u'0', u'rule_id': u'4', u'to_is_ip': u'1'}], u'success': True}
        """
         
        payload = [{VROUTER_KEY: VROUTER_NAME, 
                    DNAT_RULE: []}] 
        dnat_rules = self.get_dnat_info()
        rule_data = dict((d['trans_to'], d['rule_id']) \
                          for d in dnat_rules if 'trans_to' in d)
        dnat_data = [d['trans_to'] for d in dnats]
        for dnat in dnat_data:
            if dnat in rule_data.keys():
                rule = {'rule_id': rule_data[dnat]}
                payload[0][DNAT_RULE].append(rule)
                if SURPPORT_MULTI_VROUTER:
                    payload[0][VROUTER_KEY] = dnat['router_name']

        LOG.info("Delete Dnat Rule , payload is %s", payload)
        self.delete_request(DNAT_RESOURCE, target=DNAT_RULE, payload=payload)

    def delete_snat_rule(self, snats):
        """
        """
        payload = [{VROUTER_KEY: VROUTER_NAME,
                    SNAT_RULE: []}] 
        snat_rules = self.get_snat_info()
        rule_data = dict((s['from'], s['rule_id']) for s in snat_rules if 'from' in s)
        snat_data = [s['from'] for s in snats]
        for snat in snat_data:
            if snat in rule_data.keys():
                rule = {'rule_id': rule_data[snat]}
                payload[0][SNAT_RULE].append(rule)
                if SURPPORT_MULTI_VROUTER:
                    payload[0][VROUTER_KEY] = snat['router_name']

        LOG.info("Delete Snat Rule, payload is %s", payload)
        self.delete_request(SNAT_RESOURCE, target=SNAT_RULE, payload=payload)

    def check_default_route(self, name, interface, ip, gateway):
        route_rules = self._get_route_info(name)
        for r in route_rules:
            if name in r.values():
                for rule in r['ribv4']:
                    if rule['destination'] == '0' and \
                       rule['netmask']== '0':
                        return True
        return False

    def set_ip_address_to_interface(self, interface):
        intf = self._get_interface_name(interface['mac'])
        ip = netaddr.IPAddress(interface['address'])
        mask = netaddr.IPAddress(interface['netmask'])

        payload = [
        {
            "name": intf,
            "description": "",
            "zone": "trust",
            "ipaddr": int(ip),
            "ipaddr_mask": int(mask),
            "dns_proxy": 0,
            "mgt": {
                "manage_service": "ping",
                #"manage_service": "http"
            },
            "host_route": [],
            "duplex": "auto",
            "speed": "auto",
            "mtu": 1500,
            "arpl": "0",
            "arptimeout": 1200,
            "keepalive_ip": "",
            "mirror_enable": "",
            "mirrortype": "",
            "mac": "",
            "shutdown": "0",
            "schedule": "",
            "track_name": "",
            "monitor": "",
            "ifType": "ethernet",
            "advanceIF": {},
            "dhcpififo": {},
            "clientprofile": {},
            "ext": {
                "dns_proxy_bypass": 3
            },
            "reverse_route_entry": {
                "revs_route": "1"
            },
            "aggentryname": {},
            "tap": {},
            "rip_if": {
                "auth_mode": "text", 
                "send_ver": "",
                "recv_ver": "",
                "split_horizon": "simple"
            }
          }
        ]
        LOG.info("config interface ip address, payload is %s", payload)
        self.put_request(CONFIG_INTERFACE_RESOURCE, payload=payload)

    def reboot_device(self):
        LOG.info("reboot device")
        self.post_request(REBOOT_URL, payload=[])

    def set_default_route(self, router_data):
        if SURPPORT_MULTI_VROUTER:
            name = router['router_name']
        else:
            name = VROUTER_NAME

        for router in router_data:
            interface = self._get_interface_name(router['mac'])
            ip = netaddr.IPAddress(router['address'])
            gateway = netaddr.IPAddress(router['gateway'])
            if self.check_default_route(name, interface, ip, gateway):
                LOG.info('default route is exists, ignore')
                return
            payload = [
                         {
                             VROUTER_KEY: name,
                             "ribv4": {
                                       "route_type": "1",
                                       "destination": "0",
                                       "netmask": "0",
                                       "nexthop": int(gateway),
                                       "if_gate": "0",
                                       "nexthop_type": "3",
                                       "isp_type": "0",
                                       "gate_ifname": interface,
                                       "protocol": "Static",
                                       "weight": "1",
                                       "preference": "1",
                                       "metric": "0",
                                       "updsecond": "0",
                                       "rib_flags": "784",
                                       "nexthop_flags": "139",
                                       "reference_count": "0",
                                       "vr_name": name,
                                       "bfd": "0"
                             }
                         }
                     ]
            LOG.info("config default router, payload:%s" % payload)
            self.post_request(VROUTER_RESOURCE, target=RIBV4_TARGET, payload=payload)

    def add_router(self, router_data):
        LOG.info("create router, payload:%s" % router_data)

    def delete_router(self, router_data):
        LOG.info("delete router, payload:%s" % router_data)

    def _get_password_encryt(self, password):
        payload = {"type" : "1", "mode" : "0", "password" : password}
        data = self.post_request(PASSWORD_ENCRPT_URL, payload=payload)
        LOG.info(data)
        result = data['result']
        return result['ept_password']

    def update_hillstone_user_password(self, password):
        encrpt_password = self._get_password_encryt(password)
        payload = [
                    {
                        "vsys_id": "0",
                        "password": encrpt_password,
                        "name": USERNAME,
                        "login_type": "0x1f",
                        "role": 'admin',
                        "description": ""
                    }
                ]
        try:
            self.put_request(PASSWORD_ENCRPT_URL, payload=payload)
        except Exception as e:
            LOG.error(e)
            return

        self.password = password
        self.token = None

    def _get_operator_user(self):
        pass

    def create_operator_user(self, user_name, password):
        user_info = self.get_user_info()
        user_names = [u['name'] for u in user_info if 'name' in u]
        if user_name in user_names:
            LOG.warn('user name %s is exists, ignore it', user_name)
            return
        encrpt_password = self._get_password_encryt(password)
        payload = [
                    {
                        "vsys_id": "0",
                        "password": encrpt_password,
                        "name": user_name,
                        "login_type": OPERATER_LOGIN_TYPE,
                        "role": "operator",
                        "description": "operator user"
                    }
                ]
        self.post_request(COMMON_USER_URL, payload=payload)
