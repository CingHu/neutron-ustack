# Copyright 2014 OpenStack Foundation
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

import os

import jinja2
import six

from neutron.agent.linux import utils
from neutron.plugins.common import constants as plugin_constants
from neutron.services.loadbalancer import constants
from oslo.config import cfg
from neutron.openstack.common import log as logging
LOG = logging.getLogger(__name__)

PROTOCOL_MAP = {
    constants.PROTOCOL_TCP: 'tcp',
    constants.PROTOCOL_HTTP: 'http',
    constants.PROTOCOL_HTTPS: 'tcp',
    constants.PROTOCOL_TERMINATED_HTTPS: 'terminated_https'
}

BALANCE_MAP = {
    constants.LB_METHOD_ROUND_ROBIN: 'roundrobin',
    constants.LB_METHOD_LEAST_CONNECTIONS: 'leastconn',
    constants.LB_METHOD_SOURCE_IP: 'source'
}

STATS_MAP = {
    constants.STATS_ACTIVE_CONNECTIONS: 'scur',
    constants.STATS_MAX_CONNECTIONS: 'smax',
    constants.STATS_CURRENT_SESSIONS: 'scur',
    constants.STATS_MAX_SESSIONS: 'smax',
    constants.STATS_TOTAL_CONNECTIONS: 'stot',
    constants.STATS_TOTAL_SESSIONS: 'stot',
    constants.STATS_IN_BYTES: 'bin',
    constants.STATS_OUT_BYTES: 'bout',
    constants.STATS_CONNECTION_ERRORS: 'econ',
    constants.STATS_RESPONSE_ERRORS: 'eresp'
}

ACTIVE_PENDING_STATUSES = plugin_constants.ACTIVE_PENDING_STATUSES

TEMPLATES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'templates/'))
JINJA_ENV = None

jinja_opts = [
    cfg.StrOpt(
        'jinja_config_template',
        default=os.path.join(
            TEMPLATES_DIR,
            'haproxy_v1.4.template'),
        help=_('Jinja template file for haproxy configuration'))
]

cfg.CONF.register_opts(jinja_opts, 'haproxy')

def save_config(conf_path, loadbalancer, socket_path=None,
                user_group='nogroup'):
    """Convert a logical configuration to the HAProxy version."""
    LOG.info('save_config %s \n.',loadbalancer)
    config_str = render_loadbalancer_obj( Wrap(loadbalancer), user_group, socket_path)
    LOG.info('save_config convert to config_str %s\n.',config_str)
    utils.replace_file(conf_path, config_str)


def _get_template():
    global JINJA_ENV
    if not JINJA_ENV:
        template_loader = jinja2.FileSystemLoader(
            searchpath=os.path.dirname(cfg.CONF.haproxy.jinja_config_template))
        JINJA_ENV = jinja2.Environment(
            loader=template_loader, trim_blocks=True)
    return JINJA_ENV.get_template(os.path.basename(
        cfg.CONF.haproxy.jinja_config_template))


def render_loadbalancer_obj(loadbalancer,user_group, socket_path):
    loadbalancer_dict = _transform_loadbalancer(loadbalancer)
    LOG.info('save_config convert to config render_loadbalancer_obj_str %s\n.',loadbalancer_dict)
    return _get_template().render({'loadbalancer': loadbalancer_dict,
                                   'user_group': user_group,
                                   'stats_sock': socket_path},
                                  constants=constants)


def _transform_loadbalancer(loadbalancer):
    listeners = [_transform_listener(Wrap(x)) for x in loadbalancer.listeners if _include_listener(Wrap(x))]
    LOG.info('_transform_listener to config_str %s\n.',listeners)
    return {
        'name': loadbalancer.name,
        'id': loadbalancer.id,
        'admin_state_up':loadbalancer.admin_state_up,
        'vip_address': loadbalancer.vip_address,
        'listeners': listeners
    }


def _transform_listener(listener):
    ret_value = {
        'id': listener.id,
        'admin_state_up':listener.admin_state_up,
        'protocol_port': listener.protocol_port,
        'protocol': PROTOCOL_MAP[listener.protocol],
        'l7_policies': [],
        'keep_alive':listener.keep_alive
    }
    if listener.connection_limit and listener.connection_limit > -1:
        ret_value['connection_limit'] = listener.connection_limit
    if listener.default_pool and _include_pool(Wrap(listener.default_pool)):
        ret_value['default_pool'] = _transform_pool(Wrap(listener.default_pool))
    if listener.l7_policies:
        l7_policies = [_transform_policy(Wrap(x))
               for x in listener.l7_policies if _include_policy(Wrap(x))]
        ret_value['l7_policies'] = l7_policies

    return ret_value


