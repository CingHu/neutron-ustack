# Copyright (c) 2012 OpenStack Foundation.
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

import sqlalchemy as sa

from neutron.db import model_base


class DeviceAgentBinding(model_base.BASEV2):
    """Respresents binding between device and ServiceVM agents."""

    device_id = sa.Column(sa.String(36),
                          sa.ForeignKey("devices.id", ondelete='CASCADE'),
                          primary_key=True)
    servicevm_agent_id = sa.Column(sa.String(36),
                          sa.ForeignKey("agents.id", ondelete='CASCADE'),
                          primary_key=True)

