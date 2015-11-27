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
# @author: Yong Sheng Gong, UnitedStack, Inc
#

import json
import netaddr
import time
import webob.exc

import functools

from neutron import quota
from neutron.api import api_common
from neutron.api import extensions
from neutron.api.rpc.agentnotifiers import helo_rpc_agent_api
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.api.v2 import resource
from neutron.common import exceptions as qexception
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import uos_utils
from neutron.common import uos_constants
from neutron.db import l3_db
from neutron.db import agents_db
from neutron.db import models_v2
from neutron.db import securitygroups_db
from neutron.extensions import l3
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron import policy
from neutron import wsgi

from neutron.extensions import dhcpagentscheduler
from neutron.extensions import l3agentscheduler

from oslo.config import cfg
from sqlalchemy.orm import exc as sa_exc

LOG = logging.getLogger(__name__)

PORTFOWRADINGS = 'portforwardings'
RESOURCE = 'uos_resource'
RESOURCES = RESOURCE + 's'
DefaultSGRules = ('ingress,icmp,8,0:ingress,tcp,80,80:' +
                  'ingress,tcp,443,443:ingress,tcp,22,22:' +
                  'ingress,tcp,3389,3389')
unitedstack_opts = [
    cfg.StrOpt('external_shadow_subnet',
               default='ext_shadow_subnet',
               help=_("Where to allocate ip for router's gateway interface.")),
    cfg.ListOpt('subnets_to_exclude',
               default=[],
               help=_("Subnet ids not to allocate IPs from")),
    cfg.StrOpt('securitygroup_default_rules',
               default=DefaultSGRules,
               help=_("Default security group rules.")),
    # protocol:type:dport:pps
    cfg.BoolOpt('uos_pps_limits_enable',
                default=False,
                help=_("Enable/Disable attack defend")),
    # protocol:type:dport:pps
    cfg.ListOpt('uos_pps_limits',
                default=[],
                help=_("List of <protocol>:<type>:<dport>:<pps> in attack defend,"
                       "such as tcp:syn:80:10000")),
    cfg.ListOpt('uos_marks',
                default=[],
                help=_("List of <markno>:<rate_limit> in kbps,"
                       "such as 2:10")),
    cfg.ListOpt('uos_mark_actions',
                default=[],
                help=_("iptable actions such as'" +
                       "'-p tcp --syn -j MARK --set-mark 2'")),
    cfg.ListOpt('service_port_owners',
        default=['manila:share'],
               help=_('Service ports will allow allocate unlimited ips '
                      'on it, recongnized by device owners.')),
]


def register_uos_config():
    cfg.CONF.register_opts(unitedstack_opts, 'unitedstack')


# Uos Exceptions
class ResourceNotFound(qexception.NotFound):
    message = _("Resource %(name)s could not be found")

class ResourceMethodNotFound(qexception.NotFound):
    message = _("Resource method %(method)s could not be found")

RESOURCE = 'uos_resource'
RESOURCES = RESOURCE + 's'
RESOURCE_PLUGIN_MAP = {
    'agents': constants.CORE,
    'networks': constants.CORE,
    'subnets': constants.CORE,
    'ports': constants.CORE,
    'security_groups': constants.CORE,
    'security_group_rules': constants.CORE,
    'floatingips': constants.L3_ROUTER_NAT,
    'routers': constants.L3_ROUTER_NAT,
    'firewalls': constants.FIREWALL,
    'firewall_policies': constants.FIREWALL,
    'firewall_rules': constants.FIREWALL,
    'vips': constants.LOADBALANCER,
    'pools': constants.LOADBALANCER,
    'members': constants.LOADBALANCER,
    'health_monitors': constants.LOADBALANCER,
    'vpnservices': constants.VPN,
    'ipsec_site_connections': constants.VPN,
    'ipsecpolicies': constants.VPN,
    'ikepolicies': constants.VPN,
    'vpnusers': constants.VPN,
    'pptpconnections': constants.VPN,
    'openvpnconnections': constants.VPN,
    'tunnels': constants.TUNNEL,
    'tunnel_connections': constants.TUNNEL,
}

EXTENDED_TIMESTAMP = {
    'created_at': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
}
EXTENDED_RESOURCES = ['floatingips', 'routers', 'networks', 'ports',
                      'subnets', 'security_groups']

CONTROLLER_LIST_MAP = {
                     'dhcp-agent-list-hosting-net':
                      {
                          'class': 'dhcpagentscheduler.DhcpAgentsHostingNetworkController()',
                          'resource': 'network',
                      },
                     'net-list-on-dhcp-agent':
                      {
                          'class': 'dhcpagentscheduler.NetworkSchedulerController()',
                          'resource': 'agent',
                      },
                     'l3-agent-list-hosting-router':
                      {
                          'class': 'l3agentscheduler.L3AgentsHostingRouterController()',
                          'resource': 'router',
                      },
                     'router-list-on-l3-agent':
                     {
                         'class': 'l3agentscheduler.RouterSchedulerController()',
                         'resource':'agent',
                     },
}