def _transform_policy(policy):
    if policy.action == constants.L7_POLICY_ACTION_REJECT:
        ret_value = {
            'id': policy.id,
            'status': policy.status,
            'admin_state_up': policy.admin_state_up,
            'position': policy.position,
            'action': policy.action,
            'rules': [],
            'pool': None
        }
    elif policy.action == constants.L7_POLICY_ACTION_REDIRECT_TO_POOL:
        ret_value = {
            'id': policy.id,
            'status': policy.status,
            'redirect_pool_id': policy.redirect_pool_id,
            'admin_state_up': policy.admin_state_up,
            'position': policy.position,
            'action': policy.action,
            'rules': [],
            'pool': None
        }
        try:
            if policy.pool:
                ret_value['pool'] = _transform_pool(Wrap(policy.pool))
        except AttributeError as e:
            ret_value.pop('pool', None)
            pass
    else:
        ret_value = {
            'id': policy.id,
            'status': policy.status,
            'redirect_url': policy.redirect_url,
            'redirect_url_code': policy.redirect_url_code,
            'redirect_url_drop_query': policy.redirect_url_drop_query,
            'admin_state_up': policy.admin_state_up,
            'position': policy.position,
            'action': policy.action,
            'rules': [],
            'pool': None
        }
    rules = [_transform_rule(Wrap(x))
               for x in policy.rules if _include_rule(Wrap(x))]
    ret_value['rules'] = rules
    return ret_value


def _transform_rule(rule):
    ret_value = {
        'id': rule.id,
        'status': rule.status,
        'admin_state_up': rule.admin_state_up,
        'type': rule.type,
        'compare_type': rule.compare_type,
        'key': rule.key
        #'value': rule.value,
    }
    return ret_value


def _transform_pool(pool):
    ret_value = {
        'id': pool.id,
        'admin_state_up':pool.admin_state_up,
        'protocol': PROTOCOL_MAP[pool.protocol],
        'lb_algorithm': BALANCE_MAP.get(pool.lb_algorithm, 'roundrobin'),
        'members': [],
        'health_monitor': None,
        'session_persistence': None,
        'admin_state_up': pool.admin_state_up,
        'status': pool.status
    }
    members = [_transform_member(Wrap(x))
               for x in pool.members if _include_member(Wrap(x))]
    ret_value['members'] = members
    try:
        if (pool.healthmonitor):
            ret_value['health_monitor'] = _transform_health_monitor(
                Wrap(pool.healthmonitor))
    except AttributeError as e:
        pass

    try:
        if (pool.session_persistence):
            ret_value['session_persistence'] = _transform_session_persistence(
                Wrap(pool.session_persistence))
    except AttributeError as e:
        pass

    return ret_value


def _transform_session_persistence(persistence):
    if persistence.type == constants.SESSION_PERSISTENCE_APP_COOKIE:
        return {
            'type': persistence.type,
            'cookie_name': persistence.cookie_name
        }
    return {
        'type': persistence.type
    }


def _transform_member(member):
    return {
        'id': member.id,
        'admin_state_up': member.admin_state_up,
        'address': member.address,
        'protocol_port': member.protocol_port,
        'weight': member.weight,
        'admin_state_up': member.admin_state_up,
        'subnet_id': member.subnet_id,
        'status': member.status
    }


def _transform_health_monitor(monitor):
    if monitor.type == constants.PROTOCOL_TCP:
        return {
            'id': monitor.id,
            'type': monitor.type,
            'delay': monitor.delay,
            'timeout': monitor.timeout,
            'max_retries': monitor.max_retries,
        }
    return {
        'id': monitor.id,
        'type': monitor.type,
        'delay': monitor.delay,
        'timeout': monitor.timeout,
        'max_retries': monitor.max_retries,
        'http_method': monitor.http_method,
        'url_path': monitor.url_path,
        'expected_codes': '|'.join(
            _expand_expected_codes(monitor.expected_codes)),
    }

def _include_pool(pool):
    return (pool.status in ACTIVE_PENDING_STATUSES) or (pool.status == plugin_constants.DEFERRED)

def _include_listener(listener):
    return (listener.status in ACTIVE_PENDING_STATUSES) or  (listener.status == plugin_constants.DEFERRED)

def _include_member(member):
    return True

def _include_rule(rule):
    return ((rule.status in ACTIVE_PENDING_STATUSES) or (rule.status== plugin_constants.DEFERRED)) and rule.admin_state_up

def _include_policy(policy):
    return ((policy.status in ACTIVE_PENDING_STATUSES) or (policy.status== plugin_constants.DEFERRED)) and policy.admin_state_up

class Wrap(object):
    """A light attribute wrapper for compatibility with the interface lib."""
    def __init__(self, d):
        self.__dict__.update(d)

    def __getitem__(self, key):
        return self.__dict__[key]

def _expand_expected_codes(codes):
    """Expand the expected code string in set of codes.

    200-204 -> 200, 201, 202, 204
    200, 203 -> 200, 203
    """

    retval = set()
    for code in codes.replace(',', ' ').split(' '):
        code = code.strip()

        if not code:
            continue
        elif '-' in code:
            low, hi = code.split('-')[:2]
            retval.update(
                str(i) for i in six.moves.xrange(int(low), int(hi) + 1))
        else:
            retval.add(code)
    return retval
