# Copyright 2014 A10 Networks
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

import abc
import six

from neutron.plugins.common import constants


@six.add_metaclass(abc.ABCMeta)
class BaseManagerMixin(object):

    def __init__(self, driver):
        self.driver = driver

    @abc.abstractmethod
    def create(self, context, obj):
        pass

    @abc.abstractmethod
    def update(self, context, obj_old, obj):
        pass

    @abc.abstractmethod
    def delete(self, context, obj):
        pass


@six.add_metaclass(abc.ABCMeta)
class BaseRefreshMixin(object):

    @abc.abstractmethod
    def refresh(self, context, obj):
        pass


@six.add_metaclass(abc.ABCMeta)
class BaseStatsMixin(object):

    @abc.abstractmethod
    def stats(self, context, obj):
        pass


class BaseStatusUpdateMixin(object):

    # Status update helpers
    # Note: You must set model_class to an appropriate neutron model
    # in your base manager class.

    def active(self, context, model_id):
        self.driver.plugin.db.update_status(context, self.model_class,
                                            model_id, constants.ACTIVE)

    def failed(self, context, model_id):
        self.driver.plugin.db.update_status(context, self.model_class,
                                            model_id, constants.ERROR)

    def defer(self, context, model_id):
        self.driver.plugin.db.update_status(context, self.model_class,
                                            model_id, constants.DEFERRED)


class BaseDeleteHelperMixin(object):

    # DB delete helper
    # Must define appropriate db delete function

    def db_delete(self, context, model_id):
        self.db_delete_method(context, model_id)
