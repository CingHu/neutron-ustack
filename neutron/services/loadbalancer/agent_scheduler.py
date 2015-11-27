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

import datetime
import random
import time

from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import joinedload

from neutron.common import constants
from neutron.db import agents_db
from neutron.db import agentschedulers_db
from neutron.db import model_base
from neutron.extensions import lbaas_agentscheduler
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import timeutils
from neutron.openstack.common.gettextutils import _LI, _LW
from neutron import context as n_ctx


LOG = logging.getLogger(__name__)

LB_AGENTS_SCHEDULER_OPTS = [
    cfg.BoolOpt('allow_automatic_lbaas_agent_failover', default=False,
                help=_('Automatically reschedule loadbalancers from offline LB '
                       'agents to online LB agents.')),
]

cfg.CONF.register_opts(LB_AGENTS_SCHEDULER_OPTS)

class LoadbalancerAgentBinding(model_base.BASEV2):
    """Represents binding between neutron loadbalancer instances and agents."""

    loadbalancer_id = sa.Column(sa.String(36),
                        sa.ForeignKey("lbaas_loadbalancers.id", ondelete='CASCADE'),
                        primary_key=True)
    agent = orm.relation(agents_db.Agent)
    agent_id = sa.Column(sa.String(36), sa.ForeignKey("agents.id",
                                                      ondelete='CASCADE'),
                         nullable=False)

class LbaasAgentSchedulerDbMixin(agentschedulers_db.AgentSchedulerDbMixin,
                                 lbaas_agentscheduler
                                 .LbaasAgentSchedulerPluginBase):

    def start_periodic_agent_status_check(self):
        if not cfg.CONF.allow_automatic_lbaas_agent_failover:
            LOG.info(_LI("Skipping period LBaaS agent status check because "
                         "automatic lbaas rescheduling is disabled."))
            return

        self.periodic_agent_loop = loopingcall.FixedIntervalLoopingCall(
            self.reschedule_lbaas_from_down_agents)
        interval = max(cfg.CONF.agent_down_time / 2, 1)
        # add random initial delay to allow agents to check in after the
        # neutron server first starts. random to offset multiple servers
        self.periodic_agent_loop.start(interval=interval,
            initial_delay=random.randint(interval, interval * 2))

    def reschedule_lbaas_from_down_agents(self):
        """Reschedule lbaas from down lbaas agents if admin state is up."""
        LOG.info("reschedule_lbaas_from_down_agents called.")
        # give agents extra time to handle transient failures
        agent_dead_limit = cfg.CONF.agent_down_time * 2

        # check for an abrupt clock change since last check. if a change is
        # detected, sleep for a while to let the agents check in.
        tdelta = timeutils.utcnow() - getattr(self, '_clock_jump_canary',
                                              timeutils.utcnow())
        if timeutils.total_seconds(tdelta) > cfg.CONF.agent_down_time:
            LOG.warn(_LW("Time since last LBaaS agent reschedule check has "
                         "exceeded the interval between checks. Waiting "
                         "before check to allow agents to send a heartbeat "
                         "in case there was a clock adjustment."))
            time.sleep(agent_dead_limit)
        self._clock_jump_canary = timeutils.utcnow()

        context = n_ctx.get_admin_context()
        cutoff = timeutils.utcnow() - datetime.timedelta(
            seconds=agent_dead_limit)
        down_bindings = (
            context.session.query(LoadbalancerAgentBinding).
            join(agents_db.Agent).
            filter(agents_db.Agent.heartbeat_timestamp < cutoff,
                   agents_db.Agent.admin_state_up))
        for binding in down_bindings:
            LOG.warn(_LW("Rescheduling loadbalancer %(loadbalancer)s from agent %(agent)s "
                         "because the agent did not report to the server in "
                         "the last %(dead_time)s seconds."),
                     {'loadbalancer': binding.loadbalancer_id,
                      'agent': binding.agent_id,
                      'dead_time': agent_dead_limit})
            self.reschedule_loadbalancer_instance(binding.loadbalancer_id)

    def get_lbaas_agent_hosting_loadbalancer(self, context, loadbalancer_id, active=None):
        query = context.session.query(LoadbalancerAgentBinding)
        query = query.options(joinedload('agent'))
        binding = query.get(loadbalancer_id)

        if (binding and self.is_eligible_agent(
                active, binding.agent)):
            return {'agent': self._make_agent_dict(binding.agent)}

    def get_lbaas_agents(self, context, active=None, filters=None):
        query = context.session.query(agents_db.Agent)
        query = query.filter_by(agent_type=constants.AGENT_TYPE_LOADBALANCER)
        if active is not None:
            query = query.filter_by(admin_state_up=active)
        if filters:
            for key, value in filters.iteritems():
                column = getattr(agents_db.Agent, key, None)
                if column:
                    query = query.filter(column.in_(value))

        return [agent
                for agent in query
                if self.is_eligible_agent(active, agent)]

    def list_loadbalancers_on_lbaas_agent(self, context, id):
        query = context.session.query(LoadbalancerAgentBinding.loadbalancer_id)
        query = query.filter_by(agent_id=id)
        loadbalancer_ids = [item[0] for item in query]
        if loadbalancer_ids:
            return {'loadbalancers': self.get_loadbalancers(context, filters={'id': loadbalancer_ids})}
        else:
            return {'loadbalancers': []}

    def get_lbaas_agent_candidates(self, device_driver, active_agents):
        candidates = []
        for agent in active_agents:
            agent_conf = self.get_configuration_dict(agent)
            if device_driver in agent_conf['device_drivers']:
                candidates.append(agent)
        return candidates

    def make_lbaas_agent_dict(self,agent):
        return {'agent': self._make_agent_dict(agent)}


