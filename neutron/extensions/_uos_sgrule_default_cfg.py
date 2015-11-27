# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 OpenStack Foundation.
# All Rights Reserved.
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

from neutron.openstack.common import log as logging
from oslo.config import cfg

PROTOCOL_TCP = 'tcp'
PROTOCOL_UDP = 'udp'
PROTOCOL_ICMP = 'icmp'

INGRESS = 'ingress'
EGRESS = 'egress'

PORT_RANGE_MIN = 0
PORT_RANGE_MAX = 65535

PORT_RANGE_ICMP_MIN = 0
PORT_RANGE_ICMP_MAX = 255

RULE_CONFIG_ATTRIBUTES_COUNT = 4

LOG = logging.getLogger(__name__)


class Metro(object):
    def __init__(self, **kwargs):
        self.direction = kwargs.get('direction', "")
        self.protocol = kwargs.get('protocol', "")
        self.port_range_min = kwargs.get('port_range_min', -1)
        self.port_range_max = kwargs.get('port_range_max', -1)


class DefaultSGRulesConfig:

    sg_default_rules = []

    @classmethod
    def get_valid_rules(cls):
        """Get the default rules from specified string"""
        DefaultSGRulesConfig.sg_default_rules = []
        data = cfg.CONF.unitedstack.securitygroup_default_rules
        data = data.replace(" ", "")
        rulesiterms = data.split(':')

        for x in rulesiterms:
            iterm_attributes = x.split(',')
            if len(iterm_attributes) != RULE_CONFIG_ATTRIBUTES_COUNT:
                LOG.warning(_('ignore invalid security group rule %s '), x)
                continue
            else:
                if (not iterm_attributes[2].isdigit() and
                    iterm_attributes[2].lower != 'none'):
                    LOG.warning(_('ignore invalid security group rule %s '), x)
                    continue
                if (not iterm_attributes[3].isdigit() and
                    iterm_attributes[3].lower != 'none'):
                    LOG.warning(_('ignore invalid security group rule %s '), x)
                    continue
                _dict = {}
                _dict['direction'] = iterm_attributes[0]
                _dict['protocol'] = iterm_attributes[1]
                _min = (None if iterm_attributes[2] == 'none'
                        else int(iterm_attributes[2]))
                _max = (None if iterm_attributes[3] == 'none'
                        else int(iterm_attributes[3]))
                _dict['port_range_min'] = _min
                _dict['port_range_max'] = _max
                # make sure both are none or are digits
                if ((_min is None and _max is not None) or
                    (_min is not None and _max is None)):
                    LOG.warning(_('ignore invalid security group rule %s '), x)
                    continue
                if (any((_min is not None, _max is not None)) and
                    _min > _max and _dict['protocol'] != PROTOCOL_ICMP):
                    LOG.warning(_('ignore invalid security group rule %s '), x)
                    continue
                if (_dict['direction'] != INGRESS and
                    _dict['direction'] != EGRESS):
                    LOG.warning(_('ignore invalid security group rule %s '), x)
                    continue
                if (_dict['protocol'] != PROTOCOL_TCP and
                    _dict['protocol'] != PROTOCOL_UDP and
                    _dict['protocol'] != PROTOCOL_ICMP and
                    _dict['protocol'] != 'self' and
                    _dict['protocol'] != ''):
                    LOG.warning(_('ignore invalid security group rule %s '), x)
                    continue
                if (_dict['protocol'] != PROTOCOL_ICMP):
                    if (_dict['port_range_min'] < PORT_RANGE_MIN or
                        _dict['port_range_max'] > PORT_RANGE_MAX):
                        LOG.warning(_('ignore invalid '
                                   'security group rule %s '), x)
                        continue
                if (_dict['protocol'] == PROTOCOL_ICMP):
                    if (_dict['port_range_min'] < PORT_RANGE_ICMP_MIN or
                        _dict['port_range_min'] > PORT_RANGE_ICMP_MAX):
                        LOG.warning(_('ignore invalid '
                                   'security group rule %s '), x)
                        continue
                    if (_dict['port_range_max'] < PORT_RANGE_ICMP_MIN or
                        _dict['port_range_max'] > PORT_RANGE_ICMP_MAX):
                        LOG.warning(_('ignore invalid '
                                   'security group rule %s '), x)
                        continue
                DefaultSGRulesConfig.sg_default_rules.append(Metro(**_dict))
        return DefaultSGRulesConfig.sg_default_rules