STATISTICS_RESOURCE = ['agents',
                      'floatingips',
                      'networks',
                      'routers',
                      'subnets',
                      'ports',
                      'security_group_rules',
                      'security_groups',
]
AGENT_RESOURCE_MAP = {
                     'neutron-l3-agent':
                         {
                             'class':'l3agentscheduler.RouterSchedulerController()',
                             'resource':'routers'
                         },
                     'neutron-dhcp-agent':
                         {
                             'class':'dhcpagentscheduler.NetworkSchedulerController()',
                             'resource':'networks'
                         },
                     #'neutron-lbaas-agent':
                     #    {
                     #        'class':'lbaas_agentscheduler.LbaasAgentHostingPoolController()',
                     #        'resource':'loadbalancers'
                     #    },
                     }

TENANT_RESOURCES = ['floatingips','routers',
                    'networks','subnets',
                    'ports']

class UosController(wsgi.Controller):

    def _get_shared_resource(self, request, envstr, resource):
        _result = {}
        env = request.environ
        if envstr:
            env['QUERY_STRING'] = envstr + "&shared=False"
        else:
            env['QUERY_STRING'] = "shared=False"
        controller = self._get_resource_controller(resource)
        _result = controller.index(request)
        env['QUERY_STRING'] = "shared=True"
        _result_share = controller.index(request)
        _result[resource] += _result_share[resource]
        return _result

    def index(self, request, input_resources=None, **kwargs):
        counts = []
        fields = request.params.dict_of_lists().get('fields', ())
        for field in fields:
            if 'count:' in field:
                field_to_count = str(field).split('count:')[1]
                counts.append(field_to_count)
                request.GET.add(u'fields', field_to_count)
        LOG.info('Fileds to count: %s', counts)
        start = time.time()
        filters = api_common.get_filters(request, {},
                                         ['fields', 'sort_key', 'sort_dir',
                                          'limit', 'marker', 'page_reverse'])
        uos_resources = RESOURCE_PLUGIN_MAP.keys()
        resources = []
        if input_resources:
            resources = [input_resources]
        elif filters and 'uos_resources' in filters and input_resources is None:
            resources = filters['uos_resources']

        for name in resources:
            if name not in uos_resources:
                raise ResourceNotFound(name=name)
        if not resources:
            resources = uos_resources
        resources_set = set(resources)
        # NOTE(gongysh) shared is used since we maybe have
        # tenant_id in filters, but at the same time, we
        # want got shared resources for these tenants
        shared = request.GET.get('net_shared')
        if not shared:
            shared = request.GET.get('uos_shared')
        netflag = False
        subnetflag = False
        if shared:
            if ('networks' in resources_set):
                resources_set.remove('networks')
                netflag = True
            if ('subnets' in resources_set):
                resources_set.remove('subnets')
                subnetflag = True
        result = {}
        for name in resources_set:
            _start = time.time()
            controller = self._get_resource_controller(name)
            _result = controller.index(request)
            LOG.debug('performance %s, %.3f', name, time.time() - _start)
            if _result:
                result.update(_result)
        envstr = request.environ.get('QUERY_STRING')
        shared_resources = []
        if netflag:
            shared_resources.append('networks')
        if subnetflag:
            shared_resources.append('subnets')
        for resource in shared_resources:
            _result = self._get_shared_resource(request, envstr, resource)
            if _result:
                result.update(_result)
        LOG.debug('performance end %.3f', time.time() - start)
        #NOTE(wwei): This part should abstract to a indival func
        for attr in counts:
            if len(result) is 1 and attr in result.values()[0][0]:
                for obj in result.values()[0]:
                    obj['count:%s' % attr] = len(obj[attr])
            elif attr == 'floatingips' and 'routers' in resources:
                new_req = request.copy()
                new_req.GET.clear()
                new_req.GET.add('fields', 'router_id')
                controller = self._get_resource_controller(attr)
                _result = controller.index(new_req).values()[0]
                _result_count = []
                for item in _result:
                    _result_count.append(item.get('router_id'))
                _result_set = set(_result_count)
                _result = {}
                for item in _result_set:
                    _result[item] = _result_count.count(item)
                for router in result['routers']:
                    router['count:%s' % attr] = _result.get(router['id'], 0)
        return result

    def create(self, request, body, **kwargs):
        raise NotImplementedError()

    def delete(self, request, id, **kwargs):
        raise NotImplementedError()

    def validate(collection, attribute, action):
        """Validate input and policy, validators could be multiple

        Should be called as a decorator and API's param should
        only be "self", "request", "id" and "body".

        :param collection: The collection which API operate.
        :param attr: The attribute which API operate.
        :param action: The fucntion's name.
        """

        def _validate(f):
            @functools.wraps(f)
            def func(self, request, id, **kwargs):
                body = kwargs.get('body')
                #NOTE(weiw): for body may not input
                policy.enforce(request.context, action, body)
                if collection is None:
                    return f(self, request, id, body)
                collection_info = attr.RESOURCE_ATTRIBUTE_MAP.get(collection)
                attr_info = collection_info.get(attribute)
                validators = attr_info.get('validate')
                if attribute is "portforwardings":
                    _body = [body]
                else:
                    _body = body[attribute]
                for val in validators:
                    msg = attr.validators[val](_body)
                    if msg:
                        raise qexception.InvalidInput(message=msg)
                return f(self, request, id, body)
            return func
        return _validate

    def _get_service_plugin(self, name):
        plugin = manager.NeutronManager.get_service_plugins()[name]
        return plugin

    def _get_resource_controller(self, resource_collection):
        params = attr.RESOURCE_ATTRIBUTE_MAP.get(resource_collection)
        if not params:
            raise ResourceNotFound(name=resource_collection)
        _service_name = RESOURCE_PLUGIN_MAP[resource_collection]
        if not _service_name:
            raise ResourceNotFound(name=resource_collection)
        plugin = manager.NeutronManager.get_service_plugins()[
            _service_name]
        if not plugin:
            raise ResourceNotFound(name=resource_collection)
        controller = base.Controller(plugin, resource_collection,
                                     resource_collection[:-1], params,
                                     allow_pagination= cfg.CONF.allow_pagination)
        return controller

    @validate(None, None, "associate_floatingip_router")
    def associate_floatingip_router(self, request, id, body):
        context = request.context
        router = self._get_service_plugin(constants.L3_ROUTER_NAT)
        router_id = body['router_id']
        #NOTE(gongysh) authz
        router.get_router(context, router_id)
        result = router.associate_floatingip_router(context, id,
                                                    router_id)
        return result

    def get_router_details(self, request, id, **kwargs):
        context = request.context
        context.is_admin = False
        routers_controller = self._get_resource_controller('routers')
        _request = resource.Request({'neutron.context': context})

        router = routers_controller.show(_request, id)
        ports_controller = self._get_resource_controller('ports')
        _request = resource.Request(
            {'QUERY_STRING':
             ('device_id=%(id)s&device_owner=%(owner)s' %
              {'id': id,
               'owner': 'network:router_interface'}),
             'neutron.context': context})
        ports = ports_controller.index(_request)
        # NOTE(gongysh) fill in subnet name
        subnetid_portfixedip_dict = {}
        for port in ports['ports']:
            if port['fixed_ips']:
                fixed_ip = port['fixed_ips'][0]
                fixed_ips = subnetid_portfixedip_dict.get(
                    fixed_ip['subnet_id'], [])
                fixed_ips.append(fixed_ip)
                subnetid_portfixedip_dict[fixed_ip['subnet_id']] = fixed_ips
        subnetids = subnetid_portfixedip_dict.keys()
        plugin = manager.NeutronManager.get_service_plugins()[
            constants.CORE]
        subnets = plugin.get_subnets(context, filters={'id': subnetids})
        for subnet in subnets:
            for fixed_ip in subnetid_portfixedip_dict[subnet['id']]:
                fixed_ip['subnet_name'] = subnet['name']
                fixed_ip['cidr'] = subnet['cidr']

        # NOTE(gongysh) fill in network name
        netid_port_dict = {}
        for port in ports['ports']:
            netports = netid_port_dict.get(port['network_id'], [])
            netports.append(port)
            netid_port_dict[port['network_id']] = netports
        netids = netid_port_dict.keys()
        nets = plugin.get_networks(context, filters={'id': netids})
        for net in nets:
            for netport in netid_port_dict[net['id']]:
                netport['network_name'] = net['name']
        router.update(ports)
        return router

    @validate('routers', 'portforwardings', "add_router_portforwarding")
    def add_router_portforwarding(self, request, id, body):
        context = request.context
        tenant_id = context.tenant_id
        router = self._get_service_plugin(constants.L3_ROUTER_NAT)

        router_id = id
        # get portforwardings count by router_id from db
        kwargs = {'router_id': router_id}
        count = quota.QUOTAS.count(context, PORTFOWRADINGS,
                router, PORTFOWRADINGS,
                tenant_id, **kwargs)

        # check quota
        kwargs = {PORTFOWRADINGS: count + 1}
        quota.QUOTAS.limit_check(context,
                                 tenant_id,
                                 **kwargs)

        #NOTE(gongysh) authz
        router.get_router(context, id)
        result = router.add_router_portforwarding(context, id, body)
        router.l3_rpc_notifier.routers_updated(context, [id])
        return result

    @validate(None, None, "remove_router_portforwarding")
    def remove_router_portforwarding(self, request, id, body):
        context = request.context
        router = self._get_service_plugin(constants.L3_ROUTER_NAT)
        #NOTE(gongysh) authz
        router.get_router(context, id)
        data = router.remove_router_portforwarding(context, id, body)
        router.l3_rpc_notifier.routers_updated(context, [id])
        return data

    @validate('floatingips', 'rate_limit', "update_floatingip_ratelimit")
    def update_floatingip_ratelimit(self, request, id, body):
        context = request.context
        router = self._get_service_plugin(constants.L3_ROUTER_NAT)
        # NOTE(gongysh) to fix the privilege problem
        router.get_floatingip(context, id)
        try:
            payload = body.copy()
            rate_limit = payload.get('rate_limit')
            rate_limit = int(rate_limit)
        except (AttributeError, ValueError, TypeError):
            msg = _("Invalid format: %s") % request.body
            raise qexception.BadRequest(resource='body', msg=msg)
        payload['id'] = id
        _notifier = n_rpc.get_notifier('network')
        _notifier.info(context, 'floatingip.update_ratelimit.start', payload)
        with context.session.begin(subtransactions=True):
            try:
                fip_qry = context.session.query(l3_db.FloatingIP)
                floating_ip = fip_qry.filter_by(id=id).one()
                floating_ip.update({'rate_limit': body['rate_limit']})
            except sa_exc.NoResultFound:
                raise l3.FloatingIPNotFound(floatingip_id=id)

        router_id = floating_ip['router_id']
        if router_id:
            router.l3_rpc_notifier.routers_updated(context, [router_id])
        result = router._make_floatingip_dict(floating_ip)
        _notifier.info(context, 'floatingip.update_ratelimit.end',
                       {'floatingip': result})
        return result

    @validate(None, None, "update_floatingip_registerno")
    def update_floatingip_registerno(self, request, id, body):
        context = request.context
        router = self._get_service_plugin(constants.L3_ROUTER_NAT)
        # NOTE(gongysh) to fix the privilege problem
        router.get_floatingip(context, id)
        _notifier = n_rpc.get_notifier('network')
        payload = body.copy()
        registerno = payload.get('uos_registerno', None)
        if registerno is None:
            msg = _("Invalid format: %s") % request.body
            raise qexception.BadRequest(resource='body', msg=msg)
        payload['id'] = id
        _notifier.info(context, 'floatingip.update_registerno.start', payload)
        # NOTE(gongysh) do we need to validate it?
        with context.session.begin(subtransactions=True):
            try:
                fip_qry = context.session.query(l3_db.FloatingIP)
                floating_ip = fip_qry.filter_by(id=id).one()
            except sa_exc.NoResultFound:
                raise l3.FloatingIPNotFound(floatingip_id=id)
            floating_ip.update({'uos_registerno': registerno.strip()})
        result = router._make_floatingip_dict(floating_ip)
        _notifier.info(context, 'floatingip.update_registerno.end',
                       {'floatingip': result})
        return result

    @validate(None, None, "update_port_sg")
    def update_port_sg(self, request, id, body):
        # body like this:
        # {"port": {"security_groups":
        #           ["d85a5172-2929-48eb-84cb-a6f6defaeb2e"]}}
        context = request.context
        core_plugin = manager.NeutronManager.get_plugin()
        _notifier = n_rpc.get_notifier('network')
        if ("port" not in body or "security_groups" not in body['port']):
            msg = _("Invalid input %s") % body
            raise webob.exc.HTTPBadRequest(msg)
        new_body = {'port': {'security_groups':
                             body['port']['security_groups']}}
        payload = new_body.copy()
        payload['id'] = id
        _notifier.info(context, 'portsg.update.start', payload)
        result = core_plugin.update_port(context, id, new_body)
        _notifier.info(context, 'portsg.update.end', {'port': result})
        return result

    def add_tunnel_and_connection(self, request, **kwargs):
        context = request.context
        tunnel_controller = self._get_resource_controller('tunnels')
        tunnel_id = tunnel_controller.create(request, **kwargs).get(
                "tunnel").get("id")
        conn_controller = self._get_resource_controller('tunnel_connections')
        new_req = request.copy()
        body = json.loads(new_req.body)
        body['tunnel_connection']['tunnel_id'] = tunnel_id
        new_req.body = json.dumps(body)
        tunnel_plugin = self._get_service_plugin(constants.TUNNEL)
        try:
            tunnel_connection = conn_controller.create(new_req, body=body)
        except Exception:
            LOG.info(_("Tunnel connection create failed, so delete tunnel %s"),
                        tunnel_id)
            tunnel_plugin.delete_tunnel(context, tunnel_id)
            raise
        return tunnel_connection

    def remove_tunnel(self, request, id):
        context = request.context
        tunnel_plugin = self._get_service_plugin(constants.TUNNEL)
        #NOTE(WeiW): authz
        tunnel = tunnel_plugin.get_tunnel(context, id)
        for conn in tunnel['tunnel_connections']:
            tunnel_plugin.delete_tunnel_connection(context, conn['id'])
        data = tunnel_plugin.delete_tunnel(context, id)
        return data

    def get_fip_usage(self, request, id, **kwargs):
        context = request.context
        if not context.is_admin:
            raise qexception.NotAuthorized()
        router = self._get_service_plugin(constants.L3_ROUTER_NAT)
        core_plugin = manager.NeutronManager.get_plugin()
        nets = core_plugin.get_networks(context,
                                        filters={"router:external": [True]})
        results = []
        for net in nets:
            subnets = core_plugin.get_subnets(
                context, filters={"network_id": [net['id']]})
            for subnet in subnets:
                if (subnet['name'] and
                    subnet['name'].startswith("ext_shadow_subnet")):
                    continue
                fips = router.get_floatingips(
                    context, filters={"floating_subnet_id": [subnet['id']]})
                fip_ips = []
                fip_map = {}
                for fip in fips:
                    fip_ips.append(fip['floating_ip_address'])
                    fip_map[fip['floating_ip_address']] = fip
                ipcidr = subnet['cidr']
                ip = netaddr.IPNetwork(ipcidr)
                num_ips = len(ip)
                allocation_pools = subnet['allocation_pools']
                for index in range(num_ips):
                    anyallo = []
                    for allo in allocation_pools:
                        start = netaddr.IPAddress(allo['start']).value
                        end = netaddr.IPAddress(allo['end']).value
                        anyallo.append(start <= ip[index].value <= end)
                    if any(anyallo):
                        if str(ip[index]) in fip_ips:
                            tenant_id = fip_map[str(ip[index])]['tenant_id']
                            results.append({"subnet_id": subnet['id'],
                                            "subnet_name": subnet['name'],
                                            "tenant_id": tenant_id,
                                            "fip": str(ip[index]),
                                            "used": "yes"})
                        else:
                            results.append({"subnet_id": subnet['id'],
                                            "fip": str(ip[index]),
                                            "subnet_name": subnet['name'],
                                            "tenant_id": "",
                                            "used": "no"})
        return {"fip_usages": results}

    def ping_agent(self, request, id, body=None):
        context = request.context
        if not context.is_admin:
            raise qexception.NotAuthorized()
        host = body['agent']['host']
        topic = body['agent']['topic']
        if topic == topics.LOADBALANCER_AGENT:
            x = helo_rpc_agent_api.HeloAgentNotifyAPI(version='2.0')
        else:
            x = helo_rpc_agent_api.HeloAgentNotifyAPI()
        return x.helo_agent_host(context, host, topic)

    def _get_valid_inused_flag(self, request, id):

        if id  in ['floatingips'] :
            filters = api_common.get_filters(request, {},
                                             ['in-used'])
            in_used = filters.get('in_used', ['nocare'])[0]

            return in_used

    def get_all_resource_counter_old(self, request, id, **kwargs):
        '''get all resource counter '''
        if 'uos_staff' in request.context.roles:
            uos_utils.uos_staff_act_as_admin(request)

        data = {'resources':[]}

        for resource_name in STATISTICS_RESOURCE:
            _result = self.index(request, input_resources = resource_name, **kwargs)

            LOG.info("get resource couner result:%s, resource_name:%s" % (_result, resource_name))

            for key, value in _result.iteritems():
                if resource_name!= key :
                    msg = _("resource %s is not surpport") % resource_name
                    raise qexception.BadRequest(resource='body', msg=msg)

                if resource_name == "floatingips":
                    in_used = [v for v in value if v['port_id'] is not None]
                    no_used = [v for v in value if v['port_id'] is None]
                    info = {'in_used':len(in_used),
                            'no_used':len(no_used),
                            'total':len(value)
                           }

                elif resource_name == "agents":
                    active = [v for v in value if v['alive']]
                    error = [v for v in value if not v['alive']]
                    info = {'active':len(active),
                            'error':len(error),
                            'total':len(value)
                           }

                elif resource_name == "routers" or resource_name == "loadbalancers":
                    active = [v for v in value if v['status'] == 'ACTIVE']
                    error = [v for v in value if v['status'] != 'ACTIVE']
                    info = {'active':len(active),
                            'error':len(error),
                            'total':len(value)
                           }
                elif resource_name == "networks":
                    network = self._get_network_ports_num(request.context)
                    info = {'total':len(value),
                            'shared_ports':network['share_ports'],
                            'no_shared_ports':network['no_share_ports'],
                           }

                else:
                    info = {'total':len(value)}

                re = {'resource':resource_name, 'counter':info}
                data['resources'].append(re)

        LOG.info("get resource counter, data:%s" % data)

        return  data

    def get_all_resource_counter(self, request, id, **kwargs):
        '''Get all resources about neutron from db.

        Get resource like agents, floatings, networks,
        routers, subnets, nics and sgs.
        Store these resources into a dict named 'all_resources'

        '''
        all_resources = {}
        all_resources['resources'] = []
        context = request.context

        # Get all agents resources
        agents = {}
        agents['resource'] = 'agents'
        agents['counter'] = {}
        agt_qry = context.session.query(agents_db.Agent)
        agt_all_counts = agt_qry.count()
        agts = agt_qry.all()
        dead_counts = 0

        for agt in agts:
            if not agents_db.AgentDbMixin.is_agent_down(
            agt.heartbeat_timestamp):
                dead_counts += 1
        agents['counter']['total'] = agt_all_counts
        agents['counter']['error'] = dead_counts
        agents['counter']['active'] = agt_all_counts - dead_counts
        all_resources['resources'].append(agents)

        # Get all floatingips resources
        floatingips = {}
        floatingips['resource'] = 'floatingips'
        floatingips['counter'] = {}
        fip_qry = context.session.query(l3_db.FloatingIP)
        fip_qry2 = context.session.query(l3_db.FloatingIP).filter_by(
            fixed_ip_address=None)
        fip_all_counts = fip_qry.count()
        fip_unused_counts = fip_qry2.count()
        floatingips['counter']['total'] = fip_all_counts
        floatingips['counter']['no_used'] = fip_unused_counts
        floatingips['counter']['in_used'] = fip_all_counts - fip_unused_counts
        all_resources['resources'].append(floatingips)

        # Get all networks resources
        networks = {}
        networks['resource'] = 'networks'
        networks['counter'] = {}
        net_all_counts = context.session.query(models_v2.Network).count()
        nic_counts = context.session.query(models_v2.Port).count()

        shared_nets = context.session.query(models_v2.Network).filter_by(
            shared=True).all()

        shared_nic_counts = 0
        for shared_net in shared_nets:
            shared_nic_counts += context.session.query(models_v2.Port).filter_by(
                network_id=shared_net.id).count()
        networks['counter']['total'] = net_all_counts
        networks['counter']['no_shared_ports'] = nic_counts - shared_nic_counts
        networks['counter']['shared_ports'] = shared_nic_counts
        all_resources['resources'].append(networks)

        # Get all routers resources
        routers = {}
        routers['resource'] = 'routers'
        routers['counter'] = {}
        router_all_counts = context.session.query(l3_db.Router).count()
        router_active_counts = context.session.query(l3_db.Router).filter_by(
            status='ACTIVE').count()
        routers['counter']['total'] = router_all_counts
        routers['counter']['active'] = router_active_counts
        routers['counter']['error'] = router_all_counts - router_active_counts
        all_resources['resources'].append(routers)

        # Get all subnets
        subnets = {}
        subnets['resource'] = 'subnets'
        subnets['counter'] = {}
        subnet_all_counts = context.session.query(models_v2.Subnet).count()
        subnets['counter']['total'] = subnet_all_counts
        all_resources['resources'].append(subnets)

        # Get all nics
        nics = {}
        nics['resource'] = 'ports'
        nics['counter'] = {}
        nics['counter']['total'] = nic_counts
        all_resources['resources'].append(nics)

        # Get all sg's rules
        security_groups_rules = {}
        security_groups_rules['resource'] = 'security_group_rules'
        security_groups_rules['counter'] = {}
        sg_rule_counts = context.session.query(securitygroups_db.SecurityGroupRule).count()
        security_groups_rules['counter']['total'] = sg_rule_counts
        all_resources['resources'].append(security_groups_rules)

        # Get all sgs
        security_groups = {}
        security_groups['resource'] = 'security_groups'
        security_groups['counter'] = {}
        sg_counts = context.session.query(securitygroups_db.SecurityGroup).count()
        security_groups['counter']['total'] = sg_counts
        all_resources['resources'].append(security_groups)

        return all_resources

    def get_resource_counter_old(self, request, id, **kwargs):
        '''get resource counter '''

        data = {}

        _result = self.index(request, input_resources=id, **kwargs)

        in_used = self._get_valid_inused_flag(request, id)

        LOG.info("get resource couner result:%s, id:%s, in_used:%s" % (_result, id, in_used))

        for key, value in _result.iteritems():
            if id != key :
                msg = _("resource %s is not surpport") % id
                raise qexception.BadRequest(resource='body', msg=msg)

            if id == "floatingips" and in_used == "True":
                result = [v for v in value if v['port_id'] is not None]
            elif id == "floatingips" and in_used == "False":
                result = [v for v in value if v['port_id'] is None]
            else:
                result = value

        data = {'resources': [{'resource':id, 'counter':len(result)}]}

        LOG.info("get resource counter, data:%s" % data)
        return  data

    def _get_method(self, request):
       filters = api_common.get_filters(request, {},
                                         ['method'])
       if 'action' not in filters:
           raise ResourceMethodNotFound(method=None)

       method = filters.get('action')[0]
       counter_method = CONTROLLER_LIST_MAP.keys()
       if method not in counter_method:
           raise ResourceMethodNotFound(method=method)

       return method

    def get_resource_host_counter(self, request, id, **kwargs):

        method = self._get_method(request)
        resource_id_name = CONTROLLER_LIST_MAP[method]['resource']+'_id'
        kwargs[resource_id_name] = id

        obj_lister = getattr(eval(CONTROLLER_LIST_MAP[method]['class']), 'index')
        _result = obj_lister(request=request, **kwargs)

        LOG.info("get list couner result:%s, id:%s, resource_id_name:%s" %
                   (_result, id, resource_id_name))

        for key, value in _result.iteritems():
            data = {'lists': [{'action':method, 'id':id, 'counter':len(value)}]}
            break

    def _get_network_ports_num(self, context, filers=None):
        _plugin = self._get_service_plugin(constants.CORE)
        _result = _plugin.get_networks(context)
        share_num = 0
        no_share_num = 0
        shares = [v for v in _result if v['shared']]
        no_shares = [v for v in _result if not v['shared']]
        for share in shares:
           filters = {'network_id':[share['id']]}
           num = self._get_num(context, port=True, filters=filters)
           share_num = share_num + num['port_num']

        for no_share in no_shares:
           filters = {'network_id':[no_share['id']]}
           num = self._get_num(context, port=True, filters=filters)
           no_share_num = no_share_num + num['port_num']

        data = {}
        data['share_ports'] = share_num
        data['no_share_ports'] = no_share_num
        return data

    def _get_networks(self, context, filters=None):
        _plugin = self._get_service_plugin(constants.CORE)
        result = _plugin.get_networks(context, filters)
        return result

    def _get_ports(self, context, filters=None):
        _plugin = self._get_service_plugin(constants.CORE)
        result = _plugin.get_ports(context, filters)
        return result

    def _get_router(self, context, router_id):
        _plugin = self._get_service_plugin(constants.L3_ROUTER_NAT)
        result = _plugin.get_router(context, router_id)
        return result

    def _get_num(self, context, port=False, subnet=False, filters=None):
        data = {}
        _plugin = self._get_service_plugin(constants.CORE)
        if port:
            result = _plugin.get_ports(context, filters)
            data['port_num'] = len(result)
        if subnet:
            result = _plugin.get_subnets(context, filters)
            data['subnet_num'] = len(result)

        return data

    def get_security_group_info(self, request, id, **kwargs):
        if 'uos_staff' in request.context.roles:
            uos_utils.uos_staff_act_as_admin(request)

        context = request.context
        data = {'security_groups':[]}
        _result = self.index(request, input_resources='security_groups', **kwargs)
        single = _result.pop('security_groups')
        for sg in single:
           filters = {'security_group_id':[sg['id']]}
           num = len(sg['security_group_rules'])
           net = {
                     'tenant_id':sg['tenant_id'],
                     'name':sg['name'],
                     'rules_count':num,
                 }
           data['security_groups'].append(net)

        _result.update(data)
        return  _result


    def get_network_resource_info(self, request, id, **kwargs):
        if 'uos_staff' in request.context.roles:
            uos_utils.uos_staff_act_as_admin(request)

        context = request.context
        data = {'networks':[]}
        _result = self.index(request, input_resources='networks', **kwargs)
        single = _result.pop('networks')
        for network in single:
            filters = {'network_id':[network['id']]}
            num = self._get_num(context, port=True, subnet=True, filters=filters)
            net = {
                      'tenant_id':network['tenant_id'],
                      'id':network['id'],
                      'name':network['name'],
                      'status':network['status'],
                      'port_num':num['port_num'],
                      'subnet_num':num['subnet_num'],
                  }

            data['networks'].append(net)
        _result.update(data)
        return  _result

    def get_subnet_resource_info(self, request, id, **kwargs):
        if 'uos_staff' in request.context.roles:
            uos_utils.uos_staff_act_as_admin(request)

        context = request.context
        data = {'subnets':[]}
        _result = self.index(request, input_resources='subnets', **kwargs)
        single = _result.pop('subnets')
        for subnet in single:
           filters = {'fixed_ips':{'subnet_id':[subnet['id']]}}
           num = self._get_num(context, port=True, filters=filters)
           for device_owner in ['network:router_interface', 'network:router_gateway']:
               filters = {'device_owner':[device_owner], 'fixed_ips':{'subnet_id':[subnet['id']]}}
               port = self._get_ports(context, filters=filters)
               if port:
                  break

           router = {}
           if port and port[0]['device_id'] is not None:
               router = self._get_router(context, port[0]['device_id'])

           name = router.get('name', None)
           net = {
                     'tenant_id':subnet['tenant_id'],
                     'id':subnet['id'],
                     'name':subnet['name'],
                     'port_num':num['port_num'],
                     'cidr':subnet['cidr'],
                     'enable_dhcp':subnet['enable_dhcp'],
                     'router':name,
                 }

           data['subnets'].append(net)

        _result.update(data)
        return  _result

    def _get_agent_host_resources_count(self, context, agent,
                                           request, filters=None):

        kwargs = {}
        kwargs['agent_id'] = agent['id']

        obj_list = getattr(eval(AGENT_RESOURCE_MAP[agent['binary']]['class']),
                               'index')
        _result = obj_list(request=request, **kwargs)

        LOG.info("get agent host resources count id:%s, resource:%s, " %
                   (agent['id'],
                      AGENT_RESOURCE_MAP[agent['binary']]['resource']))

        data = {}
        for key, value in _result.iteritems():
            data = {AGENT_RESOURCE_MAP[agent['binary']]['resource']:len(value)}
            break

        return data

    def get_agent_resource_info(self, request, id, **kwargs):
        if 'uos_staff' in request.context.roles:
            uos_utils.uos_staff_act_as_admin(request)

        context = request.context
        data = {'agents' : []}
        _result = self.index(request, input_resources='agents', **kwargs)
        single = _result.pop('agents')
        for  agent in single:
           num = {}
           if agent['binary'] in AGENT_RESOURCE_MAP:
               filters = {'id' : [agent['id']]}
               num = self._get_agent_host_resources_count(context,
                        agent, request, filters=filters)
           agent_info = {
                     'id': agent['id'],
                     'admin_state_up':agent['admin_state_up'],
                     'agent_type':agent['agent_type'],
                     'alive':agent['alive'],
                     'host':agent['host'],
                 }
           agent_info = dict(agent_info.items() + num.items())
           data['agents'].append(agent_info)

        _result.update(data)
        return  _result

    def get_tenant_resource_info(self, request, id, **kwargs):

        context = request.context
        tenant_resources_map = {}
        env = request.environ
        for resource in TENANT_RESOURCES:
            _result = self.index(request, input_resources = resource, **kwargs)
            for resource_info in _result[resource]:
                if resource_info['tenant_id'] not in tenant_resources_map:
                    tenant_resources_map[resource_info['tenant_id']] = {
                                  'floatingips_count':0,
                                  'routers_count':0,
                                  'networks_count':0,
                                  'subnets_count':0,
                                  'ports_count':0,
                                  }
                else:
                    tenant_resources_map[resource_info['tenant_id']][resource+'_count']+= 1

        data = {}
        data['tenants'] = []
        for key, value in tenant_resources_map.iteritems():
            tenant_statistic_info = {'id':key}
            tenant_statistic_info = dict(tenant_statistic_info.items()+value.items())
            data['tenants'].append(tenant_statistic_info)

        return  data