class ChanceScheduler(object):
    """Allocate a loadbalancer agent for a loadbalancer instance in a random way."""

    def _make_agent_dict(self, agent, fields=None):
        attr = ext_agent.RESOURCE_ATTRIBUTE_MAP.get(
            ext_agent.RESOURCE_NAME + 's')
        res = dict((k, agent[k]) for k in attr
                   if k not in ['alive', 'configurations'])
        res['alive'] = not AgentDbMixin.is_agent_down(
            res['heartbeat_timestamp'])
        res['configurations'] = self.get_configuration_dict(agent)
        return self._fields(res, fields)

    def schedule(self, plugin, context, loadbalancer, device_driver):
        """Schedule the loadbalancer instance to an active loadbalancer agent if there
        is no enabled agent hosting it.
        """
        with context.session.begin(subtransactions=True):
            lbaas_agent = plugin.get_lbaas_agent_hosting_loadbalancer(
                context, loadbalancer.id)
            if lbaas_agent:
                LOG.info(_('Loadbalancer %(loadbalancer_id)s has already been hosted'
                            ' by lbaas agent %(agent)s'),
                          {'loadbalancer_id': loadbalancer.id,
                           'agent': lbaas_agent})
                return lbaas_agent

            active_agents = plugin.get_lbaas_agents(context, active=True)
            if not active_agents:
                LOG.warn(_('No active lbaas agents for loadbalancer %s'), loadbalancer.id)
                return

            candidates = plugin.get_lbaas_agent_candidates(device_driver,
                                                           active_agents)
            if not candidates:
                LOG.warn(_('No lbaas agent supporting device driver %s'),
                         device_driver)
                return

            chosen_agent = random.choice(candidates)
            binding = LoadbalancerAgentBinding()
            binding.agent = chosen_agent
            binding.loadbalancer_id = loadbalancer.id
            context.session.add(binding)
            LOG.debug(_('Loadbalancer_id %(loadbalancer_id_id)s is scheduled to '
                        'lbaas agent %(agent_id)s'),
                      {'loadbalancer_id': loadbalancer.id,
                       'agent_id': chosen_agent['id']})
            return plugin.make_lbaas_agent_dict(chosen_agent)

    def _unbind_loadbalancer(self, context, loadbalancer_id, agent_id):
         with context.session.begin(subtransactions=True):
            query = context.session.query(LoadbalancerAgentBinding)
            query = query.filter(
                LoadbalancerAgentBinding.loadbalancer_id == loadbalancer_id,
                LoadbalancerAgentBinding.agent_id == agent_id)
            try:
                binding = query.one()
            except Exception as exc:
                with excutils.save_and_reraise_exception():
                    LOG.exception(exc)
            context.session.delete(binding)

    def reschedule_loadbalancer_instance(self, plugin, context, loadbalancer, device_driver):
        """Reschedule loadbalancer to a new lbaas-agent

        Remove the loadbalancer from the lbaas-agent(s) currently hosting it and
        schedule it again
        """
        cur_agent = plugin.get_lbaas_agent_hosting_loadbalancer(
            context, loadbalancer.id)
        LOG.info(_('Reschedule Loadbalancer %(loadbalancer_id)s been hosted'
                            ' by lbaas agent %(agent)s'),
                          {'loadbalancer_id': id,
                           'agent': cur_agent})
        with context.session.begin(subtransactions=True):
            if cur_agent:
                self._unbind_loadbalancer(context, loadbalancer.id, cur_agent['agent']['id'])
            else:
                LOG.error("Reschedule Loadbalancer %s not found agent", id)
            new_agent = self.schedule(plugin, context, loadbalancer, device_driver)
            LOG.info(_('Reschedule Loadbalancer %(loadbalancer_id)s to '
                            ' agent %(agent)s'),
                          {'loadbalancer_id': id,
                           'agent': new_agent})
            if not new_agent:
                raise lbaas_agentscheduler.NoEligibleLbaasAgent(loadbalancer_id=id)
            return new_agent
            #lbaas_notifier = plugin.agent_notifiers.get(constants.AGENT_TYPE_LOADBALANCER)
            #if lbaas_notifier:
            #    if cur_agent:
            #        lbaas_notifier.loadbalancer_removed_from_agent(
            #                context, id, cur_agent['host'])
            #    lbaas_notifier.loadbalancer_added_to_agent(
            #        context, id, new_agent['host'])
