# Copyright (c) 2013 OpenStack Foundation.
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

import datetime

from neutron.common import rpc as proxy
from neutron.openstack.common import log as logging


LOG = logging.getLogger(__name__)


class HeloAgentNotifyAPI(proxy.RpcProxy):
    """API for plugin to ping agent."""
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=None, version=None):
        if version:
            super(HeloAgentNotifyAPI, self).__init__(
                topic=topic, default_version=version)
        else:
            super(HeloAgentNotifyAPI, self).__init__(
                topic=topic, default_version=self.BASE_RPC_API_VERSION)

    def helo_agent_host(self, context, host, topic):
        """Notify the agent on host."""
        data = 0
        try:
            data = self.call(
                context, self.make_msg('helo',
                                       time=datetime.datetime.utcnow()),
                timeout = 3,
                topic='%s.%s' % (topic, host))
        except Exception:
            LOG.exception(_("Failed to helo %(topic)s on %(host)s"),
                          {"topic": topic, "host": host})
        return data


class HeloRpcCallbackMixin(object):

    def helo(self, context, time):
        LOG.debug(time)
        return 1