class Uos(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "UnitedStack Resources"

    @classmethod
    def get_alias(cls):
        return "uos"

    @classmethod
    def get_description(cls):
        return ("Return related resources")

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/ext/neutron/uos/api/v1.0"

    @classmethod
    def get_updated(cls):
        return "2013-12-25T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns uos Resources."""
        exts = []
        controller = resource.Resource(UosController(),
                                       base.FAULT_MAP)
        collection_actions = {
                'add_tunnel_and_connection': 'POST',}
        member_actions = {
                          'get_router_details': 'GET',
                          'add_router_portforwarding': 'PUT',
                          'remove_router_portforwarding': 'PUT',
                          'update_floatingip_ratelimit': 'PUT',
                          'update_floatingip_registerno': 'PUT',
                          'associate_floatingip_router': 'PUT',
                          'update_port_sg': 'PUT',
                          'get_fip_usage': 'GET',
                          'ping_agent': 'PUT',
                          'remove_tunnel': 'PUT',
                          'get_tenant_resource_info': 'GET',
                          'get_agent_resource_info': 'GET',
                          'get_network_resource_info': 'GET',
                          'get_security_group_info': 'GET',
                          'get_subnet_resource_info': 'GET',
                          'get_resource_host_counter': 'GET',
                          'get_all_resource_counter': 'GET',
                          'get_resource_counter': 'GET', }
        ext = extensions.ResourceExtension(RESOURCES, controller,
                collection_actions=collection_actions,
                member_actions=member_actions)
        exts.append(ext)

        quota.QUOTAS.register_resource_by_name(PORTFOWRADINGS)

        return exts

    def get_extended_resources(self, version):
        attrs = {}
        if version == "2.0":
            for resources in EXTENDED_RESOURCES:
                attrs[resources] = EXTENDED_TIMESTAMP
        return attrs
